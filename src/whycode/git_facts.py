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

import functools
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whycode.cache import CacheStore

UNIT_SEP = "\x1f"
RECORD_SEP = "\x1e"

# Per-process record of commits whose authored timestamp could not be parsed
# even after defensive normalisation. We surface these once per session via
# a single stderr line so a single bad record does not spam a per-line warning
# — never on every read, never to a network.
_UNPARSEABLE_TIMESTAMPS: set[str] = set()
_BAD_TZ_WARNING_EMITTED = False
# The Unix epoch as a tz-aware UTC datetime; used as a safe fallback when a
# commit's authored_at is irrecoverably malformed. Picked over datetime.min
# because callers expect a tz-aware value (signal age math compares to UTC).
_EPOCH_FALLBACK = datetime.fromtimestamp(0, UTC)

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
# Security-advisory tokens fire as incidents on subject alone — the deliberate
# act of citing one is unambiguous high-confidence evidence.
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d+\b")
_GHSA_RE = re.compile(r"\bGHSA-[a-z0-9-]+\b", re.IGNORECASE)
# Default ``git revert`` body subject ("Reverted ...") and the human variant
# ("Reverts <sha>") are both unambiguous incident-class evidence on subject.
_REVERTED_SUBJECT_RE = re.compile(r'^Reverted\s+"', re.IGNORECASE)
_REVERTS_SUBJECT_RE = re.compile(r"\bReverts\s+[0-9a-f]{7,}\b", re.IGNORECASE)
# Subject-level "regression" usage that is descriptive rather than incident:
# "regression test(s)", "regression suite", "no regression", "regression nature".
# These prevention/test-housekeeping phrases must NOT fire as incidents.
_BENIGN_REGRESSION_RE = re.compile(
    r"\b(?:regression\s+(?:tests?|suite|nature)|no\s+regression)\b",
    re.IGNORECASE,
)
# Conversely, a subject using "regression" with a corroborating incident id
# (``#1234``, ``INC-447``, …), or as part of an unambiguous incident phrase
# like ``regression in <something>`` / ``Fixed: regression`` / ``fix the
# regression``, IS an incident.  These are the patterns that distinguish
# "split the regression-test files" (housekeeping) from "fix the refund
# regression" (a real outage marker).
_INCIDENT_REGRESSION_RE = re.compile(
    # ``regression in <something>`` — "fix the refund regression in admin".
    r"\bregression\s+in\b"
    # ``fix the regression`` / ``fix a regression`` — explicit incident verb.
    r"|\bfix(?:ed|es)?\s+(?:the\s+|a\s+)?regression\b"
    # ``regression — …`` / ``regression: …`` — stated subject category.
    r"|\bregression\s*[:—-]"
    # ``Fixed: regression`` / ``Hotfix: regression`` — pre-colon incident verb
    # explicitly framing the rest as the incident category itself.
    r"|\b(?:fix(?:ed|es)?|hotfix|revert(?:ed)?)\s*:\s*regression\b",
    re.IGNORECASE,
)
INVARIANT_TOKENS: tuple[str, ...] = (
    "do not",
    "don't",
    "must not",
    "important:",
    "danger:",
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
# A commit body line citing an RFC, PEP, or other standards document is
# strong evidence of an "honour this spec" constraint — even when no
# free-form invariant token appears alongside. These cites are the explicit
# "we follow standard X" decisions whose deviation would be a real bug.
_STANDARDS_CITE_RE = re.compile(
    r"\b(?:RFC|PEP|ISO|IEEE|W3C|GHSA|CVE)[\s\-]?\d{2,6}\b",
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


@dataclass(frozen=True)
class HunkRecord:
    """One ``@@ -OLD,COUNT +NEW,COUNT @@`` block from ``git log -p``.

    The ``new_*`` fields are 1-based line positions in the *new* file
    produced by the commit, and the ``old_*`` fields point at the parent.
    ``commits_with_hunks`` emits one record per hunk header it sees, in
    git's newest-first commit order.
    """

    sha: str
    path: str
    new_start: int
    new_count: int
    old_start: int
    old_count: int


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


def discover_repo_root(start: Path) -> Path:
    """Find the enclosing git repo root for ``start``."""
    out = run_git(start, "rev-parse", "--show-toplevel").strip()
    if not out:
        raise GitError(f"{start} is not inside a git repository")
    return Path(out)


def is_tracked(repo_root: Path, path: str) -> bool:
    """Return True if ``path`` is tracked by git in ``repo_root``."""
    try:
        out = run_git(repo_root, "ls-files", "--error-unmatch", "--", path)
    except GitError:
        return False
    return bool(out.strip())


def _normalise_tz_offset(timestamp: str) -> str:
    """Repair pathological tz offsets that ``datetime.fromisoformat`` rejects.

    Real-world git history contains commits authored on systems with broken
    timezone configuration (e.g. an offset of ``+518:00`` or ``+51800`` —
    encountered on a 2011 commit in psf/requests, where the underlying object
    really stores ``+51800``). ``fromisoformat`` raises ``ValueError`` on
    those, which would otherwise poison every command that walks history.

    We coerce the suffix into the canonical ``[+-]HH:MM`` form when we can
    recognise it. Anything else is left untouched and the caller falls back
    to a safe default.
    """
    stripped = timestamp.strip()
    if "T" not in stripped:
        return stripped
    body, _, after_t = stripped.partition("T")
    sign_idx = -1
    for i, ch in enumerate(after_t):
        if ch in "+-":
            sign_idx = i
            break
    if sign_idx < 0:
        return stripped
    prefix = body + "T" + after_t[:sign_idx]
    sign = after_t[sign_idx]
    rest = after_t[sign_idx + 1 :]
    digits = rest.replace(":", "")
    if not digits.isdigit():
        return stripped
    # Acceptable shapes: 4 digits → HHMM; 5 digits → HHHMM (broken, e.g.
    # ``+51800`` → hours ``5``, minutes ``18``); 6 digits → HHMMSS (rare).
    if len(digits) == 4:
        hh, mm = digits[:2], digits[2:]
    elif len(digits) == 5:
        hh, mm = "0" + digits[0], digits[1:3]
    elif len(digits) == 6:
        hh, mm = digits[:2], digits[2:4]
    elif len(digits) == 2:
        hh, mm = digits, "00"
    else:
        return stripped
    try:
        if int(hh) > 23 or int(mm) > 59:
            return stripped
    except ValueError:
        return stripped
    return f"{prefix}{sign}{hh}:{mm}"


def _parse_iso(timestamp: str) -> datetime:
    """Parse an ISO 8601 timestamp; tolerate malformed tz offsets.

    Returns a Unix-epoch sentinel if the offset cannot be repaired so a
    single bad record never crashes a whole-repo analysis. The bad raw
    string is tracked in a per-session set so verbose callers can mention
    which commits were affected.
    """
    raw = timestamp.strip()
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        repaired = _normalise_tz_offset(raw)
        if repaired != raw:
            try:
                return datetime.fromisoformat(repaired)
            except ValueError:
                pass
        _UNPARSEABLE_TIMESTAMPS.add(raw)
        return _EPOCH_FALLBACK


def _maybe_warn_bad_timestamps() -> None:
    """Emit a single stderr line per session if any record fell back to epoch.

    Called once at the end of a top-level read so a single bad commit never
    spams a per-line warning. Stays purely local — no network, no telemetry.
    """
    global _BAD_TZ_WARNING_EMITTED
    if _BAD_TZ_WARNING_EMITTED or not _UNPARSEABLE_TIMESTAMPS:
        return
    _BAD_TZ_WARNING_EMITTED = True
    n = len(_UNPARSEABLE_TIMESTAMPS)
    print(
        f"warning: {n} commit{'s' if n != 1 else ''} had an unparseable "
        f"authored timestamp; treating those as epoch for date math.",
        file=sys.stderr,
    )


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
    raw = run_git(repo_root, *args)
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
            head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = ""
        if head_sha and head_sha == cache.head_sha:
            cache.store_path_log(path, head_sha, [c.sha for c in commits])
    if max_count is not None and len(commits) > max_count:
        commits = commits[:max_count]
    _maybe_warn_bad_timestamps()
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
    head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
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
        _maybe_warn_bad_timestamps()
        return full if max_count is None else full[:max_count]
    args = ["log", "--no-merges", f"--pretty=format:{_log_format()}"]
    if max_count is not None:
        args.append(f"--max-count={max_count}")
    raw = run_git(repo_root, *args)
    out = _parse_log_records(raw)
    _maybe_warn_bad_timestamps()
    return out


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
    head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
    last_head = cache.head_sha
    if last_head == head_sha:
        return [_commit_from_row(r) for r in cache.fetch_all_commit_rows()]

    if last_head:
        try:
            raw_inc = run_git(
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
    raw = run_git(
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
    raw = run_git(
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
    raw = run_git(repo_root, *args)
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
    raw = run_git(repo_root, *args)
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


# ---- batch loading for whycode diff ---------------------------------------


@dataclass(frozen=True)
class DiffFacts:
    """A whole-repo snapshot built once for a single ``whycode diff`` evaluation.

    The diff command scores N changed files; previously each file fired its
    own ``git log --follow`` plus a co-change diffstat pass, so wall-clock
    cost scaled with N. ``DiffFacts`` replaces N path-restricted log walks
    with a single un-pathed walk: one ``git log --no-merges --numstat`` over
    the repo, parsed once into ``commits_by_path`` (every commit that named
    each path) and ``co_change_index`` (each commit's full file-set, used
    for in-memory coupling counts). Per-file scoring then reads from this
    map rather than re-shelling-out.

    The map deliberately does NOT follow renames: the diff command only
    scores files present in HEAD's working tree, so the tradeoff is "lose
    rename-resolved history pre-rename" against "scoring 1,927 files in
    seconds rather than minutes". Coupling against pre-rename names still
    surfaces under those names in the map; the surface diff in practice is
    a stable-tie-break difference, not a structural one.
    """

    repo_root: Path
    commits_by_path: dict[str, list[Commit]]
    """``path -> [Commit]``, newest-first, capped per path during load.

    A missing key — i.e. ``commits_by_path.get(path)`` returns ``None`` —
    means the loader walk did not see this path. ``gather_for_diff`` treats
    that the same as an empty list: a path that the un-pathed walk did not
    touch has no history to score from.
    """

    co_change_index: dict[str, tuple[str, ...]]
    """``commit_sha -> tuple of paths touched by that commit``.

    Snapshot of the same numstat parse used to build ``commits_by_path``.
    Per-file ``co_changes`` reads this for in-memory coupling counts so
    the diff pipeline never re-issues ``git log --no-walk`` per file.
    """

    cache: CacheStore | None = None
    """Optional cache, threaded through so signal detectors (specifically
    ``detect_ghost_keeper``) reuse it for ``git blame`` line ownership."""


_NUMSTAT_LINE_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


def load_diff_facts(
    repo_root: Path,
    *,
    max_commits: int | None = None,
    cache: CacheStore | None = None,
) -> DiffFacts:
    """Build a :class:`DiffFacts` snapshot from one ``git log`` invocation.

    Strategy:
      1. Walk HEAD with ``git log --no-merges --numstat --pretty=...`` once.
      2. Parse each commit + its full file-set into a single in-memory map.
      3. Return the snapshot for the diff command's per-file scorer to drive.

    With a ``cache`` supplied, the walked commits are persisted to
    ``commits``; per-file diffstat presence rows are persisted to
    ``commit_files`` so a subsequent ``why`` / ``scan`` / ``diff`` invocation
    on the same HEAD reuses what we just paid for.

    The walk is intentionally un-pathed: the diff command scores files
    that appear in ``git diff --name-only base...HEAD``, all of which exist
    at HEAD by definition. A single un-pathed walk that captures every
    commit's diffstat is strictly cheaper than N path-restricted walks
    that each re-walk the full graph. ``max_commits`` is applied per-path
    *after* the walk so callers can cap per-file depth without changing
    the cost of the walk itself.
    """
    # Pretty format: RECORD_SEP starts each commit; metadata fields are
    # UNIT_SEP-delimited; the body is the last metadata field. Numstat
    # output git appends after the body needs no further separator —
    # the next commit's leading RECORD_SEP marks the boundary.
    pretty_format = (
        f"{RECORD_SEP}%H{UNIT_SEP}%an{UNIT_SEP}%ae{UNIT_SEP}"
        f"%aI{UNIT_SEP}%s{UNIT_SEP}%b"
    )
    raw = run_git(
        repo_root,
        "log",
        "--no-merges",
        "--numstat",
        f"--pretty=format:{pretty_format}",
    )
    all_commits, commits_by_path, co_change_index = _parse_log_with_files(raw)
    if max_commits is not None:
        commits_by_path = {p: cs[:max_commits] for p, cs in commits_by_path.items()}
    if cache is not None and all_commits:
        _store_commits(cache, all_commits)
        # Persist diffstat presence rows so a subsequent `why` against the
        # same HEAD does not re-shell-out per file. Insertion/deletion
        # widths are not captured by this walk (the diff command's
        # detectors only depend on the *path set* of each commit), so they
        # are stored as zero — see the paragraph in ``DiffFacts``.
        files_rows: list[tuple[str, str, int, int]] = []
        for sha, paths in co_change_index.items():
            for p in paths:
                files_rows.append((sha, p, 0, 0))
        if files_rows:
            cache.upsert_commit_files(files_rows)
        try:
            head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = ""
        if head_sha and not cache.head_sha:
            cache.set_head_sha(head_sha)
    return DiffFacts(
        repo_root=repo_root,
        commits_by_path=commits_by_path,
        co_change_index=co_change_index,
        cache=cache,
    )


def _parse_log_with_files(
    raw: str,
) -> tuple[list[Commit], dict[str, list[Commit]], dict[str, tuple[str, ...]]]:
    """Parse ``git log --no-merges --numstat --pretty=<sep><commit>`` output.

    Returns ``(all_commits, commits_by_path, co_change_index)``:
      - ``all_commits`` is every parsed commit, newest first.
      - ``commits_by_path[path]`` is the subset whose numstat block named
        ``path``, preserving the newest-first order of the walk.
      - ``co_change_index[sha]`` is the full path tuple from the same
        numstat block, used by the diff command's in-memory coupling.

    Within one record the format is
    ``<sha>\\x1f<an>\\x1f<ae>\\x1f<aI>\\x1f<subject>\\x1f<body...>``
    followed by zero or more numstat lines (``ins\\tdel\\tpath``). The body
    is free-form prose; numstat is tab-delimited 3-column. We walk lines
    forward, holding the first line as the metadata + start-of-body, and
    accumulate further lines as either body (free-form) or numstat
    (matches :data:`_NUMSTAT_LINE_RE`). Once a numstat line fires, the
    remaining lines for that record are taken to be more numstat lines.
    """
    all_commits: list[Commit] = []
    commits_by_path: dict[str, list[Commit]] = {}
    co_change_index: dict[str, tuple[str, ...]] = {}
    for record in raw.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        lines = record.split("\n")
        # The first line carries every metadata field plus the first body
        # line (the body itself was emitted verbatim by ``%b``).
        head_parts = lines[0].split(UNIT_SEP)
        if len(head_parts) < 6:
            continue
        sha = head_parts[0].strip()
        if not sha:
            continue
        author_name = head_parts[1]
        author_email = head_parts[2]
        authored_at = head_parts[3]
        subject = head_parts[4]
        first_body = UNIT_SEP.join(head_parts[5:])
        body_lines: list[str] = [first_body] if first_body else []
        files: list[str] = []
        in_numstat = False
        for line in lines[1:]:
            m = _NUMSTAT_LINE_RE.match(line)
            if in_numstat:
                if m is not None:
                    files.append(m.group(3))
                continue
            if m is not None:
                in_numstat = True
                files.append(m.group(3))
                continue
            body_lines.append(line)
        authored = _parse_iso(authored_at)
        body = "\n".join(body_lines).strip("\n")
        commit = Commit(
            sha=sha,
            author_name=author_name,
            author_email=author_email,
            authored_at=authored,
            subject=subject,
            body=body,
            files=tuple(files),
        )
        all_commits.append(commit)
        co_change_index[sha] = commit.files
        for path in files:
            commits_by_path.setdefault(path, []).append(commit)
    return all_commits, commits_by_path, co_change_index


def gather_for_diff(
    diff_facts: DiffFacts,
    path: str,
    *,
    max_commits: int | None = None,
) -> RepoFacts:
    """Build a :class:`RepoFacts` for ``path`` using only the in-memory map.

    The diff command calls this once per changed file, replacing the per-file
    ``gather()`` (and its embedded ``git log --follow`` + co-change shell-out)
    with O(1) dict lookups. All higher-layer detectors run unchanged on the
    returned ``RepoFacts``.
    """
    commits = diff_facts.commits_by_path.get(path, [])
    if max_commits is not None:
        commits = commits[:max_commits]
    co_changed: Counter[str] = Counter()
    for commit in commits:
        touched = diff_facts.co_change_index.get(commit.sha, ())
        for other in touched:
            if other == path:
                continue
            co_changed[other] += 1
    return RepoFacts(
        repo_root=diff_facts.repo_root,
        path=path,
        commits=commits,
        co_changed_files=co_changed,
        revert_pairs=find_revert_pairs(commits),
        incident_commits=find_incidents(commits),
        invariant_quotes=extract_invariant_quotes(commits),
        cache=diff_facts.cache,
    )


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


def _is_subject_incident(subject: str, body: str) -> bool:
    """Determine whether a single commit's subject signals incident intent.

    The trickiest case is ``regression``. The 0.4.0 classifier accepted any
    subject that contained the word and so flagged routine bug fixes that
    happened to mention "regression tests" or "regression nature" as
    incidents. The new rule:

    - ``regression`` in a subject fires only when corroborated:
        * an issue / incident id on the same subject or anywhere in the body
          (``#1234``, ``INC-447``, ``SEV-1``, …); OR
        * a pre-marker that anchors the word as an incident reference,
          such as ``regression in <something>`` / ``Fixed: regression`` /
          ``fix the regression`` / ``regression — …``.
    - The phrases ``regression test(s)``, ``regression suite``,
      ``no regression``, ``regression nature`` never fire on their own.
    - Subjects citing a security advisory (``CVE-…`` / ``GHSA-…``) always
      fire — the act of naming an advisory is unambiguous high-confidence.
    - The default ``git revert`` body subject (``Reverted "…"``) and the
      human variant (``Reverts <sha>``) always fire — both are explicit
      rollback markers.
    - Other incident keywords (``hotfix``, ``outage``, ``rollback``, …)
      keep their existing subject-level acceptance.
    """
    if _CVE_RE.search(subject) or _GHSA_RE.search(subject):
        return True
    if _REVERTED_SUBJECT_RE.search(subject) or _REVERTS_SUBJECT_RE.search(subject):
        return True
    if _BREAKING_CC_RE.search(subject):
        return True
    # Regression demands corroboration: either it appears as part of a
    # high-confidence phrase, or an issue/incident id is present.
    has_regression = bool(re.search(r"\bregression\b", subject, re.IGNORECASE))
    if has_regression:
        if _BENIGN_REGRESSION_RE.search(subject):
            return False
        if _INCIDENT_REGRESSION_RE.search(subject):
            return True
        if _ISSUE_ID_RE.search(subject) or _ISSUE_ID_RE.search(body):
            return True
        # Strip the word and check whether any other incident keyword carries
        # the subject — a "rollback regression" should still fire on
        # "rollback" alone.
        without_regression = re.sub(r"\bregression\b", "", subject, flags=re.IGNORECASE)
        return bool(_INCIDENT_RE.search(without_regression))
    return bool(_INCIDENT_RE.search(subject))


def find_incidents(commits: Sequence[Commit]) -> list[Commit]:
    """Return commits whose evidence-level signals incident-flavored intent.

    Acceptance ladder (highest to lowest confidence):
      1. Subject cites a security advisory (``CVE-…`` / ``GHSA-…``) — fires
         on subject alone.
      2. Subject is a default ``git revert`` body (``Reverted "…"``) or a
         human revert pointer (``Reverts <sha>``) — fires on subject alone.
      3. Subject carries the Conventional Commits breaking marker
         (``feat!:`` / ``fix!:`` / …).
      4. Subject contains an incident keyword that is NOT a benign
         "regression test/suite/nature" phrase. ``regression`` requires
         either an issue id (on subject or body) or a pre-marker that
         anchors it as an incident reference.
      5. Body carries the structured ``BREAKING CHANGE:`` footer.
      6. Body contains an incident keyword AND an issue / incident
         identifier nearby. Filters out passing mentions in prose.

    A bare body keyword with no corroborating ID does NOT fire.
    """
    out: list[Commit] = []
    for c in commits:
        if _is_subject_incident(c.subject, c.body):
            out.append(c)
            continue
        if _BREAKING_FOOTER_RE.search(c.body):
            out.append(c)
            continue
        if _INCIDENT_RE.search(c.body) and _ISSUE_ID_RE.search(c.body):
            out.append(c)
    return out


# Body-length floor below which a commit is too terse to be a pivot. A real
# pivot worth flagging carries enough prose to justify the design move; the
# 200-char floor is shared with the L2 ``subject_blind_pivot`` detector so
# the signal and the chapter ladder agree on what counts.
_PIVOT_BODY_MIN_CHARS = 200
# Lower floor used when the subject already names a security- or auth-class
# concern. The substance of those commits often lives in the diff (a CVE fix,
# a cookie-signing tweak, a TLS verify flag toggle) rather than in long prose,
# so the default 200-char floor silences narratively load-bearing changes.
_PIVOT_BODY_MIN_CHARS_SECURITY: int = 80

# Subject tokens that signal a security- or auth-relevant change. When any
# of these appear in a commit subject, the body-length floor for the
# subject_blind_pivot detector is lowered: the commit deserves a chapter
# even when the body is terse, because the substance lives in the diff
# (a CVE fix, a cookie-signing tweak, a TLS verify flag toggle).
_SECURITY_SUBJECT_TOKENS: tuple[str, ...] = (
    "security",
    "auth",
    "authentic",
    "crypto",
    "cookie",
    "signed",
    "signing",
    "password",
    "secret",
    "token",
    "cert",
    "tls",
    "ssl",
    "proxy",
    "redirect",
    "header",
)
_SECURITY_SUBJECT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(tok) for tok in _SECURITY_SUBJECT_TOKENS) + r")\b",
    re.IGNORECASE,
)


def _subject_has_security_signal(subject: str) -> bool:
    return (
        bool(_SECURITY_SUBJECT_RE.search(subject))
        or bool(_CVE_RE.search(subject))
        or bool(_GHSA_RE.search(subject))
    )


def is_subject_blind_pivot_commit(commit: Commit) -> bool:
    """True if a commit body explains a structural change the subject does not.

    Long body + generic-looking subject + neither incident keyword nor revert
    marker. Used by the L2 signal detector AND ``classify_commit``'s chapter
    role so the signal and the chapter agree on what counts as a pivot.

    Security- and auth-class subjects ("fix cookie signing", "tighten TLS
    verify") use a lower body-length floor because the substance of those
    changes lives in the diff rather than in long explanatory prose; the
    default 200-char floor silences narratively load-bearing security work.
    """
    body = commit.body or ""
    subject = commit.subject
    if subject.startswith("Revert "):
        return False
    if subject.startswith("Merge "):
        return False
    if _REVERTED_SUBJECT_RE.search(subject):
        return False
    if _REVERTS_SUBJECT_RE.search(subject):
        return False
    if _BREAKING_CC_RE.search(subject):
        return False
    if _BREAKING_FOOTER_RE.search(body):
        return False
    if _is_subject_incident(subject, body):
        return False
    floor = _PIVOT_BODY_MIN_CHARS
    if _subject_has_security_signal(subject):
        floor = _PIVOT_BODY_MIN_CHARS_SECURITY
    return len(body) >= floor


_SHA_REF_IN_BODY_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)


# Maps commit-subject prefix token (e.g. "feat", "BUG") to a normalised
# pivot kind. Covers Conventional Commits (lower-case) and the ALL-CAPS
# style numpy / scipy / cpython use in subjects. Keys are matched
# case-sensitively against the captured prefix so "feat" -> feature
# but "FEAT" wouldn't (numpy uses ENH for that).
_PIVOT_KIND_MAP: dict[str, str] = {
    # Conventional Commits
    "feat": "feature",
    "fix": "bugfix",
    "refactor": "refactor",
    "perf": "perf",
    "docs": "doc",
    "style": "style",
    "test": "test",
    "chore": "chore",
    "build": "chore",
    "ci": "chore",
    # numpy / scipy / cpython ALL-CAPS style
    "BUG": "bugfix",
    "ENH": "feature",
    "DEP": "deprecate",
    "DOC": "doc",
    "STY": "style",
    "MAINT": "chore",
    "MNT": "chore",
    "BLD": "chore",
    "TST": "test",
    "REL": "chore",
}


# Anchor at start of subject (post-whitespace), optional ``!`` for breaking,
# then literal ``:``. The CC ``feat!:`` shape is treated as ``feat`` for
# pivot_kind purposes; the breaking-marker semantics live in the existing
# _BREAKING_CC_RE and continue firing as incident-flavoured.
_PIVOT_KIND_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in _PIVOT_KIND_MAP) + r")\s*!?\s*:",
)


