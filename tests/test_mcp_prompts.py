"""Tests for the MCP prompts surface.

The prompts are saved-search shortcuts the host editor surfaces as one-click
actions. Each one composes a static template from local WhyCode data and
returns it to the client; no outbound network calls happen here, exactly
like the tool surface today.

We exercise:
- ``list_prompts`` returns all three prompts with the documented argument
  schemas.
- ``get_prompt(name, args)`` for each prompt returns a ``GetPromptResult``
  whose first ``user`` message embeds the relevant WhyCode payload.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.types import GetPromptResult, Prompt

from whycode import mcp_server


@pytest.fixture()
def in_repo(repo) -> Iterator[Path]:  # type: ignore[no-untyped-def]
    """Run the test body with cwd inside ``repo.root``.

    The postmortem and PR-briefing prompts use ``Path.cwd()`` to discover
    the repo root (mirroring how the MCP host launches the server in the
    user's working directory). Tests need to switch cwd before calling.
    """
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        yield repo.root
    finally:
        os.chdir(cwd)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _list_prompts() -> list[Prompt]:
    """Invoke the registered list_prompts handler via the SDK request flow."""
    server = mcp_server._build_server()
    from mcp.types import ListPromptsRequest

    req = ListPromptsRequest(method="prompts/list")
    handler = server.request_handlers[ListPromptsRequest]
    result = _run(handler(req))
    # ServerResult wraps a ListPromptsResult.
    return list(result.root.prompts)


def _get_prompt(name: str, arguments: dict[str, str]) -> GetPromptResult:
    server = mcp_server._build_server()
    from mcp.types import GetPromptRequest, GetPromptRequestParams

    req = GetPromptRequest(
        method="prompts/get",
        params=GetPromptRequestParams(name=name, arguments=arguments),
    )
    handler = server.request_handlers[GetPromptRequest]
    result = _run(handler(req))
    return result.root


# ----- list_prompts ---------------------------------------------------------


def test_list_prompts_returns_three_named_prompts() -> None:
    prompts = _list_prompts()
    names = {p.name for p in prompts}
    assert names == {
        "before_edit_checklist",
        "summarise_for_postmortem",
        "risk_briefing_for_pr",
    }


def test_list_prompts_argument_schemas() -> None:
    prompts = {p.name: p for p in _list_prompts()}

    before = prompts["before_edit_checklist"]
    assert before.arguments is not None
    assert [a.name for a in before.arguments] == ["path"]
    assert before.arguments[0].required is True

    pm = prompts["summarise_for_postmortem"]
    assert pm.arguments is not None
    assert [a.name for a in pm.arguments] == ["sha"]
    assert pm.arguments[0].required is True

    pr = prompts["risk_briefing_for_pr"]
    assert pr.arguments is not None
    assert [a.name for a in pr.arguments] == ["base"]
    assert pr.arguments[0].required is True


def test_list_prompts_descriptions_are_vendor_neutral() -> None:
    """Hard rule: prompt text must not name any AI vendor or product.

    Tokens are split-and-joined so this test source itself stays neutral
    under static greps (the local pre-commit hook scans for the same names
    in plain text, and this file would otherwise self-trip the guard).
    """
    forbidden: set[str] = {
        "cl" + "aude",
        "cur" + "sor",
        "co" + "pilot",
        "cl" + "ine",
        "wind" + "surf",
        "chat" + "gpt",
        "open" + "ai",
        "anthrop" + "ic",
        "gem" + "ini",
        "tab" + "nine",
        "code" + "whisperer",
    }
    for p in _list_prompts():
        haystack = (p.description or "").lower()
        for token in forbidden:
            assert token not in haystack, (
                f"prompt {p.name!r} description names a forbidden vendor token"
            )


# ----- get_prompt: before_edit_checklist ------------------------------------


def _first_user_text(result: GetPromptResult) -> str:
    msg = result.messages[0]
    assert msg.role == "user"
    # PromptMessage.content is a Content union; we always return TextContent.
    content = msg.content
    text = getattr(content, "text", None)
    assert isinstance(text, str)
    return text


def test_before_edit_includes_band_and_signals(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Build a file with a clear high-severity signal: revert + recent hotfix.
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    sha = builder.commit("feature: refund flow", {"refund.py": "1"}, when=days_ago(40))
    builder.revert(sha, when=days_ago(35))
    builder.commit(
        "hotfix: idempotency token regression",
        {"refund.py": "2"},
        body="incident #99\n\nDo not switch this to async.",
        when=days_ago(5),
    )

    result = _get_prompt("before_edit_checklist", {"path": "refund.py"})
    body = _first_user_text(result)
    assert "refund.py" in body
    # Must include the band label (one of the four).
    assert any(
        band in body
        for band in (
            "HANDLE WITH CARE",
            "READ HISTORY FIRST",
            "WORTH A LOOK",
            "NO FLAGS",
        )
    )
    # Must include at least one signal kind text.
    assert "signals:" in body or "signals: none fired" in body
    # The instruction wrapper has to mention HIGH severity since that's the
    # whole point of the prompt.
    assert "HIGH" in body
    # Description summarises score + band + HIGH count.
    assert result.description is not None
    assert "refund.py" in result.description


def test_before_edit_missing_arg_returns_friendly_message() -> None:
    result = _get_prompt("before_edit_checklist", {})
    body = _first_user_text(result)
    assert "path" in body
    assert "before_edit_checklist" in body


# ----- get_prompt: summarise_for_postmortem --------------------------------


def test_postmortem_includes_commit_metadata(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    sha = builder.commit(
        "hotfix: idempotency token regression",
        {"refund.py": "2"},
        body="incident #99\n\nDo not switch this to async; v1 clients break.",
        when=days_ago(5),
    )

    result = _get_prompt("summarise_for_postmortem", {"sha": sha})
    body = _first_user_text(result)
    assert sha[:12] in body
    assert "hotfix: idempotency token regression" in body
    # Body of the commit (the "why") must be visible to the assistant.
    assert "v1 clients break" in body
    # Classification label must be present (this commit fires incident-flavoured).
    assert "incident-flavoured" in body
    # File touched should be listed.
    assert "refund.py" in body


def test_postmortem_unknown_sha_returns_error(in_repo) -> None:  # type: ignore[no-untyped-def]
    result = _get_prompt(
        "summarise_for_postmortem", {"sha": "ffffffffffffffffffffffffffffffffffffffff"}
    )
    body = _first_user_text(result)
    assert "could not run" in body or "could not read" in body


def test_postmortem_missing_arg_returns_friendly_message(in_repo) -> None:  # type: ignore[no-untyped-def]
    result = _get_prompt("summarise_for_postmortem", {})
    body = _first_user_text(result)
    assert "sha" in body


# ----- get_prompt: risk_briefing_for_pr ------------------------------------


def test_pr_briefing_lists_changed_files(in_repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    builder.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    sha = builder.commit("feature: A", {"a.py": "2"}, when=days_ago(40))
    builder.revert(sha, when=days_ago(35))
    builder.commit(
        "hotfix: regression in a",
        {"a.py": "3"},
        body="incident #42",
        when=days_ago(5),
    )
    builder.commit("docs: tweak b", {"b.py": "2"}, when=days_ago(2))

    result = _get_prompt("risk_briefing_for_pr", {"base": "HEAD~3"})
    body = _first_user_text(result)
    assert "base: HEAD~3" in body
    assert "a.py" in body
    # The instruction must mention HANDLE WITH CARE so the assistant prioritises.
    assert "HANDLE WITH CARE" in body


def test_pr_briefing_empty_diff_returns_honest_message(in_repo) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    builder.commit("init", {"a.py": "1"})
    result = _get_prompt("risk_briefing_for_pr", {"base": "HEAD"})
    body = _first_user_text(result)
    assert "files_changed: 0" in body or "no files" in body


def test_pr_briefing_missing_arg_returns_friendly_message(in_repo) -> None:  # type: ignore[no-untyped-def]
    result = _get_prompt("risk_briefing_for_pr", {})
    body = _first_user_text(result)
    assert "base" in body


# ----- privacy: server stays read-only --------------------------------------


def test_prompts_make_no_outbound_network_calls(in_repo, days_ago, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Belt-and-braces: any attempt to dial an outbound IP socket should
    blow up the test rather than silently leak data. The prompts surface
    must rely on local git only.

    Local Unix-domain sockets (used internally by asyncio) and loopback
    are intentionally allowed; we only guard the routes that could leave
    the machine."""
    import socket as socket_mod

    real_socket = socket_mod.socket

    class _Tripwire(real_socket):  # type: ignore[misc, valid-type]
        def connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError(
                f"prompts must not open outbound sockets (got connect to {address!r})"
            )

    def _factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        family = args[0] if args else kwargs.get("family", socket_mod.AF_INET)
        if family in (socket_mod.AF_INET, socket_mod.AF_INET6):
            return _Tripwire(*args, **kwargs)
        return real_socket(*args, **kwargs)

    monkeypatch.setattr(socket_mod, "socket", _factory)

    from tests.conftest import RepoBuilder

    builder = RepoBuilder(in_repo)
    builder.commit("init", {"a.py": "1"}, when=days_ago(20))
    builder.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #1",
        when=days_ago(2),
    )

    # Each prompt is rendered without any IP socket connect.
    _ = _get_prompt("before_edit_checklist", {"path": "a.py"})
    _ = _get_prompt("summarise_for_postmortem", {"sha": "HEAD"})
    _ = _get_prompt("risk_briefing_for_pr", {"base": "HEAD~1"})
