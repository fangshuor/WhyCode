"""Render a file's mindflow as a chapter list — its history as narrative beats.

Each chapter is one classified commit (revert / incident / reconciliation /
pivot / invariant / silence_break / edit). Routine ``edit`` commits are
dropped by default and runs of ≥3 same-role same-author edits collapse into
a single marker between non-edit beats; the goal is to leave only the
narratively-load-bearing commits in the rendered list.

Public surface
--------------
- :class:`Chapter` — one beat (``is_collapse=True`` for run-fold markers).
- :class:`Story` — the file-level container with summary metadata.
- :func:`build_story` — assemble a ``Story`` from a :class:`RepoFacts`.
- :func:`render_text` — rich-renderable for terminals.
- :func:`to_dict` — JSON-friendly dict for the MCP tool and ``--json`` CLI.
- :func:`render_markdown` — markdown body suitable for PR comments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.console import Group
from rich.text import Text

from whycode import git_facts as gf

_DEFAULT_BODY_MAX_CHARS = 600

# Same role-style mapping ``whycode show`` uses; centralised here so the
# story renderer and the show renderer can never drift.
ROLE_STYLE: dict[str, str] = {
    "revert": "bold bright_red",
    "incident": "bold red",
    "reconciliation": "bold magenta",
    "pivot": "bold magenta",
    "invariant": "bold yellow",
    "silence_break": "bold cyan",
    "edit": "dim",
}

# Run-fold threshold: a stretch of dropped edit chapters becomes a single
# collapse marker only when at least this many same-role same-author edits
# accumulate between two surfacing chapters. Three is the brief's call:
# below that the marker would be louder than the noise it hides.
_COLLAPSE_MIN_RUN = 3


@dataclass(frozen=True)
class Chapter:
    """A single beat in a file's mindflow.

    ``cited_prior_indices`` is populated AFTER chapter pruning by
    :func:`build_story` — for each prior SHA referenced by a reconciliation
    chapter, the index of the corresponding chapter in the final list (or
    omitted if pruned out, so the reader can fall back to the SHA).
    """

    sha: str | None
    authored_at: datetime | None
    author: str | None
    subject: str
    role: str
    body_excerpt: str | None
    body_truncated: bool
    body_full_chars: int
    cited_prior_shas: tuple[str, ...] = ()
    cited_prior_indices: tuple[int, ...] = ()
    invariant_lines: tuple[str, ...] = ()
    files_changed: int = 0
    is_collapse: bool = False
    collapse_count: int = 0
    is_pinned_by_citation: bool = False


@dataclass(frozen=True)
class Story:
    """A file's history rendered as ordered chapters.

    ``chapters_total`` is the count of real (non-collapse) chapters before
    any ``limit`` truncation. ``chapters_returned`` is the count of real
    (non-collapse) chapters present in ``chapters`` after truncation. Both
    numbers describe real chapters — a JSON consumer that allocates by
    ``chapters_returned`` gets the right slot count. The raw length of the
    ``chapters`` array can be larger when collapse markers are present;
    callers needing it should use ``len(chapters)``. ``collapse_markers``
    counts those folded-run markers separately.
    """

    path: str
    commit_count: int
    chapters_total: int
    chapters_returned: int
    collapse_markers: int
    chapters: tuple[Chapter, ...]
    primary_author: str | None
    summary: str


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@dataclass
class _RawChapter:
    """Mutable scratch chapter used during the build pass.

    Build_story walks the commit list once, emits these, then optionally
    collapses runs of dropped edits into a single ``is_collapse=True`` entry
    before freezing into immutable :class:`Chapter` instances. Keeping the
    scratch type separate spares the public ``Chapter`` from ever having
    "is this chapter going to be kept?" mutable state.
    """

    sha: str
    authored_at: datetime
    author: str
    subject: str
    role: str
    body_full: str
    cited_prior_shas: tuple[str, ...]
    invariant_lines: tuple[str, ...]
    files_changed: int
    keep: bool
    pinned_by_citation: bool = False


def _truncate_body(body: str, max_chars: int) -> tuple[str | None, bool, int]:
    """Return ``(excerpt, truncated, full_chars)`` for a commit body.

    An empty body returns ``(None, False, 0)``. The excerpt strips
    whitespace at the boundaries so a trailing fragment doesn't render as
    a lone newline.
    """
    if not body:
        return None, False, 0
    full_chars = len(body)
    if full_chars <= max_chars:
        return body, False, full_chars
    return body[:max_chars].rstrip() + "…", True, full_chars


def _cited_prior_shas(commit: gf.Commit, known_shas: set[str]) -> tuple[str, ...]:
    """Return the prior SHAs cited in ``commit.body`` that exist in the repo.

    Mirrors the ``cites_prior_sha`` cross-reference rule used by
    ``classify_commit``: a body token is a prior SHA only if it's 7-40
    hex chars and prefix-matches a real commit other than this one.
    Merge commits short-circuit (their SHA mentions are routine, not
    narrative).
    """
    if not commit.body or commit.subject.startswith("Merge "):
        return ()
    seen: list[str] = []
    seen_lower: set[str] = set()
    for match in gf._SHA_REF_IN_BODY_RE.finditer(commit.body):
        short = match.group(1).lower()
        if short in seen_lower:
            continue
        for k in known_shas:
            if k != commit.sha and k.lower().startswith(short):
                seen.append(k)
                seen_lower.add(short)
                break
    return tuple(seen)


def _invariant_lines_for(commit: gf.Commit) -> tuple[str, ...]:
    """Return the per-commit invariant lines (verbatim, capped at 200 chars)."""
    return tuple(line for _sha, line in gf.extract_invariant_quotes([commit]))


def _primary_author(commits: list[gf.Commit]) -> str | None:
    """Most-frequent author by commit count (ties broken by name asc)."""
    if not commits:
        return None
    counts: dict[str, int] = {}
    for c in commits:
        counts[c.author_name] = counts.get(c.author_name, 0) + 1
    return sorted(counts, key=lambda name: (-counts[name], name))[0]


def _collapse_runs(raws: list[_RawChapter]) -> list[_RawChapter | tuple[int, str, str]]:
    """Fold runs of ≥3 consecutive dropped edits sharing role+author.

    Returns a mixed list where each item is either a kept ``_RawChapter`` or
    a tuple ``(count, role, author)`` standing in for a collapse marker.
    The collapse marker is materialised into a :class:`Chapter` later so
    we can keep the run-level metadata separate from the per-commit fields
    that aren't meaningful when a run is folded.
    """
    out: list[_RawChapter | tuple[int, str, str]] = []
    run: list[_RawChapter] = []

    def flush() -> None:
        if not run:
            return
        if len(run) >= _COLLAPSE_MIN_RUN:
            # Same-role same-author run — emit one marker.
            first = run[0]
            out.append((len(run), first.role, first.author))
        # Otherwise the dropped edits stay dropped (no marker, no chapter).
        run.clear()

    for raw in raws:
        if raw.keep:
            flush()
            out.append(raw)
            continue
        # Dropped edit — try to extend the current run, but only when role
        # and author match the run's first entry.
        if not run:
            run.append(raw)
            continue
        head = run[0]
        if raw.role == head.role and raw.author == head.author:
            run.append(raw)
        else:
            flush()
            run.append(raw)
    flush()
    return out


def _resolve_cited_indices(
    chapter_shas: list[str | None],
    cited: tuple[str, ...],
) -> tuple[int, ...]:
    """Map cited prior SHAs to chapter indices in the final list.

    The mapping is done after pruning + truncation, so an LLM consumer
    can render "→ cites chapter 4" instead of bare SHA tokens. SHAs whose
    chapter was pruned or truncated drop out — the caller still has the
    raw ``cited_prior_shas`` tuple as a fall-back.
    """
    indices: list[int] = []
    for cited_sha in cited:
        cited_lower = cited_sha.lower()
        for idx, sha in enumerate(chapter_shas):
            if sha is None:
                continue
            if sha.lower().startswith(cited_lower) or cited_lower.startswith(
                sha.lower()
            ):
                indices.append(idx)
                break
    return tuple(indices)


def build_story(
    facts: gf.RepoFacts,
    *,
    limit: int | None = 12,
    roles: tuple[str, ...] | None = None,
    since: datetime | None = None,
    no_collapse: bool = False,
    body_max_chars: int = _DEFAULT_BODY_MAX_CHARS,
    all_chapters: bool = False,
) -> Story:
    """Assemble a :class:`Story` for ``facts.path`` (newest beat first).

    Algorithm:

    1. Pre-compute ``known_shas`` once from ``facts.commits`` to drive
       ``classify_commit``'s reconciliation cross-reference.
    2. For each commit (newest first) compute ``prior_commit_at`` from the
       previous (older) commit in the file's history, classify it, then
       decide whether to keep it as a standalone chapter:
         - If ``since`` is set, drop chapters older than ``since``.
         - If ``roles`` is set, only keep chapters whose role is in the
           allowlist (overrides default-drop-edit behavior).
         - Else if ``all_chapters`` is False, drop role==``edit`` chapters.
    3. Pin-by-citation pass: scan all chapters' ``cited_prior_shas`` and
       force-keep any chapter whose SHA is referenced from another body.
       A pinned chapter keeps its original role but is exempt from the
       default edit-drop, the collapse-run fold, and the ``limit`` cutoff.
    4. Unless ``no_collapse`` is set, fold runs of ≥3 same-role same-author
       dropped edits between non-edit beats into one collapse marker.
    5. Truncate the surfaced real chapters to ``limit`` (None = no
       truncation), preserving every pinned chapter even if it would
       otherwise be cut.
    6. Resolve each reconciliation chapter's cited prior SHAs to chapter
       indices in the final list (or omit if the cited chapter was pruned).

    The newest-first ordering is the same one ``facts.commits`` already
    uses, so a consumer who threads through ``[chapter for chapter in
    story.chapters]`` reads the file's history backward in time — closest
    in narrative reach first.
    """
    commits_newest_first = sorted(
        facts.commits, key=lambda c: c.authored_at, reverse=True
    )
    known_shas: set[str] = {c.sha for c in commits_newest_first}

    raws: list[_RawChapter] = []
    role_allowlist: frozenset[str] | None = (
        frozenset(roles) if roles is not None else None
    )

    for idx, commit in enumerate(commits_newest_first):
        # The "previous" commit in the file's history is the next-older
        # one — i.e. the commit at index idx+1 in this newest-first list.
        prior: gf.Commit | None = (
            commits_newest_first[idx + 1]
            if idx + 1 < len(commits_newest_first)
            else None
        )
        classification = gf.classify_commit(
            commit,
            known_shas=known_shas,
            prior_commit_at=prior.authored_at if prior is not None else None,
        )
        role = classification.chapter_role
        if since is not None and commit.authored_at < since:
            continue
        # Default: drop edit chapters unless the caller asked for everything
        # or supplied a role allowlist that includes "edit".
        if role_allowlist is not None:
            keep = role in role_allowlist
        elif all_chapters:
            keep = True
        else:
            keep = role != "edit"
        cited_priors = _cited_prior_shas(commit, known_shas)
        invariants = _invariant_lines_for(commit)
        raws.append(
            _RawChapter(
                sha=commit.sha,
                authored_at=commit.authored_at,
                author=commit.author_name,
                subject=commit.subject,
                role=role,
                body_full=commit.body or "",
                cited_prior_shas=cited_priors,
                invariant_lines=invariants,
                files_changed=len(commit.files),
                keep=keep,
            )
        )

    # Pin-by-citation pass: build the set of SHAs referenced by any chapter
    # body, then force-keep raws whose SHA matches a citation. Without this,
    # a short-body edit-role commit cited from a reconciliation body would
    # be pruned and the citation would fall back to a bare SHA instead of
    # resolving to a chapter index in the final list.
    cited_shas: set[str] = set()
    for r in raws:
        for cited in r.cited_prior_shas:
            cited_shas.add(cited.lower())
    if cited_shas:
        for r in raws:
            sha_lower = r.sha.lower()
            if any(
                sha_lower.startswith(c) or c.startswith(sha_lower)
                for c in cited_shas
            ):
                r.keep = True
                r.pinned_by_citation = True

    if no_collapse:
        # Filter to kept-only and skip the run-fold pass entirely.
        items: list[_RawChapter | tuple[int, str, str]] = [
            r for r in raws if r.keep
        ]
    else:
        items = _collapse_runs(raws)

    # Materialise into immutable Chapters. We compute cited_prior_indices in
    # a second pass once the final SHA-by-position list is known.
    materialised: list[Chapter] = []
    for item in items:
        if isinstance(item, tuple):
            count, run_role, run_author = item
            materialised.append(
                Chapter(
                    sha=None,
                    authored_at=None,
                    author=run_author,
                    subject=f"{count} routine edits",
                    role=run_role,
                    body_excerpt=None,
                    body_truncated=False,
                    body_full_chars=0,
                    is_collapse=True,
                    collapse_count=count,
                )
            )
            continue
        excerpt, truncated, full_chars = _truncate_body(item.body_full, body_max_chars)
        materialised.append(
            Chapter(
                sha=item.sha,
                authored_at=item.authored_at,
                author=item.author,
                subject=item.subject,
                role=item.role,
                body_excerpt=excerpt,
                body_truncated=truncated,
                body_full_chars=full_chars,
                cited_prior_shas=item.cited_prior_shas,
                invariant_lines=item.invariant_lines,
                files_changed=item.files_changed,
                is_pinned_by_citation=item.pinned_by_citation,
            )
        )

    # ``chapters_total`` records the count of real (non-collapse) chapters
    # before any ``limit`` truncation, so a consumer comparing it to
    # ``chapters_returned`` can tell whether the cap dropped any chapters.
    chapters_total = sum(1 for ch in materialised if not ch.is_collapse)

    if limit is not None and chapters_total > limit:
        # ``limit`` caps the count of real (non-collapse) chapters surfaced;
        # collapse markers between kept reals ride along for free so the
        # run-fold context isn't dropped just because the caller capped the
        # chapter count. Pinned chapters count against ``limit`` but cannot
        # be evicted — a ``--top 12`` story with 3 pins surfaces 3 pins + 9
        # newest non-pinned reals. When the pin count alone exceeds
        # ``limit``, surface every pin: silently dropping a citation target
        # would defeat the pinning pass.
        pinned_count = sum(1 for ch in materialised if ch.is_pinned_by_citation)
        if pinned_count >= limit:
            materialised = [ch for ch in materialised if ch.is_pinned_by_citation]
        else:
            remaining = limit - pinned_count
            taken: list[Chapter] = []
            non_pinned_real_kept = 0
            pending_markers: list[Chapter] = []
            for ch in materialised:
                if ch.is_collapse:
                    pending_markers.append(ch)
                    continue
                if ch.is_pinned_by_citation:
                    taken.extend(pending_markers)
                    pending_markers.clear()
                    taken.append(ch)
                elif non_pinned_real_kept < remaining:
                    taken.extend(pending_markers)
                    pending_markers.clear()
                    taken.append(ch)
                    non_pinned_real_kept += 1
                else:
                    pending_markers.clear()
            materialised = taken

    # Resolve cited prior SHAs to indices in the final, post-truncation list.
    chapter_shas: list[str | None] = [ch.sha for ch in materialised]
    final_chapters: list[Chapter] = []
    for ch in materialised:
        if ch.cited_prior_shas:
            indices = _resolve_cited_indices(chapter_shas, ch.cited_prior_shas)
            final_chapters.append(
                Chapter(
                    sha=ch.sha,
                    authored_at=ch.authored_at,
                    author=ch.author,
                    subject=ch.subject,
                    role=ch.role,
                    body_excerpt=ch.body_excerpt,
                    body_truncated=ch.body_truncated,
                    body_full_chars=ch.body_full_chars,
                    cited_prior_shas=ch.cited_prior_shas,
                    cited_prior_indices=indices,
                    invariant_lines=ch.invariant_lines,
                    files_changed=ch.files_changed,
                    is_collapse=ch.is_collapse,
                    collapse_count=ch.collapse_count,
                    is_pinned_by_citation=ch.is_pinned_by_citation,
                )
            )
        else:
            final_chapters.append(ch)

    primary = _primary_author(commits_newest_first)
    # ``chapters_returned`` counts only real chapters; collapse markers are
    # surfaced separately in ``collapse_markers`` so consumers allocating by
    # chapter count get the right slot total.
    chapters_returned = sum(1 for ch in final_chapters if not ch.is_collapse)
    collapse_markers = sum(1 for ch in final_chapters if ch.is_collapse)

    # One-line summary: "<path>: N chapters across M commits. Latest beat:
    # <role>: <subject[:40]>". Empty-history files (rare; the CLI gates on
    # _require_tracked) get a softer "no chapters" fallback so the line
    # never reads "Latest beat: None".
    if final_chapters:
        first = final_chapters[0]
        first_subject = first.subject[:40]
        summary = (
            f"{facts.path}: {chapters_returned} chapters across "
            f"{len(commits_newest_first)} commits. "
            f"Latest beat: {first.role}: {first_subject}"
        )
    else:
        summary = (
            f"{facts.path}: 0 chapters across {len(commits_newest_first)} commits."
        )

    return Story(
        path=facts.path,
        commit_count=len(commits_newest_first),
        chapters_total=chapters_total,
        chapters_returned=chapters_returned,
        collapse_markers=collapse_markers,
        chapters=tuple(final_chapters),
        primary_author=primary,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_authored_at(when: datetime | None) -> str:
    return str(when.date()) if when is not None else "—"


def _short_sha(sha: str | None) -> str:
    return sha[:7] if sha else "—"


def render_text(story: Story) -> Group:
    """Rich-renderable group: one block per chapter, role-styled.

    Each non-collapse chapter is a 3-5 line block:
    a header with role / sha / date / author, the subject in quotes, an
    indented body excerpt, and (for reconciliation) a "→ cites chapter N"
    line. Collapse markers are a single dim line.
    """
    blocks: list[Text] = []
    title = Text()
    title.append("story · ", style="dim")
    title.append(story.path, style="bold")
    title.append("\n")
    title.append(
        f"{story.commit_count} commits · {story.chapters_returned} chapters",
        style="dim",
    )
    if story.primary_author:
        title.append(f" · primary: {story.primary_author}", style="dim")
    blocks.append(title)

    for idx, ch in enumerate(story.chapters):
        block = Text()
        if ch.is_collapse:
            block.append(
                f"  …{ch.collapse_count} {ch.role} commits"
                + (f" by {ch.author}" if ch.author else "")
                + " (folded)",
                style="dim",
            )
            blocks.append(block)
            continue
        role_style = ROLE_STYLE.get(ch.role, "dim")
        block.append("[", style="dim")
        block.append(ch.role.upper(), style=role_style)
        block.append("]", style="dim")
        block.append(f"  {_short_sha(ch.sha)}  ·  ", style="dim")
        block.append(_format_authored_at(ch.authored_at), style="dim")
        if ch.author:
            block.append(f"  ·  {ch.author}", style="dim")
        if ch.is_pinned_by_citation:
            block.append("  ·  (pinned)", style="dim")
        block.append("\n")
        block.append(f'"{ch.subject}"', style="italic")
        if ch.body_excerpt:
            for line in ch.body_excerpt.splitlines():
                block.append("\n  ")
                block.append(line)
        # Reconciliation cross-references — rendered after the body so the
        # reader sees the cite as a follow-up line, not a header.
        if ch.cited_prior_indices:
            for prior_idx in ch.cited_prior_indices:
                block.append(f"\n  → cites chapter {prior_idx}", style="dim magenta")
        # Cited SHAs that didn't resolve to a chapter index are still useful
        # context — surface them as bare SHAs so the reader can `git show`.
        if ch.cited_prior_shas and not ch.cited_prior_indices:
            for cited in ch.cited_prior_shas:
                block.append(f"\n  → cites sha {cited[:7]}", style="dim magenta")
        blocks.append(block)
        if idx < len(story.chapters) - 1:
            blocks.append(Text(""))

    return Group(*blocks)


def to_dict(
    story: Story, *, body_max_chars: int = _DEFAULT_BODY_MAX_CHARS
) -> dict[str, Any]:
    """Render a :class:`Story` as a JSON-friendly dict.

    The ``body_max_chars`` argument is preserved for symmetry with
    :func:`build_story` but is not re-applied here — bodies were already
    truncated when the story was built. It's surfaced so a future caller
    that wants to chain ``to_dict(build_story(facts), body_max_chars=…)``
    has one consistent knob name.
    """
    del body_max_chars  # bodies are already truncated; arg kept for API parity
    chapters_out: list[dict[str, Any]] = []
    for idx, ch in enumerate(story.chapters):
        chapters_out.append(
            {
                "index": idx,
                "sha": ch.sha,
                "authored_at": ch.authored_at.isoformat() if ch.authored_at else None,
                "author": ch.author,
                "subject": ch.subject,
                "role": ch.role,
                "body_excerpt": ch.body_excerpt,
                "body_truncated": ch.body_truncated,
                "body_full_chars": ch.body_full_chars,
                "cited_prior_shas": list(ch.cited_prior_shas),
                "cited_prior_indices": list(ch.cited_prior_indices),
                "invariant_lines": list(ch.invariant_lines),
                "files_changed": ch.files_changed,
                "is_collapse": ch.is_collapse,
                "collapse_count": ch.collapse_count,
                "is_pinned_by_citation": ch.is_pinned_by_citation,
            }
        )
    # ``chapters_returned`` counts real (non-collapse) chapters present in
    # ``chapters`` so a consumer allocating by that number gets the right
    # slot count. ``collapse_markers`` is the count of folded-run markers;
    # the raw array length is recoverable as ``chapters_returned +
    # collapse_markers`` (also ``len(chapters)``).
    return {
        "path": story.path,
        "commit_count": story.commit_count,
        "chapters_total": story.chapters_total,
        "chapters_returned": story.chapters_returned,
        "collapse_markers": story.collapse_markers,
        "chapters": chapters_out,
        "primary_author": story.primary_author,
        "summary": story.summary,
    }


def render_markdown(story: Story) -> str:
    """Render a :class:`Story` as GitHub-flavoured markdown for PR comments.

    The header carries path / commit count / primary author; each chapter
    becomes a level-2 heading followed by the subject in quotes, a
    blockquoted body excerpt, and (for reconciliation) a "cites chapter N"
    line. Collapse markers render as italic single-line summaries between
    real chapters.
    """
    lines: list[str] = []
    header = f"# story · {story.path} · {story.commit_count} commits"
    if story.primary_author:
        header += f" · primary: {story.primary_author}"
    lines.append(header)
    lines.append("")
    for ch in story.chapters:
        if ch.is_collapse:
            who = f" by {ch.author}" if ch.author else ""
            lines.append(
                f"_…{ch.collapse_count} {ch.role} commits{who} (folded)_"
            )
            lines.append("")
            continue
        sha = _short_sha(ch.sha)
        date = _format_authored_at(ch.authored_at)
        author = f" · {ch.author}" if ch.author else ""
        pinned = " · _(pinned)_" if ch.is_pinned_by_citation else ""
        lines.append(f"## [{ch.role}] {sha} · {date}{author}{pinned}")
        lines.append("")
        lines.append(f'"{ch.subject}"')
        if ch.body_excerpt:
            lines.append("")
            for body_line in ch.body_excerpt.splitlines():
                lines.append(f"> {body_line}")
        if ch.cited_prior_indices:
            lines.append("")
            for prior_idx in ch.cited_prior_indices:
                lines.append(f"→ cites chapter {prior_idx}")
        if ch.cited_prior_shas and not ch.cited_prior_indices:
            lines.append("")
            for cited in ch.cited_prior_shas:
                lines.append(f"→ cites sha `{cited[:7]}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "ROLE_STYLE",
    "Chapter",
    "Story",
    "build_story",
    "render_markdown",
    "render_text",
    "to_dict",
]

