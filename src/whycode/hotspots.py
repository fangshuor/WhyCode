"""Sub-file hotspots: cluster a file's hunk history into stable line bands.

The file-level Risk Card answers "should I be careful here?" — this module
answers the follow-up "where inside the file does the risk live?". Both
surfaces share the same evidence (commits, revert pairs, incident-flagged
commits) but project it onto a different unit of analysis: the hunk.

Pipeline
--------
1. :func:`whycode.git_facts.commits_with_hunks` walks ``git log --follow -p``
   and emits one :class:`whycode.git_facts.HunkRecord` per ``@@`` header.
2. :func:`build_hotspots` projects every hunk onto current-HEAD line space,
   merges adjacent touched lines into bands, and tallies how many commits
   (and how many of those reverts / incidents) intersected each band.
3. The composite score combines touch count, revert density, and incident
   overlap, then decays older bands so cold history fades without being
   dropped.

Why hunk bands rather than per-language parsing
-----------------------------------------------
A real classifier per language (def/class boundaries for Python, function
signatures for Go, …) would be more readable, but it would also be a wall
of dialect-aware code. The hunk-band heuristic is "wherever lines clustered
together touched, that's a band" — language-agnostic, deterministic, and
correct enough that the rendered anchor (first non-blank line) still reads
like the structural boundary 90% of the time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from whycode import git_facts as gf

if TYPE_CHECKING:
    from collections.abc import Sequence

    from whycode.cache import CacheStore


# Adjacent touched lines closer than this gap merge into one band. A larger
# gap is read as "two unrelated regions". Three matches the audit's idiom
# of "small contiguous regions stay one band, large gaps break the band".
_BAND_GAP = 3

# Up to this many sample SHAs are surfaced per band — the same cap the
# file-level card uses for evidence, so a reader can match a band against
# the chapter-shaped commits they already saw.
_SAMPLE_SHA_CAP = 5

# Scoring weights. Pick so the file-level idiom (revert > incident > churn)
# is preserved sub-file. log1p(touches) keeps churn from steamrolling the
# 14-touch reverts on a 50-touch grab-bag region.
_W_TOUCHES = 8.0
_W_REVERT = 1.0
_W_INCIDENT = 1.0

# Anchor labels are clipped to keep the table column readable. Matches the
# 80-char convention shared with the file-level next_step lines.
_ANCHOR_MAX_CHARS = 80


@dataclass(frozen=True)
class Hotspot:
    """One clustered line band inside a single file.

    ``line_start`` / ``line_end`` are 1-based, on the current-HEAD line space
    of the file. ``anchor`` carries the first non-blank source line inside
    the band — readable enough to identify "that prepare_body function" at
    a glance, but never a real parse of the language structure.
    """

    line_start: int
    line_end: int
    touch_count: int
    revert_count: int
    incident_count: int
    score: float
    sample_shas: tuple[str, ...]
    anchor: str | None


def _decay_severity(score: float, days_since_most_recent: int) -> float:
    """Mirror of :func:`whycode.signals._decay_severity` for float scores.

    Same buckets as the file-level decay so a sub-file band and its host
    card age in step:
      - <2 years: full weight
      - 2-5 years: 75% weight
      - >5 years: 50% weight

    A floor of 1.0 is kept so a high-confidence band never decays to zero
    purely from age — the evidence is still real, just colder.
    """
    if days_since_most_recent > 1825:
        return max(1.0, score * 0.5)
    if days_since_most_recent > 730:
        return max(1.0, score * 0.75)
    return score


def _read_head_lines(repo_root: Path, path: str) -> list[str] | None:
    """Return the current-HEAD source of ``path`` split into lines, or None.

    Returns ``None`` when the file does not exist at HEAD (deleted) or when
    the working tree copy is missing — both legitimate states for a path
    whose history we still want to inspect.
    """
    try:
        raw = gf.run_git(repo_root, "show", f"HEAD:{path}")
    except gf.GitError:
        candidate = repo_root / path
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return None
        return None
    return raw.splitlines()


def _anchor_for_band(
    lines: list[str] | None, line_start: int, line_end: int
) -> str | None:
    """Return the first non-blank source line inside the band, clipped."""
    if lines is None:
        return None
    # 1-based to 0-based; clamp to the available range in case history
    # exceeds the current file length (lines deleted since touched).
    start_idx = max(0, line_start - 1)
    end_idx = min(len(lines), line_end)
    for idx in range(start_idx, end_idx):
        candidate = lines[idx].strip()
        if candidate:
            return candidate[:_ANCHOR_MAX_CHARS]
    return None


def _cluster_lines(touched: set[int]) -> list[tuple[int, int]]:
    """Cluster touched line numbers into ``(start, end)`` bands.

    Two adjacent touched lines belong to the same band unless the gap
    between them exceeds ``_BAND_GAP``. The function is pure: identical
    input yields identical bands regardless of insertion order.
    """
    if not touched:
        return []
    ordered = sorted(touched)
    bands: list[tuple[int, int]] = []
    band_start = ordered[0]
    prev = ordered[0]
    for line in ordered[1:]:
        if line - prev > _BAND_GAP + 1:
            bands.append((band_start, prev))
            band_start = line
        prev = line
    bands.append((band_start, prev))
    return bands


def build_hotspots(
    repo_root: Path,
    path: str,
    *,
    top: int = 5,
    min_touches: int = 2,
    since: datetime | None = None,
    cache: CacheStore | None = None,
) -> list[Hotspot]:
    """Return the top-N risky line bands inside ``path``.

    ``min_touches`` drops singletons — a one-touch band is usually a single
    feature commit and not a hotspot. ``since`` clips the input to commits
    after that timestamp; supply for time-windowed views (e.g. "the last
    six months only"). ``top`` is applied as a final slice after scoring.

    The function pulls every hunk via :func:`commits_with_hunks` and then
    enriches them with the file's :class:`whycode.git_facts.RepoFacts` so
    revert pairs and incident commits can be matched against the touching
    SHAs — same evidence as the file-level Risk Card.
    """
    hunks = gf.commits_with_hunks(repo_root, path, cache=cache)
    if not hunks:
        return []
    facts = gf.gather(repo_root, path, cache=cache)
    commits_by_sha = {c.sha: c for c in facts.commits}
    revert_shas = {sha for sha, _ in facts.revert_pairs}
    incident_shas = {c.sha for c in facts.incident_commits}

    # Optional time window: drop hunks whose commit is older than ``since``.
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        hunks = [
            h for h in hunks
            if (c := commits_by_sha.get(h.sha)) is not None
            and (c.authored_at if c.authored_at.tzinfo else c.authored_at.replace(tzinfo=UTC))
            >= since
        ]
        if not hunks:
            return []

    # Phase 1: collect every line each hunk touches, projected onto HEAD.
    touched: dict[int, list[gf.HunkRecord]] = {}
    for hunk in hunks:
        # A pure-deletion hunk (new_count=0) records an insertion point in
        # the new file; treat it as a single-line touch at new_start so
        # deleted-then-re-added bands still register.
        span = hunk.new_count if hunk.new_count > 0 else 1
        start = hunk.new_start if hunk.new_count > 0 else max(1, hunk.new_start)
        for line in range(start, start + span):
            touched.setdefault(line, []).append(hunk)

    # Cluster only over lines that ≥``min_touches`` distinct commits touched.
    # Without this filter, an initial-import hunk (``+1,N``) drags every
    # subsequent edit into one giant band whose anchor is line 1.
    seed_lines = {
        line
        for line, hunks_at_line in touched.items()
        if len({h.sha for h in hunks_at_line}) >= min_touches
    }
    bands = _cluster_lines(seed_lines)
    if not bands:
        return []

    head_lines = _read_head_lines(repo_root, path)

    # Phase 2: score each band against its set of touching commits.
    out: list[Hotspot] = []
    now = datetime.now(UTC)
    for line_start, line_end in bands:
        band_shas: dict[str, gf.Commit | None] = {}
        for line in range(line_start, line_end + 1):
            for hunk in touched.get(line, ()):
                if hunk.sha not in band_shas:
                    band_shas[hunk.sha] = commits_by_sha.get(hunk.sha)
        touch_count = len(band_shas)
        if touch_count < min_touches:
            continue
        revert_count = sum(1 for sha in band_shas if sha in revert_shas)
        incident_count = sum(1 for sha in band_shas if sha in incident_shas)
        incident_density = incident_count / max(touch_count, 1)
        revert_density = revert_count / max(touch_count, 1)
        score = (
            _W_TOUCHES * math.log1p(touch_count)
            + _W_REVERT * 100.0 * revert_density
            + _W_INCIDENT * 70.0 * incident_density
        )
        # Newest-touching commit drives the decay clock. A band whose most
        # recent activity is a decade old should rank below a band whose
        # most recent revert was last week, even at equal touch counts.
        newest: datetime | None = None
        for commit in band_shas.values():
            if commit is None:
                continue
            ts = commit.authored_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if newest is None or ts > newest:
                newest = ts
        if newest is not None:
            days = max(0, (now - newest).days)
            score = _decay_severity(score, days)
        # Newest sample SHAs first — readers want the recent reverts up
        # front, not whatever commit happened to be alphabetically lowest.
        ordered_shas: list[tuple[str, datetime]] = []
        for sha, commit in band_shas.items():
            if commit is None:
                ordered_shas.append((sha, datetime.fromtimestamp(0, UTC)))
            else:
                ts = commit.authored_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                ordered_shas.append((sha, ts))
        ordered_shas.sort(key=lambda pair: pair[1], reverse=True)
        sample_shas = tuple(sha[:7] for sha, _ in ordered_shas[:_SAMPLE_SHA_CAP])
        anchor = _anchor_for_band(head_lines, line_start, line_end)
        out.append(
            Hotspot(
                line_start=line_start,
                line_end=line_end,
                touch_count=touch_count,
                revert_count=revert_count,
                incident_count=incident_count,
                score=round(score, 2),
                sample_shas=sample_shas,
                anchor=anchor,
            )
        )

    out.sort(key=lambda h: (-h.score, h.line_start))
    return out[:top]


def to_json_payload(
    *,
    path: str,
    head_sha: str | None,
    hotspots: Sequence[Hotspot],
) -> dict[str, object]:
    """Render hotspots into the JSON shape consumed by CLI ``--json`` and MCP."""
    return {
        "path": path,
        "head_sha": head_sha,
        "hotspots": [
            {
                "rank": rank,
                "line_start": h.line_start,
                "line_end": h.line_end,
                "touch_count": h.touch_count,
                "revert_count": h.revert_count,
                "incident_count": h.incident_count,
                "score": h.score,
                "sample_shas": list(h.sample_shas),
                "anchor": h.anchor,
            }
            for rank, h in enumerate(hotspots, start=1)
        ],
    }


__all__ = ["Hotspot", "build_hotspots", "to_json_payload"]
