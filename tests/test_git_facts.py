"""Tests for Layer 1 — deterministic git facts."""

from __future__ import annotations

import os
import subprocess

from whycode import git_facts as gf


def _git_mv(repo_root, old: str, new: str) -> None:
    """Helper: ``git mv`` then commit the rename so it shows as ``R<score>``."""
    subprocess.run(
        ["git", "-C", str(repo_root), "mv", old, new],
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--no-gpg-sign", "-q", "-m",
         f"rename {old} to {new}"],
        check=True,
        capture_output=True,
        env=env,
    )


def _git_rm(repo_root, path: str) -> None:
    """Helper: ``git rm`` then commit the deletion."""
    subprocess.run(
        ["git", "-C", str(repo_root), "rm", path],
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "--no-gpg-sign", "-q", "-m",
         f"remove {path}"],
        check=True,
        capture_output=True,
        env=env,
    )


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


def test_find_incidents_rejects_regression_test_subjects(repo) -> None:  # type: ignore[no-untyped-def]
    """F3 — feature commits whose subject contains 'regression' descriptively
    must not be flagged as incidents."""
    benign_subjects = (
        "Fixed #36883 -- Split monolithic aggregation regression tests.",
        "Refs #31055 -- Augmented regression tests for database system checks.",
        "docs: clarify regression nature of data loss bug",
        "test: add regression suite for refund flow",
        "feat: include 'no regression' badge on the dashboard",
    )
    for i, subject in enumerate(benign_subjects):
        repo.commit(subject, {"a.txt": str(i)})
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert incidents == []


def test_find_incidents_keeps_real_regressions(repo) -> None:  # type: ignore[no-untyped-def]
    """A "regression in X" subject IS an incident — anchors the word as a
    reference to an actual outage marker rather than as a test category."""
    repo.commit("fix: regression in refund processing", {"a.txt": "1"})
    repo.commit("hotfix: regression in idempotency tokens", {"a.txt": "2"})
    repo.commit(
        "Fixed: regression in admin filters",
        {"a.txt": "3"},
        body="See #4567 — admin filters used to render duplicates.",
    )
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert len(incidents) == 3


def test_find_incidents_fires_on_cve_subject(repo) -> None:  # type: ignore[no-untyped-def]
    """A subject naming a CVE or GHSA always fires — the act of citing one
    is unambiguous evidence."""
    repo.commit(
        "Fixed CVE-2026-6907 -- Prevented caching of requests when Vary header contains *.",
        {"a.txt": "1"},
    )
    repo.commit(
        "GHSA-abcd-1234-efgh: patch the auth bypass",
        {"a.txt": "2"},
    )
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert len(incidents) == 2


def test_find_incidents_fires_on_revert_subjects(repo) -> None:  # type: ignore[no-untyped-def]
    """``Reverted "..."`` and ``Reverts <sha>`` are explicit rollback markers."""
    repo.commit('Reverted "feat: switch to async refund flow"', {"a.txt": "1"})
    repo.commit("Reverts a3f4b2c1234567 (cherry-picked from 2026)", {"a.txt": "2"})
    commits = gf.commits_for_path(repo.root, "a.txt")
    incidents = gf.find_incidents(commits)
    assert len(incidents) == 2


def test_find_incidents_rejects_restore_pathlib_style(repo) -> None:  # type: ignore[no-untyped-def]
    """Subjects like 'Restore support for using pathlib.Path' are descriptive
    behaviour-restoration commits, not incidents — they used to falsely fire
    on F3 when keyword matching was looser. Today they don't carry an
    incident keyword at all and stay clean."""
    repo.commit(
        "Restore support for using pathlib.Path for static_folder.",
        {"a.txt": "1"},
    )
    commits = gf.commits_for_path(repo.root, "a.txt")
    assert gf.find_incidents(commits) == []


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


