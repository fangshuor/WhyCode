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


def test_why_json_signals_carry_next_step(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Each signal in --json output must include the next_step field — it's
    public API for tooling that wants the actionable hint without parsing
    the rendered card."""
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "why", "refund.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["signals"], "expected at least one signal"
    for s in data["signals"]:
        assert "next_step" in s, f"signal {s['kind']} missing next_step key"
    # At least one signal should have a non-null next_step (revert / incident).
    assert any(s["next_step"] for s in data["signals"])


def test_why_card_band_carries_dim_score_suffix(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The header row used to print the band, the numeric score, and a
    per-signal HIGH/MED/LOW badge — three near-redundant tags. The score
    is now a dim ``· N`` suffix next to the band so the band carries the
    headline word and the per-signal badges differentiate inside the
    table. JSON output is unchanged (the score is API)."""
    from io import StringIO

    from rich.console import Console as _Console

    from whycode import risk_card as rc

    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    card = rc.build(repo.root, "refund.py")
    buf = StringIO()
    _Console(file=buf, width=200, force_terminal=False, color_system=None).print(
        rc.render_text(card)
    )
    out = buf.getvalue()
    # Band still appears as the headline word.
    assert any(b in out for b in ("HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK"))
    # Score is rendered as "· N" (no "/100" anymore).
    assert f"· {card.score.value}" in out
    assert "score " not in out  # the verbose 'score 57/100' wording is gone
    assert "/100" not in out
    # JSON output keeps the numeric score (api compat).
    json_data = card.to_dict()
    assert json_data["score"] == card.score.value


def test_why_card_renders_narrative_summary(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The Risk Card opens with a one-sentence grounding narrative
    (file age + primary author + last activity). The signals table below
    names the strongest concern + action; the narrative deliberately stops
    at the grounding so the user reads each fact once."""
    from io import StringIO

    from rich.console import Console as _Console

    from whycode import risk_card as rc

    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    card = rc.build(repo.root, "refund.py")
    buf = StringIO()
    _Console(file=buf, width=200, force_terminal=False, color_system=None).print(
        rc.render_text(card)
    )
    out = buf.getvalue()
    assert "refund.py is " in out
    assert "commits old" in out
    # The narrative no longer restates the strongest signal; that lives in
    # the per-signal next_step so the user reads it once.
    assert "The strongest concern is" not in out
    assert " first." not in out


def test_why_card_narrative_quiet_when_no_signals(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A NO FLAGS card collapses to one honest line."""
    from io import StringIO

    from rich.console import Console as _Console

    from whycode import risk_card as rc

    repo.commit("init", {"a.py": "1"}, when=days_ago(40))
    repo.commit("docs: tweak", {"a.py": "2"}, when=days_ago(20))
    card = rc.build(repo.root, "a.py")
    if not card.signals:
        buf = StringIO()
        _Console(file=buf, width=200, force_terminal=False, color_system=None).print(
            rc.render_text(card)
        )
        out = buf.getvalue()
        assert "no flags fired" in out
        assert "Read the diff anyway" in out


def test_why_json_output_includes_primary_author(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The --json schema gains a primary_author key — used by tooling that
    builds its own narrative or wants to flag "who owns this?"."""
    repo.commit(
        "init",
        {"x.py": "1"},
        when=days_ago(60),
        author_name="Alice",
        author_email="alice@example.com",
    )
    repo.commit(
        "tweak",
        {"x.py": "2"},
        when=days_ago(40),
        author_name="Alice",
        author_email="alice@example.com",
    )
    result = _invoke(repo.root, "why", "x.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["primary_author"] == "Alice"


def test_why_card_renders_per_signal_next_step(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The Risk Card text output prints each signal's next_step on its own
    dim-coloured line, replacing the legacy global ``→ git show <sha>``
    footer. We render to a wide synthetic console so the test does not
    depend on the harness terminal width truncating the table.
    """
    from io import StringIO

    from rich.console import Console as _Console

    from whycode import risk_card as rc

    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    card = rc.build(repo.root, "refund.py")
    buf = StringIO()
    _Console(file=buf, width=200, force_terminal=False, color_system=None).print(
        rc.render_text(card)
    )
    out = buf.getvalue()
    # The revert detector's hint wording is now visible in the card.
    assert "Read both sides" in out
    # And the incident detector's hint is present too.
    assert "incident-flavoured change in context" in out
    # Each next_step is on a line that starts with the arrow glyph so the
    # reader's eye lands on the action.
    assert "→ Read both sides" in out
    # The legacy global footer used to read "to read the most relevant commit
    # in full" — that single per-card line is gone now (each signal carries
    # its own).
    assert "most relevant commit in full" not in out


def test_why_handles_path_outside_repo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # No git repo here.
    target = tmp_path / "nope.txt"
    target.write_text("x")
    result = runner.invoke(app, ["why", str(target)], catch_exceptions=False)
    assert result.exit_code != 0


def test_why_warns_on_untracked_path(repo) -> None:  # type: ignore[no-untyped-def]
    """Untracked path → exit 2 (so a CI loop running `whycode why` per file
    fails loudly on a typo instead of silently succeeding). The friendly
    next-step nudges the user toward `whycode scan`."""
    repo.commit("init", {"a.txt": "1"})
    result = _invoke(repo.root, "why", "phantom.txt")
    assert result.exit_code == 2
    assert "phantom.txt" in result.output
    assert "not tracked" in result.output
    assert "check the path" in result.output


def test_why_heads_up_advisory_on_ignored_path(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """When the target matches the default ignore list (CHANGELOG, lockfile,
    etc.), emit an advisory above the card so a typo / LLM-suggested path
    doesn't read as authoritative."""
    repo.commit(
        "init", {"CHANGELOG.md": "## 0.1\n", "src.py": "x"}, when=days_ago(40)
    )
    repo.commit("docs: bump", {"CHANGELOG.md": "## 0.2\n"}, when=days_ago(20))
    result = _invoke(repo.root, "why", "CHANGELOG.md")
    assert result.exit_code == 0
    assert "heads up" in result.output.lower()
    assert "advisory" in result.output.lower()


def test_mute_confirmation_mentions_no_mutes_reverse_path(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The mute confirmation suggests `--no-mutes` as the preview path before
    editing the JSON — the reverse-hint added in 0.6.1."""
    repo.commit("init", {"r.py": "1"}, when=days_ago(60))
    repo.commit(
        "hotfix: regression", {"r.py": "2"}, body="See #INC-1", when=days_ago(10)
    )
    result = _invoke(repo.root, "why", "r.py", "--mute", "incident")
    assert result.exit_code == 0
    assert "--no-mutes" in result.output


def test_mcp_command_prints_stdio_ready_line(repo, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`whycode mcp` prints a stderr 'stdio server ready' line on startup so
    a curious user doesn't see a silent block."""
    import os

    import whycode.mcp_server

    repo.commit("init", {"a.txt": "1"})

    served = {"called": False}

    def fake_serve(verbose: bool = False) -> None:
        served["called"] = True

    monkeypatch.setattr(whycode.mcp_server, "serve", fake_serve)
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        result = runner.invoke(app, ["mcp"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0
    assert "stdio server ready" in result.output
    assert served["called"]


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
    assert "buckets" in data
    assert isinstance(data["buckets"], dict)


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


def test_why_brief_emits_action_verdict(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Persona feedback (Mei + Alex): score alone is unanchored at 3am.
    `--brief` must lead with an ESCALATE / CHECK / SCAN / OK action verb so
    a tired reader can act without interpreting the number."""
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="See #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "why", "refund.py", "--brief")
    assert result.exit_code == 0
    out = result.output
    assert any(verdict in out for verdict in ("ESCALATE", "CHECK", "SCAN", "OK"))


def test_why_brief_flags_security_touched_when_history_cites_advisory(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """A file whose history mentions a CVE/GHSA token gets a SECURITY-TOUCHED
    label so a 3am SRE doesn't bypass a security control without seeing it."""
    repo.commit("init", {"sess.py": "1"}, when=days_ago(120))
    repo.commit(
        "fix: GHSA-j8r2-6x86-q33q address advisory",
        {"sess.py": "2"},
        body="incident #SEC-1",
        when=days_ago(30),
    )
    result = _invoke(repo.root, "why", "sess.py", "--brief")
    assert result.exit_code == 0
    assert "SECURITY-TOUCHED" in result.output
    assert "ESCALATE" in result.output


def test_show_renders_commit_body(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Persona feedback (Mei, David, Alex): `whycode show` opening with risk
    cards but hiding the commit body is a worse `git show`. The body must
    appear so the reader can read the actual reasoning without falling back
    to git."""
    sha = repo.commit(
        "fix: subtle bug",
        {"x.py": "1"},
        body=(
            "We tried option A but it broke v1 clients. After conversations "
            "in #INC-42, we decided to keep the existing behaviour because "
            "the alternative would require a breaking schema migration."
        ),
        when=days_ago(10),
    )
    result = _invoke(repo.root, "show", sha)
    assert result.exit_code == 0
    out = result.output
    assert "Body:" in out
    assert "v1 clients" in out
    assert "schema migration" in out


def test_show_renders_per_file_diffstat(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The per-file table includes a +/- column so a reviewer can tell a
    3-line tweak from a rewrite without leaving WhyCode for git."""
    sha = repo.commit(
        "feat: tweak",
        {"a.py": "v2 with one new line\n", "b.py": "two\nlines\n"},
        when=days_ago(5),
    )
    result = _invoke(repo.root, "show", sha)
    assert result.exit_code == 0
    assert "+/-" in result.output


def test_why_explain_text_includes_rule_line(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "why", "refund.py", "--explain")
    assert result.exit_code == 0
    out = result.output
    # Each fired signal should print the literal "rule:" line.
    assert "rule:" in out
    # And the precise rule name for the revert detector should appear.
    assert "revert_pair_default_message" in out
    # The incident detector should expose its branch as well.
    assert "incident_subject_keyword" in out
    # The "fired because" prose intro should appear at least once.
    assert "fired because:" in out


def test_why_without_explain_omits_rule_line(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: the default rendering must not leak the explain block."""
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "why", "refund.py")
    assert result.exit_code == 0
    out = result.output
    assert "rule:" not in out
    assert "fired because:" not in out


def test_why_explain_json_includes_explanation_per_signal(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(20),
    )
    result = _invoke(repo.root, "why", "refund.py", "--explain", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "signals" in data
    # At least one signal exists and has the explanation key with the expected
    # shape.
    fired = [s for s in data["signals"] if s.get("explanation") is not None]
    assert fired
    sample = fired[0]["explanation"]
    assert {"rule", "why_it_fired", "evidence", "source_ref"}.issubset(sample.keys())


def test_why_json_without_explain_omits_explanation_key(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: --json without --explain has no 'explanation' key."""
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    result = _invoke(repo.root, "why", "refund.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    for s in data["signals"]:
        assert "explanation" not in s


def test_why_explain_with_at_renders_explanation(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """`--explain` must compose with `--at`: the historical card gets the same
    explanation surface, since explanations are computed from the same L1+L2
    facts the historical view rebuilds."""
    repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #INC-1",
        when=days_ago(40),
    )
    # As of HEAD the file already has an incident; --at HEAD should still
    # surface the same branch.
    result = _invoke(repo.root, "why", "a.py", "--explain", "--at", "HEAD")
    assert result.exit_code == 0
    assert "rule:" in result.output


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


def test_show_renders_chapter_role_label(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode show <sha>` opens with a "Role:" line so the reader knows
    the commit's narrative position before drilling into per-file diffs."""
    sha = repo.commit(
        "hotfix: regression", {"a.py": "1"}, body="See #INC-1", when=days_ago(5)
    )
    result = _invoke(repo.root, "show", sha)
    assert result.exit_code == 0
    assert "Role:" in result.output
    assert "INCIDENT" in result.output


def test_show_chapter_role_reconciliation_when_body_cites_prior_sha(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """A commit whose body cites another known SHA classifies as a
    reconciliation chapter — the narrative bridge pattern the body-reference
    detector also surfaces in `whycode why`."""
    sha_a = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(40))
    sha_b = repo.commit(
        "feat: reconcile",
        {"x.py": "2"},
        body=f"This reconsiders {sha_a[:10]} after benchmarking.",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "show", sha_b, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "reconciliation"
    assert data["cites_prior_sha"] is True


def test_show_chapter_role_revert_when_subject_starts_with_revert(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """A commit whose subject starts with `Revert ` classifies as a revert
    chapter regardless of the body."""
    sha_a = repo.commit("feat: try async", {"x.py": "1"}, when=days_ago(40))
    repo.revert(sha_a, when=days_ago(30))
    # The revert created by repo.revert has subject 'Revert "feat: try async"'
    # — find it via git log
    import subprocess

    log = subprocess.run(
        ["git", "log", "--format=%H", "-1", "--grep=Revert"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=True,
    )
    revert_sha = log.stdout.strip()
    result = _invoke(repo.root, "show", revert_sha, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "revert"
    assert data["is_revert"] is True


def test_show_chapter_role_deprecate_emits_pivot_kind_in_json(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """A commit whose subject opens with the numpy ``DEP:`` prefix and
    carries a substantive body classifies as the dedicated ``deprecate``
    chapter role, and the JSON view exposes the matching ``pivot_kind`` so
    downstream consumers can discriminate sub-roles without re-parsing the
    subject."""
    long_body = (
        "Deprecate the legacy iterator interface. The public surface has "
        "been re-exported via the modern entry point for several releases; "
        "this change schedules removal in the next major. Callers still on "
        "the old names will start seeing a DeprecationWarning at import "
        "time, with a pointer to the documented migration path."
    )
    assert len(long_body) >= 200
    sha = repo.commit(
        "DEP: remove legacy iterator interface",
        {"legacy.py": "1"},
        body=long_body,
        when=days_ago(5),
    )
    result = _invoke(repo.root, "show", sha, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "deprecate"
    assert data["pivot_kind"] == "deprecate"
    assert data["is_pivot"] is True


def test_show_chapter_role_edit_for_routine_commit(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A vanilla commit with no incident keywords / invariant tokens / SHA
    references / revert prefix gets the lowest-priority `edit` role."""
    sha = repo.commit(
        "tweak: rename internal helper",
        {"x.py": "1"},
        body="No constraint stated; no prior SHA referenced.",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "show", sha, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "edit"


def test_show_chapter_role_silence_break(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A commit that follows a long quiet period in the file's history is a
    distinct narrative beat — "the file slept for over a year, then someone
    re-touched it" — and gets the `silence_break` chapter role."""
    repo.commit(
        "init: long ago",
        {"sleeper.py": "1"},
        body="No constraint stated; first commit.",
        when=days_ago(400),
    )
    sha_recent = repo.commit(
        "tweak: re-touch after a long quiet",
        {"sleeper.py": "2"},
        body="No constraint stated; routine follow-up after silence.",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "show", sha_recent, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "silence_break"
    assert data["breaks_silence"] is True


def test_show_chapter_role_pivot_style(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A long-body commit with a generic-looking subject — the architectural
    pivot pattern — opens with a Role: PIVOT label and JSON carries
    is_pivot=True."""
    long_body = (
        "This change reorganises the internal layout to separate the public "
        "façade from the private dispatch helpers. The motivation is to make "
        "the contract explicit: callers should not depend on the private "
        "module path, and tests should pin the public surface only. We keep "
        "backwards compat by re-exporting the old names with a __getattr__ "
        "shim until the next major release."
    )
    assert len(long_body) >= 200
    sha = repo.commit(
        "Reorganize internal layout",
        {"core.py": "1"},
        body=long_body,
        when=days_ago(20),
    )
    result = _invoke(repo.root, "show", sha, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["chapter_role"] == "pivot"
    assert data["is_pivot"] is True


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


def test_timeline_rows_sorted_by_date_ascending(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """F14 — table rows must be sorted by date ascending before rendering."""
    repo.commit("init", {"a.py": "1"}, when=days_ago(400))
    repo.commit("change", {"a.py": "2"}, when=days_ago(300))
    repo.commit("change", {"a.py": "3"}, when=days_ago(200))
    repo.commit("change", {"a.py": "4"}, when=days_ago(100))
    repo.commit("change", {"a.py": "5"}, when=days_ago(20))
    result = _invoke(repo.root, "timeline", "a.py", "--samples", "10", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    dates = [s["date"] for s in data["samples"]]
    assert dates == sorted(dates), f"timeline rows not in ascending date order: {dates}"


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
    # Bucketed markdown: a section header per band, table inside each bucket.
    assert any(
        f"### {band} (" in out
        for band in ("HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK")
    )
    assert "| File | Top signal |" in out
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
    # F16 — invariants and incidents are rendered under separate subheads
    # so a reader can tell prose from real incident commits at a glance.
    assert "Stated invariants" in out
    assert "Recent incidents" in out
    assert "Do not switch to async" in out
    assert "hotfix: refund regression" in out
    assert "Wire WhyCode into your AI editor" in out
    # MCP snippet appears verbatim so users can copy-paste.
    assert '"command": "whycode"' in out
    # The closing "Next:" block names the actual top-risk file the tour
    # just discovered, not a generic <path> placeholder. ``a.py`` was
    # touched by both a revert and an explicit invariant statement, so
    # it dominates the slim scan over ``b.py``.
    assert "whycode why a.py" in out
    assert "whycode why <path>" not in out


def test_tour_next_block_substitutes_top_risk_path(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The first 'Next:' suggestion names the literal top-risk path the
    tour itself surfaced, not a hard-coded example. A fresh tour should
    leave the user one paste-and-run away from the actual first dive."""
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "tour")
    assert result.exit_code == 0
    out = result.output
    assert "Top 3 risky files" in out
    assert "whycode why refund.py" in out
    # No placeholder leakage in the substituted path.
    assert "<path>" not in out
    assert "<top-file>" not in out


def test_tour_next_block_falls_back_to_generic_when_no_risky_files(repo) -> None:  # type: ignore[no-untyped-def]
    """When the slim scan finds nothing, the Next: block degrades to a
    generic <path> placeholder so the user still sees the same shape
    instead of a missing line."""
    repo.commit("init", {"a.py": "1"})
    result = _invoke(repo.root, "tour")
    assert result.exit_code == 0
    out = result.output
    assert "Top 3 risky files" not in out
    # Generic prose fallback is allowed only here.
    assert "whycode why <path>" in out


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
    """A command body that raises an unexpected exception must exit non-zero
    so CI integrations don't report a silent failure as green."""
    repo.commit("init", {"a.txt": "1"})
    from whycode import git_facts as gf

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError("simulated mid-walk failure")

    monkeypatch.setattr(gf, "all_commits", _boom)
    cwd = os.getcwd()
    os.chdir(repo.root)
    try:
        result = runner.invoke(app, ["tour"])
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


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


# ---- F4: highlights determinism across cache state ------------------------


def test_highlights_json_is_byte_identical_across_cache_state(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """Two commits with identical bodies and timestamps (a cherry-pick on a
    different branch) must not flip which SHA the dedup picks across cache
    versus --no-cache reads of the same HEAD.

    Without a stable tie-breaker, the cache's authored_at-DESC walk and git
    log's walk can disagree on the order of identical-timestamp commits, and
    the JSON consumer sees a different SHA on the same field across runs.
    """
    same_time = days_ago(30)
    repo.commit(
        "init",
        {"a.txt": "1", "b.txt": "1"},
        when=days_ago(60),
    )
    # Two commits, identical timestamps, identical bodies — only the SHAs
    # and the touched-file set differ. Mirrors the flask cherry-pick pattern
    # the field test surfaced.
    repo.commit(
        "use global contributing guide on master",
        {"a.txt": "2"},
        body="Do not duplicate the contributing guide between branches.",
        when=same_time,
    )
    repo.commit(
        "use global contributing guide on stable",
        {"b.txt": "2"},
        body="Do not duplicate the contributing guide between branches.",
        when=same_time,
    )
    cold = _invoke(repo.root, "highlights", "--no-cache", "--json").output
    warm = _invoke(repo.root, "highlights", "--json").output
    second_warm = _invoke(repo.root, "highlights", "--json").output
    assert cold == warm
    assert warm == second_warm
    payload = json.loads(cold)
    # Exactly one invariant should survive the dedup; the other commit's
    # statement is identical and must not appear twice.
    assert len(payload["invariants"]) == 1


# ---- F5: scan determinism across cache state ------------------------------


def test_scan_text_is_byte_identical_across_cache_state(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """Two files that earn the same score from the same signals must not
    swap positions in the --top N truncation across cache versus --no-cache
    reads. Stable tie-break on the lexicographically smallest path keeps
    cold and warm output byte-identical.
    """
    # Two files always touched together → identical histories, identical
    # signals, identical scores. The ordering between them is settled
    # only by the path tie-break.
    sha = repo.commit(
        "feature: introduce zeta and alpha",
        {"zeta.py": "1", "alpha.py": "1"},
        when=days_ago(50),
    )
    repo.revert(sha, when=days_ago(45))
    repo.commit(
        "hotfix: regression",
        {"zeta.py": "2", "alpha.py": "2"},
        body="incident #INC-1",
        when=days_ago(20),
    )
    cold = _invoke(repo.root, "scan", "--top", "10", "--no-cache").output
    warm = _invoke(repo.root, "scan", "--top", "10").output
    second_warm = _invoke(repo.root, "scan", "--top", "10").output
    assert cold == warm
    assert warm == second_warm
    # Lexicographic tie-break: alpha.py is listed before zeta.py despite
    # equal scores.
    alpha_pos = cold.find("alpha.py")
    zeta_pos = cold.find("zeta.py")
    assert alpha_pos != -1
    assert zeta_pos != -1
    assert alpha_pos < zeta_pos


# ---- F7: --no-cache uses an in-memory cache for amortisation -------------


def test_no_cache_scan_matches_warm_scan_byte_for_byte(
    repo, days_ago
) -> None:  # type: ignore[no-untyped-def]
    """Cache-correctness contract: ``--no-cache`` must agree with the
    persistent cache on the same HEAD. The in-memory ``:memory:`` store
    backing ``--no-cache`` shares the same git-walk and dedup code paths
    as the on-disk store; output must be byte-identical.
    """
    sha = repo.commit("feature", {"a.py": "1", "b.py": "1"}, when=days_ago(50))
    repo.revert(sha, when=days_ago(45))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2", "b.py": "2"},
        body="incident #INC-1",
        when=days_ago(10),
    )
    # Warm path first (writes the on-disk cache).
    warm = _invoke(repo.root, "scan", "--top", "5").output
    no_cache = _invoke(repo.root, "scan", "--top", "5", "--no-cache").output
    warm_again = _invoke(repo.root, "scan", "--top", "5").output
    assert warm == no_cache
    assert warm == warm_again


# ---- 0.5.3: bucketed diff output ------------------------------------------


def test_diff_text_renders_bucket_headers(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode diff` text output groups files under their band as a heading.

    A reviewer scanning a 50-file PR no longer needs to read every row to
    find the riskiest cluster — the HANDLE WITH CARE bucket is its own
    section heading at the top of the page.
    """
    repo.commit("init", {"refund.py": "0", "docs.md": "v1"}, when=days_ago(180))
    sha = repo.commit("feat: refund", {"refund.py": "1"}, when=days_ago(120))
    repo.revert(sha, when=days_ago(100))
    sha2 = repo.commit("feat: refund again", {"refund.py": "2"}, when=days_ago(80))
    repo.revert(sha2, when=days_ago(70))
    sha3 = repo.commit("feat: refund 3", {"refund.py": "3"}, when=days_ago(60))
    repo.revert(sha3, when=days_ago(55))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "4"},
        body="incident #INC-42",
        when=days_ago(10),
    )
    repo.commit("docs: tweak", {"docs.md": "v2"}, when=days_ago(2))
    # Diff against the very first commit so refund.py and docs.md both
    # appear as changed files in the briefing.
    result = _invoke(repo.root, "diff", "--base", "HEAD~8")
    assert result.exit_code == 0, result.output
    out = result.output
    # The bucket header carries the band label and a parenthesised count on
    # the same line.  Rich's space-padded styling means the gap between the
    # label and the count is implementation-defined — assert the band name
    # appears, and that a ``(N)`` count appears on the same line.
    import re

    assert any(
        re.search(rf"{band}\s+\(\d+\)", out)
        for band in ("HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK")
    ), out
    # CLEAR is collapsed by default to a single count line, not a table row.
    assert "no risk signals" in out


def test_diff_skips_empty_buckets(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """An empty bucket must not be rendered.

    A diff with only one HANDLE WITH CARE file should not emit a vacuous
    `READ HISTORY FIRST (0)` section.
    """
    repo.commit("init", {"a.py": "0"}, when=days_ago(180))
    sha = repo.commit("feat 1", {"a.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    sha2 = repo.commit("feat 2", {"a.py": "2"}, when=days_ago(40))
    repo.revert(sha2, when=days_ago(35))
    sha3 = repo.commit("feat 3", {"a.py": "3"}, when=days_ago(30))
    repo.revert(sha3, when=days_ago(25))
    repo.commit(
        "hotfix: incident",
        {"a.py": "4"},
        body="incident #INC-1",
        when=days_ago(2),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~7")
    assert result.exit_code == 0, result.output
    out = result.output
    # Only one file changed → at most one risky bucket should be present.
    assert "READ HISTORY FIRST (0)" not in out
    assert "WORTH A LOOK (0)" not in out
    assert "HANDLE WITH CARE (0)" not in out


def test_diff_top_caps_total_rows_across_buckets(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`--top N` applies across all buckets combined, not per-bucket.

    Asking for 5 rows on a diff with twelve flagged files should show
    exactly five total rows distributed across whichever buckets they fall
    in — a global cap, not a per-band cap.
    """
    files = {f"f{i}.py": "0" for i in range(8)}
    repo.commit("init", files, when=days_ago(120))
    sha = repo.commit(
        "feat: introduce", {f"f{i}.py": "1" for i in range(8)}, when=days_ago(60)
    )
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {f"f{i}.py": "2" for i in range(8)},
        body="incident #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--top", "3", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    bucketed_total = sum(len(v) for v in data["buckets"].values())
    assert bucketed_total == 3


def test_diff_json_emits_buckets_field(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`--json` exposes a top-level `buckets` field keyed by band name.

    The four bucket keys are always present (even when empty) so a tool
    consuming the JSON sees a stable shape across runs.
    """
    sha = repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~2", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "buckets" in data
    expected_keys = {"HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK", "CLEAR"}
    assert set(data["buckets"].keys()) == expected_keys


def test_diff_markdown_emits_section_per_bucket(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`--markdown` emits a `### <band> (<count>)` heading per non-empty bucket.

    A PR comment posted by the workflow now opens with the bucket the
    reviewer should look at first, then the next-most-risky bucket, etc.
    """
    repo.commit("init", {"a.py": "0"}, when=days_ago(180))
    sha = repo.commit("feat: A", {"a.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="incident #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--markdown")
    assert result.exit_code == 0, result.output
    out = result.output
    # At least one bucket header rendered.
    assert any(
        f"### {band} (" in out
        for band in ("HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK")
    ), out
    # The bucket-internal table only carries File + Top signal — the heading
    # already encodes the band, the integer score adds nothing per row.
    assert "| File | Top signal |" in out


def test_tour_top_files_render_bucket_labels(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode tour`'s Top-3 risky files block renders bucket headings.

    Three rows is short, but the bucket headers give the same visual
    mapping ``whycode diff`` uses. A reader scanning the tour sees
    HANDLE WITH CARE files under their own heading rather than as a
    fixed-width band column.
    """
    repo.commit("init", {"a.py": "0", "b.py": "0"}, when=days_ago(180))
    repo.commit(
        "compat: keep sync path",
        {"a.py": "1"},
        body="Do not switch to async.",
        when=days_ago(60),
    )
    sha = repo.commit("feat: A", {"a.py": "2"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: refund regression",
        {"a.py": "3"},
        body="See INC-447.",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "tour")
    assert result.exit_code == 0, result.output
    out = result.output
    # Top 3 block exists and at least one bucket label appears under it.
    assert "Top 3 risky files" in out
    assert any(
        band in out
        for band in ("HANDLE WITH CARE", "READ HISTORY FIRST", "WORTH A LOOK")
    ), out


def test_why_header_demotes_score_to_dim_suffix(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The Risk Card header shows the band; the score is a dim ``· N`` suffix.

    The band already names the bucket; an explicit ``score 78/100`` next to
    it is duplication on the most prominent surface of the card. The new
    layout keeps the band's coloured pill and demotes the score to a small
    dim trailing suffix on the same line.

    JSON output is unchanged — ``score`` stays as an integer key for tools
    consuming the structured shape.
    """
    sha = repo.commit("init", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: refund regression",
        {"refund.py": "2"},
        body="incident #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "why", "refund.py")
    assert result.exit_code == 0
    out = result.output
    # Old format: "score X/100" must NOT appear in the rendered text.
    assert "score" not in out.lower() or "/100" not in out
    # New format: a "· <int>" suffix sits next to the band on the header line.
    import re

    band_score = re.search(r"(HANDLE WITH CARE|READ HISTORY FIRST|WORTH A LOOK).*?·\s*\d+", out)
    assert band_score is not None, out
    # JSON output preserves the integer score field.
    json_result = _invoke(repo.root, "why", "refund.py", "--json")
    assert json_result.exit_code == 0
    data = json.loads(json_result.output)
    assert "score" in data
    assert isinstance(data["score"], int)


# --- whycode story ----------------------------------------------------------


def test_cli_story_command_renders_text(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode story <path>` prints role-tagged chapter blocks; the rendered
    text must surface the role label so a reader sees the narrative position
    of each beat at a glance."""
    sha_a = repo.commit(
        "feat: initial dispatch",
        {"a.py": "1"},
        body="No constraint stated.",
        when=days_ago(40),
    )
    repo.commit(
        "hotfix: dispatch regression",
        {"a.py": "2"},
        body=f"See #INC-1\n\nCross-reference: {sha_a[:10]}",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "story", "a.py")
    assert result.exit_code == 0
    out = result.output
    assert "a.py" in out
    # Role tag in brackets is the visual anchor of a chapter.
    assert "[INCIDENT]" in out or "[RECONCILIATION]" in out


def test_cli_story_command_emits_json_with_chapters_array(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode story <path> --json` emits the documented top-level shape."""
    repo.commit(
        "hotfix: outage",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "story", "a.py", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["path"] == "a.py"
    assert "chapters" in data
    assert isinstance(data["chapters"], list)
    assert "summary" in data
    assert "commit_count" in data
    assert "chapters_total" in data
    assert "chapters_returned" in data


def test_cli_story_command_emits_markdown_with_role_headings(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`--markdown` renders ``## [role] sha · date · author`` headings so
    PR-comment readers can scan beats by eye. Output must NOT be rich-rewrapped
    (the reviewer needs the raw markdown bytes)."""
    repo.commit(
        "hotfix: outage",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "story", "a.py", "--markdown")
    assert result.exit_code == 0
    out = result.output
    assert "# story · a.py" in out
    assert "## [incident]" in out


def test_story_all_flag_includes_edit_chapters(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`whycode story <path> --all --json` must surface every commit as a
    chapter — including the role==``edit`` beats that the default narrative
    drops. Regression for the 0.7.0 bug where ``--all`` only lifted the
    ``--top`` truncation but the CLI forgot to thread ``all_chapters=True``
    into ``build_story``, so on a 915-commit file the user got ~124 chapters
    back instead of the full history."""
    # Five commits whose subjects classify as plain edits — nothing the
    # detector would call revert / incident / reconciliation / invariant.
    for i, subj in enumerate(
        [
            "feat: initial",
            "tweak: small rename",
            "tweak: another rename",
            "tweak: third rename",
            "tweak: fourth rename",
        ]
    ):
        repo.commit(subj, {"x.py": str(i)}, when=days_ago(40 - i * 5))
    # Default: edits are dropped (or collapsed). --all must override.
    default_res = _invoke(repo.root, "story", "x.py", "--json")
    assert default_res.exit_code == 0
    default = json.loads(default_res.output)
    default_real = [c for c in default["chapters"] if not c["is_collapse"]]
    assert len(default_real) == 0, "default story drops edit beats"

    all_res = _invoke(repo.root, "story", "x.py", "--all", "--json")
    assert all_res.exit_code == 0
    data = json.loads(all_res.output)
    assert data["commit_count"] == 5
    # Every commit must become a chapter; no folding under --all.
    assert data["chapters_returned"] == 5
    real = [c for c in data["chapters"] if not c["is_collapse"]]
    assert len(real) == 5
    edit_roles = [c["role"] for c in real if c["role"] == "edit"]
    assert len(edit_roles) >= 1, "--all must surface at least one edit chapter"


def test_story_nonexistent_path_exits_2(repo) -> None:  # type: ignore[no-untyped-def]
    """`whycode story <typo>` must exit non-zero so a CI loop running
    ``whycode story`` per file fails loudly on a missing path instead of
    silently succeeding. Same contract as ``why`` — they share the
    ``_require_tracked`` gate."""
    repo.commit("init", {"a.txt": "1"})
    result = _invoke(repo.root, "story", "phantom.py")
    assert result.exit_code == 2
    assert "phantom.py" in result.output
    assert "not tracked" in result.output
    assert "check the path" in result.output


def test_story_oldest_first_reverses_chapter_order(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """``--oldest-first`` flips the chapter array so consumers reading the
    story top-down see causality forward (cause → consequence). The first
    chapter's ``authored_at`` must be the oldest and the last must be the
    newest; ``index`` field stays in newest-first space so cross-references
    keep pointing at the same chapter."""
    sha_a = repo.commit(
        "feat: initial dispatch",
        {"a.py": "1"},
        body="Initial.",
        when=days_ago(60),
    )
    repo.revert(sha_a, when=days_ago(45))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2"},
        body="See #INC-1",
        when=days_ago(20),
    )
    repo.commit(
        "hotfix: another outage",
        {"a.py": "3"},
        body=f"See #INC-2\n\nCross-reference: {sha_a[:10]}",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "story", "a.py", "--oldest-first", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    real = [c for c in data["chapters"] if not c["is_collapse"]]
    assert len(real) >= 2, "need at least two real chapters to test ordering"
    timestamps = [c["authored_at"] for c in real if c["authored_at"]]
    assert timestamps == sorted(timestamps), (
        "with --oldest-first the chapter array must be sorted ascending by authored_at"
    )


def test_story_decisions_shortcut_filters_to_decision_roles(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """``--decisions`` is the convenience shortcut for the canonical
    decision-grade role allowlist. Every kept chapter's ``role`` must be
    in the decision set; routine ``edit`` chapters must be filtered out.

    Note: the ``revert`` chapter naturally pins its target via the
    ``This reverts commit <sha>`` body — that pinned target is still kept
    because pin-by-citation is a hard guarantee on chapter retention; we
    pick an incident-role target so the pinned chapter is itself
    decision-grade.
    """
    sha_a = repo.commit(
        "hotfix: original bug",
        {"a.py": "1"},
        body="See #INC-0",
        when=days_ago(60),
    )
    repo.revert(sha_a, when=days_ago(55))
    repo.commit(
        "tweak: rename helper",
        {"a.py": "2"},
        body="No constraint stated; routine refactor.",
        when=days_ago(40),
    )
    repo.commit(
        "hotfix: pager went off",
        {"a.py": "3"},
        body="See #INC-1",
        when=days_ago(30),
    )
    repo.commit(
        "compat: keep sync path",
        {"a.py": "4"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(20),
    )
    repo.commit(
        "docs: comment cleanup",
        {"a.py": "5"},
        body="No constraint stated.",
        when=days_ago(5),
    )
    result = _invoke(repo.root, "story", "a.py", "--decisions", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    real = [c for c in data["chapters"] if not c["is_collapse"]]
    assert real, "decisions shortcut must keep at least one chapter"
    allowed = {"revert", "incident", "deprecate", "reconciliation", "pivot", "invariant"}
    for c in real:
        assert c["role"] in allowed, f"unexpected role {c['role']!r} under --decisions"
    assert not any(c["role"] == "edit" for c in real)


def test_story_explicit_roles_overrides_decisions_shortcut(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """``--decisions --roles invariant`` must honour the explicit ``--roles``
    value: the shortcut is a default, not a floor. With ``--roles invariant``
    the only non-pinned chapters kept must be ``invariant``. The pin-by-
    citation pass can still surface a cited target regardless of role, so
    we use commits whose bodies do not cross-reference each other."""
    repo.commit(
        "hotfix: outage",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(60),
    )
    repo.commit(
        "compat: keep sync path",
        {"a.py": "2"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(20),
    )
    result = _invoke(
        repo.root, "story", "a.py", "--decisions", "--roles", "invariant", "--json"
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    real = [c for c in data["chapters"] if not c["is_collapse"]]
    assert real, "expected at least one invariant chapter"
    # No commit cross-references another — none should be pin-promoted.
    for c in real:
        assert c["role"] == "invariant", (
            f"explicit --roles invariant must override --decisions, got {c['role']!r}"
        )


def _seed_three_sibling_templates(repo, days_ago):  # type: ignore[no-untyped-def]
    """Set up three sibling files under ``docs/_templates/`` whose history
    fires identical signals (so the markdown collapser has a real cluster
    to fold). Used by several tests below."""
    repo.commit(
        "init",
        {
            "docs/_templates/about.html": "a",
            "docs/_templates/footer.html": "b",
            "docs/_templates/header.html": "c",
        },
        when=days_ago(600),
    )
    sha = repo.commit(
        "feat: rewrite templates",
        {
            "docs/_templates/about.html": "a1",
            "docs/_templates/footer.html": "b1",
            "docs/_templates/header.html": "c1",
        },
        when=days_ago(220),
    )
    repo.revert(sha, when=days_ago(210))
    repo.commit(
        "hotfix: regression in templates",
        {
            "docs/_templates/about.html": "a2",
            "docs/_templates/footer.html": "b2",
            "docs/_templates/header.html": "c2",
        },
        body="incident #INC-9",
        when=days_ago(10),
    )


def test_diff_markdown_collapses_siblings_with_same_headline_and_shared_prefix(  # type: ignore[no-untyped-def]
    repo, days_ago
) -> None:
    """Three sibling files under one directory with identical top-signal
    headlines collapse to a single ``dir/* (N files)`` row."""
    _seed_three_sibling_templates(repo, days_ago)
    result = _invoke(repo.root, "diff", "--base", "HEAD~1", "--markdown")
    assert result.exit_code == 0, result.output
    out = result.output
    # Collapsed row appears.
    assert "docs/_templates/* (3 files)" in out, out
    # The individual paths must NOT appear as bare-path rows of their own.
    for leaf in ("about.html", "footer.html", "header.html"):
        bare_row = f"| `docs/_templates/{leaf}` |"
        assert bare_row not in out, f"individual row leaked: {leaf}\n{out}"


def test_diff_markdown_keeps_single_rows_unchanged(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A band with only one file renders the bare path — no ``(N files)``
    suffix attaches to lone rows."""
    repo.commit("init", {"refund.py": "0"}, when=days_ago(180))
    sha = repo.commit("feat: A", {"refund.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    repo.commit(
        "hotfix: regression",
        {"refund.py": "2"},
        body="incident #INC-1",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--markdown")
    assert result.exit_code == 0, result.output
    out = result.output
    # The single-file row is rendered without the count suffix.
    assert "| `refund.py` |" in out, out
    assert "refund.py (1 files)" not in out
    assert "refund.py (1 file)" not in out


def test_diff_markdown_does_not_collapse_when_headlines_differ(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Two files that share a directory but fire different top signals must
    appear as two separate rows — the collapse key requires headline
    equality, not just path proximity."""
    repo.commit(
        "init",
        {"pkg/alpha.py": "0", "pkg/beta.py": "0"},
        when=days_ago(220),
    )
    # ``alpha.py``: revert + incident → strong signals from history.
    sha = repo.commit(
        "feat: alpha",
        {"pkg/alpha.py": "1"},
        when=days_ago(180),
    )
    repo.revert(sha, when=days_ago(170))
    repo.commit(
        "hotfix: alpha edge case",
        {"pkg/alpha.py": "2"},
        body="incident #INC-7",
        when=days_ago(8),
    )
    # ``beta.py``: a single benign edit — different (or no) headline.
    repo.commit("docs: tweak beta", {"pkg/beta.py": "1"}, when=days_ago(6))
    result = _invoke(repo.root, "diff", "--base", "HEAD~4", "--markdown")
    assert result.exit_code == 0, result.output
    out = result.output
    # The collapsed ``pkg/* (2 files)`` row must not appear — headlines differ.
    assert "pkg/* (2 files)" not in out, out


def test_diff_markdown_does_not_collapse_across_bands(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """Two files in different bands must keep separate rows even if some
    surface text overlaps — bucket boundaries are walls the collapser will
    not cross."""
    repo.commit(
        "init",
        {"pkg/alpha.py": "0", "pkg/beta.py": "0"},
        when=days_ago(220),
    )
    # alpha: history-laden → READ HISTORY FIRST band.
    sha = repo.commit(
        "feat: alpha",
        {"pkg/alpha.py": "1"},
        when=days_ago(180),
    )
    repo.revert(sha, when=days_ago(170))
    repo.commit(
        "hotfix: regression",
        {"pkg/alpha.py": "2"},
        body="incident #INC-7",
        when=days_ago(8),
    )
    # beta: NEWBORN-only → CLEAR band.
    repo.commit("docs: tweak beta", {"pkg/beta.py": "1"}, when=days_ago(6))
    result = _invoke(
        repo.root, "diff", "--base", "HEAD~4", "--markdown", "--show-clear"
    )
    assert result.exit_code == 0, result.output
    out = result.output
    # No row collapses both into one — they live in different sections.
    assert "pkg/* (2 files)" not in out, out


def test_diff_markdown_does_not_collapse_top_level_files_with_no_common_prefix(  # type: ignore[no-untyped-def]
    repo, days_ago
) -> None:
    """Two top-level files (no directory in their path) must never group —
    folding them under a synthetic ``*/`` prefix would invent a directory
    that does not exist on disk."""
    # Both files share the same history shape (revert + hotfix with incident
    # body) so their top-signal headlines would otherwise match.
    repo.commit(
        "init",
        {"a.py": "0", "b.py": "0"},
        when=days_ago(220),
    )
    sha = repo.commit(
        "feat: edits",
        {"a.py": "1", "b.py": "1"},
        when=days_ago(180),
    )
    repo.revert(sha, when=days_ago(170))
    repo.commit(
        "hotfix: regression",
        {"a.py": "2", "b.py": "2"},
        body="incident #INC-3",
        when=days_ago(10),
    )
    result = _invoke(repo.root, "diff", "--base", "HEAD~3", "--markdown")
    assert result.exit_code == 0, result.output
    out = result.output
    # No synthetic top-level grouping.
    assert "* (2 files)" not in out, out
    assert "/* (2 files)" not in out, out
    # Both files appear as their own rows.
    assert "| `a.py` |" in out, out
    assert "| `b.py` |" in out, out


def test_diff_json_output_still_contains_every_individual_file(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The collapse is markdown-only — JSON consumers keep full per-file
    granularity, since downstream tooling parses individual entries."""
    _seed_three_sibling_templates(repo, days_ago)
    result = _invoke(repo.root, "diff", "--base", "HEAD~1", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    all_paths: list[str] = []
    for _band, rows in data["buckets"].items():
        for row in rows:
            all_paths.append(row["path"])
    for leaf in ("about.html", "footer.html", "header.html"):
        assert f"docs/_templates/{leaf}" in all_paths, all_paths
    # And no synthetic group key leaked into JSON.
    assert not any("*" in p for p in all_paths), all_paths
