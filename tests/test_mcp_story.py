"""Tests for the ``get_file_story`` MCP tool."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.types import TextContent

from whycode import mcp_server


@pytest.fixture()
def in_repo(repo) -> Iterator[Path]:  # type: ignore[no-untyped-def]
    """Run the test body with cwd inside ``repo.root``.

    The MCP tool resolves paths relative to ``Path.cwd()`` to mirror how
    the host editor launches the server in the user's working directory.
    """
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        yield repo.root
    finally:
        os.chdir(cwd)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _list_tools() -> list[object]:
    server = mcp_server._build_server()
    from mcp.types import ListToolsRequest

    req = ListToolsRequest(method="tools/list")
    handler = server.request_handlers[ListToolsRequest]
    result = _run(handler(req))
    return list(result.root.tools)


def _call_tool(name: str, arguments: dict[str, object]) -> list[TextContent]:
    server = mcp_server._build_server()
    from mcp.types import CallToolRequest, CallToolRequestParams

    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.request_handlers[CallToolRequest]
    result = _run(handler(req))
    # ServerResult wraps a CallToolResult with .content (list of TextContent).
    return list(result.root.content)


def test_mcp_get_file_story_listed_in_tools() -> None:
    """The story tool must appear in the catalog so MCP-aware editors can
    surface it as a one-click action."""
    names = {t.name for t in _list_tools()}  # type: ignore[attr-defined]
    assert "get_file_story" in names


def test_mcp_get_file_story_returns_chapters_payload(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A successful call returns one ``TextContent`` whose body is the
    documented top-level dict with a ``chapters`` array."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    sha_a = builder.commit(
        "feat: initial dispatch",
        {"a.py": "1"},
        body="No constraint stated.",
        when=days_ago(40),
    )
    builder.commit(
        "hotfix: dispatch regression",
        {"a.py": "2"},
        body=f"See #INC-1\n\nCross-reference: {sha_a[:10]}",
        when=days_ago(5),
    )
    contents = _call_tool("get_file_story", {"path": "a.py"})
    assert contents
    payload = json.loads(contents[0].text)
    assert payload["path"] == "a.py"
    assert isinstance(payload["chapters"], list)
    assert payload["chapters"], "expected at least one chapter"
    # Top-level documented keys all present.
    for key in (
        "path",
        "commit_count",
        "chapters_total",
        "chapters_returned",
        "primary_author",
        "summary",
    ):
        assert key in payload
