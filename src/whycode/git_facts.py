"""Layer 1: deterministic git facts.

Pure git plumbing wrapped in safe Python. Never interprets, never guesses.
The output is the bedrock that Layer 2 builds on.

Design notes
------------
- We delimit log output with ASCII unit (0x1f) and record (0x1e) separators
  because they essentially never appear in commit messages or paths.
- We use ``--follow`` so file rename history is traced through.
- We never invoke a subcommand that mutates the repo.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    "breaking change",
)
_INCIDENT_RE = re.compile(
    r"|".join(rf"\b{re.escape(tok)}\b" for tok in INCIDENT_TOKENS),
    re.IGNORECASE,
)
# Conventional Commits "breaking" indicator: ``feat!:``, ``fix!:``, ``refactor!:``…
# Anchored to the start of the subject line (or after whitespace) and limited
# to known type tokens so we don't match URL fragments like ``foo!:bar``.
_BREAKING_CC_RE = re.compile(
    r"(?:^|\s)(?:feat|fix|chore|refactor|perf|build|ci|docs|test|style|revert)!:",
    re.IGNORECASE,
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


class GitError(RuntimeError):
    """Raised when a git invocation fails or produces unexpected output."""


def _run_git(repo_root: Path, *args: str) -> str:
    """Invoke git, return stdout. Raises GitError on non-zero exit."""
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
) -> list[Commit]:
    """Return commits that touched ``path`` (rename-aware), newest first."""
    args = [
        "log",
        "--follow",
        "--no-merges",
        f"--pretty=format:{_log_format()}",
    ]
    if max_count is not None:
        args.append(f"--max-count={max_count}")
    args.extend(["--", path])
    raw = _run_git(repo_root, *args)
    return _parse_log_records(raw)


def all_commits(repo_root: Path, *, max_count: int | None = None) -> list[Commit]:
    """Return all commits in repo, newest first. Used for revert / ghost-author scans."""
    args = ["log", "--no-merges", f"--pretty=format:{_log_format()}"]
    if max_count is not None:
        args.append(f"--max-count={max_count}")
    raw = _run_git(repo_root, *args)
    return _parse_log_records(raw)


def files_changed_in(repo_root: Path, sha: str) -> list[FileChange]:
    """Return the list of files (with diffstat) changed in ``sha``."""
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
    return out


def co_changes(
    repo_root: Path,
    commits: Sequence[Commit],
    target_path: str,
) -> Counter[str]:
    """Count, across the given commits, how often other files changed alongside ``target_path``.

    The target file is excluded from the result.
    """
    counter: Counter[str] = Counter()
    for commit in commits:
        for change in files_changed_in(repo_root, commit.sha):
            if change.path == target_path:
                continue
            counter[change.path] += 1
    return counter


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
    """Return commits with an incident keyword OR a Conventional Commits breaking marker.

    Both signals justify treating the change as incident-flavored:
      - keyword match in subject/body (``hotfix:``, ``incident``, ``regression``, …)
      - Conventional Commits breaking-change indicator (``feat!: …``, ``fix!: …``)
    """
    out: list[Commit] = []
    for c in commits:
        text = c.subject + "\n" + c.body
        if _INCIDENT_RE.search(text) or _BREAKING_CC_RE.search(c.subject):
            out.append(c)
    return out


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


def line_ownership(repo_root: Path, path: str) -> dict[str, int]:
    """Return ``{author_email: line_count}`` from ``git blame`` of HEAD's ``path``.

    Empty dict if blame is unavailable (file deleted, binary, etc.). Used by
    Layer 2 to refine ghost-keeper detection: line ownership is a stronger
    signal than commit count, which can be skewed by a single big initial
    commit followed by many tiny fixes.
    """
    try:
        raw = _run_git(repo_root, "blame", "--line-porcelain", "HEAD", "--", path)
    except GitError:
        return {}
    counts: dict[str, int] = {}
    current_email: str | None = None
    for line in raw.splitlines():
        if line.startswith("author-mail "):
            current_email = line[len("author-mail "):].strip().strip("<>")
        elif line.startswith("\t") and current_email:
            counts[current_email] = counts.get(current_email, 0) + 1
    return counts


def gather(
    repo_root: Path,
    path: str,
    *,
    max_commits: int | None = None,
) -> RepoFacts:
    """Top-level convenience: build a RepoFacts snapshot for ``path``."""
    commits = commits_for_path(repo_root, path, max_count=max_commits)
    return RepoFacts(
        repo_root=repo_root,
        path=path,
        commits=commits,
        co_changed_files=co_changes(repo_root, commits, path),
        revert_pairs=find_revert_pairs(commits),
        incident_commits=find_incidents(commits),
        invariant_quotes=extract_invariant_quotes(commits),
    )
