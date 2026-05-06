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
from whycode.scorer import Band, Score, score

if TYPE_CHECKING:
    from pathlib import Path


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
        }


def build(
    repo_root: Path,
    path: str,
    *,
    max_commits: int | None = None,
    ref: str | None = None,
) -> RiskCard:
    facts = gf.gather(repo_root, path, max_commits=max_commits, ref=ref)
    signals = sig.all_signals(facts)
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


def _signals_table(signals: tuple[sig.Signal, ...]) -> Table | Text:
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


def render_text(card: RiskCard) -> Group:
    pieces: list[Any] = [
        _header(card),
        Padding(_signals_table(card.signals), (0, 1, 0, 1)),
    ]
    hint = _next_step_hint(card.signals)
    if hint is not None:
        pieces.append(Padding(hint, (0, 1, 1, 2)))
    return Group(*pieces)


__all__ = ["RiskCard", "build", "render_text"]
