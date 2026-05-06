"""MCP server for WhyCode.

Exposes WhyCode's Risk Card to MCP-aware editors and assistants so the host
LLM can pull a file's risk profile *before* it edits the code.

Tools
-----
- ``get_risk_profile(path)`` — full Risk Card.
- ``get_file_decisions(path, limit=5)`` — decision-flavoured signals only
  (incidents, reverts, invariants), highest severity first.

Prompts
-------
Reusable prompt templates the host can offer the user as one-click actions.
The server fills in WhyCode data; the host LLM does the actual reasoning.
No outbound network calls happen here -- prompts are pure local data plus a
short instruction wrapper, exactly like tools.

- ``before_edit_checklist(path)`` -- fetch the Risk Card and ask the model to
  walk the user through every HIGH-severity signal before suggesting an edit.
- ``summarise_for_postmortem(sha)`` -- fetch a commit's metadata and
  classification and ask the model to draft a postmortem-ready summary.
- ``risk_briefing_for_pr(base)`` -- fetch the diff risk briefing and ask the
  model to summarise it for a reviewer in 3-5 bullets.

The server speaks stdio. Configure your client with:

    {
      "mcpServers": {
        "whycode": {"command": "whycode", "args": ["mcp"]}
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
)

from whycode import git_facts as gf
from whycode import risk_card as rc
from whycode.signals import SignalKind

DECISION_KINDS = {
    SignalKind.REVERT_CHAIN,
    SignalKind.INCIDENT_HISTORY,
    SignalKind.INVARIANT_QUOTE,
    SignalKind.GHOST_KEEPER,
}


def _resolve(path: str) -> tuple[Path, str]:
    p = Path(path).resolve()
    start = p if p.is_dir() else p.parent if p.exists() else Path.cwd()
    repo_root = gf.discover_repo_root(start)
    if p.exists():
        try:
            return repo_root, str(p.relative_to(repo_root))
        except ValueError as exc:
            raise gf.GitError(f"{p} is not inside {repo_root}") from exc
    return repo_root, path


def _log_call(name: str, arguments: dict[str, Any]) -> None:
    """Print a one-line audit record to stderr (for `whycode mcp --verbose`)."""
    stamp = time.strftime("%H:%M:%S")
    path = arguments.get("path", "?")
    print(f"[whycode {stamp}] {name}(path={path!r})", file=sys.stderr, flush=True)


def _build_server(verbose: bool = False) -> Server:
    server: Server = Server("whycode")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_risk_profile",
                description=(
                    "Return the WhyCode Risk Card for the given file path: a 0..100 "
                    "score, a band label, and the list of fired signals (revert "
                    "chains, incidents, coupling, silence, ghost keeper, invariant "
                    "quotes). Call this BEFORE editing any file you are unfamiliar "
                    "with — the response includes the SHAs that justify each flag."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file (absolute or repo-relative).",
                        },
                        "max_commits": {
                            "type": "integer",
                            "description": "Optional cap on commits scanned.",
                        },
                    },
                    "required": ["path"],
                },
            ),
            Tool(
                name="get_file_decisions",
                description=(
                    "Return decision-flavoured signals only — past reverts, "
                    "incident-tagged changes, ghost keepers, and invariants stated "
                    "verbatim by past authors. Use when you specifically want the "
                    "'why' of past changes, not the broader risk picture."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["path"],
                },
            ),
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if verbose:
            _log_call(name, arguments)
        if name == "get_risk_profile":
            return _handle_risk_profile(arguments)
        if name == "get_file_decisions":
            return _handle_file_decisions(arguments)
        raise ValueError(f"Unknown tool: {name}")

    @server.list_prompts()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_prompts() -> list[Prompt]:
        return list(_PROMPTS)

    @server.get_prompt()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> GetPromptResult:
        if verbose:
            _log_call(f"prompt:{name}", dict(arguments or {}))
        return _render_prompt(name, arguments or {})

    return server


def _summary_text(card: rc.RiskCard) -> str:
    """One-paragraph prose summary of the card. Designed to be quotable verbatim
    by an LLM consumer without further processing."""
    if not card.signals:
        return (
            f"{card.path}: {card.score.band.value} ({card.score.value}/100). "
            f"No flagged signals across {card.commit_count} commits — but read "
            f"the diff anyway."
        )
    top = card.signals[0]
    extras = ""
    if len(card.signals) > 1:
        extras = f" Plus {len(card.signals) - 1} more signal(s) in the full card."
    return (
        f"{card.path}: {card.score.band.value} ({card.score.value}/100). "
        f"Top concern: {top.headline}.{extras}"
    )


def _handle_risk_profile(arguments: dict[str, Any]) -> list[TextContent]:
    path = str(arguments["path"])
    max_commits = arguments.get("max_commits")
    try:
        repo_root, rel = _resolve(path)
        card = rc.build(repo_root, rel, max_commits=max_commits)
    except gf.GitError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    payload = card.to_dict()
    payload["summary"] = _summary_text(card)
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _handle_file_decisions(arguments: dict[str, Any]) -> list[TextContent]:
    path = str(arguments["path"])
    limit = int(arguments.get("limit", 5))
    try:
        repo_root, rel = _resolve(path)
        card = rc.build(repo_root, rel)
    except gf.GitError as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
    decisions = [s for s in card.signals if s.kind in DECISION_KINDS][:limit]
    payload = {
        "path": card.path,
        "score": card.score.value,
        "band": card.score.band.value,
        "summary": _summary_text(card),
        "decisions": [
            {
                "kind": s.kind.value,
                "severity": s.severity,
                "headline": s.headline,
                "detail": s.detail,
                "evidence": list(s.evidence),
            }
            for s in decisions
        ],
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
#
# Prompts are saved-search shortcuts: the host editor surfaces them as
# one-click actions; the server fills in WhyCode data; the host LLM does
# the reasoning. They never make outbound network calls -- the data is
# strictly local git history, exactly like the tool surface.

_BEFORE_EDIT = "before_edit_checklist"
_POSTMORTEM = "summarise_for_postmortem"
_PR_BRIEFING = "risk_briefing_for_pr"

_PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        name=_BEFORE_EDIT,
        description=(
            "Fetch the Risk Card for a file and ask the assistant to walk the "
            "user through every HIGH-severity signal before suggesting any edit. "
            "Call this from the editor before you start changing an unfamiliar file."
        ),
        arguments=[
            PromptArgument(
                name="path",
                description="Path to the file (absolute or repo-relative).",
                required=True,
            ),
        ],
    ),
    Prompt(
        name=_POSTMORTEM,
        description=(
            "Fetch a commit's metadata and WhyCode classification and ask the "
            "assistant to draft a concise incident summary suitable for a "
            "postmortem document, citing specific evidence SHAs."
        ),
        arguments=[
            PromptArgument(
                name="sha",
                description="Commit SHA (full or short) to summarise.",
                required=True,
            ),
        ],
    ),
    Prompt(
        name=_PR_BRIEFING,
        description=(
            "Fetch the WhyCode risk briefing for files changed against a base "
            "ref and ask the assistant to summarise it for a PR reviewer in "
            "3-5 bullets, emphasising HANDLE WITH CARE files."
        ),
        arguments=[
            PromptArgument(
                name="base",
                description="Base ref to diff against (e.g. origin/main, main, HEAD~1).",
                required=True,
            ),
        ],
    ),
)


def _missing_arg(name: str, arg: str) -> GetPromptResult:
    """Render a friendly error as a user-role message, so the host displays it."""
    text = f"WhyCode prompt {name!r} requires the {arg!r} argument."
    return GetPromptResult(
        description=text,
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text)),
        ],
    )


def _git_error(name: str, exc: gf.GitError) -> GetPromptResult:
    text = f"WhyCode prompt {name!r} could not run: {exc}"
    return GetPromptResult(
        description=text,
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text)),
        ],
    )


def _render_prompt(name: str, arguments: dict[str, str]) -> GetPromptResult:
    if name == _BEFORE_EDIT:
        return _render_before_edit(arguments)
    if name == _POSTMORTEM:
        return _render_postmortem(arguments)
    if name == _PR_BRIEFING:
        return _render_pr_briefing(arguments)
    raise ValueError(f"Unknown prompt: {name}")


def _format_card_for_prompt(card: rc.RiskCard) -> str:
    """Render a Risk Card as plain text fit for embedding in a prompt body."""
    lines: list[str] = []
    lines.append(
        f"file: {card.path}\n"
        f"band: {card.score.band.value}\n"
        f"score: {card.score.value}/100\n"
        f"commits: {card.commit_count}"
    )
    if card.most_recent_subject:
        lines.append(
            f"latest: {card.most_recent_sha} -- {card.most_recent_subject} "
            f"({card.most_recent_author})"
        )
    if not card.signals:
        lines.append("signals: none fired")
        return "\n".join(lines)
    lines.append("signals:")
    for s in card.signals:
        sev = "HIGH" if s.severity >= 4 else "MED" if s.severity == 3 else "LOW"
        lines.append(f"  [{sev}] {s.kind.value}: {s.headline}")
        if s.detail:
            lines.append(f"      {s.detail}")
        if s.evidence:
            lines.append(f"      evidence: {', '.join(s.evidence)}")
    return "\n".join(lines)


def _render_before_edit(arguments: dict[str, str]) -> GetPromptResult:
    path = arguments.get("path")
    if not path:
        return _missing_arg(_BEFORE_EDIT, "path")
    try:
        repo_root, rel = _resolve(path)
        card = rc.build(repo_root, rel)
    except gf.GitError as exc:
        return _git_error(_BEFORE_EDIT, exc)

    high_signals = [s for s in card.signals if s.severity >= 4]
    body = (
        "WhyCode pulled the following Risk Card from local git history.\n"
        "Before suggesting any edit to this file, walk the user through every "
        "HIGH-severity signal below and ask them to confirm they understand "
        "each one. Quote the headline verbatim and cite the evidence SHAs. "
        "If no HIGH signals fired, say so explicitly and remind the user to "
        "read the diff anyway.\n\n"
        f"{_format_card_for_prompt(card)}\n\n"
        f"high-severity signals: {len(high_signals)}"
    )
    return GetPromptResult(
        description=(
            f"Pre-edit checklist for {card.path}: "
            f"{card.score.band.value} ({card.score.value}/100), "
            f"{len(high_signals)} HIGH-severity signal(s)."
        ),
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=body)),
        ],
    )


def _render_postmortem(arguments: dict[str, str]) -> GetPromptResult:
    sha = arguments.get("sha")
    if not sha:
        return _missing_arg(_POSTMORTEM, "sha")
    try:
        repo_root = gf.discover_repo_root(Path.cwd())
    except gf.GitError as exc:
        return _git_error(_POSTMORTEM, exc)
    commit = gf.read_commit(repo_root, sha)
    if commit is None:
        return _git_error(_POSTMORTEM, gf.GitError(f"could not read commit {sha!r}"))

    classification = gf.classify_commit(commit)
    invariants = gf.extract_invariant_quotes([commit])
    file_changes = gf.files_changed_in(repo_root, commit.sha)

    badges: list[str] = []
    if classification.incident_flavoured:
        badges.append("incident-flavoured")
    if invariants:
        badges.append(f"states {len(invariants)} invariant(s)")
    if not badges:
        badges.append("no special classification")

    lines: list[str] = []
    lines.append(f"sha: {commit.sha[:12]}")
    lines.append(f"author: {commit.author_name} <{commit.author_email}>")
    lines.append(f"authored_at: {commit.authored_at.isoformat()}")
    lines.append(f"subject: {commit.subject}")
    lines.append(f"classification: {', '.join(badges)}")
    lines.append(f"files_changed: {len(file_changes)}")
    if commit.body:
        lines.append("body:")
        for raw_line in commit.body.splitlines():
            lines.append(f"  {raw_line}")
    if invariants:
        lines.append("invariants stated by this commit:")
        for inv_sha, inv_line in invariants:
            lines.append(f"  ({inv_sha[:7]}) {inv_line}")
    if file_changes:
        lines.append("paths touched:")
        for change in file_changes[:20]:
            lines.append(f"  {change.path}")
        if len(file_changes) > 20:
            lines.append(f"  ... and {len(file_changes) - 20} more")

    body = (
        "WhyCode pulled the following commit metadata from local git history.\n"
        "Compose a concise incident summary suitable for a postmortem "
        "document. Cover what changed, why (drawing on the commit body), "
        "which files were touched, and any invariants the author stated. "
        "Cite specific evidence SHAs verbatim -- never invent commits not "
        "listed below. Keep it under 200 words; use plain prose, not bullet "
        "lists.\n\n" + "\n".join(lines)
    )
    return GetPromptResult(
        description=(
            f"Postmortem summary for {commit.sha[:12]}: "
            f"{', '.join(badges)}."
        ),
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=body)),
        ],
    )


def _render_pr_briefing(arguments: dict[str, str]) -> GetPromptResult:
    base = arguments.get("base")
    if not base:
        return _missing_arg(_PR_BRIEFING, "base")
    try:
        repo_root = gf.discover_repo_root(Path.cwd())
        raw = gf.run_git(repo_root, "diff", "--name-only", f"{base}...HEAD")
    except gf.GitError as exc:
        return _git_error(_PR_BRIEFING, exc)

    files = [line for line in raw.splitlines() if line.strip()]
    cards: list[rc.RiskCard] = []
    for f in files:
        try:
            cards.append(rc.build(repo_root, f))
        except gf.GitError:
            continue
    cards.sort(key=lambda c: -c.score.value)

    lines: list[str] = []
    lines.append(f"base: {base}")
    lines.append(f"files_changed: {len(files)}")
    if not cards:
        lines.append("no files with computable risk against this base")
    else:
        lines.append("risk-ranked files (highest first):")
        for c in cards[:20]:
            top = c.signals[0].headline if c.signals else "no flags"
            lines.append(
                f"  [{c.score.value:>3}] {c.score.band.value:<20} "
                f"{c.path} -- {top}"
            )

    body = (
        "WhyCode produced the following risk briefing for files changed "
        "against the base ref. Summarise it for a PR reviewer in 3-5 bullets, "
        "putting HANDLE WITH CARE files first and naming each by path and "
        "top signal. Do not invent risk that is not listed below; if the "
        "briefing is empty, say so honestly.\n\n" + "\n".join(lines)
    )
    handle_with_care = [c for c in cards if c.score.band.value == "HANDLE WITH CARE"]
    return GetPromptResult(
        description=(
            f"PR risk briefing vs {base}: {len(files)} file(s), "
            f"{len(handle_with_care)} HANDLE WITH CARE."
        ),
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=body)),
        ],
    )


async def _run(verbose: bool) -> None:
    server = _build_server(verbose=verbose)
    if verbose:
        print(
            "[whycode] MCP server up. Tool calls from the AI will be logged below.",
            file=sys.stderr,
            flush=True,
        )
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def serve(verbose: bool = False) -> None:
    """Block on the MCP server. Used by ``whycode mcp``."""
    asyncio.run(_run(verbose))


__all__ = ["serve"]
