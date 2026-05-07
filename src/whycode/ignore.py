"""Default ignore patterns for repo-wide scans.

These are paths/files that almost always pollute risk analysis without
adding signal: changelogs (touched on every release, so they look "tightly
coupled to everything"), lockfiles (regenerated on every dependency bump),
vendored third-party code, machine-generated stubs, CI / packaging
metadata, project-membership files (``AUTHORS``, ``LICENSE``), and
translation catalogues (``*.po`` / ``*.mo``).

A field test against django (10,000 commits, 7,043 files) showed the
top-10 risk list was dominated by these high-touch metadata files —
``AUTHORS``, ``.github/workflows/*.yml``, locale ``.po``, ``.gitignore``
— and no application code at all reached the top 10. A scan-top list
that surfaces zero source files is unactionable; demoting these
metadata files lets real source code rank.

Users can extend this list with a ``.whycodeignore`` file at repo root,
one ``fnmatch``-style pattern per line. Comments start with ``#``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    # Changelogs / release-notes — touched every release, never the source of risk.
    "CHANGELOG*",
    "CHANGES*",
    "HISTORY*",
    "NEWS*",
    "RELEASE_NOTES*",
    # Lockfiles — regenerated on every dependency bump.
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    # Generated stubs.
    "*.pb.go",
    "*.pb.py",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.go",
    "*.generated.ts",
    "*.generated.js",
    # Minified / bundled web assets.
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    # Vendored third-party trees.
    "vendor/**",
    "_vendor/**",
    "third_party/**",
    "third-party/**",
    "node_modules/**",
    "bower_components/**",
    # Built docs.
    "_build/**",
    "site/**",
    "docs/_build/**",
    "docs/build/**",
    # Common binary / data formats that aren't code.
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.svg",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.otf",
    "*.eot",
    # CI / repo metadata — high-touch but never the source of risk in code.
    ".github/**",
    ".gitlab/**",
    ".circleci/**",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".readthedocs.yaml",
    ".readthedocs.yml",
    ".flake8",
    ".coveragerc",
    "tox.ini",
    "pytest.ini",
    "Makefile",
    # Example / demo trees — flag-bait on any repo that ships a /examples/ dir
    # alongside its source: untouched for years, low-stakes when they break.
    "examples/**",
    "example/**",
    "demo/**",
    "demos/**",
    "samples/**",
    # Project-membership / licensing files — touched on every contributor add.
    "AUTHORS",
    "AUTHORS.*",
    "CONTRIBUTORS",
    "CONTRIBUTORS.*",
    "LICENSE",
    "LICENSE.*",
    "LICENSES/**",
    "COPYING",
    "COPYING.*",
    "NOTICE",
    "NOTICE.*",
    # Python packaging metadata — low-signal-per-touch.
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    # Translation catalogues — bulk-edited every release, never an indicator
    # of code risk.
    "*.po",
    "*.mo",
    "*.pot",
    # Release-notes-style ``*.txt`` files only — narrow patterns; we are
    # deliberately conservative here so a random ``requirements.txt`` is not
    # ignored. The shapes below match common repo layouts (django, flask).
    "release_notes/*.txt",
    "docs/releases/*.txt",
    "docs/release-notes/*.txt",
    "release-notes/*.txt",
)

_USER_IGNORE_FILE = ".whycodeignore"


def load_user_patterns(repo_root: Path) -> tuple[str, ...]:
    """Read ``.whycodeignore`` if present. One pattern per line; ``#`` comments."""
    target = repo_root / _USER_IGNORE_FILE
    if not target.exists():
        return ()
    out: list[str] = []
    for raw in target.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return tuple(out)


def is_ignored(path: str, patterns: Iterable[str]) -> bool:
    """True if ``path`` matches any pattern (``fnmatch`` semantics)."""
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        # Also match basename for non-recursive patterns like ``CHANGELOG*``.
        if "/" not in pat and "/" in path and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pat):
            return True
    return False


def effective_patterns(repo_root: Path) -> tuple[str, ...]:
    """Combine the built-in defaults with the user's ``.whycodeignore``."""
    return DEFAULT_IGNORE_PATTERNS + load_user_patterns(repo_root)


__all__ = [
    "DEFAULT_IGNORE_PATTERNS",
    "effective_patterns",
    "is_ignored",
    "load_user_patterns",
]
