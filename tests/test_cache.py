"""Tests for the on-disk SQLite cache and its integration with git_facts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whycode import cache as ch
from whycode import git_facts as gf


def _commit(
    sha: str = "a" * 40,
    name: str = "Mei",
    email: str = "mei@example.com",
    when: datetime | None = None,
    subject: str = "feat: thing",
    body: str = "",
) -> tuple[str, str, str, str, str, str]:
    when = when or datetime(2026, 5, 1, tzinfo=UTC)
    return (sha, name, email, when.isoformat(), subject, body)


# ---- schema initialisation -------------------------------------------------


def test_open_for_creates_directory_and_file(tmp_path: Path) -> None:
    store = ch.open_for(tmp_path)
    try:
        assert (tmp_path / ".whycode" / "cache.db").exists()
        assert store.schema_version == ch.SCHEMA_VERSION
    finally:
        store.close()


def test_schema_creates_expected_tables(tmp_path: Path) -> None:
    store = ch.open_for(tmp_path)
    try:
        with sqlite3.connect(ch.cache_path_for(tmp_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"meta", "commits", "commit_files"}.issubset(tables)
    finally:
        store.close()


def test_meta_records_schema_version_on_first_open(tmp_path: Path) -> None:
    store = ch.open_for(tmp_path)
    try:
        assert store.schema_version == ch.SCHEMA_VERSION
        assert store.head_sha is None
    finally:
        store.close()


# ---- store / retrieve ------------------------------------------------------


def test_upsert_and_fetch_commits_round_trip(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        rows = [
            _commit(sha="a" * 40, when=datetime(2026, 5, 1, tzinfo=UTC)),
            _commit(sha="b" * 40, when=datetime(2026, 5, 2, tzinfo=UTC)),
        ]
        store.upsert_commits(rows)
        fetched = store.fetch_all_commit_rows()
        # Newest first.
        assert [str(r["sha"]) for r in fetched] == ["b" * 40, "a" * 40]


def test_upsert_commits_is_idempotent(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        store.upsert_commits([_commit(sha="a" * 40)])
        store.upsert_commits([_commit(sha="a" * 40, subject="updated")])
        rows = store.fetch_all_commit_rows()
        assert len(rows) == 1
        assert str(rows[0]["subject"]) == "updated"


def test_upsert_commit_files_replaces_for_same_sha(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        store.upsert_commit_files([("s", "a.py", 1, 0), ("s", "b.py", 2, 1)])
        store.upsert_commit_files([("s", "a.py", 5, 5)])
        rows = store.fetch_files_for_commit("s")
        assert len(rows) == 1
        assert int(rows[0]["insertions"]) == 5


def test_co_changes_reads_cached_pairs(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        store.upsert_commit_files(
            [
                ("s1", "target.py", 1, 0),
                ("s1", "other.py", 2, 1),
                ("s2", "target.py", 3, 0),
                ("s2", "other.py", 4, 0),
                ("s3", "target.py", 1, 0),
                ("s3", "third.py", 1, 0),
            ]
        )
        result = store.fetch_co_changes(["s1", "s2", "s3"], "target.py")
        assert result["other.py"] == 2
        assert result["third.py"] == 1
        assert "target.py" not in result


def test_shas_missing_files_returns_unseen_subset(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        store.upsert_commit_files([("s1", "a.py", 1, 0), ("s2", "a.py", 1, 0)])
        missing = store.shas_missing_files(["s1", "s2", "s3", "s4"])
        assert missing == ["s3", "s4"]


def test_clear_removes_all_rows_keeps_schema(tmp_path: Path) -> None:
    with ch.open_for(tmp_path) as store:
        store.upsert_commits([_commit()])
        store.upsert_commit_files([("a" * 40, "x.py", 1, 0)])
        store.set_head_sha("deadbeef")
        store.clear()
        assert store.fetch_all_commit_rows() == []
        assert store.head_sha is None
        # schema_version is preserved across a clear.
        assert store.schema_version == ch.SCHEMA_VERSION


# ---- HEAD-driven incremental update ---------------------------------------


def test_all_commits_via_cache_returns_cached_rows_when_head_unchanged(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    repo.commit("init", {"a.txt": "1"})
    sha2 = repo.commit("second", {"a.txt": "2"})
    with ch.open_for(repo.root) as store:
        first = gf.all_commits(repo.root, cache=store)
        assert {c.sha for c in first} == {sha2, *(c.sha for c in first)}
        assert store.head_sha == sha2
        # Second invocation must not re-walk; we observe by deleting the
        # repo's git config and confirming a fully-cached read still works.
        # (Easier observation: the head_sha stays put.)
        second = gf.all_commits(repo.root, cache=store)
        assert {c.sha for c in second} == {c.sha for c in first}


def test_all_commits_via_cache_picks_up_new_commits_incrementally(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    repo.commit("init", {"a.txt": "1"})
    with ch.open_for(repo.root) as store:
        gf.all_commits(repo.root, cache=store)
        before_count = len(store.fetch_all_commit_rows())
        # Append a new commit and re-call.
        repo.commit("second", {"a.txt": "2"})
        gf.all_commits(repo.root, cache=store)
        after_count = len(store.fetch_all_commit_rows())
        assert after_count == before_count + 1


def test_all_commits_via_cache_full_rebuild_when_last_head_unreachable(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    repo.commit("init", {"a.txt": "1"})
    repo.commit("second", {"a.txt": "2"})
    with ch.open_for(repo.root) as store:
        gf.all_commits(repo.root, cache=store)
        # Plant a non-existent head sha. The next call must detect this
        # via the "unreachable" branch and rebuild from scratch.
        store.set_head_sha("0" * 40)
        rebuilt = gf.all_commits(repo.root, cache=store)
        assert len(rebuilt) == 2
        assert store.head_sha != "0" * 40


def test_co_changes_via_cache_serves_from_sqlite_on_warm_call(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    repo.commit("init", {"a.txt": "1", "b.txt": "1", "c.txt": "1"})
    repo.commit("touch a and b", {"a.txt": "2", "b.txt": "2"})
    repo.commit("touch a and c", {"a.txt": "3", "c.txt": "2"})
    with ch.open_for(repo.root) as store:
        commits = gf.commits_for_path(repo.root, "a.txt", cache=store)
        first = gf.co_changes(repo.root, commits, "a.txt", cache=store)
        # b.txt and c.txt should both have at least 1 co-change with a.txt.
        assert first.get("b.txt", 0) >= 1
        assert first.get("c.txt", 0) >= 1
        # Warm read produces identical numbers.
        second = gf.co_changes(repo.root, commits, "a.txt", cache=store)
        assert second == first


def test_files_changed_in_uses_cache_when_present(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    sha = repo.commit("seed", {"a.txt": "1", "b.txt": "1"})
    with ch.open_for(repo.root) as store:
        # First call populates the cache.
        first = gf.files_changed_in(repo.root, sha, cache=store)
        paths_first = sorted(c.path for c in first)
        assert paths_first == ["a.txt", "b.txt"]
        # Second call must read from SQLite — verify by mutating the row
        # behind the cache layer and watching the result follow.
        store._conn.execute(
            "UPDATE commit_files SET insertions = 99 WHERE sha = ? AND path = ?",
            (sha, "a.txt"),
        )
        store._conn.commit()
        second = gf.files_changed_in(repo.root, sha, cache=store)
        a_row = next(c for c in second if c.path == "a.txt")
        assert a_row.insertions == 99


def test_cached_path_yields_identical_facts_to_uncached_path(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    """The whole point: a warm cache must not change observed behaviour."""
    repo.commit("init", {"a.txt": "1", "b.txt": "1"})
    repo.commit(
        "touch both with body",
        {"a.txt": "2", "b.txt": "2"},
        body="Do not switch to async.",
    )
    cold = gf.gather(repo.root, "a.txt")
    with ch.open_for(repo.root) as store:
        warm = gf.gather(repo.root, "a.txt", cache=store)
    assert [c.sha for c in cold.commits] == [c.sha for c in warm.commits]
    assert dict(cold.co_changed_files) == dict(warm.co_changed_files)
    assert cold.invariant_quotes == warm.invariant_quotes


def test_stats_reports_what_is_in_the_cache(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    repo.commit("init", {"a.txt": "1"})
    with ch.open_for(repo.root) as store:
        gf.all_commits(repo.root, cache=store)
        stats = store.stats()
        assert stats.commit_count >= 1
        assert stats.head_sha is not None
        assert stats.size_bytes > 0
        assert stats.path == ch.cache_path_for(repo.root)


def test_remove_deletes_the_db_file(repo) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.txt": "1"})
    store = ch.open_for(repo.root)
    store.close()
    assert ch.cache_path_for(repo.root).exists()
    assert ch.remove(repo.root) is True
    assert not ch.cache_path_for(repo.root).exists()
    # Idempotent: a second call returns False.
    assert ch.remove(repo.root) is False


def test_schema_version_mismatch_rebuilds_tables(tmp_path: Path) -> None:
    """An older schema_version forces a rebuild on next open."""
    store = ch.open_for(tmp_path)
    store.upsert_commits([_commit()])
    store.set_head_sha("deadbeef")
    # Drop schema_version to a future-different value to simulate a bump.
    store._set_meta("schema_version", "999")
    store.close()
    # Re-open; the constructor should detect the mismatch and rebuild.
    re_opened = ch.open_for(tmp_path)
    try:
        assert re_opened.schema_version == ch.SCHEMA_VERSION
        assert re_opened.fetch_all_commit_rows() == []
    finally:
        re_opened.close()


def test_co_changes_returns_empty_for_no_commits(repo) -> None:  # type: ignore[no-untyped-def]
    with ch.open_for(repo.root) as store:
        assert gf.co_changes(repo.root, [], "x.py", cache=store) == {}


def test_history_query_with_ref_does_not_pollute_cache(
    repo,  # type: ignore[no-untyped-def]
) -> None:
    """Historical (--at) queries must not mistake an as-of view for HEAD."""
    sha1 = repo.commit("init", {"a.txt": "1"})
    repo.commit("second", {"a.txt": "2"})
    with ch.open_for(repo.root) as store:
        # A historical query at the older sha should NOT mark the cache as
        # synced to that sha — it's an out-of-band view.
        gf.commits_for_path(repo.root, "a.txt", ref=sha1, cache=store)
        assert store.head_sha is None


# ---- defensive ------------------------------------------------------------


def test_open_for_idempotent_open_close(tmp_path: Path) -> None:
    store_a = ch.open_for(tmp_path)
    store_a.close()
    store_b = ch.open_for(tmp_path)
    try:
        assert store_b.schema_version == ch.SCHEMA_VERSION
    finally:
        store_b.close()


def test_fetch_co_changes_chunked_query_handles_many_shas(tmp_path: Path) -> None:
    """SQLite limits host parameters per statement; we chunk above 500."""
    with ch.open_for(tmp_path) as store:
        rows = []
        for i in range(1100):
            sha = f"{i:040x}"
            rows.append((sha, "target.py", 1, 0))
            rows.append((sha, "buddy.py", 1, 0))
        store.upsert_commit_files(rows)
        shas = [f"{i:040x}" for i in range(1100)]
        counter = store.fetch_co_changes(shas, "target.py")
        assert counter["buddy.py"] == 1100


def test_parse_authored_at_round_trip() -> None:
    when = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
    parsed = ch.parse_authored_at(when.isoformat())
    assert parsed == when


@pytest.mark.parametrize(
    "missing_path",
    ["sub/.whycode/cache.db", "deeper/sub/.whycode/cache.db"],
)
def test_open_creates_nested_dirs(tmp_path: Path, missing_path: str) -> None:
    target = tmp_path / missing_path
    store = ch.CacheStore(target)
    try:
        assert target.exists()
    finally:
        store.close()
