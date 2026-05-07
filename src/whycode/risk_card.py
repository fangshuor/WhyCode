"""Render a Risk Card for a single file.

Two output modes:
- ``render_text`` returns a rich-renderable Group suitable for terminals.
- ``to_dict`` returns a JSON-friendly dict (used by --json and the MCP server).
"""

from __future__ import annotations

from dataclasses import dataclass
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

    decisions: tuple[Decision, ...] = ()
    """L3 — LLM-extracted structured decisions. Empty unless ``--llm`` was on."""

    def with_decisions(self, decisions: tuple[Decision, ...]) -> RiskCard:
        """Return a copy with the L3 ``decisions`` field populated."""
        from dataclasses import replace

        return replace(self, decisions=decisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "score": self.score.value,
            "band": self.score.band.value,
            "commit_count": self.commit_count,
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
            "signals": [
                {
                    "kind": s.kind.value,
                    "severity": s.severity,
                    "headline": s.headline,
                    "detail": s.detail,
                    "evidence": list(s.evidence),
                }
                for s in self.signals
            ],
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
    if skip_ghost_keeper:
        signals = _all_signals_without_ghost_keeper(facts)
    else:
        signals = sig.all_signals(facts)
    if apply_suppressions:
        suppressions = supp.load(repo_root)
        signals = supp.filter_signals(signals, suppressions, path)
    s = score(signals)
    head = facts.commits[0] if facts.commits else None
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
    )


# Detectors whose evidence is already in :class:`RepoFacts` (no git blame, no
# follow-up shell-out). The ghost-keeper detector is the only one missing
# here — it calls ``git blame`` per-file, which is the diff command's
# remaining bottleneck after the log walk is shared.
_FAST_DETECTORS = (
    sig.detect_revert_chain,
    sig.detect_incident_history,
    sig.detect_invariant_quotes,
    sig.detect_coupling,
    sig.detect_high_churn,
    sig.detect_silence,
    sig.detect_newborn,
)


def _all_signals_without_ghost_keeper(facts: gf.RepoFacts) -> list[sig.Signal]:
    """Re-implement the public ``all_signals`` ladder, minus ghost-keeper.

    Mirrors the NEWBORN-suppression rule that ``signals.all_signals`` uses
    so that an empty signal list collapses cleanly to NEWBORN-only when the
    other detectors are all silent. If any other detector fires, NEWBORN is
    dropped, exactly as the canonical helper does.
    """
    out: list[sig.Signal] = []
    for detector in _FAST_DETECTORS:
        signal = detector(facts)
        if signal is not None:
            out.append(signal)
    if any(s.kind is not sig.SignalKind.NEWBORN for s in out):
        out = [s for s in out if s.kind is not sig.SignalKind.NEWBORN]
    out.sort(key=lambda s: (-s.severity, s.kind.value))
    return out


# ----- rendering ------------------------------------------------------------

_BAND_STYLE: dict[Band, str] = {
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
    style = _BAND_STYLE[card.score.band]
    title = Text()
    title.append(" ")
    title.append(card.score.band.value, style=style)
    title.append("  ")
    title.append(f"score {card.score.value}/100", style="bold")
    if card.as_of_sha:
        title.append(f"   as of {card.as_of_sha}", style="dim")
    body = Text()
    body.append(card.path, style="bold")
    body.append(f"   ({card.commit_count} commits)\n", style="dim")
    if card.most_recent_subject:
        # Subject must fit on one line inside an 80-col Panel: 80 minus borders,
        # padding, and the 8-char "Latest: " prefix leaves ~64 chars usable.
        subj = card.most_recent_subject
        if len(subj) > 64:
            subj = subj[:63] + "…"
        body.append("Latest: ", style="dim")
        body.append(subj + "\n", style="")
        body.append(
            f"        {card.most_recent_sha}  {card.most_recent_author}  "
            f"{(card.most_recent_at or '')[:10]}",
            style="dim",
        )
    return Panel(body, title=title, title_align="left", border_style="grey50")


def _evidence_redundant(evidence: tuple[str, ...], detail: str) -> bool:
    """True if every evidence token already appears verbatim in the detail."""
    if not evidence:
        return True
    return all(token in detail for token in evidence)


def _signals_table(signals: tuple[sig.Signal, ...], *, explain: bool = False) -> Table | Text:
    if not signals:
        return Text(
            "No flags fired. The history is quiet — this is information, "
            "not safety. Read the diff anyway.",
            style="italic dim",
        )
    table = Table(show_header=False, box=None, padding=(0, 1, 1, 1), expand=True)
    table.add_column(width=7, no_wrap=True)
    table.add_column(ratio=1)
    for s in signals:
        block = Text()
        block.append(s.headline + "\n", style="bold")
        block.append(s.detail, style="")
        if s.evidence and not _evidence_redundant(s.evidence, s.detail):
            block.append("\nevidence: " + ", ".join(s.evidence), style="dim")
        if explain and s.explanation is not None:
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
        table.add_row(_severity_badge(s.severity), block)
    return table


def _next_step_hint(signals: tuple[sig.Signal, ...]) -> Text | None:
    """Suggest a single concrete next action if a SHA-shaped evidence exists."""
    for s in signals:
        for token in s.evidence:
            if 7 <= len(token) <= 12 and all(c in "0123456789abcdef" for c in token):
                hint = Text()
                hint.append("→ ", style="bold")
                hint.append(f"git show {token}", style="bold cyan")
                hint.append("   to read the most relevant commit in full", style="dim")
                return hint
    return None


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


def render_text(card: RiskCard, *, explain: bool = False) -> Group:
    pieces: list[Any] = [
        _header(card),
        Padding(_signals_table(card.signals, explain=explain), (0, 1, 0, 1)),
    ]
    if card.decisions:
        pieces.append(_decisions_block(card.decisions))
    hint = _next_step_hint(card.signals)
    if hint is not None:
        pieces.append(Padding(hint, (0, 1, 1, 2)))
    return Group(*pieces)


__all__ = ["RiskCard", "build", "build_from_diff_facts", "render_text"]