def test_extract_invariant_quotes_recognises_rfc_cite_in_body(repo) -> None:  # type: ignore[no-untyped-def]
    """A body that cites an RFC is an explicit "honour this spec" decision
    even when no free-form invariant token appears alongside."""
    body = "Strip Authorization header per RFC 7235 scheme+authority rule."
    repo.commit("auth: drop header on redirect", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    assert len(quotes) == 1
    assert "RFC 7235" in quotes[0][1]


def test_extract_invariant_quotes_recognises_pep_and_cve_cites(repo) -> None:  # type: ignore[no-untyped-def]
    """PEP and CVE cites also fire — both are standards / advisory references
    that anchor a "we follow this" decision in the body."""
    repo.commit(
        "build: respect externally managed env",
        {"a.py": "1"},
        body="Follows PEP 668 — pip refuses to install into a system env.",
    )
    repo.commit(
        "auth: patch upstream issue",
        {"b.py": "1"},
        body="Per CVE-2024-1234 advisory we now reject zero-length tokens.",
    )
    a_quotes = gf.extract_invariant_quotes(
        gf.commits_for_path(repo.root, "a.py")
    )
    b_quotes = gf.extract_invariant_quotes(
        gf.commits_for_path(repo.root, "b.py")
    )
    assert any("PEP 668" in line for _, line in a_quotes)
    assert any("CVE-2024-1234" in line for _, line in b_quotes)


def test_extract_invariant_quotes_ignores_unrelated_4_digit_numbers(repo) -> None:  # type: ignore[no-untyped-def]
    """A body that mentions a year or a ticket number — without a standards
    prefix — must not fire as a standards cite; the regex is anchored to the
    prefix and four free-standing digits are not enough."""
    body = "Fixed in 2024 by user 1234, see meeting notes for context."
    repo.commit("docs: note who fixed it", {"x.py": "1"}, body=body)
    commits = gf.commits_for_path(repo.root, "x.py")
    quotes = gf.extract_invariant_quotes(commits)
    assert quotes == []


def _synth_pivot_commit(subject: str, body: str) -> gf.Commit:
    from datetime import UTC, datetime
    return gf.Commit(
        sha="a" * 40,
        author_name="Test",
        author_email="test@example.com",
        authored_at=datetime(2026, 5, 1, tzinfo=UTC),
        subject=subject,
        body=body,
    )


def test_is_subject_blind_pivot_short_body_fires_with_security_subject() -> None:
    """A subject naming a security-class concern lowers the body-length floor
    so terse-but-load-bearing security commits surface as pivots."""
    body = "x" * 100  # between 80 (security floor) and 200 (default floor)
    commit = _synth_pivot_commit("fix cookie signing", body)
    assert gf.is_subject_blind_pivot_commit(commit) is True


def test_is_subject_blind_pivot_short_body_silent_without_security_subject() -> None:
    """The same body length but without a security token in the subject stays
    silent — the lower floor only applies to security-class subjects."""
    body = "x" * 100
    commit = _synth_pivot_commit("tweak internal naming", body)
    assert gf.is_subject_blind_pivot_commit(commit) is False


def test_is_subject_blind_pivot_long_body_fires_regardless_of_subject_tokens() -> None:
    """Existing behaviour: any non-incident, non-revert subject with a body
    >= 200 chars fires, with or without security tokens in the subject."""
    body = "x" * 500
    commit = _synth_pivot_commit("Reorganize internal layout", body)
    assert gf.is_subject_blind_pivot_commit(commit) is True


def test_is_subject_blind_pivot_security_subject_with_short_body_below_80() -> None:
    """The security floor is 80, not zero — a sub-80-char body still doesn't
    pass, so the lowered threshold isn't trivially bypassed."""
    body = "x" * 50
    commit = _synth_pivot_commit("fix cookie signing", body)
    assert gf.is_subject_blind_pivot_commit(commit) is False


def test_is_subject_blind_pivot_hotfix_security_does_not_double_fire() -> None:
    """A "hotfix: cookie signing" subject trips the incident rule and the
    pivot detector must stay silent — the chapter ladder routes the commit
    through ``incident`` either way, but suppressing pivot keeps the per-
    commit classification clean and prevents double-counting."""
    body = "x" * 100
    commit = _synth_pivot_commit("hotfix: cookie signing regression", body)
    assert gf.is_subject_blind_pivot_commit(commit) is False


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


# ---- DiffFacts batch loader (perf/diff-batched) ----------------------------


def test_load_diff_facts_indexes_commits_by_path(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1"})
    repo.commit("update a", {"a.py": "2"})
    repo.commit("update b", {"b.py": "2"})
    repo.commit("update both", {"a.py": "3", "b.py": "3"})

    facts = gf.load_diff_facts(repo.root)

    a_subjects = [c.subject for c in facts.commits_by_path["a.py"]]
    b_subjects = [c.subject for c in facts.commits_by_path["b.py"]]
    # newest first
    assert a_subjects == ["update both", "update a", "init"]
    assert b_subjects == ["update both", "update b", "init"]


def test_load_diff_facts_co_change_index_lists_full_file_set(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1", "c.py": "1"})
    sha = repo.commit("change a and b", {"a.py": "2", "b.py": "2"})

    facts = gf.load_diff_facts(repo.root)

    paths = facts.co_change_index[sha]
    assert set(paths) == {"a.py", "b.py"}


def test_gather_for_diff_returns_repo_facts_from_in_memory_map(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feature: A", {"a.py": "1"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: regression in a",
        {"a.py": "2"},
        body="incident #42",
        when=days_ago(5),
    )

    diff_facts = gf.load_diff_facts(repo.root)
    facts = gf.gather_for_diff(diff_facts, "a.py")

    assert len(facts.commits) == 3
    assert len(facts.revert_pairs) == 1
    assert any("hotfix" in c.subject for c in facts.incident_commits)


def test_gather_for_diff_co_changes_match_per_file_pipeline(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1", "c.py": "1"})
    repo.commit("change a and b", {"a.py": "2", "b.py": "2"})
    repo.commit("change a and b again", {"a.py": "3", "b.py": "3"})
    repo.commit("change a alone", {"a.py": "4"})

    diff_facts = gf.load_diff_facts(repo.root)
    batched = gf.gather_for_diff(diff_facts, "a.py")
    legacy = gf.gather(repo.root, "a.py")

    assert dict(batched.co_changed_files) == dict(legacy.co_changed_files)


def test_gather_for_diff_for_unseen_path_returns_empty_facts(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})

    diff_facts = gf.load_diff_facts(repo.root)
    facts = gf.gather_for_diff(diff_facts, "never-touched.py")

    assert facts.commits == []
    assert dict(facts.co_changed_files) == {}


def test_load_diff_facts_handles_multiline_body_then_numstat(repo) -> None:  # type: ignore[no-untyped-def]
    body = "first paragraph\n\nsecond paragraph with: colons, commas, etc."
    repo.commit("subject line", {"x.txt": "1"}, body=body)

    facts = gf.load_diff_facts(repo.root)
    [commit] = facts.commits_by_path["x.txt"]
    assert commit.subject == "subject line"
    assert "second paragraph" in commit.body
    assert commit.files == ("x.txt",)


def test_load_diff_facts_max_commits_caps_per_path(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"})
    for i in range(5):
        repo.commit(f"tweak {i}", {"a.py": str(i + 2)})

    facts = gf.load_diff_facts(repo.root, max_commits=2)
    assert len(facts.commits_by_path["a.py"]) == 2


# ---- build_rename_map ------------------------------------------------------


def test_build_rename_map_resolves_simple_rename(repo) -> None:  # type: ignore[no-untyped-def]
    """A single ``git mv a.py b.py`` registers an ``a.py → b.py`` mapping
    while ``b.py`` (already at its terminal name) is absent from the map."""
    # Clear any process-local lru memoisation between repos.
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "x = 1\n"})
    _git_mv(repo.root, "a.py", "b.py")
    rmap = gf.build_rename_map(repo.root)
    assert rmap.get("a.py") == "b.py"
    assert "b.py" not in rmap


def test_build_rename_map_chains_terminal_name(repo) -> None:  # type: ignore[no-untyped-def]
    """A chain ``a.py → b.py → c.py`` collapses to terminal ``c.py``
    under both pre-rename keys."""
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "x = 1\n"})
    _git_mv(repo.root, "a.py", "b.py")
    _git_mv(repo.root, "b.py", "c.py")
    rmap = gf.build_rename_map(repo.root)
    assert rmap.get("a.py") == "c.py"
    assert rmap.get("b.py") == "c.py"
    assert "c.py" not in rmap


