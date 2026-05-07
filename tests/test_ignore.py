"""Tests for the ignore-pattern matcher."""

from __future__ import annotations

from whycode.ignore import (
    DEFAULT_IGNORE_PATTERNS,
    effective_patterns,
    is_ignored,
    load_user_patterns,
)


def test_default_patterns_match_changelog() -> None:
    assert is_ignored("CHANGELOG.md", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("CHANGES.rst", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("HISTORY.txt", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("RELEASE_NOTES.md", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_lockfiles() -> None:
    assert is_ignored("package-lock.json", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("yarn.lock", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("Cargo.lock", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("uv.lock", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_vendored_dirs() -> None:
    assert is_ignored("node_modules/foo/index.js", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("vendor/github.com/foo/bar.go", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("third_party/x/y.cc", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_generated_stubs() -> None:
    assert is_ignored("api_pb2.py", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("foo.pb.go", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("schema.generated.ts", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_do_not_match_normal_code() -> None:
    assert not is_ignored("src/whycode/cli.py", DEFAULT_IGNORE_PATTERNS)
    assert not is_ignored("README.md", DEFAULT_IGNORE_PATTERNS)
    assert not is_ignored("tests/test_cli.py", DEFAULT_IGNORE_PATTERNS)
    assert not is_ignored("requirements.txt", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_ci_and_repo_metadata() -> None:
    """F8 — django's top-10 risk list was dominated by .github workflows,
    AUTHORS, .gitignore, locale ``.po`` files, and similar high-touch
    metadata. These get filtered out by default so real code can rank."""
    assert is_ignored(".github/workflows/tests.yml", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".github/workflows/linters.yml", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".gitignore", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".gitattributes", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".editorconfig", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".pre-commit-config.yaml", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".readthedocs.yaml", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored(".flake8", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("tox.ini", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("pytest.ini", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("Makefile", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_membership_and_licensing() -> None:
    assert is_ignored("AUTHORS", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("AUTHORS.rst", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("CONTRIBUTORS", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("LICENSE", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("LICENSE.txt", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("LICENSE.md", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("COPYING", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("NOTICE", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_packaging() -> None:
    assert is_ignored("setup.py", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("setup.cfg", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("MANIFEST.in", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_match_translation_catalogues() -> None:
    assert is_ignored("django/conf/locale/en/LC_MESSAGES/django.po", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("django/conf/locale/mn/LC_MESSAGES/django.mo", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("locale/messages.pot", DEFAULT_IGNORE_PATTERNS)


def test_default_patterns_conservative_about_txt_files() -> None:
    """Only release-notes-shaped ``*.txt`` paths are filtered; ordinary
    ``requirements.txt`` / ``constraints.txt`` stay visible because they
    are real dependency surfaces."""
    assert is_ignored("docs/releases/4.2.txt", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("release_notes/v1.txt", DEFAULT_IGNORE_PATTERNS)
    assert not is_ignored("requirements.txt", DEFAULT_IGNORE_PATTERNS)
    assert not is_ignored("constraints/runtime.txt", DEFAULT_IGNORE_PATTERNS)


def test_basename_match_for_root_pattern_in_subdir() -> None:
    # `CHANGELOG*` should match `docs/CHANGELOG.md` even though the pattern has no slash.
    assert is_ignored("docs/CHANGELOG.md", DEFAULT_IGNORE_PATTERNS)
    assert is_ignored("packages/foo/CHANGES.rst", DEFAULT_IGNORE_PATTERNS)


def test_user_patterns_loaded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".whycodeignore").write_text(
        "# this is a comment\n"
        "*.proto\n"
        "scripts/\n"
        "\n"  # blank line
        "internal/legacy.py\n"
    )
    patterns = load_user_patterns(tmp_path)
    assert patterns == ("*.proto", "scripts/", "internal/legacy.py")


def test_user_patterns_empty_when_no_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert load_user_patterns(tmp_path) == ()


def test_effective_patterns_combines_defaults_and_user(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".whycodeignore").write_text("internal/legacy.py\n")
    eff = effective_patterns(tmp_path)
    assert "internal/legacy.py" in eff
    assert "*.lock" in eff  # default still present
    assert is_ignored("internal/legacy.py", eff)
