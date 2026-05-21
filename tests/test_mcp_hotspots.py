"""Tests for the ``get_file_hotspots`` MCP tool.

Mirrors the JSON shape that ``whycode hotspots --json`` emits so an MCP
host can pivot from a HANDLE-WITH-CARE risk profile into the sub-file
band detail without parsing terminal output.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from whycode import mcp_server


@pytest.fixture()
def in_repo(repo) -> Iterator[Path]:  # type: ignore[no-untyped-def]
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        yield repo.root
    finally:
        os.chdir(cwd)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _payload(text_contents) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(text_contents[0].text)


def test_mcp_get_file_hotspots_returns_payload(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: build history that produces a real band, then call the
    MCP tool and confirm the payload carries the expected shape."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    lines = [f"line {i}" for i in range(1, 201)]
    lines[99] = "def prepare_body(self, data, files, json):"
    lines[100] = "    # critical region"
    builder.commit(
        "init refund flow",
        {"refund.py": "\n".join(lines) + "\n"},
        when=days_ago(120),
    )
    for i, day in enumerate((100, 80, 60), start=1):
        lines[99] = f"def prepare_body(self, data{i}, files, json):"
        lines[100] = f"    # critical region rev{i}"
        builder.commit(
            f"refactor prepare_body pass {i}",
            {"refund.py": "\n".join(lines) + "\n"},
            when=days_ago(day),
        )
    lines[99] = "def prepare_body(self, data_v4, files, json, headers):"
    bad_sha = builder.commit(
        "tweak prepare_body signature",
        {"refund.py": "\n".join(lines) + "\n"},
        when=days_ago(40),
    )
    builder.revert(bad_sha, when=days_ago(35))
    lines[99] = "def prepare_body(self, data_v5, files, json):"
    lines[100] = "    # critical region after hotfix #401"
    builder.commit(
        "hotfix: refund regression in prepare_body",
        {"refund.py": "\n".join(lines) + "\n"},
        body="incident #401",
        when=days_ago(10),
    )

    out = mcp_server._handle_file_hotspots({"path": "refund.py", "top": 5})
    payload = _payload(out)
    assert payload["path"] == "refund.py"
    assert isinstance(payload["head_sha"], str)
    assert isinstance(payload["hotspots"], list)
    assert payload["hotspots"], payload
    first = payload["hotspots"][0]
    for key in (
        "rank",
        "line_start",
        "line_end",
        "touch_count",
        "revert_count",
        "incident_count",
        "score",
        "sample_shas",
        "anchor",
    ):
        assert key in first, (key, first)
    assert first["rank"] == 1
    # The seeded prepare_body region must surface as the top band.
    assert first["anchor"] and "prepare_body" in first["anchor"]


def test_mcp_get_file_hotspots_includes_tool_in_list(in_repo) -> None:  # type: ignore[no-untyped-def]
    """The tool must be advertised in the MCP server's tool list."""
    server = mcp_server._build_server()
    from mcp.types import ListToolsRequest

    req = ListToolsRequest(method="tools/list")
    handler = server.request_handlers[ListToolsRequest]
    result = _run(handler(req))
    names = {t.name for t in result.root.tools}
    assert "get_file_hotspots" in names


def test_mcp_get_file_hotspots_empty_when_no_history(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A quiet file with no qualifying bands returns an empty list,
    not an error — the consuming agent should be able to call this any
    time without crashing the conversation."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    builder.commit("init", {"calm.py": "1\n"}, when=days_ago(20))

    out = mcp_server._handle_file_hotspots({"path": "calm.py"})
    payload = _payload(out)
    assert payload["path"] == "calm.py"
    assert payload["hotspots"] == []
