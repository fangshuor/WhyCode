"""MCP server for WhyCode.

Exposes WhyCode's Risk Card to MCP-aware editors and assistants so the host
LLM can pull a file's risk profile *before* it edits the code.

Tools
-----
- ``get_risk_profile(path)`` — full Risk Card.
- ``get_file_decisions(path, limit=5)`` — decision-flavoured signals only
  (incidents, reverts, invariants), highest severity first.

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
from mcp.types import TextContent, Tool

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
