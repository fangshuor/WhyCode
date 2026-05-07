"""Render a Risk Card for a single file.

Two output modes:
- ``render_text`` returns a rich-renderable Group suitable for terminals.
- ``to_dict`` returns a JSON-friendly dict (used by --json and the MCP server).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from whycode import git_facts as gf
from whycode import signals as sig
from whycode import suppressions as supp
from whycode.scorer import Band, Score, score

if TYPE_CHECKING:
    from pathlib import Path

    from whycode.cache import CacheStore
    from whycode.decisions import Decision


@dataclass(frozen=True)
class RiskCard:
    path: str
    score: Score
    signals: tuple[sig.Signal, ...]
    commit_count: int
    most_recent_sha: str | None
    most_recent_subject: str | None
    most_recent_author: str | None
    most_recent_at: str | None
    as_of_sha: str | None = None
    """When set, the card was computed *as of* this commit (historical view)."""

    primary_author: str | None = None
    """Dominant contributor by commit count (cheaper than ghost-keeper's blame)."""

    decisions: tuple[Decision, ...] = ()
    """L3 — LLM-extracted structured decisions. Empty unless ``--llm`` was on."""

    def with_decisions(self, decisions: tuple[Decision, ...]) -> RiskCard:
        """Return a copy with the L3 ``decisions`` field populated."""
        from dataclasses import replace

        return replace(self, decisions=decisions)

    def to_dict(self, *, explain: bool = False) -> dict[str, Any]:
        """Render the card as a JSON-friendly dict.

        With ``explain=True``, each signal entry grows an ``explanation``
        key carrying the rule identifier, prose, evidence, and source
        location. The key is omitted entirely when ``explain`` is off so
        default consumers see no shape change.
        """
        signals_out: list[dict[str, Any]] = []
        for s in self.signals:
            entry: dict[str, Any] = {
                "kind": s.kind.value,
                "severity": s.severity,
                "headline": s.headline,
                "detail": s.detail,
                "evidence": list(s.evidence),
                "next_step": s.next_step,
            }
            if explain:
                entry["explanation"] = (
                    {
                        "rule": s.explanation.rule,
                        "why_it_fired": s.explanation.why_it_fired,
                        "evidence": list(s.explanation.evidence),
                        "source_ref": s.explanation.source_ref,
                    }
                    if s.explanation is not None
                    else None
                )
            signals_out.append(entry)
        return {
            "path": self.path,
            "score": self.score.value,
            "band": self.score.band.value,
            "commit_count": self.commit_count,
            "primary_author": self.primary_author,
            "as_of": self.as_of_sha,
            "most_recent": (
                {
                    "sha": self.most_recent_sha,
                    "subject": self.most_recent_subject,
                    "author": self.most_recent_author,
                    "authored_at": self.most_recent_at,
                }
                if self.most_recent_sha
                else None
            ),
            "signals": signals_out,
            "decisions": [d.to_dict() for d in self.decisions],
        }


def build(
    repo_root: Path,
    path: str,
    *,
    max_commits: int | None = None,
    ref: str | None = None,
    apply_suppressions: bool = True,
    cache: CacheStore | None = None,
) -> RiskCard:
    """Build a Risk Card.

    By default, signals matching the local ``.whycode/suppressed.json`` list
    are dropped — that file is the user's "this signal is wrong, hide it"
    feedback. Pass ``apply_suppressions=False`` to bypass it (useful for
    debug or auditing what was hidden).

    A ``cache`` may be supplied so repeat invocations of this function on
    the same repo (e.g. inside ``scan`` or ``diff``) share a warm cache.
    """
    facts = gf.gather(repo_root, path, max_commits=max_commits, ref=ref, cache=cache)
    return _from_facts(
        path=path,
        facts=facts,
        repo_root=repo_root,
        ref=ref,
        apply_suppressions=apply_suppressions,
    )


def build_from_diff_facts(
    diff_facts: gf.DiffFacts,
    path: str,
    *,
    max_commits: int | None = None,
    apply_suppressions: bool = True,
    skip_ghost_keeper: bool = False,
) -> RiskCard:
    """Build a Risk Card from an in-memory :class:`DiffFacts` map.

    The diff command pre-loads one ``DiffFacts`` for the whole evaluation
    via :func:`whycode.git_facts.load_diff_facts`, then calls this helper
    once per changed file. The card's signals, score, and ``most_recent_*``
    fields all derive from the same in-memory map, so per-file cost is
    O(1) rather than the per-file ``git log --follow`` it replaces.

    With ``skip_ghost_keeper=True`` the per-file ``git blame`` call is
    deferred — the diff command uses this for its first pass over every
    changed file, then re-evaluates only the top-N with full signals.
    Without this skip, scoring 1,927 files spends ~4-5 minutes inside
    ``git blame`` even though > 95% of those files never reach the table
    the user sees.
    """
    facts = gf.gather_for_diff(diff_facts, path, max_commits=max_commits)
    return _from_facts(
        path=path,
        facts=facts,
        repo_root=diff_facts.repo_root,
        ref=None,
        apply_suppressions=apply_suppressions,
        skip_ghost_keeper=skip_ghost_keeper,
    )


def _from_facts(
    *,
    path: str,
    facts: gf.RepoFacts,
    repo_root: Path,
    ref: str | None,
    apply_suppressions: bool,
    skip_ghost_keeper: bool = False,
) -> RiskCard:
    """Common tail of :func:`build` and :func:`build_from_diff_facts`."""
    signals = sig.all_signals(facts, skip_ghost_keeper=skip_ghost_keeper)
    if apply_suppressions:
        suppressions = supp.load(repo_root)
        signals = supp.filter_signals(signals, suppressions, path)
    s = score(signals)
    head = facts.commits[0] if facts.commits else None
    primary = _primary_author(facts.commits)
    return RiskCard(
        path=path,
        score=s,
        signals=tuple(signals),
        commit_count=len(facts.commits),
        most_recent_sha=head.sha[:12] if head else None,
        most_recent_subject=head.subject if head else None,
        most_recent_author=head.author_name if head else None,
        most_recent_at=head.authored_at.isoformat() if head else None,
        as_of_sha=ref[:12] if ref else None,
        primary_author=primary,
    )


def _primary_author(commits: list[gf.Commit]) -> str | None:
    if not commits:
        return None
    counts: dict[str, int] = {}
    for c in commits:
        counts[c.author_name] = counts.get(c.author_name, 0) + 1
    return sorted(counts, key=lambda name: (-counts[name], name))[0]


# ----- rendering ------------------------------------------------------------

BAND_STYLE: dict[Band, str] = {
    Band.HANDLE_WITH_CARE: "bold white on red",
    Band.READ_HISTORY_FIRST: "bold black on yellow",
    Band.WORTH_A_LOOK: "bold black on cyan",
    Band.NO_FLAGS: "bold black on green",
}


def _severity_badge(severity: int) -> Text:
    """Replace cryptic glyphs with a labelled, colour-coded severity tag."""
    if severity >= 4:
        return Text(" HIGH ", style="bold white on red")
    if severity == 3:
        return Text(" MED  ", style="bold black on yellow")
    return Text(" LOW  ", style="bold black on cyan")


def _header(card: RiskCard) -> Panel:
    style = BAND_STYLE[card.score.band]
    title = Text()
    title.append(" ")
    title.append(card.score.band.value, style=style)
    title.append(f"  · {card.score.value}", style="dim")
    if card.as_of_sha:
        title.append(f"   as of {card.as_of_sha}", style="dim")
    body = Text()
    body.append(card.path, style="bold")
    body.append(f"   ({card.commit_count} commits)", style="dim")
    return Panel(body, title=title, title_align="left", border_style="grey50")


def _evidence_redundant(
    evidence: tuple[str, ...],
    detail: str,
    *,
    extra_context: str = "",
) -> bool:
    """True if every evidence token already appears verbatim somewhere visible.

    The signals table renders ``evidence: <sha>, <sha>`` only when the
    detail does not already mention those tokens. ``extra_context`` lets
    the caller add more visible surface — the card header, for instance,
    where ``most_recent_sha`` is already printed in dim text. Without this,
    the silence detector's single-SHA evidence line repeats what the header
    just showed two rows up.
    """
    if not evidence:
        return True
    haystack = detail + " " + extra_context
    return all(token in haystack for token in evidence)


def _signals_table(
    signals: tuple[sig.Signal, ...],
    *,
    explain: bool = False,
    extra_context: str = "",
) -> Table | Text:
    if not signals:
        return Text("No flags. Read the diff anyway.", style="italic dim")
    table = Table(show_header=False, box=None, padding=(0, 1, 1, 1), expand=True)
    table.add_column(width=7, no_wrap=True)
    table.add_column(ratio=1)
    for s in signals:
        block = Text()
        block.append(s.headline, style="bold")
        if s.detail:
            block.append("\n")
            block.append(s.detail, style="")
        if s.evidence and not _evidence_redundant(
            s.evidence, s.detail, extra_context=extra_context
        ):
            block.append("\nevidence: " + ", ".join(s.evidence), style="dim")
        if explain and s.explanation is not None:
            # --explain replaces the next_step line with the rule trace, so
            # each signal stays at one action-line regardless of mode.
            ex = s.explanation
            block.append("\n", style="")
            block.append("─ rule: ", style="dim")
            block.append(ex.rule, style="dim bold")
            if ex.source_ref:
                block.append("  ", style="dim")
                block.append(ex.source_ref, style="dim")
            block.append("\n  fired because: ", style="dim")
            block.append(ex.why_it_fired, style="dim")
            if ex.evidence:
                block.append("\n  evidence: " + ", ".join(ex.evidence), style="dim")
        elif s.next_step:
            block.append("\n→ " + s.next_step, style="dim")
        table.add_row(_severity_badge(s.severity), block)
    return table


def _decisions_block(decisions: tuple[Decision, ...]) -> Padding:
    """Render the L3 decisions section inside a labelled panel."""
    body = Text()
    for i, d in enumerate(decisions):
        if i:
            body.append("\n\n")
        # Header: type + confidence badge.
        body.append(f"{d.decision_type.replace('_', ' ').upper()}", style="bold cyan")
        body.append(f"   confidence {int(d.confidence * 100)}%\n", style="dim")
        body.append(d.what_changed + "\n", style="bold")
        body.append("Why: ", style="dim")
        body.append(d.why + "\n", style="italic")
        if d.do_not:
            body.append("Don't: ", style="bold red")
            body.append(d.do_not + "\n", style="")
        if d.evidence:
            short = ", ".join(s[:7] for s in d.evidence)
            body.append(f"evidence: {short}", style="dim")
    panel = Panel(
        body,
        title=Text(" DECISIONS (L3) ", style="bold white on magenta"),
        title_align="left",
        border_style="grey50",
    )
    return Padding(panel, (1, 1, 0, 1))


def _last_touched_phrase(most_recent_at: str | None) -> str:
    if not most_recent_at:
        return "recently"
    try:
        when = datetime.fromisoformat(most_recent_at)
    except ValueError:
        return "recently"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    days = max(0, (datetime.now(UTC) - when).days)
    return sig.age_phrase(days)


def _narrative_summary(card: RiskCard) -> Text:
    """Grounding sentence: how old the file is, who wrote it, when last touched.

    The signals table below already names the strongest concern + action; the
    narrative deliberately stops at the grounding so the user reads each fact
    once.
    """
    n = card.commit_count
    plural = "s" if n != 1 else ""
    summary = Text()
    summary.append(card.path, style="bold")
    if card.signals:
        last_touched = _last_touched_phrase(card.most_recent_at)
        primary = card.primary_author or "an unknown author"
        summary.append(f" is {n} commit{plural} old, primarily authored by ", style="")
        summary.append(primary, style="bold")
        summary.append(f", last touched {last_touched}.", style="")
    else:
        summary.append(
            f": no flags fired across {n} commit{plural}. Read the diff anyway.",
            style="",
        )
    return summary


def render_text(card: RiskCard, *, explain: bool = False) -> Group:
    extra_context = card.most_recent_sha or ""
    pieces: list[Any] = [
        _header(card),
        Padding(_narrative_summary(card), (0, 2, 1, 2)),
        Padding(
            _signals_table(card.signals, explain=explain, extra_context=extra_context),
            (0, 1, 0, 1),
        ),
    ]
    if card.decisions:
        pieces.append(_decisions_block(card.decisions))
    return Group(*pieces)


__all__ = ["BAND_STYLE", "RiskCard", "build", "build_from_diff_facts", "render_text"]
