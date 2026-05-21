"""Layer 1 cache: persist git-derived facts in a per-repo SQLite database.

Why a cache exists at all
-------------------------
``whycode scan``, ``highlights`` and friends drive the same handful of
``git log`` invocations on every run. On a 3000-commit repo each run
re-parses the same output; on a 50000-commit repo it scales to minutes.
The repo's history is append-only, so the obvious win is to remember
what we've already parsed and only ask git for what's new.

Design notes
------------
- One database per repository, at ``<repo>/.whycode/cache.db``.
- Schema is intentionally tiny and hand-editable. ``schema_version`` lives
  in ``meta`` so we can grow it without breaking existing caches.
- Invalidation key is ``git rev-parse HEAD``.  When unchanged, every read
  is served from SQLite.  When changed, we ask git only for commits in
  ``<last_head>..HEAD`` and append them; if ``last_head`` is unreachable
  (force-push, branch switch) we fall back to a full rebuild.
- The cache is local-only: never uploaded, never queried by the network.
  ``.whycode/`` is gitignored by default, the same as every other on-disk
  state this project keeps.

Schema versioning
-----------------
When ``SCHEMA_VERSION`` is bumped, ``_initialise`` drops every cache table
and rebuilds the schema from scratch. The cache is a derived artefact —
re-deriving it on the next run is never destructive, so a one-shot
drop-and-recreate is preferable to per-version migration code that has
to stay correct across every bump.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2
CACHE_DIRNAME = ".whycode"
CACHE_FILENAME = "cache.db"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    sha          TEXT PRIMARY KEY,
    author_name  TEXT NOT NULL,
    author_email TEXT NOT NULL,
    authored_at  TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_files (
    sha        TEXT NOT NULL,
    path       TEXT NOT NULL,
    insertions INTEGER NOT NULL,
    deletions  INTEGER NOT NULL,
    PRIMARY KEY (sha, path)
);

-- Per-path rename-resolved sha lists, populated by the first git log --follow
-- call for that path. The (path, head_sha) pair is the validity key: if the
-- HEAD a row was resolved against differs from the current HEAD, the row is
-- ignored. Position preserves git's newest-first ordering.
CREATE TABLE IF NOT EXISTS path_log (
    path     TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    position INTEGER NOT NULL,
    sha      TEXT NOT NULL,
    PRIMARY KEY (path, head_sha, position)
);

-- Per-path blame ownership: author_email -> line count, scoped to a HEAD.
-- Cached so the ghost-keeper detector does not re-shell-out to git blame
-- on every scan of the same repo.
CREATE TABLE IF NOT EXISTS line_ownership (
    path         TEXT NOT NULL,
    head_sha     TEXT NOT NULL,
    author_email TEXT NOT NULL,
    line_count   INTEGER NOT NULL,
    PRIMARY KEY (path, head_sha, author_email)
);

-- Per-(path, head_sha) hunk records, parsed from ``git log --follow -p
-- --unified=0``. Same validity key as ``path_log``: a row is only valid
-- when its head_sha matches the cache's recorded HEAD. ``rowid`` order
-- preserves the newest-first commit order the parser emits.
CREATE TABLE IF NOT EXISTS commit_hunks (
    path      TEXT NOT NULL,
    head_sha  TEXT NOT NULL,
    sha       TEXT NOT NULL,
    new_start INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    old_start INTEGER NOT NULL,
    old_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(path);
CREATE INDEX IF NOT EXISTS idx_commits_authored_at ON commits(authored_at);
CREATE INDEX IF NOT EXISTS idx_path_log_path_head ON path_log(path, head_sha);
CREATE INDEX IF NOT EXISTS idx_commit_hunks_path_head
    ON commit_hunks(path, head_sha);
"""


@dataclass(frozen=True)
class CacheStats:
    """Summary returned by ``whycode cache stats`` and friends."""

    path: Path
    exists: bool
    schema_version: int | None
    head_sha: str | None
    commit_count: int
    file_row_count: int
    size_bytes: int


