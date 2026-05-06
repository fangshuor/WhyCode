"""Layer 1: deterministic git facts.

Pure git plumbing wrapped in safe Python. Never interprets, never guesses.
The output is the bedrock that Layer 2 builds on.

Design notes
------------
- We delimit log output with ASCII unit (0x1f) and record (0x1e) separators
  because they essentially never appear in commit messages or paths.
- We use ``--follow`` so file rename history is traced through.
- We never invoke a subcommand that mutates the repo.
- An optional :class:`whycode.cache.CacheStore` can be threaded through the
  read helpers; when present, repeat invocations skip the ``git log`` parse
  entirely as long as ``HEAD`` has not advanced.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whycode.cache import CacheStore

UNIT_SEP = "\x1f"
RECORD_SEP = "\x1e"

# A commit subject/body containing one of these markers is treated as evidence
# that the original author flagged something worth carrying forward.
INCIDENT_TOKENS: tuple[str, ...] = (
    "hotfix",
    "incident",
    "outage",
    "p0",
    "p1",
    "sev1",
    "sev2",
    "production down",
    "rollback",
    "regression",
)
_INCIDENT_RE = re.compile(
    r"|".join(rf"\b{re.escape(tok)}\b" for tok in INCIDENT_TOKENS),
    re.IGNORECASE,
)
# A Conventional Commits structured marker. Unlike free-form keywords above,
# this is a deliberate, anchored footer — high enough confidence to fire on
# body alone with no need for a corroborating issue ID.
_BREAKING_FOOTER_RE = re.compile(r"\bBREAKING[- ]CHANGE:", re.IGNORECASE)
# Conventional Commits "breaking" indicator: ``feat!:``, ``fix!:``, ``refactor!:``…
# Anchored to the start of the subject line (or after whitespace) and limited
# to known type tokens so we don't match URL fragments like ``foo!:bar``.
_BREAKING_CC_RE = re.compile(
    r"(?:^|\s)(?:feat|fix|chore|refactor|perf|build|ci|docs|test|style|revert)!:",
    re.IGNORECASE,
)

# Issue / incident identifiers that corroborate a body-only incident keyword:
# - GitHub-style: #1234
# - Jira-style:   ABC-123
# - Severity:     SEV-1, sev1, P0, P1
# Used to raise body matches above the "passing mention in prose" floor.
_ISSUE_ID_RE = re.compile(
    r"(?:#\d+|\b[A-Z][A-Z0-9_]+-\d+|\bSEV[- ]?\d\b|\bP[01]\b)",
)
INVARIANT_TOKENS: tuple[str, ...] = (
    "do not",
    "don't",
    "must not",
    "warning:",
    "important:",
    "danger:",
    "note:",
    "invariant",
    "workaround",
    "tradeoff",
)
# Compiled once: each token must appear as a whole phrase. Tokens that already
# end in a colon or apostrophe are treated literally; otherwise we require word
# boundaries so e.g. "guard" does not match "scope guard" of "guard rail".
_INVARIANT_RE = re.compile(
    r"|".join(
        rf"\b{re.escape(tok)}\b" if re.match(r"^[a-z][a-z ]*$", tok) else re.escape(tok)
        for tok in INVARIANT_TOKENS
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    authored_at: datetime
    subject: str
    body: str
    files: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return f"{self.subject}\n\n{self.body}".strip()


@dataclass(frozen=True)
class FileChange:
    sha: str
    path: str
    insertions: int
    deletions: int


@dataclass
class RepoFacts:
    """Snapshot of facts relevant to a single file."""

    repo_root: Path
    path: str
    commits: list[Commit] = field(default_factory=list)
    co_changed_files: Counter[str] = field(default_factory=Counter)
    revert_pairs: list[tuple[str, str]] = field(default_factory=list)
    """Pairs of (revert_commit_sha, reverted_commit_sha)."""

    incident_commits: list[Commit] = field(default_factory=list)
    invariant_quotes: list[tuple[str, str]] = field(default_factory=list)
    """Pairs of (commit_sha, line containing an invariant token)."""

    cache: CacheStore | None = None
    """Optional cache, threaded through so signal detectors can reuse it
    for follow-up queries (e.g. ``git blame`` for ghost-keeper detection)."""


class GitError(RuntimeError):
    """Raised when a git invocation fails or produces unexpected output."""


def run_git(repo_root: Path, *args: str) -> str:
    """Invoke ``git -C <repo_root> <args>`` and return stdout.

    Public API: callers (CLI, MCP server) use this to run git commands
    that aren't already wrapped in a higher-level helper here. Raises
    :class:`GitError` on non-zero exit or when ``git`` itself is missing.
    """
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


# Back-compat alias. Prefer ``run_git`` in new code.
_run_git = run_git


def discover_repo_root(start: Path) -> Path:
    """Find the enclosing git repo root for ``start``."""
    out = _run_git(start, "rev-parse", "--show-toplevel").strip()
    if not out:
        raise GitError(f"{start} is not inside a git repository")
    return Path(out)


def is_tracked(repo_root: Path, path: str) -> bool:
    """Return True if ``path`` is tracked by git in ``repo_root``."""
    try:
        out = _run_git(repo_root, "ls-files", "--error-unmatch", "--", path)
    except GitError:
        return False
    return bool(out.strip())


def _parse_iso(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.strip())


def _log_format() -> str:
    """The format used to serialise a commit on a single record."""
    fields = ["%H", "%an", "%ae", "%aI", "%s", "%b"]
    return UNIT_SEP.join(fields) + RECORD_SEP


def _parse_log_records(raw: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in raw.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(UNIT_SEP)
        if len(parts) < 6:
            # Body may contain a UNIT_SEP only if a contributor pasted one in
            # — vanishingly rare, but be defensive: re-stitch the trailing fields.
            head = parts[:5]
            body = UNIT_SEP.join(parts[5:])
            parts = [*head, body]
        sha, author_name, author_email, authored_at, subject, body = parts
        commits.append(
            Commit(
                sha=sha.strip(),
                author_name=author_name,
                author_email=author_email,
                authored_at=_parse_iso(authored_at),
                subject=subject,
                body=body.strip("\n"),
            )
        )
    return commits


def commits_for_path(
    repo_root: Path,
    path: str,
    *,
    max_count: int | None = None,
    ref: str | None = None,
    cache: CacheStore | None = None,
) -> list[Commit]:
    """Return commits that touched ``path`` (rename-aware), newest first.

    When ``ref`` is given, only commits reachable from that revision are
    returned — i.e., the file's history *as of* that point in time.

    A :class:`whycode.cache.CacheStore` may be passed to serve repeat queries
    from SQLite. The cache path is bypassed when ``ref`` is set (history
    queries operate on a different sha-set). On first call for an unseen
    path we let git resolve the rename history once, persist the resulting
    sha-ordered list under the current HEAD, then serve every subsequent
    call directly out of SQLite.
    """
    if cache is not None and ref is None:
        # Make sure the cache has a HEAD pinned; we need it to seal the
        # rename-resolved log against the right snapshot. Do this via
        # all_commits with a cache, which records head_sha as a side effect.
        if cache.head_sha is None:
            all_commits(repo_root, cache=cache)
        cached = _commits_for_path_via_cache(repo_root, path, cache, max_count)
        if cached is not None:
            return cached
    fetch_max = (
        None if (cache is not None and ref is None) else max_count
    )
    args = [
        "log",
        "--follow",
        "--no-merges",
        f"--pretty=format:{_log_format()}",
    ]
    if fetch_max is not None:
        args.append(f"--max-count={fetch_max}")
    if ref is not None:
        args.append(ref)
    args.extend(["--", path])
    raw = _run_git(repo_root, *args)
    commits = _parse_log_records(raw)
    if cache is not None and ref is None:
        if commits:
            _store_commits(cache, commits)
        # Persist the full rename-resolved sha order for this path so that
        # a later scan with a different --scan-depth hits the cache too.
        # Only seal under the live HEAD; if the cache hasn't recorded a
        # head_sha yet we skip the seal and rely on the next all_commits()
        # call to set it.
        try:
            head_sha = _run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = ""
        if head_sha and head_sha == cache.head_sha:
            cache.store_path_log(path, head_sha, [c.sha for c in commits])
    if max_count is not None and len(commits) > max_count:
        commits = commits[:max_count]
    return commits


def _commits_for_path_via_cache(
    repo_root: Path,
    path: str,
    cache: CacheStore,
    max_count: int | None,
) -> list[Commit] | None:
    """Return cached rename-resolved commits for ``path``, or None on miss.

    When the cache holds a path_log entry sealed at the current HEAD, the
    full sha list (and its commit metadata) is read straight from SQLite.
    Any miss — unknown HEAD, no path_log row, missing commit row —
    returns None and the caller falls through to the git path.
    """
    head_sha = _run_git(repo_root, "rev-parse", "HEAD").strip()
    if cache.head_sha != head_sha:
        return None
    cached_shas = cache.fetch_path_log(path, head_sha)
    if cached_shas is None:
        return None
    if max_count is not None:
        cached_shas = cached_shas[:max_count]
    if not cached_shas:
        return []
    rows: list[Commit] = []
    rows_by_sha = {
        str(r["sha"]): r for r in cache.fetch_all_commit_rows()
    }
    for sha in cached_shas:
        row = rows_by_sha.get(sha)
        if row is None:
            # The path_log references a sha we don't have a metadata row for.
            # Fall back to git rather than returning a partial answer.
            return None
        rows.append(_commit_from_row(row))
    return rows


def all_commits(
    repo_root: Path,
    *,
    max_count: int | None = None,
    cache: CacheStore | None = None,
) -> list[Commit]:
    """Return all commits in repo, newest first. Used for revert / ghost-author scans.

    With a cache: a fresh ``git rev-parse HEAD`` is compared against the
    last-seen head. If unchanged, every row is read from SQLite.
    Otherwise we ask git only for ``<last_head>..HEAD`` and append the
    new rows; if ``last_head`` is unreachable we rebuild from scratch.
    A ``max_count`` is always applied as a Python slice on the way out so
    the cache stays seeded with the full log regardless of caller depth.
    """
    if cache is not None:
        full = _all_commits_via_cache(repo_root, cache)
        return full if max_count is None else full[:max_count]
    args = ["log", "--no-merges", f"--pretty=format:{_log_format()}"]
    if max_count is not None:
        args.append(f"--max-count={max_count}")
    raw = _run_git(repo_root, *args)
    return _parse_log_records(raw)


def _store_commits(cache: CacheStore, commits: Sequence[Commit]) -> None:
    """Persist a batch of commits to the cache."""
    rows = [
        (
            c.sha,
            c.author_name,
            c.author_email,
            c.authored_at.isoformat(),
            c.subject,
            c.body,
        )
        for c in commits
    ]
    cache.upsert_commits(rows)


def _commit_from_row(row: object) -> Commit:
    """Rehydrate a Commit from a sqlite3.Row."""
    sha = str(row["sha"])  # type: ignore[index]
    return Commit(
        sha=sha,
        author_name=str(row["author_name"]),  # type: ignore[index]
        author_email=str(row["author_email"]),  # type: ignore[index]
        authored_at=_parse_iso(str(row["authored_at"])),  # type: ignore[index]
        subject=str(row["subject"]),  # type: ignore[index]
        body=str(row["body"]),  # type: ignore[index]
    )


def _all_commits_via_cache(repo_root: Path, cache: CacheStore) -> list[Commit]:
    """Cache-backed implementation of :func:`all_commits` (no max_count).

    Strategy:
      1. Ask git for the current HEAD sha.
      2. If it matches the cache's recorded head, every row is already
         present — read them all back and return.
      3. Otherwise try an incremental ``git log <last_head>..HEAD`` to
         pull only new commits. If ``last_head`` is unreachable
         (force-push, branch swap), fall back to a full rebuild.
    """
    head_sha = _run_git(repo_root, "rev-parse", "HEAD").strip()
    last_head = cache.head_sha
    if last_head == head_sha:
        return [_commit_from_row(r) for r in cache.fetch_all_commit_rows()]

    if last_head:
        try:
            raw_inc = _run_git(
                repo_root,
                "log",
                "--no-merges",
                f"--pretty=format:{_log_format()}",
                f"{last_head}..HEAD",
            )
        except GitError:
            # last_head not reachable — fall through to a full rebuild.
            pass
        else:
            new_commits = _parse_log_records(raw_inc)
            if new_commits:
                _store_commits(cache, new_commits)
            cache.set_head_sha(head_sha)
            return [_commit_from_row(r) for r in cache.fetch_all_commit_rows()]

    # Full rebuild: clear existing rows so an unreachable branch's commits
    # don't linger after a force-push or branch swap.
    cache.clear()
    raw = _run_git(
        repo_root, "log", "--no-merges", f"--pretty=format:{_log_format()}"
    )
    commits = _parse_log_records(raw)
    if commits:
        _store_commits(cache, commits)
    cache.set_head_sha(head_sha)
    return commits


def read_commit(repo_root: Path, ref: str) -> Commit | None:
    """Resolve ``ref`` (SHA, tag, branch, ``HEAD~3`` …) to a single ``Commit``.

    Returns ``None`` when the ref doesn't exist or doesn't resolve to a
    commit. Used by ``whycode show <sha>`` and similar single-commit views.
    """
    try:
        full_sha = run_git(
            repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}"
        ).strip()
    except GitError:
        return None
    raw = run_git(
        repo_root, "log", "-1", "--no-merges", f"--pretty=format:{_log_format()}", full_sha
    )
    parsed = _parse_log_records(raw)
    return parsed[0] if parsed else None


def files_changed_in(
    repo_root: Path,
    sha: str,
    *,
    cache: CacheStore | None = None,
) -> list[FileChange]:
    """Return the list of files (with diffstat) changed in ``sha``.

    With a cache supplied, the diffstat for any sha already populated is
    served from SQLite and the ``git show`` invocation is skipped.
    """
    if cache is not None and cache.has_commit_files(sha):
        return [
            FileChange(
                sha=str(row["sha"]),
                path=str(row["path"]),
                insertions=int(row["insertions"]),
                deletions=int(row["deletions"]),
            )
            for row in cache.fetch_files_for_commit(sha)
        ]
    raw = _run_git(
        repo_root, "show", "--no-renames", "--numstat", "--format=", sha
    )
    out: list[FileChange] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        ins_s, del_s, path = parts
        # Binary files appear as "-" "-".
        try:
            insertions = int(ins_s) if ins_s != "-" else 0
            deletions = int(del_s) if del_s != "-" else 0
        except ValueError:
            continue
        out.append(FileChange(sha=sha, path=path, insertions=insertions, deletions=deletions))
    if cache is not None and out:
        cache.upsert_commit_files(
            [(c.sha, c.path, c.insertions, c.deletions) for c in out]
        )
    return out


def co_changes(
    repo_root: Path,
    commits: Sequence[Commit],
    target_path: str,
    *,
    max_count: int | None = None,
    cache: CacheStore | None = None,
) -> Counter[str]:
    """Count, across the file's history, how often other files changed alongside ``target_path``.

    Implemented as a single ``git log --no-walk --numstat`` call over the
    pre-fetched SHA list, rather than one ``git show`` per commit. On a
    200-commit file this drops the cost from 200 git invocations to 1 —
    typically a 30-50x speedup for the coupling signal in ``scan``.

    Note: we cannot just pass ``--follow -- <path>`` to a single log call,
    because git limits the numstat output to the followed path itself in
    that mode. So we depend on the caller having already resolved the
    relevant SHAs (in ``commits``), then pass them via ``--no-walk``.

    With a cache supplied, the per-sha diffstat rows are persisted on the
    way through. The next call covering any subset of those shas is
    served entirely from SQLite without spawning a git process.
    """
    del max_count  # depth was already applied when ``commits`` was built
    if not commits:
        return Counter()
    shas = [c.sha for c in commits]
    if cache is not None:
        missing = cache.shas_missing_files(shas)
        if not missing:
            return cache.fetch_co_changes(shas, target_path)
        # Only fetch the missing ones from git. That's the whole point of
        # the cache: incremental warm-up across runs and across files.
        _populate_diffstat_cache(repo_root, cache, missing)
        return cache.fetch_co_changes(shas, target_path)

    return _co_changes_via_git(repo_root, shas, target_path)


def _co_changes_via_git(
    repo_root: Path, shas: Sequence[str], target_path: str
) -> Counter[str]:
    """The original cache-free implementation of :func:`co_changes`."""
    args = ["log", "--no-walk", "--numstat", "--format=%x1eCOMMIT"]
    args.extend(shas)
    raw = _run_git(repo_root, *args)
    counter: Counter[str] = Counter()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(RECORD_SEP):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path = parts[2]
        if path == target_path:
            continue
        counter[path] += 1
    return counter


def _populate_diffstat_cache(
    repo_root: Path, cache: CacheStore, shas: Sequence[str]
) -> None:
    """Run ``git log --no-walk --numstat`` for ``shas`` and upsert rows.

    The single batched git call replaces what would otherwise be one
    ``git show`` per commit when warming the cache for a new file.
    Output is split on the record separator emitted in the ``--pretty``
    format rather than on lines, because Python's ``splitlines`` would
    otherwise consume that separator silently.
    """
    if not shas:
        return
    args = ["log", "--no-walk", "--numstat", f"--pretty=format:{RECORD_SEP}%H"]
    args.extend(shas)
    raw = _run_git(repo_root, *args)
    rows: list[tuple[str, str, int, int]] = []
    for record in raw.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        lines = record.splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        if not sha:
            continue
        for diffstat in lines[1:]:
            stripped = diffstat.strip()
            if not stripped:
                continue
            parts = stripped.split("\t")
            if len(parts) != 3:
                continue
            ins_s, del_s, path = parts
            try:
                insertions = int(ins_s) if ins_s != "-" else 0
                deletions = int(del_s) if del_s != "-" else 0
            except ValueError:
                continue
            rows.append((sha, path, insertions, deletions))
    if rows:
        cache.upsert_commit_files(rows)


_REVERT_PREFIX = 'this reverts commit '


def find_revert_pairs(commits: Sequence[Commit]) -> list[tuple[str, str]]:
    """Detect (revert_sha, reverted_sha) pairs from commit messages.

    Git's default revert message body contains ``This reverts commit <sha>.``.
    We are tolerant of leading whitespace and trailing punctuation.
    """
    pairs: list[tuple[str, str]] = []
    for commit in commits:
        for line in commit.message.splitlines():
            stripped = line.strip().lower()
            if not stripped.startswith(_REVERT_PREFIX):
                continue
            after = stripped[len(_REVERT_PREFIX) :].strip().rstrip(".")
            # The first whitespace-separated token is the SHA.
            token = after.split()[0] if after else ""
            if len(token) >= 7 and all(c in "0123456789abcdef" for c in token):
                pairs.append((commit.sha, token))
                break
    return pairs


def find_incidents(commits: Sequence[Commit]) -> list[Commit]:
    """Return commits whose evidence-level signals incident-flavored intent.

    Acceptance ladder (highest to lowest confidence):
      1. Subject contains an incident keyword.  A commit's subject is its
         declared purpose, so a subject hit is treated as ground truth.
      2. Subject carries the Conventional Commits breaking marker
         (``feat!:`` / ``fix!:`` / …).
      3. Body carries the structured ``BREAKING CHANGE:`` footer.  This is a
         deliberate, anchored marker, not free-form prose.
      4. Body contains an incident keyword AND an issue / incident
         identifier nearby (``#1234``, ``INC-447``, ``SEV-1``, ``P0``).
         This filters out passing mentions in prose like "feat: add
         incident-aware logging" where the keyword describes a *feature*.

    A bare body keyword with no corroborating ID does NOT fire.
    """
    out: list[Commit] = []
    for c in commits:
        if _INCIDENT_RE.search(c.subject) or _BREAKING_CC_RE.search(c.subject):
            out.append(c)
            continue
        if _BREAKING_FOOTER_RE.search(c.body):
            out.append(c)
            continue
        if _INCIDENT_RE.search(c.body) and _ISSUE_ID_RE.search(c.body):
            out.append(c)
    return out


@dataclass(frozen=True)
class CommitClassification:
    """Light-weight summary of what kind of work a single commit represents."""

    incident_flavoured: bool
    invariant_count: int


def classify_commit(commit: Commit) -> CommitClassification:
    """Classify a single commit by reusing the same rules ``find_incidents`` and
    ``extract_invariant_quotes`` apply to a list. Public API for ``whycode show``
    and any other surface that wants a single-commit verdict without
    re-implementing the regex ladder.
    """
    return CommitClassification(
        incident_flavoured=bool(find_incidents([commit])),
        invariant_count=len(extract_invariant_quotes([commit])),
    )


# Straight, backtick, and the four common Unicode "smart" quote code points.
# We build the string from chr() calls because ruff's RUF001 ambiguous-char
# check rejects the literal Unicode quotes inline.
_QUOTE_CHARS = "\"'`" + "".join(chr(c) for c in (0x2018, 0x2019, 0x201C, 0x201D))


def _all_matches_are_quoted(line: str, regex: re.Pattern[str]) -> bool:
    """True iff every match of ``regex`` in ``line`` is immediately bracketed
    by quote characters — i.e. the tokens are being *named* rather than used.
    """
    matches = list(regex.finditer(line))
    if not matches:
        return False
    for m in matches:
        before = line[m.start() - 1] if m.start() > 0 else ""
        after = line[m.end()] if m.end() < len(line) else ""
        if before in _QUOTE_CHARS and after in _QUOTE_CHARS:
            continue
        return False
    return True


def extract_invariant_quotes(commits: Sequence[Commit]) -> list[tuple[str, str]]:
    """Pull lines from commit *bodies* that match invariant tokens.

    Returns pairs of (sha, the matching line) — verbatim, capped at 200 chars.

    Body-only because the subject describes what the commit did; an actual
    constraint is almost always stated in the body. Skipping the subject also
    eliminates the meta-mention failure mode where a commit *about* an
    invariant token (e.g. "fix invariant matcher") would self-flag.

    Lines where every matching token is wrapped in quotes (``"do not"``) are
    treated as references rather than statements and are skipped.
    """
    out: list[tuple[str, str]] = []
    for commit in commits:
        for raw_line in commit.body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not _INVARIANT_RE.search(line):
                continue
            if _all_matches_are_quoted(line, _INVARIANT_RE):
                continue
            out.append((commit.sha, line[:200]))
    return out


def author_last_activity(repo_root: Path, email: str) -> datetime | None:
    """Most recent commit timestamp by ``email`` anywhere in the repo, or None."""
    raw = _run_git(
        repo_root,
        "log",
        "-1",
        "--all",
        f"--author={email}",
        "--pretty=format:%aI",
    )
    raw = raw.strip()
    if not raw:
        return None
    try:
        return _parse_iso(raw)
    except ValueError:
        return None


def line_ownership(
    repo_root: Path, path: str, *, cache: CacheStore | None = None
) -> dict[str, int]:
    """Return ``{author_email: line_count}`` from ``git blame`` of HEAD's ``path``.

    Empty dict if blame is unavailable (file deleted, binary, etc.). Used by
    Layer 2 to refine ghost-keeper detection: line ownership is a stronger
    signal than commit count, which can be skewed by a single big initial
    commit followed by many tiny fixes.

    With a cache supplied, the result is keyed by ``(path, head_sha)`` so a
    repo-wide scan only invokes ``git blame`` once per file per HEAD.
    """
    head_sha: str | None = None
    if cache is not None:
        try:
            head_sha = _run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = None
        if head_sha:
            cached = cache.fetch_line_ownership(path, head_sha)
            if cached is not None:
                return cached
    try:
        raw = _run_git(repo_root, "blame", "--line-porcelain", "HEAD", "--", path)
    except GitError:
        if cache is not None and head_sha:
            cache.store_line_ownership(path, head_sha, {})
        return {}
    counts: dict[str, int] = {}
    current_email: str | None = None
    for line in raw.splitlines():
        if line.startswith("author-mail "):
            current_email = line[len("author-mail "):].strip().strip("<>")
        elif line.startswith("\t") and current_email:
            counts[current_email] = counts.get(current_email, 0) + 1
    if cache is not None and head_sha:
        cache.store_line_ownership(path, head_sha, counts)
    return counts


def gather(
    repo_root: Path,
    path: str,
    *,
    max_commits: int | None = None,
    ref: str | None = None,
    cache: CacheStore | None = None,
) -> RepoFacts:
    """Top-level convenience: build a RepoFacts snapshot for ``path``.

    Pass ``ref`` to compute facts as of a past commit (e.g., for postmortem
    "what did this file's risk look like at the time of the outage" queries).

    With a ``cache`` supplied, both the per-path commit history and the
    co-change diffstat reads are routed through SQLite. Historical (``ref``)
    queries skip the cache because the sha-set differs from current HEAD.
    """
    use_cache = cache if ref is None else None
    commits = commits_for_path(
        repo_root, path, max_count=max_commits, ref=ref, cache=use_cache
    )
    return RepoFacts(
        repo_root=repo_root,
        path=path,
        commits=commits,
        co_changed_files=co_changes(
            repo_root, commits, path, max_count=max_commits, cache=use_cache
        ),
        revert_pairs=find_revert_pairs(commits),
        incident_commits=find_incidents(commits),
        invariant_quotes=extract_invariant_quotes(commits),
        cache=use_cache,
    )
