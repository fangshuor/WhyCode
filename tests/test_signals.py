"""Tests for Layer 2 — heuristic signals."""

from __future__ import annotations

from datetime import UTC, datetime

from whycode import git_facts as gf
from whycode import signals as sig
from whycode.git_facts import Commit


def _facts_for(repo, path: str):  # type: ignore[no-untyped-def]
    return gf.gather(repo.root, path)


def test_no_signals_for_quiet_recent_file(repo, now) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init: add a", {"a.txt": "1"}, when=now)
    facts = _facts_for(repo, "a.txt")
    signals = sig.all_signals(facts)
    # The only signal should be NEWBORN (very young file).
    kinds = {s.kind for s in signals}
    assert kinds.issubset({sig.SignalKind.NEWBORN})


def test_revert_chain_fires(repo, now, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit(
        "feat: payment refactor",
        {"refund.py": "v1"},
        when=days_ago(60),
    )
    repo.revert(sha, when=days_ago(55))
    facts = _facts_for(repo, "refund.py")
    signals = sig.all_signals(facts)
    kinds = {s.kind for s in signals}
    assert sig.SignalKind.REVERT_CHAIN in kinds


def test_revert_chain_recent_keeps_full_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    # 1 revert → base severity 3, < 2yr → no decay → 3
    assert signal.severity == 3
    assert "weeks ago" in signal.headline or "month" in signal.headline


def test_revert_chain_old_decays_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # A revert from > 5 years ago should weigh at least one step less than a fresh one.
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(2200))
    repo.revert(sha, when=days_ago(2100))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    # 1 revert → base 3, > 5yr → decay 2 → 1
    assert signal.severity == 1
    assert "year" in signal.headline


def test_invariant_quote_old_decays_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.\nImportant: legacy header.",
        when=days_ago(800),  # ~2.2 years ago
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    # 2 invariants → base 3, 2-5yr → decay 1 → 2
    assert signal.severity == 2
    assert "year" in signal.headline or "month" in signal.headline


def test_incident_history_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("feat: refunds", {"refund.py": "1"}, when=days_ago(120))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "refund.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.severity >= 3


def test_invariant_quotes_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.\nImportant: legacy header must stay.",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert "Do not switch to async" in signal.detail or "legacy header" in signal.detail.lower()


def test_silence_fires_for_old_files(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"old.py": "1"}, when=days_ago(400))
    facts = _facts_for(repo, "old.py")
    signal = sig.detect_silence(facts)
    assert signal is not None
    assert signal.severity >= 2