class CacheStore:
    """A thin wrapper over a per-repo SQLite database.

    The store is responsible only for read/write of cached commits and the
    head-pointer used to drive incremental updates. Higher layers
    (``git_facts``) decide *what* to cache and how to fall back when the
    cache misses; this class never invokes ``git`` itself.
    """

    def __init__(self, db_path: Path, *, in_memory: bool = False) -> None:
        """Open (creating if needed) the SQLite cache at ``db_path``.

        ``in_memory=True`` opens a transient ``:memory:`` connection
        instead — the disk file is never created and is never read.
        Used by ``--no-cache`` to retain in-session amortisation
        (matches the cold-fill code path) without persisting anything.
        """
        self.db_path = db_path
        self._in_memory = in_memory
        if in_memory:
            self._conn = sqlite3.connect(":memory:")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
        # row_factory makes column access readable in tests / debug.
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialise()

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CacheStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- schema -----------------------------------------------------------

    def _initialise(self) -> None:
        # Probe the meta marker before creating any tables. On a stale cache
        # (older SCHEMA_VERSION) we drop the whole schema first so a newly
        # added column on an existing table is not silently skipped by
        # ``CREATE TABLE IF NOT EXISTS``.
        existing: str | None = None
        try:
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
            if row is not None:
                existing = str(row["value"])
        except sqlite3.OperationalError:
            # ``meta`` doesn't exist yet — fresh database, normal install path.
            existing = None
        if existing is not None and int(existing) != SCHEMA_VERSION:
            # Drop every cache table and rebuild from scratch. The cache is
            # a derived artefact — losing it is never destructive, and a
            # per-version migration ladder would have to stay correct across
            # every future bump.
            self._conn.executescript(
                "DROP TABLE IF EXISTS commit_hunks;"
                "DROP TABLE IF EXISTS line_ownership;"
                "DROP TABLE IF EXISTS path_log;"
                "DROP TABLE IF EXISTS commit_files;"
                "DROP TABLE IF EXISTS commits;"
                "DROP TABLE IF EXISTS meta;"
            )
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)
            if self._get_meta("schema_version") is None:
                self._set_meta("schema_version", str(SCHEMA_VERSION))

    # ---- meta -------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @property
    def head_sha(self) -> str | None:
        return self._get_meta("head_sha")

    def set_head_sha(self, sha: str) -> None:
        self._set_meta("head_sha", sha)

    @property
    def schema_version(self) -> int:
        v = self._get_meta("schema_version")
        return int(v) if v is not None else 0

    # ---- writes -----------------------------------------------------------

    def upsert_commits(self, rows: Iterable[tuple[str, str, str, str, str, str]]) -> None:
        """Bulk-upsert commit rows.

        Each row is ``(sha, author_name, author_email, authored_at, subject, body)``.
        ``authored_at`` is stored as the ISO-8601 string git produces — we never
        re-parse it on the way in, only on the way out.
        """
        with self._conn:
            self._conn.executemany(
                "INSERT INTO commits(sha, author_name, author_email, authored_at, subject, body) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sha) DO UPDATE SET "
                "author_name = excluded.author_name, "
                "author_email = excluded.author_email, "
                "authored_at = excluded.authored_at, "
                "subject = excluded.subject, "
                "body = excluded.body",
                rows,
            )

    def upsert_commit_files(self, rows: Iterable[tuple[str, str, int, int]]) -> None:
        """Bulk-upsert per-file diffstat rows.

        Each row is ``(sha, path, insertions, deletions)``. We treat the
        diffstat for a given commit as the source of truth: re-uploading
        for an existing sha wholly replaces the previous rows for that sha.
        """
        rows = list(rows)
        if not rows:
            return
        seen_shas = {sha for sha, _, _, _ in rows}
        with self._conn:
            self._conn.executemany(
                "DELETE FROM commit_files WHERE sha = ?",
                [(s,) for s in seen_shas],
            )
            self._conn.executemany(
                "INSERT INTO commit_files(sha, path, insertions, deletions) "
                "VALUES(?, ?, ?, ?)",
                rows,
            )

    def has_commit_files(self, sha: str) -> bool:
        """True iff ``commit_files`` already has rows for ``sha``."""
        cur = self._conn.execute(
            "SELECT 1 FROM commit_files WHERE sha = ? LIMIT 1", (sha,)
        )
        return cur.fetchone() is not None

    def clear(self) -> None:
        """Wipe every row, keeping the schema in place."""
        with self._conn:
            self._conn.execute("DELETE FROM commit_files")
            self._conn.execute("DELETE FROM commits")
            self._conn.execute("DELETE FROM path_log")
            self._conn.execute("DELETE FROM line_ownership")
            self._conn.execute("DELETE FROM commit_hunks")
            self._conn.execute("DELETE FROM meta WHERE key != 'schema_version'")

    # ---- path log (rename-resolved per-path SHA list) --------------------

    def fetch_path_log(self, path: str, head_sha: str) -> list[str] | None:
        """Return the cached rename-resolved sha list for ``path`` at ``head_sha``.

        Returns ``None`` if no row was stored at that HEAD; an empty list
        means we know there is no history (e.g. the path was never touched).
        """
        cur = self._conn.execute(
            "SELECT sha FROM path_log WHERE path = ? AND head_sha = ? "
            "ORDER BY position ASC",
            (path, head_sha),
        )
        rows = cur.fetchall()
        if not rows:
            # Distinguish "absent" from "empty": check the meta marker.
            sentinel = self._get_meta(f"path_log_known:{head_sha}:{path}")
            if sentinel == "1":
                return []
            return None
        return [str(r["sha"]) for r in rows]

    def store_path_log(self, path: str, head_sha: str, shas: Sequence[str]) -> None:
        """Persist the rename-resolved sha list for ``path`` at ``head_sha``."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM path_log WHERE path = ? AND head_sha = ?",
                (path, head_sha),
            )
            self._conn.executemany(
                "INSERT INTO path_log(path, head_sha, position, sha) "
                "VALUES(?, ?, ?, ?)",
                [(path, head_sha, i, sha) for i, sha in enumerate(shas)],
            )
        # Mark the (path, head) as resolved even if shas is empty.
        self._set_meta(f"path_log_known:{head_sha}:{path}", "1")

    def fetch_line_ownership(
        self, path: str, head_sha: str
    ) -> dict[str, int] | None:
        """Return cached line-ownership counts for ``path`` at ``head_sha``."""
        cur = self._conn.execute(
            "SELECT author_email, line_count FROM line_ownership "
            "WHERE path = ? AND head_sha = ?",
            (path, head_sha),
        )
        rows = cur.fetchall()
        if not rows:
            sentinel = self._get_meta(f"blame_known:{head_sha}:{path}")
            if sentinel == "1":
                return {}
            return None
        return {str(r["author_email"]): int(r["line_count"]) for r in rows}

    def store_line_ownership(
        self, path: str, head_sha: str, counts: dict[str, int]
    ) -> None:
        """Persist line-ownership counts for ``path`` at ``head_sha``."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM line_ownership WHERE path = ? AND head_sha = ?",
                (path, head_sha),
            )
            self._conn.executemany(
                "INSERT INTO line_ownership(path, head_sha, author_email, line_count) "
                "VALUES(?, ?, ?, ?)",
                [(path, head_sha, email, count) for email, count in counts.items()],
            )
        self._set_meta(f"blame_known:{head_sha}:{path}", "1")

    # ---- per-path hunk records (sub-file hotspots) ------------------------

    def fetch_hunks(
        self, path: str, head_sha: str
    ) -> list[tuple[str, int, int, int, int]] | None:
        """Return the cached hunk rows for ``path`` at ``head_sha``.

        Each row is ``(sha, new_start, new_count, old_start, old_count)`` in
        the order originally stored (newest-first commits, hunks within a
        commit in git's emission order). Returns ``None`` when no row has
        been stored for that ``(path, head_sha)`` pair; an empty list means
        the parse ran and produced no hunks (e.g. binary file).
        """
        cur = self._conn.execute(
            "SELECT sha, new_start, new_count, old_start, old_count "
            "FROM commit_hunks WHERE path = ? AND head_sha = ? "
            "ORDER BY rowid ASC",
            (path, head_sha),
        )
        rows = cur.fetchall()
        if not rows:
            sentinel = self._get_meta(f"hunks_known:{head_sha}:{path}")
            if sentinel == "1":
                return []
            return None
        return [
            (
                str(r["sha"]),
                int(r["new_start"]),
                int(r["new_count"]),
                int(r["old_start"]),
                int(r["old_count"]),
            )
            for r in rows
        ]

    def store_hunks(
        self,
        path: str,
        head_sha: str,
        hunks: Sequence[tuple[str, int, int, int, int]],
    ) -> None:
        """Persist hunk rows for ``path`` at ``head_sha``.

        Each ``hunks`` entry is ``(sha, new_start, new_count, old_start,
        old_count)``. Stored as-is so the caller controls ordering — a
        replay of the fetch must hand back commits newest-first to keep
        the band-clustering decay arithmetic stable.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM commit_hunks WHERE path = ? AND head_sha = ?",
                (path, head_sha),
            )
            self._conn.executemany(
                "INSERT INTO commit_hunks"
                "(path, head_sha, sha, new_start, new_count, old_start, old_count) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path,
                        head_sha,
                        sha,
                        new_start,
                        new_count,
                        old_start,
                        old_count,
                    )
                    for (sha, new_start, new_count, old_start, old_count) in hunks
                ],
            )
        self._set_meta(f"hunks_known:{head_sha}:{path}", "1")

    # ---- reads ------------------------------------------------------------

    def has_commit(self, sha: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM commits WHERE sha = ? LIMIT 1", (sha,))
        return cur.fetchone() is not None

    def fetch_all_commit_rows(self) -> list[sqlite3.Row]:
        """Return every cached commit row, newest first by authored_at."""
        cur = self._conn.execute(
            "SELECT sha, author_name, author_email, authored_at, subject, body "
            "FROM commits ORDER BY authored_at DESC"
        )
        return cur.fetchall()

    def fetch_commits_for_path(self, path: str) -> list[sqlite3.Row]:
        """Return every cached commit that touches ``path`` (newest first).

        Note: the cache stores diffstat rows under the path that git emitted
        at the time. Rename history that ``git log --follow`` would expand is
        NOT pre-resolved here — callers that need rename-following must fall
        back to git when the cache returns nothing.
        """
        cur = self._conn.execute(
            "SELECT c.sha, c.author_name, c.author_email, c.authored_at, c.subject, c.body "
            "FROM commits c "
            "JOIN commit_files f ON f.sha = c.sha "
            "WHERE f.path = ? "
            "ORDER BY c.authored_at DESC",
            (path,),
        )
        return cur.fetchall()

    def fetch_co_changes(self, shas: Sequence[str], target_path: str) -> Counter[str]:
        """Count co-changed files for a fixed list of commit SHAs.

        Replaces the ``git log --no-walk --numstat`` pass when every input
        sha is already cached. Callers that detect a miss must fall back.
        """
        if not shas:
            return Counter()
        # SQLite caps the number of host parameters in a single statement at
        # 999 by default. Chunk so we never exceed that, no matter how
        # large the file's history is.
        counter: Counter[str] = Counter()
        chunk_size = 500
        for i in range(0, len(shas), chunk_size):
            chunk = shas[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            sql = (
                f"SELECT path, COUNT(*) AS n FROM commit_files "
                f"WHERE sha IN ({placeholders}) AND path != ? "
                f"GROUP BY path"
            )
            params: list[str] = [*chunk, target_path]
            cur = self._conn.execute(sql, params)
            for row in cur.fetchall():
                counter[str(row["path"])] += int(row["n"])
        return counter

    def fetch_files_for_commit(self, sha: str) -> list[sqlite3.Row]:
        """Return diffstat rows for a single commit (used by ``files_changed_in``)."""
        cur = self._conn.execute(
            "SELECT sha, path, insertions, deletions "
            "FROM commit_files WHERE sha = ?",
            (sha,),
        )
        return cur.fetchall()

    def shas_missing_files(self, shas: Sequence[str]) -> list[str]:
        """Return the subset of ``shas`` for which we have no diffstat rows."""
        if not shas:
            return []
        present: set[str] = set()
        chunk_size = 500
        for i in range(0, len(shas), chunk_size):
            chunk = shas[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cur = self._conn.execute(
                f"SELECT DISTINCT sha FROM commit_files WHERE sha IN ({placeholders})",
                list(chunk),
            )
            present.update(str(row["sha"]) for row in cur.fetchall())
        return [s for s in shas if s not in present]

    # ---- stats ------------------------------------------------------------

    def stats(self) -> CacheStats:
        commit_count = int(
            self._conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        )
        file_row_count = int(
            self._conn.execute("SELECT COUNT(*) FROM commit_files").fetchone()[0]
        )
        if self._in_memory:
            size_bytes = 0
            exists = False
        else:
            try:
                size_bytes = self.db_path.stat().st_size
            except OSError:
                size_bytes = 0
            exists = self.db_path.exists()
        return CacheStats(
            path=self.db_path,
            exists=exists,
            schema_version=self.schema_version,
            head_sha=self.head_sha,
            commit_count=commit_count,
            file_row_count=file_row_count,
            size_bytes=size_bytes,
        )


# ---------- module-level helpers --------------------------------------------


def cache_path_for(repo_root: Path) -> Path:
    """Return the canonical cache file location for ``repo_root``."""
    return repo_root / CACHE_DIRNAME / CACHE_FILENAME


def open_for(repo_root: Path) -> CacheStore:
    """Open (creating if absent) the cache store for ``repo_root``."""
    return CacheStore(cache_path_for(repo_root))


def open_in_memory(repo_root: Path) -> CacheStore:
    """Open a transient in-memory cache for ``repo_root``.

    Used by ``--no-cache`` to keep within-session amortisation (the same
    cold-fill code path everything else uses) while never touching disk.
    The store is destroyed on ``close()`` and has no after-effects.
    """
    return CacheStore(cache_path_for(repo_root), in_memory=True)


def parse_authored_at(value: str) -> datetime:
    """Parse the ``authored_at`` string we stored from git.

    Centralised so callers don't each invent their own parsing dance —
    ``datetime.fromisoformat`` accepts what ``%aI`` emits.
    """
    return datetime.fromisoformat(value.strip())


def remove(repo_root: Path) -> bool:
    """Delete the cache file. Returns True if a file was removed."""
    p = cache_path_for(repo_root)
    if p.exists():
        p.unlink()
        # Try to drop the parent dir too, but only if we left it empty.
        with contextlib.suppress(OSError):
            p.parent.rmdir()
        return True
    return False



