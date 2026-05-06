"""Tests for the CLI: covers `why`, `scan`, `version`, and the JSON path."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from whycode.cli import app

runner = CliRunner()


def _invoke(repo_root: Path, *args: str):  # type: ignore[no-untyped-def]
    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        return runner.invoke(app, list(args), catch_exceptions=False)
    finally:
        os.chdir(cwd)


def test_version_prints(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    result = _invoke(repo.root, "version")
    assert result.exit_code == 0
    assert result.output.strip()


def test_why_emits_card(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feature", {"refund.py": "1"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: edge case",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "why", "refund.py")
    assert result.exit_code == 0
    out = result.output
    assert "refund.py" in out
    assert any(band in out for band in ("HANDLE WITH CARE", "READ HISTORY", "WORTH A LOOK"))


def test_why_json_output_is_valid_json(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(40))
    result = _invoke(repo.root, "why", "a.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "a.py"
    assert "score" in data
    assert "signals" in data
    assert isinstance(data["signals"], list)


def test_why_handles_path_outside_repo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # No git repo here.
    target = tmp_path / "nope.txt"
    target.write_text("x")
    result = runner.invoke(app, ["why", str(target)], catch_exceptions=False)
    assert result.exit_code != 0


def test_why_warns_on_untracked_path(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    result = _invoke(repo.root, "why", "phantom.txt")
    assert result.exit_code == 1
    assert "warning" in result.output.lower()
    assert "phantom.txt" in result.output


def test_diff_lists_changed_files_in_risk_order(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Base commit: two files exist.
    repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    sha = repo.commit("feature: A", {"a.py": "2"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    # Now make a.py "changed against HEAD~3".
    repo.commit(
        "hotfix: regression in a",
        {"a.py": "3"},
        body="incident #42",
        when=days_ago(5),
    )
    repo.commit("docs: tweak b", {"b.py": "2"}, when=days_ago(2))
    result = _invoke(repo.root, "diff", "--base", "HEAD~3")
    assert result.exit_code == 0
    assert "a.py" in result.output


def test_diff_clean_when_no_changes(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "diff", "--base", "HEAD")
    assert result.exit_code == 0
    assert "no changes" in result.output.lower()


def test_diff_json_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(40))
    repo.commit("feat: A", {"a.py": "2"}, when=days_ago(20))
    result = _invoke(repo.root, "diff", "--base", "HEAD~1", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "base" in data
    assert "files" in data
    assert isinstance(data["files"], list)


def test_why_brief_one_line_format(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(40))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #1",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "why", "a.py", "--brief")
    assert result.exit_code == 0
    out = result.output.strip()
    assert "\n" not in out  # one line, no rich panels
    assert "a.py" in out
    assert any(band in out for band in ("HANDLE", "READ", "WORTH", "NO FLAGS"))


def test_diff_fail_on_triggers_when_threshold_breached(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #1",
        when=days_ago(20),
    )
    repo.commit("docs: tweak", {"a.py": "3"}, when=days_ago(2))
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--fail-on", "history")
    # a.py should reach READ HISTORY FIRST or higher → exit 1.
    assert result.exit_code == 1


def test_diff_fail_on_passes_below_threshold(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(20))
    repo.commit("docs: tweak", {"a.py": "2"}, when=days_ago(2))
    result = _invoke(repo.root, "diff", "--base", "HEAD~1", "--fail-on", "handle")
    # No high-severity signals here → score below 75 → exit 0.
    assert result.exit_code == 0


def test_diff_fail_on_unknown_band_errors(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "diff", "--base", "HEAD", "--fail-on", "bogus")
    assert result.exit_code != 0


def test_diff_staged_uses_index(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    # Stage a new change without committing.
    (repo.root / "a.py").write_text("v2")
    import subprocess

    subprocess.run(["git", "-C", str(repo.root), "add", "a.py"], check=True)
    result = _invoke(repo.root, "diff", "--staged")
    assert result.exit_code == 0
    assert "staged" in result.output.lower() or "a.py" in result.output


def test_scan_empty_state_explains_itself(repo) -> None:  # type: ignore[no-untyped-def]
    # A single trivial commit produces no flagged files (NEWBORN-only).
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "scan")
    assert result.exit_code == 0
    out = result.output.lower()
    # Must mention "no flagged" AND give the user a reason.
    assert "no flagged" in out
    assert "commit messages" in out or "terse" in out


def test_show_renders_for_a_real_commit(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit(
        "hotfix: refund regression",
        {"a.py": "1", "b.py": "1"},
        body="incident #INC-42",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "show", sha)
    assert result.exit_code == 0
    out = result.output
    assert sha[:12] in out
    assert "hotfix" in out.lower()
    assert "incident-flavored" in out.lower()
    assert "a.py" in out


def test_show_json_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit(
        "feat!: drop legacy api",
        {"a.py": "1"},
        body="BREAKING CHANGE: clients must migrate.",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "show", sha[:7], "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["sha"].startswith(sha[:7])
    assert data["incident_flavored"] is True
    assert data["files_changed"] == 1


def test_show_unknown_sha_errors(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "show", "deadbeefdeadbeef")
    assert result.exit_code != 0


def test_init_writes_workflow_and_hook(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "init")
    assert result.exit_code == 0
    workflow = repo.root / ".github" / "workflows" / "whycode.yml"
    hook = repo.root / ".git" / "hooks" / "pre-commit"
    assert workflow.exists()
    assert hook.exists()
    assert "name: WhyCode" in workflow.read_text()
    assert "whycode diff --staged" in hook.read_text()
    # Hook must be executable.
    import os
    assert os.access(hook, os.X_OK)


def test_init_skips_existing_without_force(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    workflow = repo.root / ".github" / "workflows" / "whycode.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("custom: do not overwrite\n")
    result = _invoke(repo.root, "init")
    assert result.exit_code == 0
    assert "skipped" in result.output.lower()
    # File must be untouched.
    assert workflow.read_text() == "custom: do not overwrite\n"


def test_init_force_overwrites(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    workflow = repo.root / ".github" / "workflows" / "whycode.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("old content\n")
    result = _invoke(repo.root, "init", "--force")
    assert result.exit_code == 0
    assert "name: WhyCode" in workflow.read_text()


def test_init_outside_repo_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["init"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0


def test_why_at_excludes_later_commits(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode why X --at <sha>` reflects history as of <sha>, not HEAD."""
    sha_old = repo.commit("init", {"refund.py": "1"}, when=days_ago(60))
    repo.commit(
        "hotfix: refund regression",
        {"refund.py": "2"},
        body="See #INC-447",
        when=days_ago(10),
    )
    # Asking "as of the init commit" must not see the later hotfix.
    result_old = _invoke(repo.root, "why", "refund.py", "--at", sha_old, "--json")
    assert result_old.exit_code == 0
    data_old = json.loads(result_old.output)
    assert data_old["commit_count"] == 1
    assert data_old["as_of"] is not None
    assert all(s["kind"] != "incident_history" for s in data_old["signals"])

    # And the current view DOES see it.
    result_now = _invoke(repo.root, "why", "refund.py", "--json")
    data_now = json.loads(result_now.output)
    assert data_now["commit_count"] == 2
    assert any(s["kind"] == "incident_history" for s in data_now["signals"])