def test_build_rename_map_drops_renames_to_deleted_files(repo) -> None:  # type: ignore[no-untyped-def]
    """When the terminal new name has since been deleted, the mapping is
    dropped: following it would point a reader at a non-existent file."""
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "x = 1\n"})
    _git_mv(repo.root, "a.py", "b.py")
    _git_rm(repo.root, "b.py")
    rmap = gf.build_rename_map(repo.root)
    # Both pre- and post-rename names are gone from HEAD, so neither
    # should surface as a resolvable target.
    assert rmap == {}


def test_build_rename_map_handles_rename_back_and_forth(repo) -> None:  # type: ignore[no-untyped-def]
    """A ``a.py → b.py → a.py`` round-trip leaves a sensible terminal map:
    ``b.py → a.py`` (b.py was an interim state, a.py is the terminal name).
    """
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "x = 1\n"})
    _git_mv(repo.root, "a.py", "b.py")
    _git_mv(repo.root, "b.py", "a.py")
    rmap = gf.build_rename_map(repo.root)
    assert rmap.get("b.py") == "a.py"
    # The a.py entry depends on chain compression — newest record wins,
    # but since a.py is the terminal name itself, the map's contract is
    # "a.py is at HEAD" rather than "a.py needs resolving". Either no
    # entry for a.py at all, or a.py → a.py, is acceptable. We document
    # the current behaviour (newest record overrides a self-loop):
    if "a.py" in rmap:
        assert rmap["a.py"] == "a.py"


