"""Tests for Layer 1 — deterministic git facts."""

from __future__ import annotations

from whycode import git_facts as gf


def test_discover_repo_root(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "hello"})
    sub = repo.root / "deep" / "nested"
    sub.mkdir(parents=True)
    assert gf.discover_repo_root(sub) == repo.root


def test_commits_for_path_returns_only_relevant_commits(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("add a", {"a.txt": "v1"})
    repo.commit("add b", {"b.txt": "v1"})
    repo.commit("update a", {"a.txt": "v2"})
    a_commits = gf.commits_for_path(repo.root, "a.txt")
    assert [c.subject for c in a_commits] == ["update a", "add a"]


def test_log_record_separators_handle_multiline_bodies(repo) -> None:  # type: ignore[no-untyped-def]
    body = "first paragraph\n\nsecond paragraph with: colons, commas, etc."
    repo.commit("subject line", {"x.txt": "1"}, body=body)
    [commit] = gf.commits_for_path(repo.root, "x.txt")
    assert commit.subject == "subject line"
    assert "second paragraph" in commit.body


def test_find_revert_pairs_picks_default_revert_message(repo) -> None:  # type: ignore[no-untyped-def]
    sha1 = repo.commit("feature: add X", {"x.txt": "1"})
    repo.commit("noise", {"y.txt": "1"})
    repo.revert(sha1)
    commits = gf.all_commits(repo.root)
    pairs = gf.find_revert_pairs(commits)
    assert len(pairs) == 1
    revert_sha, reverted_sha = pairs[0]
    assert reverted_sha.startswith(sha1[:7])
    assert revert_sha != sha1


def test_find_incidents_matches_keywords(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("feat: nothing exciting", {"a.txt": "1"})
    repo.commit("hotfix: payment double-charge", {"a.txt": "2"})
    repo.commit("fix: minor", {"a.txt": "3"}, body="related to incident #INC-447")
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    subjects = [c.subject for c in incidents]
    assert "hotfix: payment double-charge" in subjects
    assert any("incident" in c.body.lower() for c in incidents)


def test_find_incidents_matches_conventional_commits_breaking(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("feat: ordinary feature", {"a.txt": "1"})
    repo.commit("feat!: rewrite the public API", {"a.txt": "2"})
    repo.commit("fix!: drop legacy header", {"a.txt": "3"})
    repo.commit("chore: bump deps", {"a.txt": "4"})
    commits = gf.commits_for_path(repo.root, "a.txt")
    subjects = {c.subject for c in gf.find_incidents(commits)}
    assert "feat!: rewrite the public API" in subjects
    assert "fix!: drop legacy header" in subjects
    assert "feat: ordinary feature" not in subjects
    assert "chore: bump deps" not in subjects


def test_find_incidents_matches_breaking_change_in_body(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "refactor: payment module",
        {"a.txt": "1"},
        body="BREAKING CHANGE: charges are now async; consumers must await.",
    )
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert len(incidents) == 1


def test_find_incidents_matches_regression_keyword(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("fix: regression in refunds", {"a.txt": "1"})
    commits = gf.commits_for_path(repo.root, "a.txt")
    assert len(gf.find_incidents(commits)) == 1


def test_find_incidents_ignores_passing_body_mentions(repo) -> None:  # type: ignore[no-untyped-def]
    """A body that mentions 'incident' as part of a feature description, with
    no issue id nearby, is not an incident commit."""
    repo.commit(
        "feat: add structured logging",
        {"a.txt": "1"},
        body=(
            "WhyCode reads commit messages, including incident-tagged commits.\n"
            "This change adds structured fields so downstream tooling can index them."
        ),
    )
    commits = gf.commits_for_path(repo.root, "a.txt")
    assert gf.find_incidents(commits) == []


def test_find_incidents_fires_when_body_has_keyword_and_issue_id(repo) -> None:  # type: ignore[no-untyped-def]
    """A body keyword corroborated by an issue id IS an incident commit."""
    for issue_marker in ("#1234", "INC-447", "JIRA-123", "SEV-1", "P0"):
        repo.commit(
            "fix: small change",
            {"a.txt": issue_marker},
            body=f"Resolved an incident — see {issue_marker} for context.",
        )
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert len(incidents) == 5


def test_extract_invariant_quotes_pulls_warning_lines(repo) -> None:  # type: ignore[no-untyped-def]
    body = (
        "We must keep the synchronous call here.\n"
        "Do not switch to async — v1 clients break.\n"
        "Important: keep the legacy header in place."
    )
    repo.commit("compat: keep sync path", {"a.txt": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "a.txt")
    quotes = gf.extract_invariant_quotes(commits)
    lines = [line for _, line in quotes]
    assert any("Do not switch to async" in line for line in lines)
    assert any(line.lower().startswith("important:") for line in lines)


def test_extract_invariant_quotes_ignores_quoted_tokens(repo) -> None:  # type: ignore[no-untyped-def]
    body = (
        'fix the matcher so "do not" and "warning:" still fire as expected.\n'
        "Genuine constraint: Do not call this from threads."
    )
    repo.commit("fix: matcher", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    lines = [line for _, line in gf.extract_invariant_quotes(commits)]
    assert any("Do not call this from threads" in line for line in lines)
    assert not any('"do not"' in line for line in lines)


def test_extract_invariant_quotes_caps_a_single_paste_at_two(repo) -> None:  # type: ignore[no-untyped-def]
    """F2 — a spell-check commit on django used to supply 12 ALLCAPS warnings
    that all flowed through as 'invariants', dominating the highlights view.

    The per-commit cap of 2 keeps the loudest noise commit from drowning
    out genuine invariants from other commits.
    """
    body = "\n".join(
        f"WARNING: spell check found misspelling number {i}" for i in range(12)
    )
    repo.commit("docs: fix spelling typos", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    # All twelve are tool-output ALLCAPS lines — they're filtered before the
    # cap even applies. Total quotes from this commit: 0.
    assert len(quotes) == 0


def test_extract_invariant_quotes_drops_path_line_prefixed_lines(repo) -> None:  # type: ignore[no-untyped-def]
    """A ``tools/spelling.py:50:`` linter prefix is unmistakably tool output."""
    body = (
        "tools/spelling.py:50: warning: misspelled 'recieve'\n"
        "tools/spelling.py:51: warning: misspelled 'occured'\n"
        "tools/spelling.py:52: warning: misspelled 'seperate'"
    )
    repo.commit("docs: fix typos", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    assert len(quotes) == 0


def test_extract_invariant_quotes_keeps_real_invariant(repo) -> None:  # type: ignore[no-untyped-def]
    """A genuine author-stated invariant is still surfaced as one entry."""
    body = (
        "Refactored the refund flow.\n"
        "Do not switch to async — v1 clients break."
    )
    repo.commit("compat: keep sync", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    assert len(quotes) == 1
    assert "Do not switch to async" in quotes[0][1]


def test_extract_invariant_quotes_caps_at_two_real_invariants(repo) -> None:  # type: ignore[no-untyped-def]
    """When a commit body has 5 genuine invariants, only the first 2 surface."""
    body = "\n".join(
        [
            "Do not call this from threads.",
            "Do not switch to async.",
            "Do not bypass the rate limiter.",
            "Do not log the auth header.",
            "Do not delete the legacy endpoint.",
        ]
    )
    repo.commit("compat: hardening", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    assert len(quotes) == 2
    # First two are preserved (most informative-looking ranks).
    assert "Do not call this from threads" in quotes[0][1]
    assert "Do not switch to async" in quotes[1][1]


def test_co_changes_excludes_target_file(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1", "b.txt": "1", "c.txt": "1"})
    repo.commit("change a and b", {"a.txt": "2", "b.txt": "2"})
    repo.commit("change a and b again", {"a.txt": "3", "b.txt": "3"})
    repo.commit("change a alone", {"a.txt": "4"})
    a_commits = gf.commits_for_path(repo.root, "a.txt")
    counter = gf.co_changes(repo.root, a_commits, "a.txt")
    assert counter["b.txt"] == 3  # init + 2 changes
    assert "a.txt" not in counter


def test_author_last_activity_returns_none_for_unknown(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    assert gf.author_last_activity(repo.root, "nobody@nowhere.invalid") is None


def test_author_last_activity_returns_recent_for_known(repo, now, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init",
        {"a.txt": "1"},
        when=days_ago(2),
        author_name="Alice",
        author_email="alice@example.com",
    )
    seen = gf.author_last_activity(repo.root, "alice@example.com")
    assert seen is not None
    # Within a day of the configured commit time.
    assert abs((seen - days_ago(2)).total_seconds()) < 86400


def test_line_ownership_returns_email_to_line_count(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init",
        {"shared.py": "alice line 1\nalice line 2\nalice line 3\n"},
        author_name="Alice",
        author_email="alice@example.com",
    )
    repo.commit(
        "tweak",
        {"shared.py": "alice line 1\nalice line 2\nalice line 3\nbob line 4\n"},
        author_name="Bob",
        author_email="bob@example.com",
    )
    counts = gf.line_ownership(repo.root, "shared.py")
    assert counts.get("alice@example.com", 0) == 3
    assert counts.get("bob@example.com", 0) == 1


def test_line_ownership_empty_for_missing_file(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    assert gf.line_ownership(repo.root, "no-such-file.py") == {}


def test_parse_log_records_tolerates_pathological_timezone() -> None:
    """A 2011 commit on psf/requests has tz offset ``+518:00``.

    ``datetime.fromisoformat`` rejects that. The repair must normalise it
    to ``+05:18`` so a single bad record cannot poison the whole walk.
    """
    raw = (
        "abc1234"
        + gf.UNIT_SEP
        + "Author"
        + gf.UNIT_SEP
        + "a@b"
        + gf.UNIT_SEP
        + "2011-09-08T02:38:50+518:00"
        + gf.UNIT_SEP
        + "subject"
        + gf.UNIT_SEP
        + "body"
        + gf.RECORD_SEP
    )
    # Must not raise.
    commits = gf._parse_log_records(raw)
    assert len(commits) == 1
    assert commits[0].sha == "abc1234"
    # The repaired offset is +05:18 — i.e. tz info attached.
    assert commits[0].authored_at.utcoffset() is not None


def test_parse_log_records_tolerates_compact_offset_form() -> None:
    """``+51800`` (no colon) — the underlying object form — also normalises."""
    raw = (
        "deadbee"
        + gf.UNIT_SEP
        + "Author"
        + gf.UNIT_SEP
        + "a@b"
        + gf.UNIT_SEP
        + "2011-09-08T02:38:50+51800"
        + gf.UNIT_SEP
        + "subject"
        + gf.UNIT_SEP
        + "body"
        + gf.RECORD_SEP
    )
    commits = gf._parse_log_records(raw)
    assert len(commits) == 1
    assert commits[0].authored_at.utcoffset() is not None


def test_parse_log_records_irrecoverable_falls_back_to_epoch() -> None:
    """A truly unrepairable timestamp falls back to the epoch sentinel
    so the walk continues — never crashes the analysis."""
    raw = (
        "feedfac"
        + gf.UNIT_SEP
        + "Author"
        + gf.UNIT_SEP
        + "a@b"
        + gf.UNIT_SEP
        + "totally not a date"
        + gf.UNIT_SEP
        + "subject"
        + gf.UNIT_SEP
        + "body"
        + gf.RECORD_SEP
    )
    commits = gf._parse_log_records(raw)
    assert len(commits) == 1
    # Still a tz-aware datetime so callers can compare it.
    assert commits[0].authored_at.tzinfo is not None
