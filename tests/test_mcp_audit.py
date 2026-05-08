"""Tests for the ``audit_signal`` MCP tool and the ``get_risk_profile``
explain-split.

The audit tool is a deliberate side door: editor LLMs should rarely call it,
but when they do, the response must contain the rule trace fields a curious
human reader would otherwise have to dig out of WhyCode source manually. The
default ``get_risk_profile`` payload, by contrast, must NOT carry that debug
provenance — it costs context and adds nothing to narrative triage.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.types import Tool

from whycode import mcp_server


@pytest.fixture()
def in_repo(repo) -> Iterator[Path]:  # type: ignore[no-untyped-def]
    """Run the test body with cwd inside ``repo.root`` so ``_resolve``
    discovers the synthetic repo and not the developer's checkout."""
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        yield repo.root
    finally:
        os.chdir(cwd)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _list_tools() -> list[Tool]:
    """Drive the registered list_tools handler through the SDK request flow,
    matching how the MCP host actually queries the server."""
    server = mcp_server._build_server()
    from mcp.types import ListToolsRequest

    req = ListToolsRequest(method="tools/list")
    handler = server.request_handlers[ListToolsRequest]
    result = _run(handler(req))
    return list(result.root.tools)


def _payload(text_contents) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(text_contents[0].text)


# ----- audit_signal: fired-signal trace -------------------------------------


def test_audit_signal_returns_rule_trace_for_fired_signal(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A real revert chain must yield a populated rule trace pointing into
    the WhyCode source so a human can audit the detector ladder."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    sha = builder.commit("feature: refund flow", {"refund.py": "1"}, when=days_ago(40))
    builder.revert(sha, when=days_ago(35))
    builder.commit(
        "hotfix: idempotency token regression",
        {"refund.py": "2"},
        body="incident #99",
        when=days_ago(5),
    )

    out = mcp_server._handle_audit_signal(
        {"path": "refund.py", "kind": "revert_chain"}
    )
    payload = _payload(out)
    assert payload["fired"] is True
    assert payload["path"] == "refund.py"
    assert payload["kind"] == "revert_chain"
    assert isinstance(payload["rule"], str) and payload["rule"]
    assert isinstance(payload["evidence"], list)
    assert isinstance(payload["source_ref"], str)
    assert "signals.py" in payload["source_ref"] or "git_facts.py" in payload["source_ref"]


# ----- audit_signal: signal absent ------------------------------------------


def test_audit_signal_returns_not_fired_for_absent_signal(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A quiet file must report fired=False with a kind-naming message
    rather than fabricating a trace."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    builder.commit("init", {"calm.py": "1"}, when=days_ago(20))

    out = mcp_server._handle_audit_signal(
        {"path": "calm.py", "kind": "ghost_keeper"}
    )
    payload = _payload(out)
    assert payload["fired"] is False
    assert payload["kind"] == "ghost_keeper"
    assert "ghost_keeper" in payload["message"]


# ----- audit_signal: bogus path ---------------------------------------------


def test_audit_signal_handles_missing_path_with_error_payload(tmp_path) -> None:
    """A path outside any git repo must not raise; the handler returns
    an error payload the host can show to the user."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        out = mcp_server._handle_audit_signal(
            {"path": "/no/such/place/file.py", "kind": "revert_chain"}
        )
    finally:
        os.chdir(cwd)
    payload = _payload(out)
    assert "error" in payload


# ----- get_risk_profile: explanation-block removal --------------------------


def test_get_risk_profile_no_longer_emits_explanation_block(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Default ``get_risk_profile`` payload must contain no per-signal
    ``explanation`` key — that surface moved to ``audit_signal``."""
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    sha = builder.commit("feature: refund flow", {"refund.py": "1"}, when=days_ago(40))
    builder.revert(sha, when=days_ago(35))
    builder.commit(
        "hotfix: idempotency token regression",
        {"refund.py": "2"},
        body="incident #99",
        when=days_ago(5),
    )

    out = mcp_server._handle_risk_profile({"path": "refund.py"})
    payload = _payload(out)
    assert payload["signals"], "test scenario must fire at least one signal"
    for entry in payload["signals"]:
        assert "explanation" not in entry


def test_get_risk_profile_input_schema_no_longer_advertises_explain() -> None:
    """The schema announced to MCP clients must drop the ``explain``
    property so editors don't try to set it."""
    tools = {t.name: t for t in _list_tools()}
    assert "get_risk_profile" in tools
    properties = tools["get_risk_profile"].inputSchema["properties"]
    assert "explain" not in properties
    # Sanity: the remaining shape stays as documented.
    assert "path" in properties
    assert "max_commits" in properties


def test_list_tools_includes_audit_signal() -> None:
    """The new tool must be discoverable in the standard MCP tools/list flow."""
    tools = {t.name: t for t in _list_tools()}
    assert "audit_signal" in tools
    schema = tools["audit_signal"].inputSchema
    assert set(schema["required"]) == {"path", "kind"}
    assert set(schema["properties"]) == {"path", "kind"}