def test_build_rename_map_uses_cache_when_present(repo) -> None:  # type: ignore[no-untyped-def]
    """A second invocation under the same HEAD must serve from the cache
    rather than re-shelling-out to git log."""
    from whycode import cache as ch
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "x = 1\n"})
    _git_mv(repo.root, "a.py", "b.py")
    with ch.open_for(repo.root) as store:
        first = gf.build_rename_map(repo.root, cache=store)
        # Plant a sentinel in the cache that would corrupt a real git walk
        # but is invisible to the in-process lru_cache. The second read
        # must come from the SQLite layer.
        gf._build_rename_map_at_head.cache_clear()
        store._conn.execute(
            "INSERT OR REPLACE INTO rename_map(head_sha, old_path, new_path) "
            "VALUES (?, ?, ?)",
            (store.head_sha or gf.run_git(repo.root, "rev-parse", "HEAD").strip(),
             "sentinel.py", "marker.py"),
        )
        store._conn.commit()
        second = gf.build_rename_map(repo.root, cache=store)
    assert first.get("a.py") == "b.py"
    assert second.get("sentinel.py") == "marker.py"


def test_build_rename_map_empty_on_repo_with_no_renames(repo) -> None:  # type: ignore[no-untyped-def]
    """A repo with zero rename history yields an empty map (not an error)."""
    gf._build_rename_map_at_head.cache_clear()
    repo.commit("init", {"a.py": "1"})
    repo.commit("tweak", {"a.py": "2"})
    rmap = gf.build_rename_map(repo.root)
    assert rmap == {}
