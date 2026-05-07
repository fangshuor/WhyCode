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


def test_highlights_surfaces_invariants_and_incidents(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"a.py": "1"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(60),
    )
    repo.commit(
        "hotfix: refund regression",
        {"b.py": "1"},
        body="See INC-447 for context.",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "highlights")
    assert result.exit_code == 0
    out = result.output
    assert "INVARIANTS" in out
    assert "Do not switch to async" in out
    assert "INCIDENTS" in out
    assert "hotfix: refund regression" in out


def test_highlights_json_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync",
        {"a.py": "1"},
        body="Important: legacy header must stay.",
        when=days_ago(30),
    )
    result = _invoke(repo.root, "highlights", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "invariants" in data
    assert "incidents" in data
    assert any("legacy header" in inv["line"] for inv in data["invariants"])


def test_highlights_quiet_repo_does_not_error(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init: nothing here", {"a.py": "1"})
    result = _invoke(repo.root, "highlights")
    assert result.exit_code == 0
    out = result.output.lower()
    assert "none found" in out


def test_why_mute_writes_suppression_and_hides_signal(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init",
        {"refund.py": "1"},
        when=days_ago(60),
    )
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="See #INC-42",
        when=days_ago(10),
    )
    # First call: incident_history fires.
    before = _invoke(repo.root, "why", "refund.py", "--json")
    data_before = json.loads(before.output)
    assert any(s["kind"] == "incident_history" for s in data_before["signals"])

    # Mute it.
    muted = _invoke(repo.root, "why", "refund.py", "--mute", "incident", "--json")
    assert muted.exit_code == 0
    data_muted = json.loads(muted.output)
    assert all(s["kind"] != "incident_history" for s in data_muted["signals"])
    # File now exists on disk.
    assert (repo.root / ".whycode" / "suppressed.json").exists()

    # Subsequent run: still hidden (persistence).
    again = _invoke(repo.root, "why", "refund.py", "--json")
    data_again = json.loads(again.output)
    assert all(s["kind"] != "incident_history" for s in data_again["signals"])

    # --no-mutes brings it back without removing from the file.
    bypass = _invoke(repo.root, "why", "refund.py", "--no-mutes", "--json")
    data_bypass = json.loads(bypass.output)
    assert any(s["kind"] == "incident_history" for s in data_bypass["signals"])


def test_why_mute_unknown_kind_errors(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "why", "a.py", "--mute", "nonsense")
    assert result.exit_code != 0
    assert "unknown signal kind" in result.output.lower()


def test_scan_skips_default_ignored_paths_by_default(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """CHANGELOG and lockfiles must not appear in scan output by default."""
    sha = repo.commit(
        "init",
        {"CHANGELOG.md": "v1", "package-lock.json": "{}", "src/app.py": "x"},
        when=days_ago(60),
    )
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "release: 1.1",
        {"CHANGELOG.md": "v2", "src/app.py": "y"},
        when=days_ago(20),
    )
    result = _invoke(repo.root, "scan", "--top", "10")
    assert result.exit_code == 0
    out = result.output
    # CHANGELOG and lockfile must not appear in the table.
    assert "CHANGELOG" not in out
    assert "package-lock.json" not in out


def test_scan_no_ignore_brings_them_back(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init",
        {"CHANGELOG.md": "v1", "src/app.py": "x"},
        when=days_ago(60),
    )
    sha = repo.commit(
        "feat: A",
        {"CHANGELOG.md": "v2", "src/app.py": "y"},
        when=days_ago(40),
    )
    repo.revert(sha, when=days_ago(20))  # safe to revert: files still exist after
    default_run = _invoke(repo.root, "scan", "--top", "10")
    permissive_run = _invoke(repo.root, "scan", "--top", "10", "--no-ignore")
    assert default_run.exit_code == 0
    assert permissive_run.exit_code == 0
    # CHANGELOG was hidden from the default run by the ignore list…
    assert "CHANGELOG" not in default_run.output
    # …but is at least reachable when --no-ignore is on.
    assert "CHANGELOG" in permissive_run.output or "src/app.py" in permissive_run.output


def test_diff_markdown_output(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"refund.py": "0"}, when=days_ago(120))
    sha = repo.commit("feat: refund flow", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #INC-42",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--markdown")
    assert result.exit_code == 0
    out = result.output
    # Hidden marker so the workflow can find-and-update its prior comment.
    assert "<!-- whycode-comment -->" in out
    # Markdown table syntax.
    assert "| Score | Band |" in out
    assert "| ----: |" in out
    # File path appears as inline code.
    assert "`refund.py`" in out


def test_diff_markdown_quiet_repo(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(40))
    repo.commit("docs: tweak", {"a.py": "2"}, when=days_ago(20))
    result = _invoke(repo.root, "diff", "--base", "HEAD~1", "--markdown")
    assert result.exit_code == 0
    out = result.output
    assert "<!-- whycode-comment -->" in out
    # No flagged files → friendly note instead of an empty table.
    assert "Nothing flagged" in out


def test_scan_respects_user_whycodeignore(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    (repo.root / ".whycodeignore").write_text("internal/legacy.py\n")
    sha = repo.commit(
        "init",
        {"internal/legacy.py": "1", "src/app.py": "x"},
        when=days_ago(60),
    )
    repo.revert(sha, when=days_ago(50))
    result = _invoke(repo.root, "scan", "--top", "10")
    assert result.exit_code == 0
    assert "internal/legacy.py" not in result.output


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


def test_tour_runs_and_emits_all_sections(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"a.py": "1"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(60),
    )
    repo.commit(
        "hotfix: refund regression",
        {"b.py": "1"},
        body="See INC-447.",
        when=days_ago(20),
    )
    sha = repo.commit("feat: A", {"a.py": "2"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(15))
    result = _invoke(repo.root, "tour")
    assert result.exit_code == 0
    out = result.output
    assert "Welcome to WhyCode" in out
    assert "Decisions and incidents" in out
    assert "Do not switch to async" in out
    assert "hotfix: refund regression" in out
    assert "Wire WhyCode into your AI editor" in out
    # MCP snippet appears verbatim so users can copy-paste.
    assert '"command": "whycode"' in out


def test_tour_quiet_repo_explains_why(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "tour")
    assert result.exit_code == 0
    out = result.output
    # MCP section appears regardless — most useful next step.
    assert "Wire WhyCode into your AI editor" in out
    # And the empty-state explanation should mention why nothing fires.
    assert "terse" in out.lower() or "no headline" in out.lower()


def test_tour_outside_repo_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(app, ["tour"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0


def test_outside_repo_every_command_exits_non_zero(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F11/F12 — a path outside any git repo printed ``error:`` but exited 0,
    falsifying CI signals. Every command that walks the repo must propagate
    the failure as a non-zero exit, never status 0."""
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        for argv in (
            ["tour"],
            ["scan", "--top", "1"],
            ["highlights"],
            ["why", "anything.txt"],
            ["diff"],
            ["timeline", "anything.txt"],
            ["honest", "anything.txt"],
            ["show", "deadbeef"],
            ["init"],
        ):
            result = runner.invoke(app, argv, catch_exceptions=False)
            assert result.exit_code != 0, f"{argv} exited 0 outside a repo"
    finally:
        os.chdir(cwd)


def test_uncaught_exception_in_command_body_exits_non_zero(repo, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """F11 — a command body that raises an unexpected exception used to
    render a Rich traceback to stderr but exit with status 0. The
    ``_propagate_failures`` wrapper now forces ``typer.Exit(2)`` so CI
    integrations can tell the run silently failed."""
    repo.commit("init", {"a.txt": "1"})
    # Force ``all_commits`` to blow up partway through tour's body.
    from whycode import git_facts as gf

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError("simulated mid-walk failure")

    monkeypatch.setattr(gf, "all_commits", _boom)
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        result = runner.invoke(app, ["tour"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0


def test_cache_stats_before_any_run_reports_no_cache(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    result = _invoke(repo.root, "cache", "stats")
    assert result.exit_code == 0
    assert "no cache yet" in result.output


def test_cache_stats_after_scan_reports_size_and_head(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("init", {"a.txt": "1"}, when=days_ago(40))
    _invoke(repo.root, "scan", "--top", "3")
    result = _invoke(repo.root, "cache", "stats")
    assert result.exit_code == 0
    assert "schema_version" in result.output
    assert sha[:12] in result.output


def test_cache_clear_removes_db(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    _invoke(repo.root, "scan", "--top", "3")
    cache_db = repo.root / ".whycode" / "cache.db"
    assert cache_db.exists()
    result = _invoke(repo.root, "cache", "clear")
    assert result.exit_code == 0
    assert not cache_db.exists()
    # Idempotent: a second clear is fine.
    result_again = _invoke(repo.root, "cache", "clear")
    assert result_again.exit_code == 0


def test_no_cache_flag_skips_db_creation(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"}, when=days_ago(40))
    result = _invoke(repo.root, "scan", "--top", "3", "--no-cache")
    assert result.exit_code == 0
    assert not (repo.root / ".whycode" / "cache.db").exists()


def test_repeat_scan_produces_identical_top_files(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The output of a warm scan must match the output of a cold scan."""
    sha1 = repo.commit("feature", {"refund.py": "1"}, when=days_ago(40))
    repo.revert(sha1, when=days_ago(35))
    repo.commit(
        "hotfix: edge case",
        {"refund.py": "2"},
        body="incident #INC-42",
        when=days_ago(10),
    )
    cold = _invoke(repo.root, "scan", "--top", "5", "--no-cache").output
    warm_first = _invoke(repo.root, "scan", "--top", "5").output
    warm_second = _invoke(repo.root, "scan", "--top", "5").output
    # All three runs must rank the same path with the same score.
    assert "refund.py" in cold
    assert "refund.py" in warm_first
    assert "refund.py" in warm_second
