"""Tests for ``whycode.story``: chapter list builder + renderers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tests.conftest import RepoBuilder
from whycode import git_facts as gf
from whycode import story as st


def _facts_for(repo: RepoBuilder, path: str) -> gf.RepoFacts:
    return gf.gather(repo.root, path)


def test_build_story_orders_newest_first(repo: RepoBuilder, days_ago: Callable[[int], datetime]) -> None:
    """The chapter list must walk the file's history newest-first; consumers
    rely on ``story.chapters[0]`` being "the latest beat" for summary text."""
    repo.commit(
        "hotfix: oldest incident",
        {"refund.py": "1"},
        body="See #INC-1",
        when=days_ago(40),
    )
    repo.commit(
        "Revert \"hotfix: oldest incident\"",
        {"refund.py": "2"},
        body="This reverts commit deadbeef.",
        when=days_ago(30),
    )
    repo.commit(
        "hotfix: newest incident",
        {"refund.py": "3"},
        body="See #INC-2",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "refund.py")
    story = st.build_story(facts)
    assert story.chapters_returned >= 2
    # First chapter must be the newest commit.
    first = story.chapters[0]
    assert first.subject == "hotfix: newest incident"
    # Each chapter's authored_at must be ≥ the one after it.
    timestamps = [c.authored_at for c in story.chapters if c.authored_at is not None]
    assert timestamps == sorted(timestamps, reverse=True)


def test_build_story_drops_edit_role_by_default(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """Routine ``edit`` commits — no incident, no revert, no SHA reference,
    no invariant — must be excluded from the chapter list by default."""
    repo.commit(
        "tweak: rename helper",
        {"a.py": "1"},
        body="No constraint stated; routine refactor.",
        when=days_ago(20),
    )
    repo.commit(
        "hotfix: pager went off",
        {"a.py": "2"},
        body="See #INC-7",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts)
    roles = [c.role for c in story.chapters]
    # "edit" must not appear among kept (non-collapse) chapters.
    for c in story.chapters:
        if c.is_collapse:
            continue
        assert c.role != "edit", f"unexpected edit chapter: {c.subject!r}"
    assert "incident" in roles


def test_build_story_keeps_edit_when_all_chapters_true(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """``all_chapters=True`` is the escape hatch: every commit becomes a
    chapter regardless of its role classification."""
    repo.commit(
        "tweak: rename helper",
        {"a.py": "1"},
        body="No constraint stated.",
        when=days_ago(20),
    )
    repo.commit(
        "tweak: another internal rename",
        {"a.py": "2"},
        body="No constraint stated.",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts, all_chapters=True)
    # No collapse markers either — every commit shows.
    non_collapse = [c for c in story.chapters if not c.is_collapse]
    assert len(non_collapse) == 2
    assert all(c.role == "edit" for c in non_collapse)


def test_build_story_collapses_runs_of_three_or_more_same_role_same_author_edits(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """A run of ≥3 same-role same-author dropped edits between two surfacing
    chapters folds into a single ``is_collapse=True`` marker. With only edits
    in the history the run still folds."""
    # Bracket the run with two real beats so the collapse sits between them.
    repo.commit(
        "hotfix: prior outage",
        {"a.py": "0"},
        body="See #INC-1",
        when=days_ago(60),
    )
    for i, n in enumerate([50, 45, 40, 35]):
        repo.commit(
            f"tweak: refactor pass {i}",
            {"a.py": str(i + 1)},
            body="No constraint stated.",
            when=days_ago(n),
        )
    repo.commit(
        "hotfix: newer outage",
        {"a.py": "5"},
        body="See #INC-2",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts)
    collapses = [c for c in story.chapters if c.is_collapse]
    assert len(collapses) == 1
    assert collapses[0].collapse_count == 4
    assert collapses[0].role == "edit"


def test_build_story_no_collapse_flag_disables_folding(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """With ``no_collapse=True`` the dropped edits stay dropped and no
    collapse markers are emitted at all."""
    repo.commit(
        "hotfix: prior outage",
        {"a.py": "0"},
        body="See #INC-1",
        when=days_ago(60),
    )
    for i, n in enumerate([50, 45, 40, 35]):
        repo.commit(
            f"tweak: refactor pass {i}",
            {"a.py": str(i + 1)},
            body="No constraint stated.",
            when=days_ago(n),
        )
    repo.commit(
        "hotfix: newer outage",
        {"a.py": "5"},
        body="See #INC-2",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts, no_collapse=True)
    assert all(not c.is_collapse for c in story.chapters)


def test_build_story_roles_allowlist_overrides_default_edit_drop(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """A roles allowlist is the explicit "give me only these" filter — when
    it includes ``edit``, edit chapters survive the default-drop pass."""
    repo.commit(
        "hotfix: regression",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(20),
    )
    repo.commit(
        "tweak: rename internals",
        {"a.py": "2"},
        body="No constraint stated.",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts, roles=("edit",))
    # Only edit chapters; the incident is filtered OUT because it wasn't asked for.
    roles = {c.role for c in story.chapters if not c.is_collapse}
    assert roles == {"edit"}


def test_build_story_since_filter_drops_older_chapters(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """A ``since`` cutoff drops every commit older than that timestamp,
    even when the role would otherwise be kept."""
    repo.commit(
        "hotfix: ancient incident",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(100),
    )
    repo.commit(
        "hotfix: recent incident",
        {"a.py": "2"},
        body="See #INC-2",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "a.py")
    cutoff = days_ago(50)
    story = st.build_story(facts, since=cutoff)
    subjects = [c.subject for c in story.chapters if not c.is_collapse]
    assert "hotfix: recent incident" in subjects
    assert "hotfix: ancient incident" not in subjects


def test_build_story_truncates_body_to_max_chars(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """Bodies longer than ``body_max_chars`` truncate with an ellipsis and
    set ``body_truncated=True``; the full character count is preserved."""
    long_body = "x" * 1500
    repo.commit(
        "hotfix: long body",
        {"a.py": "1"},
        body=f"See #INC-1\n\n{long_body}",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts, body_max_chars=200)
    chapter = story.chapters[0]
    assert chapter.body_truncated is True
    assert chapter.body_excerpt is not None
    assert len(chapter.body_excerpt) <= 201  # +1 for the "…" marker
    assert chapter.body_full_chars > 200


def test_build_story_populates_cited_prior_indices_for_reconciliation(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """A reconciliation chapter (body cites a prior known SHA) carries a
    ``cited_prior_indices`` tuple naming the chapter index of the cited
    commit in the FINAL chapter list."""
    sha_a = repo.commit(
        "feat: initial dispatch",
        {"a.py": "1"},
        body="No constraint stated.",
        when=days_ago(40),
    )
    repo.commit(
        "hotfix: keep dispatch alive",
        {"a.py": "2"},
        body="See #INC-1",
        when=days_ago(30),
    )
    repo.commit(
        "feat: reconcile dispatch",
        {"a.py": "3"},
        body=f"This rethinks {sha_a[:10]} after benchmarking.",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts, all_chapters=True)
    # The newest chapter is the reconciliation.
    recon = story.chapters[0]
    assert recon.role == "reconciliation"
    assert recon.cited_prior_shas
    assert recon.cited_prior_shas[0] == sha_a
    # The cited chapter must be present in the chapter list at the resolved index.
    assert recon.cited_prior_indices, "reconciliation must resolve to a chapter index"
    cited_idx = recon.cited_prior_indices[0]
    assert story.chapters[cited_idx].sha == sha_a


def test_to_dict_emits_documented_top_level_keys(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """The documented JSON schema lists exactly these top-level keys; the
    dict-emission contract is public API for the MCP tool and the CLI."""
    repo.commit(
        "hotfix: regression",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts)
    payload = st.to_dict(story)
    expected = {
        "path",
        "commit_count",
        "chapters_total",
        "chapters_returned",
        "chapters",
        "primary_author",
        "summary",
    }
    assert expected.issubset(payload.keys())
    # Per-chapter shape sanity: index, sha, role, body fields all present.
    assert isinstance(payload["chapters"], list)
    if payload["chapters"]:
        first = payload["chapters"][0]
        for key in (
            "index",
            "sha",
            "authored_at",
            "author",
            "subject",
            "role",
            "body_excerpt",
            "body_truncated",
            "body_full_chars",
            "cited_prior_shas",
            "cited_prior_indices",
            "invariant_lines",
            "files_changed",
            "is_collapse",
            "collapse_count",
        ):
            assert key in first, f"missing per-chapter key {key!r}"


def test_to_dict_chapters_returned_excludes_collapse_markers(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """``chapters_returned`` describes real chapters surfaced to the consumer,
    not the raw array length. Synthetic collapse markers (folded edit runs)
    are surfaced separately as ``collapse_markers`` so an allocator gets the
    right slot count by reading ``chapters_returned`` alone."""
    repo.commit(
        "hotfix: prior outage",
        {"a.py": "0"},
        body="See #INC-1",
        when=days_ago(60),
    )
    for i, n in enumerate([50, 45, 40, 35]):
        repo.commit(
            f"tweak: refactor pass {i}",
            {"a.py": str(i + 1)},
            body="No constraint stated.",
            when=days_ago(n),
        )
    repo.commit(
        "hotfix: newer outage",
        {"a.py": "5"},
        body="See #INC-2",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts)
    payload = st.to_dict(story)
    # Guard: this test is only meaningful when a collapse marker actually
    # appears, so flag a regression in collapse emission rather than letting
    # the assertion below trivially pass.
    assert any(c.get("is_collapse") for c in payload["chapters"])
    real = sum(1 for c in payload["chapters"] if not c.get("is_collapse"))
    assert payload["chapters_returned"] == real
    assert payload["chapters_returned"] == payload["chapters_total"]
    assert payload["collapse_markers"] == sum(
        1 for c in payload["chapters"] if c.get("is_collapse")
    )


def test_render_markdown_emits_role_headings(
    repo: RepoBuilder, days_ago: Callable[[int], datetime]
) -> None:
    """The markdown rendering uses ``## [role] sha · date · author`` headings
    so a PR-comment reader can scan the role tags by eye."""
    repo.commit(
        "hotfix: outage",
        {"a.py": "1"},
        body="See #INC-1",
        when=days_ago(5),
    )
    facts = _facts_for(repo, "a.py")
    story = st.build_story(facts)
    md = st.render_markdown(story)
    assert "# story · a.py" in md
    assert "## [incident]" in md
    assert "hotfix: outage" in md
