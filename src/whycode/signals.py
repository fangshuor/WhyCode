"""Layer 2: heuristic signal extraction.

Each signal answers one specific question. Signals never invent evidence —
every one carries the SHAs that produced it, so a careful reader can verify.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from whycode import git_facts as gf
from whycode import ignore as ign

if TYPE_CHECKING:
    from whycode.git_facts import Commit, RepoFacts


class SignalKind(StrEnum):
    REVERT_CHAIN = "revert_chain"
    INCIDENT_HISTORY = "incident_history"
    HIGH_CHURN = "high_churn"
    COUPLING = "coupling"
    SILENCE = "silence"
    GHOST_KEEPER = "ghost_keeper"
    INVARIANT_QUOTE = "invariant_quote"
    NEWBORN = "newborn"


@dataclass(frozen=True)
class Explanation:
    """Structured trace of why a single signal fired.

    Each detector that produces a :class:`Signal` populates this with the
    concrete rule branch that matched, the literal evidence (token, count,
    threshold) the rule looked at, and a stable ``file:function`` reference
    so a curious reader can open the source and audit the ladder.
    """

    rule: str
    """Short stable identifier, e.g. ``"incident_subject_keyword"``."""

    why_it_fired: str
    """One-sentence prose describing the matching condition."""

    evidence: tuple[str, ...] = field(default_factory=tuple)
    """Literal matched substrings, threshold values, or counts."""

    source_ref: str = ""
    """``path:function`` pointer into the project source for the rule."""


@dataclass(frozen=True)
class Signal:
    kind: SignalKind
    severity: int  # 1..5; 5 is loudest.
    headline: str
    detail: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    """Commit SHAs (or other identifiers) backing this signal."""

    explanation: Explanation | None = None
    """Per-rule trace populated when ``whycode why --explain`` is on."""


# ----- thresholds -----------------------------------------------------------
COUPLING_MIN_COCHANGES = 3
SILENCE_DAYS = 180
GHOST_KEEPER_DAYS = 365
HIGH_CHURN_WINDOW_DAYS = 90
HIGH_CHURN_MIN_COMMITS = 6
NEWBORN_DAYS = 14


def _now() -> datetime:
    return datetime.now(UTC)


def _days_since(when: datetime) -> int:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, (_now() - when).days)


def _short(sha: str) -> str:
    return sha[:7]


def _age_phrase(days: int) -> str:
    """Render a days count as a human phrase used in headlines."""
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 90:
        return f"{days // 7} weeks ago"
    if days < 730:
        return f"{days // 30} months ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def _decay_severity(severity: int, days_since_most_recent: int) -> int:
    """Reduce severity for older signals; never below 1.

    Buckets chosen for legibility:
      - < 2 years: full weight (the team likely still remembers this)
      - 2-5 years: drop one severity step (memory is fading)
      - > 5 years: drop two steps, but keep at least 1 (still real evidence)
    """
    if days_since_most_recent > 1825:
        return max(1, severity - 2)
    if days_since_most_recent > 730:
        return max(1, severity - 1)
    return severity


def _classify_incident_commit(commit: Commit) -> tuple[str, str, tuple[str, ...]]:
    """Return ``(rule, why, evidence_tokens)`` for the rule branch that fired.

    Mirrors the acceptance ladder in :func:`whycode.git_facts.find_incidents`
    so an :class:`Explanation` can name which clause matched on this specific
    commit. Evaluation order matches the ladder; the first match wins.
    """
    subject = commit.subject
    body = commit.body
    cve = gf._CVE_RE.search(subject)
    if cve:
        return (
            "incident_subject_security_advisory",
            f"subject cites the security advisory {cve.group(0)!r}",
            (cve.group(0),),
        )
    ghsa = gf._GHSA_RE.search(subject)
    if ghsa:
        return (
            "incident_subject_security_advisory",
            f"subject cites the security advisory {ghsa.group(0)!r}",
            (ghsa.group(0),),
        )
    if gf._REVERTED_SUBJECT_RE.search(subject) or gf._REVERTS_SUBJECT_RE.search(subject):
        return (
            "incident_subject_revert_marker",
            "subject is a default git revert pointer (Reverted/Reverts)",
            ("Reverted",) if gf._REVERTED_SUBJECT_RE.search(subject) else ("Reverts",),
        )
    cc = gf._BREAKING_CC_RE.search(subject)
    if cc:
        return (
            "incident_subject_conventional_commits_breaking",
            f"subject carries the Conventional Commits breaking marker {cc.group(0).strip()!r}",
            (cc.group(0).strip(),),
        )
    # Subject keyword path (regression handled with corroboration inside the helper).
    if gf._is_subject_incident(subject, body):
        # Find which keyword actually matched in subject.
        match = gf._INCIDENT_RE.search(subject)
        if match:
            tok = match.group(0)
            return (
                "incident_subject_keyword",
                f"subject {subject[:60]!r} matched the literal token {tok!r}",
                (tok,),
            )
        # _is_subject_incident also accepts the regression+id corroboration
        # path even when no other keyword is in the subject.
        return (
            "incident_subject_regression_corroborated",
            "subject contains 'regression' and a corroborating issue id",
            ("regression",),
        )
    if gf._BREAKING_FOOTER_RE.search(body):
        return (
            "incident_body_breaking_change_footer",
            "body carries the structured 'BREAKING CHANGE:' footer",
            ("BREAKING CHANGE:",),
        )
    body_kw = gf._INCIDENT_RE.search(body)
    issue = gf._ISSUE_ID_RE.search(body)
    if body_kw and issue:
        return (
            "incident_body_keyword_with_issue_id",
            (
                f"body keyword {body_kw.group(0)!r} corroborated by issue id "
                f"{issue.group(0)!r}"
            ),
            (body_kw.group(0), issue.group(0)),
        )
    # Should not happen — find_incidents only returns commits that match one
    # of the branches above. Falling through means the ladder grew without
    # this helper. Return a neutral classification so the explanation surface
    # never crashes.
    return ("incident_unclassified", "matched the incident ladder", ())


def detect_revert_chain(facts: RepoFacts) -> Signal | None:
    if not facts.revert_pairs:
        return None
    n = len(facts.revert_pairs)
    severity = min(5, 2 + n)
    revert_shas = {sha for sha, _ in facts.revert_pairs}
    revert_commits = [c for c in facts.commits if c.sha in revert_shas]
    age_phrase = ""
    if revert_commits:
        days = _days_since(max(c.authored_at for c in revert_commits))
        severity = _decay_severity(severity, days)
        age_phrase = f" (most recent: {_age_phrase(days)})"
    evidence = tuple(_short(rev) for rev, _ in facts.revert_pairs)
    pairs_text = ", ".join(f"{_short(rev)} reverts {_short(orig)}" for rev, orig in facts.revert_pairs)
    explanation = Explanation(
        rule="revert_pair_default_message",
        why_it_fired=(
            f"{n} commit{' body matches' if n == 1 else ' bodies match'} the default git "
            "revert footer 'This reverts commit <sha>'"
        ),
        evidence=evidence,
        source_ref="src/whycode/git_facts.py:find_revert_pairs",
    )
    return Signal(
        kind=SignalKind.REVERT_CHAIN,
        severity=severity,
        headline=f"{n} revert{'s' if n != 1 else ''} touched this file{age_phrase}",
        detail=f"Reverts in this file's history: {pairs_text}.",
        evidence=evidence,
        explanation=explanation,
    )


def detect_incident_history(facts: RepoFacts) -> Signal | None:
    if not facts.incident_commits:
        return None
    most_recent = max(facts.incident_commits, key=lambda c: c.authored_at)
    days = _days_since(most_recent.authored_at)
    n = len(facts.incident_commits)
    severity = 4 if (days < 90 and n >= 2) else 3 if days < 365 else 2
    detail = (
        f"{n} commit{'s' if n != 1 else ''} matched incident keywords "
        f"(latest {days} day{'s' if days != 1 else ''} ago: '{most_recent.subject[:80]}')."
    )
    rule, why, tokens = _classify_incident_commit(most_recent)
    explanation = Explanation(
        rule=rule,
        why_it_fired=why,
        evidence=tokens,
        source_ref="src/whycode/git_facts.py:find_incidents",
    )
    return Signal(
        kind=SignalKind.INCIDENT_HISTORY,
        severity=severity,
        headline=f"{n} incident-flagged change{'s' if n != 1 else ''} in history",
        detail=detail,
        evidence=tuple(_short(c.sha) for c in facts.incident_commits[:5]),
        explanation=explanation,
    )


def detect_high_churn(facts: RepoFacts) -> Signal | None:
    cutoff_days = HIGH_CHURN_WINDOW_DAYS
    recent = [c for c in facts.commits if _days_since(c.authored_at) <= cutoff_days]
    if len(recent) < HIGH_CHURN_MIN_COMMITS:
        return None
    severity = 3 if len(recent) < 12 else 4
    explanation = Explanation(
        rule="high_churn_recent_window",
        why_it_fired=(
            f"{len(recent)} commits within the last {cutoff_days} days crossed the "
            f"threshold of {HIGH_CHURN_MIN_COMMITS}"
        ),
        evidence=(
            f"recent_commits={len(recent)}",
            f"window_days={cutoff_days}",
            f"threshold={HIGH_CHURN_MIN_COMMITS}",
        ),
        source_ref="src/whycode/signals.py:detect_high_churn",
    )
    return Signal(
        kind=SignalKind.HIGH_CHURN,
        severity=severity,
        headline=f"High churn: {len(recent)} commits in last {cutoff_days} days",
        detail="Code that changes this often is rarely settled — read recent diffs first.",
        evidence=tuple(_short(c.sha) for c in recent[:5]),
        explanation=explanation,
    )


def detect_coupling(facts: RepoFacts) -> Signal | None:
    """Files that change together with the target file, ranked by frequency.

    Co-change candidates are filtered through the same ignore list that
    powers ``whycode scan`` (built-in defaults plus an optional repo-local
    ``.whycodeignore``). Without this filter, a per-file coupling signal
    would surface ``CHANGELOG``, ``.github/workflows/*.yml``, ``AUTHORS``
    and similar high-touch metadata as the file's "tight coupling" — the
    field-test report flagged ``flask/app.py``'s top co-changers as 60%
    metadata, leaving only two genuinely informative entries. Applying
    the same filter here keeps the most-shown signal honest.
    """
    patterns = ign.effective_patterns(facts.repo_root)
    paired = [
        (p, n)
        for p, n in facts.co_changed_files.items()
        if n >= COUPLING_MIN_COCHANGES and not ign.is_ignored(p, patterns)
    ]
    if not paired:
        return None
    paired.sort(key=lambda x: (-x[1], x[0]))
    top = paired[:5]
    severity = 3 if top[0][1] < 6 else 4
    listed = "; ".join(f"{p} (x{n})" for p, n in top)
    top_path, top_count = top[0]
    explanation = Explanation(
        rule="coupling_co_change_threshold",
        why_it_fired=(
            f"{top_path!r} changed together with this file {top_count} times "
            f"(threshold {COUPLING_MIN_COCHANGES})"
        ),
        evidence=(
            f"top_co_changer={top_path}",
            f"co_changes={top_count}",
            f"threshold={COUPLING_MIN_COCHANGES}",
        ),
        source_ref="src/whycode/signals.py:detect_coupling",
    )
    return Signal(
        kind=SignalKind.COUPLING,
        severity=severity,
        headline=f"Tightly coupled to {len(top)} other file{'s' if len(top) != 1 else ''}",
        detail=f"Tends to change together with: {listed}.",
        evidence=tuple(p for p, _ in top),
        explanation=explanation,
    )


def detect_silence(facts: RepoFacts) -> Signal | None:
    if not facts.commits:
        return None
    most_recent = facts.commits[0]
    days = _days_since(most_recent.authored_at)
    if days < SILENCE_DAYS:
        return None
    severity = 2 if days < 365 else 3
    explanation = Explanation(
        rule="silence_untouched_threshold",
        why_it_fired=(
            f"most recent commit on this file is {days} days old, exceeding the "
            f"{SILENCE_DAYS}-day silence threshold"
        ),
        evidence=(
            f"days_since_last_commit={days}",
            f"threshold={SILENCE_DAYS}",
        ),
        source_ref="src/whycode/signals.py:detect_silence",
    )
    return Signal(
        kind=SignalKind.SILENCE,
        severity=severity,
        headline=f"Untouched for {days} days",
        detail=(
            "Long-quiet code is often load-bearing. Verify it is still exercised "
            "before assuming the silence means stability."
        ),
        evidence=(_short(most_recent.sha),),
        explanation=explanation,
    )


def detect_newborn(facts: RepoFacts) -> Signal | None:
    if not facts.commits:
        return None
    oldest = facts.commits[-1]
    days = _days_since(oldest.authored_at)
    if days > NEWBORN_DAYS:
        return None
    explanation = Explanation(
        rule="newborn_first_commit_window",
        why_it_fired=(
            f"oldest commit on this file is {days} day(s) old, within the "
            f"{NEWBORN_DAYS}-day newborn window"
        ),
        evidence=(
            f"days_since_first_commit={days}",
            f"threshold={NEWBORN_DAYS}",
        ),
        source_ref="src/whycode/signals.py:detect_newborn",
    )
    return Signal(
        kind=SignalKind.NEWBORN,
        severity=1,
        headline=f"New file (first commit {days} day{'s' if days != 1 else ''} ago)",
        detail="Limited history — the usual signals are not yet trustworthy.",
        evidence=(_short(oldest.sha),),
        explanation=explanation,
    )


def detect_ghost_keeper(facts: RepoFacts) -> Signal | None:
    """Has the file's primary author left the project?

    Ownership is measured by ``git blame`` lines on HEAD (so a single big
    initial commit by Alice still names Alice as primary even if Bob has 20
    small fixes after). When blame is unavailable, fall back to commit count.

    The signal fires only if the primary owner's last commit anywhere in the
    repo is older than ``GHOST_KEEPER_DAYS``. Severity scales with both how
    long they have been gone and how much of the current file they own.
    """
    if not facts.commits:
        return None

    blame = gf.line_ownership(facts.repo_root, facts.path, cache=facts.cache)
    ownership_share: float | None = None
    if blame:
        total_lines = sum(blame.values())
        if total_lines == 0:
            return None
        primary_email = max(blame, key=lambda e: blame[e])
        ownership_share = blame[primary_email] / total_lines
    else:
        counts: dict[str, int] = {}
        for commit in facts.commits:
            counts[commit.author_email] = counts.get(commit.author_email, 0) + 1
        primary_email = max(counts, key=lambda e: counts[e])

    primary_last_seen = gf.author_last_activity(facts.repo_root, primary_email)
    if primary_last_seen is None or _days_since(primary_last_seen) < GHOST_KEEPER_DAYS:
        return None
    days_since_seen = _days_since(primary_last_seen)

    primary_commit: gf.Commit | None = None
    for commit in facts.commits:
        if commit.author_email == primary_email:
            primary_commit = commit
            break
    if primary_commit is None:
        primary_commit = facts.commits[0]

    severity = 3
    if days_since_seen > 730 or (ownership_share is not None and ownership_share >= 0.7):
        severity = 4
    if days_since_seen > 1825 and (ownership_share is None or ownership_share >= 0.5):
        severity = 5

    if ownership_share is not None:
        ownership_phrase = f"wrote {ownership_share:.0%} of current lines"
        ownership_evidence = f"line_ownership_share={ownership_share:.2f}"
        rule = "ghost_keeper_inactive_line_owner"
    else:
        share = sum(1 for c in facts.commits if c.author_email == primary_email)
        ownership_phrase = f"wrote {share} of {len(facts.commits)} commits here"
        ownership_evidence = f"commits_by_primary={share}"
        rule = "ghost_keeper_inactive_commit_owner"

    explanation = Explanation(
        rule=rule,
        why_it_fired=(
            f"primary author of this file has not committed anywhere for "
            f"{days_since_seen} days (threshold {GHOST_KEEPER_DAYS})"
        ),
        evidence=(
            f"days_since_active={days_since_seen}",
            f"threshold={GHOST_KEEPER_DAYS}",
            ownership_evidence,
        ),
        source_ref="src/whycode/signals.py:detect_ghost_keeper",
    )

    return Signal(
        kind=SignalKind.GHOST_KEEPER,
        severity=severity,
        headline=f"Primary author last active {days_since_seen} days ago",
        detail=(
            f"{primary_commit.author_name} {ownership_phrase}, but has not "
            f"committed anywhere in this repo for {days_since_seen} days. "
            f"Knowledge may have left the team."
        ),
        evidence=(_short(primary_commit.sha),),
        explanation=explanation,
    )


_INVARIANT_BULLETS = 3
_INVARIANT_BULLET_LEN = 110


_SENTENCE_MIN = 25


def _first_sentence(line: str, limit: int) -> str:
    """Return the first complete clause from ``line``, capped at ``limit``.

    A separator only counts as a clause-break once the prefix is at least
    ``_SENTENCE_MIN`` characters long — otherwise short tokens like "1." or
    "e.g." would split off into useless one-token bullets.
    """
    candidates = []
    for sep in (". ", "; ", " — ", " - "):
        idx = line.find(sep, _SENTENCE_MIN)
        if idx > 0:
            candidates.append(idx + (1 if sep.startswith(".") else 0))
    if candidates:
        cut = min(candidates)
        sliced = line[:cut].rstrip()
        if len(sliced) <= limit:
            return sliced
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def detect_invariant_quotes(facts: RepoFacts) -> Signal | None:
    if not facts.invariant_quotes:
        return None
    # Dedupe by line, preserving the first SHA.
    seen: dict[str, str] = {}
    for sha, line in facts.invariant_quotes:
        if line not in seen:
            seen[line] = sha
    total = len(seen)
    quotes = list(seen.items())[:_INVARIANT_BULLETS]
    bullets = [
        f"  > {_first_sentence(line, _INVARIANT_BULLET_LEN)}  ({_short(sha)})"
        for line, sha in quotes
    ]
    if total > _INVARIANT_BULLETS:
        bullets.append(f"  > …and {total - _INVARIANT_BULLETS} more in this file's history.")
    rendered = "\n".join(bullets)
    severity = 3 if total >= 2 else 2
    # Look up the most recent invariant-bearing commit and decay severity by age.
    quote_shas = {sha for _, sha in seen.items()}
    quote_commits = [c for c in facts.commits if c.sha in quote_shas]
    age_phrase = ""
    if quote_commits:
        days = _days_since(max(c.authored_at for c in quote_commits))
        severity = _decay_severity(severity, days)
        age_phrase = f" (most recent: {_age_phrase(days)})"
    evidence_shas: list[str] = []
    seen_shas: set[str] = set()
    for _, sha in quotes:
        short = _short(sha)
        if short not in seen_shas:
            seen_shas.add(short)
            evidence_shas.append(short)
    matched_tokens: list[str] = []
    seen_tokens: set[str] = set()
    for line in seen:
        m = gf._INVARIANT_RE.search(line)
        if m and m.group(0).lower() not in seen_tokens:
            seen_tokens.add(m.group(0).lower())
            matched_tokens.append(m.group(0))
    explanation = Explanation(
        rule="invariant_token_match_in_body",
        why_it_fired=(
            f"{total} commit body line{'s' if total != 1 else ''} contained a known "
            "invariant token (e.g. 'do not', 'must not', 'workaround', …)"
        ),
        evidence=tuple(matched_tokens) if matched_tokens else (f"matches={total}",),
        source_ref="src/whycode/git_facts.py:extract_invariant_quotes",
    )
    return Signal(
        kind=SignalKind.INVARIANT_QUOTE,
        severity=severity,
        headline=(
            f"{total} invariant{'s' if total != 1 else ''} stated by past authors"
            + age_phrase
        ),
        detail="Past authors used cautionary language in commit messages:\n" + rendered,
        evidence=tuple(evidence_shas),
        explanation=explanation,
    )


_DETECTORS = (
    detect_revert_chain,
    detect_incident_history,
    detect_invariant_quotes,
    detect_ghost_keeper,
    detect_coupling,
    detect_high_churn,
    detect_silence,
    detect_newborn,
)


def all_signals(facts: RepoFacts) -> list[Signal]:
    """Run every detector and return signals sorted by severity (loudest first).

    NEWBORN is suppressed when any other signal fires: it's a "we don't have
    enough history" hedge that becomes contradictory when the file has already
    surfaced real flags.
    """
    out: list[Signal] = []
    for detector in _DETECTORS:
        signal = detector(facts)
        if signal is not None:
            out.append(signal)
    if any(s.kind is not SignalKind.NEWBORN for s in out):
        out = [s for s in out if s.kind is not SignalKind.NEWBORN]
    out.sort(key=lambda s: (-s.severity, s.kind.value))
    return out


__all__ = [
    "Explanation",
    "Signal",
    "SignalKind",
    "all_signals",
    "detect_coupling",
    "detect_ghost_keeper",
    "detect_high_churn",
    "detect_incident_history",
    "detect_invariant_quotes",
    "detect_newborn",
    "detect_revert_chain",
    "detect_silence",
]


def __dir__() -> Sequence[str]:
    return __all__
