"""Shared fixtures: build small synthetic git repositories on demand."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    # Make commit timestamps reproducible so date-sensitive tests are stable.
    env.setdefault("GIT_AUTHOR_NAME", "Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout


class RepoBuilder:
    """Build deterministic repos in tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "commit.gpgsign", "false")

    def commit(
        self,
        subject: str,
        files: dict[str, str] | None = None,
        body: str = "",
        when: datetime | None = None,
        author_name: str = "Test",
        author_email: str = "test@example.com",
    ) -> str:
        if files:
            for relpath, content in files.items():
                target = self.root / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        _git(self.root, "add", "-A")
        message = subject if not body else f"{subject}\n\n{body}"
        env_extra: dict[str, str] = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        if when is not None:
            iso = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
            env_extra["GIT_AUTHOR_DATE"] = iso
            env_extra["GIT_COMMITTER_DATE"] = iso
        _git(
            self.root,
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "-q",
            "-m",
            message,
            env_extra=env_extra,
        )
        sha = _git(self.root, "rev-parse", "HEAD").strip()
        return sha

    def revert(self, sha: str, when: datetime | None = None) -> str:
        env_extra: dict[str, str] = {}
        if when is not None:
            iso = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
            env_extra["GIT_AUTHOR_DATE"] = iso
            env_extra["GIT_COMMITTER_DATE"] = iso
        _git(self.root, "revert", "--no-edit", "--no-gpg-sign", sha, env_extra=env_extra)
        return _git(self.root, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[RepoBuilder]:
    yield RepoBuilder(tmp_path)


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture()
def days_ago(now: datetime):  # type: ignore[no-untyped-def]
    def _do(n: int) -> datetime:
        return now - timedelta(days=n)

    return _do