def commit_pivot_kind(commit: Commit) -> str | None:
    """Return the normalised pivot kind for a commit subject, or None.

    Reads the leading Conventional Commits / numpy-style prefix token; maps
    via the ``_PIVOT_KIND_MAP``. Returns ``None`` when the subject does not
    open with a recognised prefix.
    """
    m = _PIVOT_KIND_RE.match(commit.subject)
    if not m:
        return None
    return _PIVOT_KIND_MAP.get(m.group(1))


_SILENCE_BREAK_DAYS = 180


@dataclass(frozen=True)
class CommitClassification:
    """Light-weight summary of what kind of work a single commit represents.

    ``is_revert``, ``cites_prior_sha``, ``breaks_silence`` and ``is_pivot``
    are False by default so existing callers that only read
    ``incident_flavoured`` / ``invariant_count`` keep working unchanged.
    ``cites_prior_sha`` is only set when ``classify_commit`` is called with
    a ``known_shas`` set — without that cross-reference set, the body could
    match SHA-shaped tokens (version numbers etc.) that aren't real commit
    references. ``breaks_silence`` is only set when ``classify_commit`` is
    given a ``prior_commit_at`` to compare against; otherwise the gap is
    unknown and the field stays False.
    """

    incident_flavoured: bool
    invariant_count: int
    is_revert: bool = False
    cites_prior_sha: bool = False
    breaks_silence: bool = False
    is_pivot: bool = False
    pivot_kind: str | None = None
    """Normalised commit-subject prefix kind: one of ``feature`` / ``bugfix``
    / ``refactor`` / ``perf`` / ``doc`` / ``style`` / ``test`` / ``chore`` /
    ``deprecate`` / None. Only meaningful when ``is_pivot`` is True; ``None``
    means the commit subject didn't carry a recognised prefix."""

    @property
    def chapter_role(self) -> str:
        """Single-word narrative-role label used by ``whycode show`` and the
        upcoming ``whycode story``. Priority ladder (first match wins):
        ``revert`` > ``incident`` > ``deprecate`` > ``reconciliation`` >
        ``pivot`` > ``invariant`` > ``silence_break`` > ``edit``.
        Deprecation announcements are decision-grade — they signal a future
        breakage deliberately — and earn their own chapter role. They sit
        above ``reconciliation`` because deprecations often precede the
        refactors that resolve them. ``pivot`` is the "long body explains
        a structural change the subject does not" chapter — the
        architectural decisions other detectors miss because the subject
        reads generic. ``silence_break`` is the "the file slept for half
        a year, then someone re-touched it" chapter — a narratively
        distinct beat the other roles do not capture. Reconciliation
        requires a body cross-reference to a prior known SHA.
        """
        if self.is_revert:
            return "revert"
        if self.incident_flavoured:
            return "incident"
        if self.is_pivot and self.pivot_kind == "deprecate":
            return "deprecate"
        if self.cites_prior_sha:
            return "reconciliation"
        if self.is_pivot:
            return "pivot"
        if self.invariant_count > 0:
            return "invariant"
        if self.breaks_silence:
            return "silence_break"
        return "edit"