def test_why_at_unknown_ref_errors(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "why", "a.py", "--at", "deadbeefdeadbeef")
    assert result.exit_code != 0


def test_timeline_lists_sample_points(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha1 = repo.commit("init", {"refund.py": "1"}, when=days_ago(120))
    repo.commit("feat: stuff", {"refund.py": "2"}, when=days_ago(80))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "3"},
        body="incident #INC-42",
        when=days_ago(40),
    )
    result = _invoke(repo.root, "timeline", "refund.py", "--samples", "10")
    assert result.exit_code == 0
    out = result.output
    assert "refund.py" in out
    # The first commit's short sha should be a sample point.
    assert sha1[:7] in out
    # And the table mentions the top signal of the most recent state, which
    # must include the hotfix's incident-flagged classification.
    assert "incident" in out.lower()


def test_timeline_json_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.commit("update", {"a.py": "2"}, when=days_ago(30))
    result = _invoke(repo.root, "timeline", "a.py", "--samples", "5", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "a.py"
    assert isinstance(data["samples"], list)
    assert len(data["samples"]) >= 2
    for sample in data["samples"]:
        assert {"date", "sha", "score", "band", "top_signal"} <= set(sample.keys())


def test_timeline_warns_on_untracked(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "timeline", "phantom.py")
    assert result.exit_code != 0


def test_honest_prints_full_invariants(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    long_line = (
        "Important: do not call this from threads. "
        "There is a global lock above that gets confused; we tried "
        "switching to async in 2023 and rolled it back the same week."
    )
    repo.commit(
        "compat: thread safety",
        {"x.py": "1"},
        body=long_line,
        when=days_ago(40),
    )
    result = _invoke(repo.root, "honest", "x.py")
    assert result.exit_code == 0
    out = result.output
    # Full sentence must appear (no first-sentence truncation).
    assert "we tried switching to async in 2023" in out


def test_honest_json_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync",
        {"x.py": "1"},
        body="Do not switch to async. Important: legacy header must stay.",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "honest", "x.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "x.py"
    assert len(data["invariants"]) >= 1
    first = data["invariants"][0]
    assert "lines" in first
    assert any("Do not switch to async" in line for line in first["lines"])


def test_honest_silent_when_no_invariants(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "feat: add feature",
        {"x.py": "1"},
        body="Just a normal feature. No constraints.",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "honest", "x.py")
    assert result.exit_code == 0
    assert "no invariants" in result.output.lower()


def test_mcp_summary_field_present_in_json(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Verify the MCP server includes a quotable summary string in get_risk_profile."""
    sha = repo.commit("feat: A", {"a.py": "1"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: edge case",
        {"a.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    # Test the underlying handler directly (avoids spawning a real MCP server).
    from whycode import risk_card as rc
    from whycode.mcp_server import _summary_text

    card = rc.build(repo.root, "a.py")
    summary = _summary_text(card)
    assert "a.py" in summary
    assert any(band in summary for band in ("HANDLE", "READ", "WORTH", "NO FLAGS"))
    # Top concern surfaced in the prose
    if card.signals:
        assert "Top concern" in summary or "no flags" in summary.lower()


def test_scan_lists_top_files(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "scan", "--top", "3")
    assert result.exit_code == 0
    assert "a.py" in result.output
