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

if TYPE_CHECKING:
    from whycode.git_facts import RepoFacts


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
class Signal:
    kind: SignalKind
    severity: int  # 1..5; 5 is loudest.
    headline: str
    detail: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    """Commit SHAs (or other identifiers) backing this signal."""


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
    return Signal(
        kind=SignalKind.REVERT_CHAIN,
        severity=severity,
        headline=f"{n} revert{'s' if n != 1 else ''} touched this file{age_phrase}",
        detail=f"Reverts in this file's history: {pairs_text}.",
        evidence=evidence,
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
    return Signal(
        kind=SignalKind.INCIDENT_HISTORY,
        severity=severity,
        headline=f"{n} incident-flagged change{'s' if n != 1 else ''} in history",
        detail=detail,
        evidence=tuple(_short(c.sha) for c in facts.incident_commits[:5]),
    )


def detect_high_churn(facts: RepoFacts) -> Signal | None:
    cutoff_days = HIGH_CHURN_WINDOW_DAYS
    recent = [c for c in facts.commits if _days_since(c.authored_at) <= cutoff_days]
    if len(recent) < HIGH_CHURN_MIN_COMMITS:
        return None
    severity = 3 if len(recent) < 12 else 4
    return Signal(
        kind=SignalKind.HIGH_CHURN,
        severity=severity,
        headline=f"High churn: {len(recent)} commits in last {cutoff_days} days",
        detail="Code that changes this often is rarely settled — read recent diffs first.",
        evidence=tuple(_short(c.sha) for c in recent[:5]),
    )


def detect_coupling(facts: RepoFacts) -> Signal | None:
    paired = [(p, n) for p, n in facts.co_changed_files.items() if n >= COUPLING_MIN_COCHANGES]
    if not paired:
        return None
    paired.sort(key=lambda x: (-x[1], x[0]))
    top = paired[:5]
    severity = 3 if top[0][1] < 6 else 4
    listed = "; ".join(f"{p} (x{n})" for p, n in top)
    return Signal(
        kind=SignalKind.COUPLING,
        severity=severity,
        headline=f"Tightly coupled to {len(top)} other file{'s' if len(top) != 1 else ''}",
        detail=f"Tends to change together with: {listed}.",
        evidence=tuple(p for p, _ in top),
    )


def detect_silence(facts: RepoFacts) -> Signal | None:
    if not facts.commits:
        return None
    most_recent = facts.commits[0]
    days = _days_since(most_recent.authored_at)
    if days < SILENCE_DAYS:
        return None
    severity = 2 if days < 365 else 3
    return Signal(
        kind=SignalKind.SILENCE,
        severity=severity,
        headline=f"Untouched for {days} days",
        detail=(
            "Long-quiet code is often load-bearing. Verify it is still exercised "
            "before assuming the silence means stability."
        ),
        evidence=(_short(most_recent.sha),),
    )


def detect_newborn(facts: RepoFacts) -> Signal | None:
    if not facts.commits:
        return None
    oldest = facts.commits[-1]
    days = _days_since(oldest.authored_at)
    if days > NEWBORN_DAYS:
        return None
    return Signal(
        kind=SignalKind.NEWBORN,
        severity=1,
        headline=f"New file (first commit {days} day{'s' if days != 1 else ''} ago)",
        detail="Limited history — the usual signals are not yet trustworthy.",
        evidence=(_short(oldest.sha),),
    )


def detect_ghost_keeper(facts: RepoFacts) -> Signal | None:
    """Is the file's primary author still active in the repo?

    The "primary author" is the email that wrote the most commits we have for
    this file. If their last activity anywhere in the repo is older than
    ``GHOST_KEEPER_DAYS``, the file has lost its keeper.
    """
    if not facts.commits:
        return None
    counts: dict[str, int] = {}
    sample: dict[str, gf.Commit] = {}
    for commit in facts.commits:
        counts[commit.author_email] = counts.get(commit.author_email, 0) + 1
        sample.setdefault(commit.author_email, commit)
    primary_email = max(counts, key=lambda e: counts[e])
    primary_commit = sample[primary_email]
    last_seen = gf.author_last_activity(facts.repo_root, primary_email)
    if last_seen is None:
        return None
    days_since_seen = _days_since(last_seen)
    if days_since_seen < GHOST_KEEPER_DAYS:
        return None
    severity = 4 if days_since_seen > 730 else 3
    return Signal(
        kind=SignalKind.GHOST_KEEPER,
        severity=severity,
        headline=f"Primary author last active {days_since_seen} days ago",
        detail=(
            f"{primary_commit.author_name} wrote {counts[primary_email]} of "
            f"{len(facts.commits)} commits here, but has not committed anywhere in "
            f"this repo for {days_since_seen} days. Knowledge may have left the team."
        ),
        evidence=(_short(primary_commit.sha),),
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
    return Signal(
        kind=SignalKind.INVARIANT_QUOTE,
        severity=severity,
        headline=(
            f"{total} invariant{'s' if total != 1 else ''} stated by past authors"
            + age_phrase
        ),
        detail="Past authors used cautionary language in commit messages:\n" + rendered,
        evidence=tuple(evidence_shas),
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