def classify_commit(
    commit: Commit,
    *,
    known_shas: Iterable[str] | None = None,
    prior_commit_at: datetime | None = None,
) -> CommitClassification:
    """Classify a single commit by reusing the same rules ``find_incidents`` and
    ``extract_invariant_quotes`` apply to a list.

    ``known_shas`` enables the ``cites_prior_sha`` cross-reference check —
    pass the repo's commit-SHA set and the body is scanned for short SHA
    tokens (7-40 hex chars) that match a real commit. Merge commits are
    never flagged (their SHA mentions are routine, not narrative).

    ``prior_commit_at`` enables the ``breaks_silence`` chapter check —
    pass the authored timestamp of the file's previous commit (the one
    immediately before this SHA in the file's history). If the gap to
    ``commit.authored_at`` is at least ``_SILENCE_BREAK_DAYS`` (180), the
    classification carries ``breaks_silence=True``. Without a
    ``prior_commit_at`` the gap is unknown and the field stays False.
    """
    body = commit.body or ""
    is_revert = (
        commit.subject.startswith("Revert ")
        or "this reverts commit" in body.lower()
    )
    cites_prior = False
    if known_shas and body and not commit.subject.startswith("Merge "):
        known = set(known_shas)
        for match in _SHA_REF_IN_BODY_RE.finditer(body):
            short = match.group(1).lower()
            if any(k != commit.sha and k.lower().startswith(short) for k in known):
                cites_prior = True
                break
    breaks_silence = False
    if prior_commit_at is not None:
        prior = prior_commit_at
        if prior.tzinfo is None:
            prior = prior.replace(tzinfo=UTC)
        current = commit.authored_at
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        gap_days = (current - prior).days
        if gap_days >= _SILENCE_BREAK_DAYS:
            breaks_silence = True
    is_pivot = is_subject_blind_pivot_commit(commit)
    pivot_kind = commit_pivot_kind(commit) if is_pivot else None
    return CommitClassification(
        incident_flavoured=bool(find_incidents([commit])),
        invariant_count=len(extract_invariant_quotes([commit])),
        is_revert=is_revert,
        cites_prior_sha=cites_prior,
        breaks_silence=breaks_silence,
        is_pivot=is_pivot,
        pivot_kind=pivot_kind,
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


# An ALLCAPS line prefix (e.g. ``WARNING:``, ``ERROR:``, ``DEBUG:``) is the
# canonical signature of pasted compiler / linter / spell-checker output.
# A genuine human invariant statement opens with a normal sentence ("Do
# not...", "Important: ...") and never with two or more uppercase letters
# followed by an immediate colon.
_TOOL_OUTPUT_ALLCAPS_RE = re.compile(r"^[A-Z]{2,}:\s")
# A ``path:line:`` or ``path:line:col:`` prefix near the start of a line is
# the unmistakable shape of compiler / aspell output. We accept any path-
# shaped token (slashes, dots, hyphens, underscores, alnum) followed by
# ``:<digits>:`` — anchored so it also catches ``./foo/bar.py:50:``.
_TOOL_OUTPUT_PATH_RE = re.compile(r"^[\w./-]+:\d+:")
# Per-(commit, file) cap on invariant lines pulled from one body. A real
# author rarely states more than two crisp invariants in a single message;
# anything beyond is almost certainly a paste. Set deliberately low so a
# single noisy commit can no longer dominate the "highlights" view.
_PER_COMMIT_INVARIANT_CAP = 2


def _is_tool_output_line(line: str, prev_line: str) -> bool:
    """True if ``line`` looks like quoted compiler / linter / aspell output.

    Heuristics:
      - ALLCAPS followed immediately by a colon (``WARNING:``, ``ERROR:``,
        ``DEBUG:``…) — pasted tool output.
      - ``path/to/file:line:`` prefix near the start — clang / mypy / aspell.
      - Preceded by a ``> `` block-quote line — markdown-style "this is
        what the tool said" framing.
    """
    if _TOOL_OUTPUT_ALLCAPS_RE.match(line):
        return True
    if _TOOL_OUTPUT_PATH_RE.match(line):
        return True
    return prev_line.startswith("> ")


def extract_invariant_quotes(commits: Sequence[Commit]) -> list[tuple[str, str]]:
    """Pull lines from commit *bodies* that match invariant tokens.

    Returns pairs of (sha, the matching line) — verbatim, capped at 200 chars.

    Body-only because the subject describes what the commit did; an actual
    constraint is almost always stated in the body. Skipping the subject also
    eliminates the meta-mention failure mode where a commit *about* an
    invariant token (e.g. "fix invariant matcher") would self-flag.

    A line also qualifies when it cites a standards document (RFC, PEP, ISO,
    IEEE, W3C, GHSA, CVE). These cites are the explicit "we honour spec X"
    decisions whose deviation would be a real bug; firing on them catches
    the constraints that the free-form token list misses.

    Two filters keep pasted tool output out of the "stated invariants"
    surface:

    1. Lines that look like quoted compiler / linter / aspell output are
       dropped (``WARNING: …``, ``foo/bar.py:50: …``, lines preceded by a
       ``> `` block-quote). One noisy spell-check commit on django used to
       supply 15 of the top-20 highlights; this rule kills it at the
       source.
    2. A per-commit cap of two invariants. Real authors rarely state more
       than two crisp constraints in one message; anything beyond is
       almost certainly a paste. The first two matches are preserved
       (most informative-looking entries rank).

    Lines where every matching token is wrapped in quotes (``"do not"``) are
    treated as references rather than statements and are skipped.
    """
    out: list[tuple[str, str]] = []
    for commit in commits:
        per_commit = 0
        prev_line = ""
        for raw_line in commit.body.splitlines():
            line = raw_line.strip()
            if not line:
                prev_line = raw_line
                continue
            if _is_tool_output_line(line, prev_line):
                prev_line = raw_line
                continue
            prev_line = raw_line
            invariant_hit = _INVARIANT_RE.search(line)
            standards_hit = _STANDARDS_CITE_RE.search(line)
            if not invariant_hit and not standards_hit:
                continue
            # If the only signal is a free-form invariant token AND every
            # such token is wrapped in quotes, treat it as a meta-mention.
            # A standards cite alone is never a quote-only reference: it
            # is unambiguous evidence by itself.
            if invariant_hit and not standards_hit and _all_matches_are_quoted(
                line, _INVARIANT_RE
            ):
                continue
            if per_commit >= _PER_COMMIT_INVARIANT_CAP:
                continue
            out.append((commit.sha, line[:200]))
            per_commit += 1
    return out


def dedupe_invariant_lines(
    pairs: Sequence[tuple[str, str]],
    sha_to_commit: dict[str, Commit],
) -> list[tuple[str, str]]:
    """Collapse identical invariant lines to one canonical (sha, line) pair.

    When two commits state the same invariant line — typically a cherry-pick
    onto a maintenance branch, or a rebase that duplicated the message — we
    must pick exactly one to surface. Without a deterministic rule the cache
    and ``--no-cache`` paths can disagree (their walk orders differ when
    timestamps tie), and downstream JSON consumers see flaky output across
    runs.

    The rule:

    1. Earliest ``authored_at`` wins. The original statement is canonical;
       cherry-picks and rebases are derivatives.
    2. Lexicographically smallest ``sha`` breaks ties on identical timestamps.

    The returned list preserves first-encounter order of the (now-unique)
    lines so downstream code that sorts by date sees a stable input.
    Pairs whose ``sha`` is not in ``sha_to_commit`` keep their first-seen
    record (no metadata to compare on).
    """
    canonical: dict[str, str] = {}
    for sha, line in pairs:
        existing = canonical.get(line)
        if existing is None:
            canonical[line] = sha
            continue
        old_commit = sha_to_commit.get(existing)
        new_commit = sha_to_commit.get(sha)
        if old_commit is None or new_commit is None:
            continue
        old_key = (old_commit.authored_at, existing)
        new_key = (new_commit.authored_at, sha)
        if new_key < old_key:
            canonical[line] = sha
    return [(sha, line) for line, sha in canonical.items()]


def author_last_activity(repo_root: Path, email: str) -> datetime | None:
    """Most recent commit timestamp by ``email`` anywhere in the repo, or None."""
    raw = run_git(
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
            head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = None
        if head_sha:
            cached = cache.fetch_line_ownership(path, head_sha)
            if cached is not None:
                return cached
    try:
        raw = run_git(repo_root, "blame", "--line-porcelain", "HEAD", "--", path)
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


# Matches ``R<score>\tOLD\tNEW`` from ``git log --diff-filter=R --name-status``.
# The score percentage is variable (R50, R85, R100), so the digits are captured
# loosely; the actual value doesn't drive anything we do — only OLD and NEW.
_RENAME_NAME_STATUS_RE = re.compile(r"^R\d+\t(.+?)\t(.+)$")


@functools.lru_cache(maxsize=8)
def _build_rename_map_at_head(repo_root: Path, head_sha: str) -> dict[str, str]:
    """Walk the rename history and return ``{old_path: terminal_new_path}``.

    Process-local memoisation only — the ``(repo_root, head_sha)`` key is
    used so a HEAD advance naturally invalidates the cached map. The
    persistent SQLite cache layer is handled by :func:`build_rename_map`.

    Empty dict on git error: coupling falls back to today's
    no-rename-resolution behaviour rather than crashing the run.
    """
    del head_sha  # only present to participate in the lru_cache key.
    try:
        raw = run_git(
            repo_root,
            "log",
            "--all",
            "--diff-filter=R",
            "--name-status",
            "-M50%",
            "--pretty=format:",
        )
    except GitError:
        return {}
    mapping: dict[str, str] = {}
    # Newest-first walk: each ``R<score>\tOLD\tNEW`` line yields one
    # (OLD, NEW) mapping. If NEW appears later as the OLD of an even-newer
    # rename, the already-stored value is already terminal — natural chain
    # compression without an extra pass.
    for line in raw.splitlines():
        m = _RENAME_NAME_STATUS_RE.match(line)
        if m is None:
            continue
        old_path = m.group(1)
        new_path = m.group(2)
        if old_path == new_path:
            continue
        if old_path in mapping:
            continue
        # Resolve through any rename we have already recorded so chained
        # renames (a → b → c) collapse to ``a -> c`` rather than ``a -> b``.
        mapping[old_path] = mapping.get(new_path, new_path)
    if not mapping:
        return {}
    # Drop entries whose terminal NEW is no longer present at HEAD — those
    # were renamed to a name that was later deleted, and following them
    # would point an editor at a non-existent file. A git error talking to
    # ls-files (vs. an empty index) is the only reason to skip the filter:
    # we'd rather keep the map as-is than discard it on a transient failure.
    try:
        tracked_out = run_git(repo_root, "ls-files")
    except GitError:
        return mapping
    tracked = {line for line in tracked_out.splitlines() if line.strip()}
    return {old: new for old, new in mapping.items() if new in tracked}


def build_rename_map(
    repo_root: Path, *, cache: CacheStore | None = None
) -> dict[str, str]:
    """Map every historically-known path to its terminal (current-HEAD) name.

    One ``git log --all --diff-filter=R --name-status -M50%`` walk per repo,
    parsed once into an ``{old_path: terminal_new_path}`` dict. Walking
    newest-first means the dict value is always the terminal current name;
    chained renames (a → b → c) collapse naturally because if NEW later
    appears as OLD in an even-newer record, the stored value already
    points at the terminal name.

    Any entry whose terminal NEW is not in ``git ls-files`` at the current
    HEAD is dropped: those were renamed to a name that was later deleted,
    and following them would point a reader at a non-existent file.

    The persistent ``CacheStore`` (when supplied) is consulted first: a
    HEAD-keyed hit returns immediately; otherwise the git walk runs once,
    the result is stored, and subsequent invocations under the same HEAD
    hit the SQLite layer. Without a cache, an in-process ``lru_cache``
    keyed on ``(repo_root, head_sha)`` still amortises repeat calls.

    Empty dict on git error — coupling falls back to today's
    no-rename-resolution behaviour rather than crashing the run.
    """
    try:
        head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
    except GitError:
        return {}
    if not head_sha:
        return {}
    if cache is not None:
        cached = cache.fetch_rename_map(head_sha)
        if cached is not None:
            return cached
    mapping = _build_rename_map_at_head(repo_root, head_sha)
    if cache is not None:
        cache.store_rename_map(head_sha, mapping)
    return mapping


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


# ---- hunk extraction (sub-file hotspots) ----------------------------------


# Header form: ``@@ -OLD[,COUNT] +NEW[,COUNT] @@`` (count defaults to 1 when
# omitted). With ``--unified=0`` the headers carry every changed region.
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
# Diff "+++ b/<path>" preamble emitted once per commit. ``--follow`` rewrites
# the right-hand path to the requested name even across renames, so we lean
# on the commit boundary marker rather than parsing per-file headers. The
# ``\x1e`` byte is a record-separator that ``splitlines`` treats as its own
# break — see ``_iter_hunk_lines`` for the stitching that recombines them.
_COMMIT_BOUNDARY_PREFIX = "commit "


def commits_with_hunks(
    repo_root: Path,
    path: str,
    *,
    cache: CacheStore | None = None,
) -> list[HunkRecord]:
    """Parse ``git log --follow -p --unified=0`` into a flat list of hunks.

    Each ``@@`` header becomes one :class:`HunkRecord`. Newest commits come
    first; within a commit, hunks appear in git's emitted order (typically
    top-of-file to bottom).

    With ``cache`` set, the (path, head_sha) row pair seals the parse so a
    subsequent call hits SQLite. The cache key matches ``path_log`` — when
    HEAD advances, the seal is invalidated and we re-walk; when ``path_log``
    is fresh, the hunks usually are too.
    """
    head_sha: str | None = None
    if cache is not None:
        try:
            head_sha = run_git(repo_root, "rev-parse", "HEAD").strip()
        except GitError:
            head_sha = None
        if head_sha:
            cached = cache.fetch_hunks(path, head_sha)
            if cached is not None:
                return [
                    HunkRecord(
                        sha=sha,
                        path=path,
                        new_start=new_start,
                        new_count=new_count,
                        old_start=old_start,
                        old_count=old_count,
                    )
                    for (sha, new_start, new_count, old_start, old_count) in cached
                ]
    try:
        # ``\x1e`` is a stable record separator that ``git`` round-trips
        # verbatim and that ``splitlines`` is happy to treat as a break,
        # so the parser can split the whole raw blob on it without worrying
        # about embedded newlines in commit hashes or in the diff body.
        raw = run_git(
            repo_root,
            "log",
            "--follow",
            "--no-merges",
            "--no-color",
            "--unified=0",
            f"--pretty=format:{RECORD_SEP}{_COMMIT_BOUNDARY_PREFIX}%H",
            "-p",
            "--",
            path,
        )
    except GitError:
        if cache is not None and head_sha:
            cache.store_hunks(path, head_sha, [])
        return []

    hunks: list[HunkRecord] = []
    for record in raw.split(RECORD_SEP):
        record = record.strip("\n")
        if not record.startswith(_COMMIT_BOUNDARY_PREFIX):
            continue
        # First line is ``commit <sha>``; subsequent lines are the diff body.
        first_nl = record.find("\n")
        if first_nl < 0:
            continue
        sha_header = record[: first_nl]
        body = record[first_nl + 1 :]
        current_sha = sha_header[len(_COMMIT_BOUNDARY_PREFIX) :].strip()
        if not current_sha:
            continue
        for line in body.splitlines():
            if not line.startswith("@@"):
                continue
            m = _HUNK_HEADER_RE.match(line)
            if m is None:
                continue
            old_start = int(m.group(1))
            old_count = 1 if m.group(2) is None else int(m.group(2))
            new_start = int(m.group(3))
            new_count = 1 if m.group(4) is None else int(m.group(4))
            hunks.append(
                HunkRecord(
                    sha=current_sha,
                    path=path,
                    new_start=new_start,
                    new_count=new_count,
                    old_start=old_start,
                    old_count=old_count,
                )
            )

    if cache is not None and head_sha:
        cache.store_hunks(
            path,
            head_sha,
            [
                (h.sha, h.new_start, h.new_count, h.old_start, h.old_count)
                for h in hunks
            ],
        )
    return hunks