def test_coupling_fires_when_files_co_change_often(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    repo.commit("change", {"a.py": "2", "b.py": "2"}, when=days_ago(50))
    repo.commit("change", {"a.py": "3", "b.py": "3"}, when=days_ago(40))
    repo.commit("change", {"a.py": "4", "b.py": "4"}, when=days_ago(30))
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_coupling(facts)
    assert signal is not None
    assert "b.py" in signal.detail


def test_is_likely_self_reference_helper_path_normalisation() -> None:
    """Pre-rename self-reference detection rules.

    A renamed file (``src/click/core.py`` was once ``click/core.py``) shouldn't
    appear as its own #1 co-changer. Also: same-basename files in unrelated
    dirs (``a/b/__init__.py`` vs ``c/d/__init__.py``) must NOT collide.
    """
    is_self = sig._is_likely_self_reference
    assert is_self("src/click/core.py", "src/click/core.py") is True
    assert is_self("src/click/core.py", "click/core.py") is True
    assert is_self("click/core.py", "src/click/core.py") is True
    assert is_self("cli.py", "src/cli.py") is True

    assert is_self("src/whycode/cli.py", "tests/test_cli/cli.py") is False
    assert is_self("a/b/__init__.py", "c/d/__init__.py") is False
    assert is_self("src/click/core.py", "src/click/types.py") is False
    assert is_self("src/click/core.py", "tests/test_basic.py") is False


def test_coupling_excludes_pre_rename_self_reference(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """When the same physical file appears under multiple historical paths
    in the co-change index (a real ``src/`` move pattern), the coupling
    detector must not list it as its own loudest co-changer.

    We can't easily replay a real git rename in a synthetic test, so we
    exercise the public ``detect_coupling`` against a co-change map that
    includes the target's own path — which is what the diff command's
    in-memory load does until rename-resolution is threaded through.
    """
    # Five commits touching both a real co-changer and the target file,
    # plus a synthesised pre-rename self-reference seeded by hand below.
    for i in range(5):
        repo.commit(
            f"step {i}",
            {"src/x/core.py": str(i), "src/x/types.py": str(i)},
            when=days_ago(60 - i * 5),
        )
    facts = _facts_for(repo, "src/x/core.py")
    # Manually inject a pre-rename self-reference into the co-change map.
    facts = gf.RepoFacts(
        path=facts.path,
        repo_root=facts.repo_root,
        commits=facts.commits,
        co_changed_files={
            "x/core.py": 7,
            "src/x/types.py": facts.co_changed_files.get("src/x/types.py", 4),
        },
        revert_pairs=facts.revert_pairs,
        incident_commits=facts.incident_commits,
        invariant_quotes=facts.invariant_quotes,
        cache=facts.cache,
    )
    signal = sig.detect_coupling(facts)
    assert signal is not None
    # The pre-rename self path must NOT appear in coupling output.
    assert "x/core.py" not in signal.detail.replace("src/x/core.py", "")
    assert "src/x/types.py" in signal.detail


def test_body_references_fires_when_body_cites_prior_sha(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A commit body that cites another SHA from this file's history is the
    narrative bridge linking past decisions to present ones."""
    sha_a = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.commit("feat: B", {"x.py": "2"}, when=days_ago(40))
    repo.commit(
        "feat: reconciliation",
        {"x.py": "3"},
        body=f"This reconsiders the approach in {sha_a[:10]} after benchmarking.",
        when=days_ago(20),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_body_references(facts)
    assert signal is not None
    assert signal.kind is sig.SignalKind.BODY_REFERENCE
    assert sha_a[:7] in signal.detail


def test_body_references_silent_when_no_cross_reference(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A body that mentions an unrelated SHA-shaped string (e.g. a
    version number, an unrelated repo's SHA) must not fire."""
    repo.commit("init", {"x.py": "1"}, when=days_ago(40))
    repo.commit(
        "follow up",
        {"x.py": "2"},
        body="Bumps version from 1234567 to abcdef9 — unrelated.",
        when=days_ago(20),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_body_references(facts)
    assert signal is None


def test_body_references_skips_merge_commits(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """`Merge ...` commits routinely include SHA mentions that are not
    narrative bridges; they must be ignored."""
    sha_a = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.commit(
        f"Merge branch 'feature' — fast-forward of {sha_a[:7]}",
        {"x.py": "2"},
        body=f"Merge commit referencing {sha_a[:8]} routinely.",
        when=days_ago(20),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_body_references(facts)
    assert signal is None


def test_subject_blind_pivot_fires_on_long_body_non_incident_commit(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """An architectural pivot — generic subject, long explanatory body — is
    invisible to the incident / revert / merge ladders but is exactly the
    load-bearing decision a careful reader needs to find."""
    long_body = (
        "This change reorganises the internal layout to separate the public "
        "façade from the private dispatch helpers. The motivation is to make "
        "the contract explicit: callers should not depend on the private "
        "module path, and tests should pin the public surface only. We keep "
        "backwards compat by re-exporting the old names with a __getattr__ "
        "shim until the next major release."
    )
    assert len(long_body) >= 200
    repo.commit(
        "Reorganize internal layout",
        {"core.py": "1"},
        body=long_body,
        when=days_ago(20),
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_subject_blind_pivot(facts)
    assert signal is not None
    assert signal.kind is sig.SignalKind.SUBJECT_BLIND_PIVOT
    assert "pivot" in signal.headline.lower()


def test_subject_blind_pivot_silent_on_short_body(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """A short-body commit doesn't pass the floor — could be just a tweak."""
    repo.commit(
        "Reorganize internal layout",
        {"core.py": "1"},
        body="small refactor; see notes.",  # ~26 chars, well below floor
        when=days_ago(20),
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_subject_blind_pivot(facts)
    assert signal is None


def test_subject_blind_pivot_silent_on_incident_subject(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """An incident-keyword subject with a long body must NOT fire — the
    incident detector already surfaces it; firing twice would double-count."""
    long_body = (
        "Hotfix details: the regression manifested at 02:00 UTC and was "
        "rolled back within fifteen minutes after pager went off. Root cause "
        "is the missing null-check in the decoder path, exposed by the "
        "upstream client library upgrade in the release tag earlier today."
    )
    assert len(long_body) >= 200
    repo.commit(
        "hotfix: regression",
        {"core.py": "1"},
        body=long_body,
        when=days_ago(10),
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_subject_blind_pivot(facts)
    assert signal is None


def test_subject_blind_pivot_silent_on_revert(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """A revert subject with a long body is the revert detector's territory."""
    long_body = (
        "Reverting the prior change because the assumption baked into the "
        "fast path turns out to be wrong on Windows: the path separator "
        "handling collides with the cache key normalisation we introduced "
        "two weeks ago, breaking every CI run on the win32 builders today."
    )
    assert len(long_body) >= 200
    repo.commit(
        "Revert \"feat: speculative cache\"",
        {"core.py": "1"},
        body=long_body,
        when=days_ago(10),
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_subject_blind_pivot(facts)
    assert signal is None


def test_subject_blind_pivot_silent_on_merge(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    """Merge commits routinely carry long bodies (PR descriptions, conflict
    notes); they aren't narratively load-bearing on their own."""
    long_body = (
        "Merging the long-running branch back into main. Includes the "
        "rebased commits from the contributor PR plus two CI fixes the "
        "reviewer requested in line with the new style guide; conflict "
        "resolution was straightforward, all tests pass on the local box."
    )
    assert len(long_body) >= 200
    repo.commit(
        "Merge branch 'feature/restructure'",
        {"core.py": "1"},
        body=long_body,
        when=days_ago(10),
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_subject_blind_pivot(facts)
    assert signal is None


# ---- chapter_role pivot ladder --------------------------------------------
#
# Synthetic Commits drive these tests directly: classify_commit's pivot slot
# is a per-commit decision and the helper does not read the on-disk repo.
# That keeps the ladder tests independent of the conftest RepoBuilder.


def _synth_commit(subject: str, body: str, sha: str = "a" * 40) -> Commit:
    return Commit(
        sha=sha,
        author_name="Test",
        author_email="test@example.com",
        authored_at=datetime(2026, 5, 1, tzinfo=UTC),
        subject=subject,
        body=body,
    )


def test_classify_commit_role_pivot_when_long_body_generic_subject() -> None:
    """A long-body commit with a generic-looking subject — neither incident
    keyword nor revert marker nor merge prefix — classifies as a pivot. Same
    shape the L2 detector fires on; the chapter ladder must agree."""
    long_body = (
        "This change reorganises the internal layout to separate the public "
        "façade from the private dispatch helpers. The motivation is to make "
        "the contract explicit: callers should not depend on the private "
        "module path, and tests should pin the public surface only. We keep "
        "backwards compat by re-exporting the old names with a __getattr__ "
        "shim until the next major release."
    )
    assert len(long_body) >= 200
    commit = _synth_commit("Reorganize internal layout", long_body)
    classification = gf.classify_commit(commit)
    assert classification.is_pivot is True
    assert classification.chapter_role == "pivot"


def test_classify_commit_role_revert_wins_over_pivot() -> None:
    """A long-body commit whose subject is a default revert pointer is
    handled by the revert detector; pivot is suppressed earlier in the
    ladder."""
    long_body = (
        "Reverting the prior change because the assumption baked into the "
        "fast path turns out to be wrong on Windows: the path separator "
        "handling collides with the cache key normalisation we introduced "
        "two weeks ago, breaking every CI run on the win32 builders today."
    )
    assert len(long_body) >= 200
    commit = _synth_commit('Revert "feat: speculative cache"', long_body)
    classification = gf.classify_commit(commit)
    assert classification.is_revert is True
    assert classification.chapter_role == "revert"


def test_classify_commit_role_incident_wins_over_pivot() -> None:
    """A long-body commit whose subject carries an incident keyword is the
    incident detector's territory; the chapter ladder must keep firing
    incident first."""
    long_body = (
        "Hotfix details: the regression manifested at 02:00 UTC and was "
        "rolled back within fifteen minutes after pager went off. Root cause "
        "is the missing null-check in the decoder path, exposed by the "
        "upstream client library upgrade in the release tag earlier today."
    )
    assert len(long_body) >= 200
    commit = _synth_commit("hotfix: regression", long_body)
    classification = gf.classify_commit(commit)
    assert classification.incident_flavoured is True
    assert classification.chapter_role == "incident"


def test_classify_commit_role_pivot_above_invariant() -> None:
    """A commit body that BOTH passes the 200-char pivot floor AND states
    an invariant must classify as pivot, not invariant — pivot sits above
    invariant in the priority ladder."""
    long_body = (
        "This change reorganises the internal dispatch so the synchronous "
        "façade can grow without dragging the async path into every caller. "
        "Don't switch to async — v1 clients still depend on the sync entry "
        "and the library cannot break that contract until the next major. "
        "The shim re-exports the existing names so reverse-compat holds."
    )
    assert len(long_body) >= 200
    commit = _synth_commit("Reorganize dispatch internals", long_body)
    classification = gf.classify_commit(commit)
    assert classification.is_pivot is True
    assert classification.invariant_count > 0
    assert classification.chapter_role == "pivot"


def test_coupling_filters_ignored_paths(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """F10 — co-change candidates that scan would hide must not appear in
    the per-file coupling signal either. ``CHANGELOG.md`` is in the default
    ignore list; it co-changes 4x with ``src/foo.py`` here but must not
    surface in the headline.
    """
    repo.commit(
        "init",
        {"src/foo.py": "1", "CHANGELOG.md": "v1", "src/bar.py": "1"},
        when=days_ago(60),
    )
    repo.commit(
        "change",
        {"src/foo.py": "2", "CHANGELOG.md": "v2", "src/bar.py": "2"},
        when=days_ago(50),
    )
    repo.commit(
        "change",
        {"src/foo.py": "3", "CHANGELOG.md": "v3", "src/bar.py": "3"},
        when=days_ago(40),
    )
    repo.commit(
        "change",
        {"src/foo.py": "4", "CHANGELOG.md": "v4", "src/bar.py": "4"},
        when=days_ago(30),
    )
    facts = _facts_for(repo, "src/foo.py")
    signal = sig.detect_coupling(facts)
    assert signal is not None
    assert "src/bar.py" in signal.detail
    # CHANGELOG.md must not appear in the headline / detail / evidence.
    assert "CHANGELOG" not in signal.detail
    assert "CHANGELOG" not in " ".join(signal.evidence)


def test_coupling_returns_none_when_only_ignored_paths_co_change(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A file whose only co-changers are filtered must produce no signal —
    not a "tightly coupled to 0 files" surface."""
    repo.commit(
        "init",
        {"src/foo.py": "1", "CHANGELOG.md": "v1", "AUTHORS": "alice"},
        when=days_ago(60),
    )
    repo.commit(
        "change",
        {"src/foo.py": "2", "CHANGELOG.md": "v2", "AUTHORS": "bob"},
        when=days_ago(50),
    )
    repo.commit(
        "change",
        {"src/foo.py": "3", "CHANGELOG.md": "v3", "AUTHORS": "carol"},
        when=days_ago(40),
    )
    facts = _facts_for(repo, "src/foo.py")
    signal = sig.detect_coupling(facts)
    assert signal is None


def test_high_churn_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    for i in range(7):
        repo.commit(f"tweak {i}", {"hot.py": str(i)}, when=days_ago(80 - i * 5))
    facts = _facts_for(repo, "hot.py")
    signal = sig.detect_high_churn(facts)
    assert signal is not None


def test_ghost_keeper_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Ghost author committed > GHOST_KEEPER_DAYS ago, never since.
    repo.commit(
        "init", {"legacy.py": "1"},
        when=days_ago(800),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    repo.commit(
        "tweak", {"legacy.py": "2"},
        when=days_ago(750),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    facts = _facts_for(repo, "legacy.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    assert signal.severity >= 3


def test_ghost_keeper_uses_line_ownership_to_promote_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Ghost wrote almost all the lines; severity should reflect dominant ownership.
    body = "\n".join(f"line {i}" for i in range(20)) + "\n"
    repo.commit(
        "feat: big initial commit", {"core.py": body},
        when=days_ago(900),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    # One small drive-by commit by an active author shouldn't take ownership away.
    repo.commit(
        "fix: typo", {"core.py": body + "# tiny comment\n"},
        when=days_ago(5),
        author_name="Active Bob",
        author_email="bob@example.com",
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    # Old Owner still owns ~20 of 21 lines → ghost keeper still fires.
    assert "ghost@example.com" not in signal.detail  # email should NOT leak; name should
    assert "Old Owner" in signal.detail
    assert "lines" in signal.detail.lower()


def test_ghost_keeper_silent_when_line_owner_still_active(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Old contributor wrote lines and then left; but a CURRENT author rewrote them.
    repo.commit(
        "feat: original", {"x.py": "old line\n"},
        when=days_ago(900),
        author_name="Old",
        author_email="old@example.com",
    )
    # Active author rewrites the whole file recently.
    new_body = "\n".join(f"new line {i}" for i in range(15)) + "\n"
    repo.commit(
        "refactor: rewrite", {"x.py": new_body},
        when=days_ago(10),
        author_name="Active",
        author_email="active@example.com",
    )
    facts = _facts_for(repo, "x.py")
    # Although `Old` has more time-since-activity, they don't own current lines.
    # The active author owns them. So no ghost keeper.
    signal = sig.detect_ghost_keeper(facts)
    assert signal is None


def test_newborn_suppressed_when_other_signals_fire(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"y.py": "1"},
        body="Do not switch to async.",
        when=days_ago(2),
    )
    facts = _facts_for(repo, "y.py")
    signals = sig.all_signals(facts)
    kinds = {s.kind for s in signals}
    assert sig.SignalKind.INVARIANT_QUOTE in kinds
    assert sig.SignalKind.NEWBORN not in kinds


def test_invariant_subject_alone_does_not_match(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A commit subject mentioning 'invariant' is meta, not a stated invariant."""
    repo.commit(
        "fix: tighten invariant matcher",
        {"z.py": "1"},
        body="Just a refactor — no behaviour change.",
        when=days_ago(2),
    )
    facts = _facts_for(repo, "z.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is None


def test_first_sentence_does_not_split_at_short_prefix() -> None:
    # "1." should NOT be treated as a sentence break — too short to be useful.
    out = sig._first_sentence("1. The first item is the one to remember.", 80)
    assert out.startswith("1.")
    assert "first item" in out


def test_first_sentence_splits_at_real_boundary() -> None:
    out = sig._first_sentence(
        "Important: do not call this from threads. There is more text after.",
        120,
    )
    assert out == "Important: do not call this from threads."
    assert "more text" not in out


def test_invariant_signal_caps_at_three_bullets(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Five invariants across five commits. The per-commit cap of two only
    # applies within a single body, so each commit contributes one phrase.
    # Tokens used: "do not", "important:", "danger:", "workaround", "tradeoff" —
    # the loud lowercase "warning:" / "note:" tokens are deliberately excluded
    # because real OSS history has lint-paste lines like "the warning:" that
    # self-flag as bogus invariants.
    bodies = [
        "Do not switch to async — v1 clients break.",
        "Important: keep the legacy header in place.",
        "Danger: idempotency token must be unique.",
        "Workaround for upstream bug #5: noop when input is empty.",
        "Tradeoff: we accept ~10ms latency to keep correctness.",
    ]
    for i, body in enumerate(bodies):
        repo.commit(
            f"compat: constraint {i}", {"x.py": str(i)}, body=body, when=days_ago(20 + i)
        )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert "5 invariants stated" in signal.headline
    # Detail shows 3 bullets + a "...and 2 more" line.
    bullets = [line for line in signal.detail.splitlines() if line.strip().startswith("> ")]
    assert len(bullets) == 4
    assert "and 2 more" in bullets[-1]


def test_signals_sorted_by_severity_desc(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feature", {"x.py": "1"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: corner case",
        {"x.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "x.py")
    signals = sig.all_signals(facts)
    severities = [s.severity for s in signals]
    assert severities == sorted(severities, reverse=True)


# ----- Explanation: each detector names the rule branch that fired ---------


def test_revert_chain_explanation_names_default_revert_message(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "revert_pair_default_message"
    assert "find_revert_pairs" in signal.explanation.source_ref
    assert signal.explanation.evidence  # at least one short SHA


def test_incident_explanation_names_subject_keyword_branch(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"refund.py": "1"}, when=days_ago(120))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "refund.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "incident_subject_keyword"
    assert "hotfix" in signal.explanation.evidence
    assert "find_incidents" in signal.explanation.source_ref


def test_incident_explanation_names_breaking_cc_branch(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.commit(
        "feat!: drop legacy refund path",
        {"a.py": "2"},
        body="Backwards-incompatible change.",
        when=days_ago(20),
    )
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "incident_subject_conventional_commits_breaking"


def test_incident_explanation_names_breaking_footer_branch(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.commit(
        "feat: refactor refund flow",
        {"a.py": "2"},
        body="Body explains the change.\n\nBREAKING CHANGE: removed sync API.",
        when=days_ago(20),
    )
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "incident_body_breaking_change_footer"


def test_incident_explanation_names_body_keyword_with_issue_id(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=days_ago(60))
    repo.commit(
        "fix: tighten input validation",
        {"a.py": "2"},
        body="Resolves outage observed in #INC-99.",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "incident_body_keyword_with_issue_id"
    # The classifier should record both the keyword and the issue id.
    joined = " ".join(signal.explanation.evidence)
    assert "outage" in joined
    assert "INC-99" in joined


def test_high_churn_explanation_names_threshold(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    for i in range(7):
        repo.commit(f"tweak {i}", {"hot.py": str(i)}, when=days_ago(80 - i * 5))
    facts = _facts_for(repo, "hot.py")
    signal = sig.detect_high_churn(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "high_churn_recent_window"
    joined = " ".join(signal.explanation.evidence)
    assert "recent_commits=" in joined
    assert "threshold=" in joined


def test_coupling_explanation_names_top_co_changer(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    repo.commit("change", {"a.py": "2", "b.py": "2"}, when=days_ago(50))
    repo.commit("change", {"a.py": "3", "b.py": "3"}, when=days_ago(40))
    repo.commit("change", {"a.py": "4", "b.py": "4"}, when=days_ago(30))
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_coupling(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "coupling_co_change_threshold"
    joined = " ".join(signal.explanation.evidence)
    assert "b.py" in joined
    assert "threshold=" in joined


def test_silence_explanation_names_threshold(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"old.py": "1"}, when=days_ago(400))
    facts = _facts_for(repo, "old.py")
    signal = sig.detect_silence(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "silence_untouched_threshold"
    joined = " ".join(signal.explanation.evidence)
    assert "threshold=" in joined


def test_newborn_explanation_names_window(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"new.py": "1"}, when=days_ago(2))
    facts = _facts_for(repo, "new.py")
    signal = sig.detect_newborn(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "newborn_first_commit_window"


def test_invariant_quotes_explanation_records_matched_token(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule == "invariant_token_match_in_body"
    joined = " ".join(signal.explanation.evidence).lower()
    assert "do not" in joined


def test_ghost_keeper_explanation_names_inactive_owner(
    repo, days_ago,
) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init", {"legacy.py": "1"},
        when=days_ago(800),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    facts = _facts_for(repo, "legacy.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    assert signal.explanation is not None
    assert signal.explanation.rule.startswith("ghost_keeper_")
    joined = " ".join(signal.explanation.evidence)
    assert "days_since_active=" in joined


# ---------------------------------------------------------------------------
# next_step — each detector populates a concrete action a careful reader
# would take given the signal. Locks down the wording per kind so a future
# refactor doesn't accidentally lose the actionable hint.
# ---------------------------------------------------------------------------


def test_revert_chain_next_step_names_both_sides(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert "Read both sides" in signal.next_step
    assert "git show" in signal.next_step
    assert sha[:7] in signal.next_step


def test_incident_history_next_step_names_most_recent(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("feat: refunds", {"refund.py": "1"}, when=days_ago(120))
    sha = repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "refund.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert sha[:7] in signal.next_step
    assert "incident-flavoured" in signal.next_step


def test_invariant_quote_next_step_restates_the_constraint(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert signal.next_step.startswith("Honour the invariant:")
    assert "Do not switch to async" in signal.next_step


def test_coupling_next_step_lists_top_coupled_paths(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    repo.commit("change", {"a.py": "2", "b.py": "2"}, when=days_ago(50))
    repo.commit("change", {"a.py": "3", "b.py": "3"}, when=days_ago(40))
    repo.commit("change", {"a.py": "4", "b.py": "4"}, when=days_ago(30))
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_coupling(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert "tend to change together" in signal.next_step
    assert "b.py" in signal.next_step


def test_silence_next_step_recommends_running_tests(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"old.py": "1"}, when=days_ago(400))
    facts = _facts_for(repo, "old.py")
    signal = sig.detect_silence(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert "Verify it's still exercised" in signal.next_step
    assert "tests" in signal.next_step.lower()


def test_high_churn_next_step_includes_path(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    for i in range(7):
        repo.commit(f"tweak {i}", {"hot.py": str(i)}, when=days_ago(80 - i * 5))
    facts = _facts_for(repo, "hot.py")
    signal = sig.detect_high_churn(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert "git log -p" in signal.next_step
    assert "hot.py" in signal.next_step


def test_ghost_keeper_next_step_names_path_and_email(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "init",
        {"legacy.py": "1"},
        when=days_ago(800),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    repo.commit(
        "tweak",
        {"legacy.py": "2"},
        when=days_ago(750),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    facts = _facts_for(repo, "legacy.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    assert signal.next_step is not None
    assert "Surface the change to your team" in signal.next_step
    assert "legacy.py" in signal.next_step
    assert "ghost@example.com" in signal.next_step


def test_newborn_has_no_next_step(repo, now) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1"}, when=now)
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_newborn(facts)
    assert signal is not None
    # Brief: not enough history to recommend anything.
    assert signal.next_step is None
