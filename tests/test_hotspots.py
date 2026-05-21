"""Tests for sub-file hotspot extraction and band clustering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from whycode import git_facts as gf
from whycode import hotspots as hs


@pytest.fixture()
def repo_with_hotspots(repo, days_ago):  # type: ignore[no-untyped-def]
    """Build a small repo where line 100-120 absorbs reverts/incidents."""

    def _build(*, gap_lines: int = 0, deleted: bool = False) -> tuple[Path, str]:
        # Seed the file with enough lines that hunk math is meaningful.
        lines = [f"line {i}" for i in range(1, 201)]
        # Plant a recognisable anchor at line 100 so we can verify anchor
        # extraction. Lines are 1-based, list is 0-based.
        lines[99] = "def prepare_body(self, data, files, json):"
        lines[100] = "    # critical region"
        # Plant a second region anchor near 130-140; the gap between is
        # configurable so we can test band-merge thresholds.
        lines[120 + gap_lines] = "def iter_content(self, chunk_size=1):"
        text_initial = "\n".join(lines) + "\n"
        sha_init = repo.commit(
            "init refund flow",
            {"refund.py": text_initial},
            when=days_ago(120),
        )
        # Touch line 100-105 a couple more times.
        for i, day in enumerate((100, 80, 60), start=1):
            lines[99] = f"def prepare_body(self, data{i}, files, json):"
            lines[100] = f"    # critical region rev{i}"
            lines[101] = f"    # additional context {i}"
            repo.commit(
                f"refactor prepare_body pass {i}",
                {"refund.py": "\n".join(lines) + "\n"},
                when=days_ago(day),
            )
        # Insert a revertable change to lines 100-105.
        lines[99] = "def prepare_body(self, data_v4, files, json, headers):"
        sha_bad = repo.commit(
            "tweak prepare_body signature",
            {"refund.py": "\n".join(lines) + "\n"},
            when=days_ago(40),
        )
        # Revert it: the body carries the default ``This reverts commit <sha>``.
        repo.revert(sha_bad, when=days_ago(35))
        # An incident-flagged hotfix that touches lines 100-105.
        lines[99] = "def prepare_body(self, data_v5, files, json):"
        lines[100] = "    # critical region after hotfix #401"
        repo.commit(
            "hotfix: refund regression in prepare_body",
            {"refund.py": "\n".join(lines) + "\n"},
            body="incident #401",
            when=days_ago(10),
        )
        # If asked, delete the file at HEAD so anchor=None coverage runs.
        if deleted:
            target = repo.root / "refund.py"
            target.unlink()
            repo.commit(
                "remove refund module",
                files=None,
                when=days_ago(2),
            )
        return repo.root, sha_init

    return _build


def test_band_clustering_merges_adjacent_hunks_within_3_lines(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0)
    bands = hs.build_hotspots(repo_root, "refund.py", top=5, min_touches=2)
    # The seeded region around line 100 is touched 5+ times across
    # commits — it must surface as a single contiguous band, not splinter
    # into one-line slivers.
    around_100 = [b for b in bands if b.line_start <= 100 <= b.line_end]
    assert len(around_100) == 1, bands
    band = around_100[0]
    # Adjacent line touches within the gap threshold belong to one band.
    assert band.line_start <= 100
    assert band.line_end >= 102


def test_band_clustering_breaks_at_4_line_gap() -> None:
    # Synthetic test: directly drive the clustering helper. The function is
    # the load-bearing piece, so prove it on its own.
    bands = hs._cluster_lines({10, 11, 12, 17, 18})
    # Gap of 4 (12 -> 17) exceeds the 3-line threshold: two distinct bands.
    assert bands == [(10, 12), (17, 18)]


def test_band_clustering_merges_within_three_line_gap() -> None:
    bands = hs._cluster_lines({10, 14})
    # Gap of 3 (10 -> 14) sits exactly at the threshold and stays merged.
    assert bands == [(10, 14)]


def test_hotspot_score_revert_density_dominates_touch_count(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Build a file with two distinct regions: one touched many times with
    # no reverts, the other touched fewer times but with a real revert.
    lines = [f"line {i}" for i in range(1, 201)]
    lines[49] = "def churny_region(self):"
    lines[149] = "def revert_region(self):"
    text = "\n".join(lines) + "\n"
    repo.commit("init", {"mixed.py": text}, when=days_ago(120))

    # Churn region (lines 50-52) — touched 5 times, no reverts.
    for i, day in enumerate((100, 90, 80, 70, 60), start=1):
        lines[49] = f"def churny_region_v{i}(self):"
        lines[50] = f"    # tweak {i}"
        lines[51] = f"    # additional {i}"
        repo.commit(
            f"refactor churny pass {i}",
            {"mixed.py": "\n".join(lines) + "\n"},
            when=days_ago(day),
        )

    # Revert region (lines 150-152) — fewer touches but a revert.
    lines[149] = "def revert_region_bad(self):"
    bad_sha = repo.commit(
        "regress revert_region",
        {"mixed.py": "\n".join(lines) + "\n"},
        when=days_ago(50),
    )
    repo.revert(bad_sha, when=days_ago(45))

    bands = hs.build_hotspots(repo.root, "mixed.py", top=10, min_touches=2)
    assert bands
    # Find each region's band.
    churn = next(b for b in bands if 50 <= b.line_start <= 52)
    revert = next(b for b in bands if 148 <= b.line_start <= 152)
    # Even though the churn region has more touches, the revert region's
    # density wins on score.
    assert revert.revert_count >= 1
    assert churn.revert_count == 0
    assert revert.score > churn.score


def test_hotspot_drops_bands_below_min_touches(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0)
    high = hs.build_hotspots(repo_root, "refund.py", top=10, min_touches=2)
    very_high = hs.build_hotspots(repo_root, "refund.py", top=10, min_touches=50)
    assert high  # qualifying bands exist at min_touches=2
    assert not very_high  # nothing survives at min_touches=50


def test_hotspot_anchor_extracts_first_non_blank_line(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0)
    bands = hs.build_hotspots(repo_root, "refund.py", top=5, min_touches=2)
    around_100 = next(b for b in bands if b.line_start <= 100 <= b.line_end)
    assert around_100.anchor is not None
    assert "prepare_body" in around_100.anchor


def test_hotspot_anchor_none_when_file_deleted_on_head(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0, deleted=True)
    bands = hs.build_hotspots(repo_root, "refund.py", top=5, min_touches=2)
    assert bands  # history still exists even though HEAD is empty
    for band in bands:
        assert band.anchor is None


def test_hotspot_payload_has_required_keys(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0)
    bands = hs.build_hotspots(repo_root, "refund.py", top=3, min_touches=2)
    payload = hs.to_json_payload(path="refund.py", head_sha="abc", hotspots=bands)
    assert payload["path"] == "refund.py"
    assert payload["head_sha"] == "abc"
    assert isinstance(payload["hotspots"], list)
    if payload["hotspots"]:
        first = payload["hotspots"][0]
        for key in (
            "rank",
            "line_start",
            "line_end",
            "touch_count",
            "revert_count",
            "incident_count",
            "score",
            "sample_shas",
            "anchor",
        ):
            assert key in first, key


def test_hotspot_since_filter_drops_old_commits(repo_with_hotspots) -> None:  # type: ignore[no-untyped-def]
    repo_root, _ = repo_with_hotspots(gap_lines=0)
    cutoff = datetime.now(UTC).replace(microsecond=0)
    # A future cutoff drops every commit: no hunks remain.
    bands = hs.build_hotspots(
        repo_root,
        "refund.py",
        top=5,
        min_touches=2,
        since=cutoff,
    )
    assert bands == []


def test_commits_with_hunks_returns_hunk_records(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """The raw parser must emit one HunkRecord per @@ header."""
    repo.commit(
        "init",
        {"a.py": "one\ntwo\nthree\nfour\nfive\n"},
        when=days_ago(10),
    )
    repo.commit(
        "edit",
        {"a.py": "one\ntwo MUTATED\nthree\nfour\nfive\n"},
        when=days_ago(5),
    )
    hunks = gf.commits_with_hunks(repo.root, "a.py")
    assert hunks
    for h in hunks:
        assert isinstance(h, gf.HunkRecord)
        assert h.new_start >= 1
        assert h.new_count >= 0
        assert h.path == "a.py"
