"""The ``whycode`` CLI.

Commands
--------
- ``whycode tour``              — first-run walkthrough: highlights + top risk + MCP setup.
- ``whycode why <path>``        — print the Risk Card for a single file.
- ``whycode why <path> --at SHA`` — risk card as of a past commit.
- ``whycode why <path> --mute KIND`` — locally suppress a noisy signal kind.
- ``whycode why <path> --llm`` — opt-in L3: LLM-extracted structured decisions.
- ``whycode highlights``        — repo-wide treasure map of decisions and incidents.
- ``whycode diff [--base REF]`` — risk-rank files changed against a base ref.
- ``whycode show <sha>``        — risk-flavored summary for one commit.
- ``whycode timeline <path>``   — risk score evolution across the file's history.
- ``whycode story <path>``      — chronological mindflow: chapters by classified role.
- ``whycode honest <path>``     — every invariant line, verbatim, untruncated.
- ``whycode scan [--top N]``    — print the top-N riskiest files in the repo.
- ``whycode init``              — install CI workflow + pre-commit risk gate.
- ``whycode mcp``               — start the MCP stdio server.
- ``whycode version``           — print version.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from whycode import __version__
from whycode import cache as ch
from whycode import git_facts as gf
from whycode import ignore as ign
from whycode import risk_card as rc
from whycode import signals as sig
from whycode import story as st
from whycode import suppressions as supp

app = typer.Typer(
    add_completion=False,
    help="WhyCode — tells you what to be afraid of before touching a file.",
    epilog="Start here: whycode tour",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)


def _open_cache(repo_root: Path, no_cache: bool) -> ch.CacheStore | None:
    """Open the cache for ``repo_root`` according to the no-cache flag.

    Modes:
      * ``no_cache=False`` (the default): persistent on-disk SQLite at
        ``.whycode/cache.db``.
      * ``no_cache=True``: a transient ``:memory:`` SQLite store. The
        same git-walk code path runs as for the cold-fill, but the
        database is destroyed on ``close()`` — nothing lands on disk
        and the next run starts cold. Keeping per-run amortisation
        (one ``git log`` walk shared across files) is what makes
        ``--no-cache`` at most as slow as a cold persistent fill;
        the previous ``cache=None`` short-circuit lost that and so
        ``--no-cache`` re-issued per-file walks every iteration.

    A ``None`` return means "do not pass a cache through git_facts".
    Happens only when even an in-memory open fails — very rare and
    we never want a cache problem to block the main read path.
    """
    try:
        if no_cache:
            return ch.open_in_memory(repo_root)
        return ch.open_for(repo_root)
    except OSError:
        return None


def _resolve_repo_and_path(path_arg: str) -> tuple[Path, str]:
    """Translate a user-provided path into (repo_root, repo-relative path)."""
    p = Path(path_arg).resolve()
    start = p if p.is_dir() else p.parent if p.exists() else Path.cwd()
    try:
        repo_root = gf.discover_repo_root(start)
    except gf.GitError as exc:
        # Friendly framing for the most common cause; keep raw error visible
        # for the rare disk / corrupted-repo path so users can still file a bug.
        if "not a git repository" in str(exc).lower():
            err.print(
                "[red]error:[/red] not inside a git repository.\n"
                "[dim]→ cd into a git repo, or pass --repo PATH on commands that take it.[/dim]"
            )
        else:
            err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not p.exists():
        # Allow the user to pass a path that was deleted in HEAD but lived in
        # history — we still want to report on it.
        rel = path_arg
    else:
        try:
            rel = str(p.relative_to(repo_root))
        except ValueError:
            err.print(f"[red]error:[/red] {p} is not inside {repo_root}")
            raise typer.Exit(2) from None
    return repo_root, rel


def _path_is_known_to_git(repo_root: Path, rel: str) -> bool:
    """Has git ever seen this path? (tracked OR appears in history)"""
    if gf.is_tracked(repo_root, rel):
        return True
    try:
        out = gf.run_git(repo_root, "log", "--oneline", "-1", "--all", "--", rel)
    except gf.GitError:
        return False
    return bool(out.strip())


def _require_tracked(path_arg: str) -> tuple[Path, str]:
    """Resolve ``path_arg`` to ``(repo_root, rel)`` or exit with a friendly warning.

    Used by every command that takes a path argument and needs git history
    to be useful (``why``, ``timeline``, ``honest``). Combines the two earlier
    helpers so callers don't repeat the warn-and-exit boilerplate.
    """
    repo_root, rel = _resolve_repo_and_path(path_arg)
    if not _path_is_known_to_git(repo_root, rel):
        # Exit 2 (not 1) so a CI loop running ``whycode why`` per file fails
        # loudly on a typo / wrong path instead of silently succeeding.
        err.print(
            f"[red]error:[/red] [bold]{rel}[/bold] is not tracked by git "
            f"and has no history in this repo.\n"
            f"[dim]→ check the path, or run [bold]whycode scan --top 5[/bold] "
            f"to see what's there.[/dim]"
        )
        raise typer.Exit(2)
    return repo_root, rel


@contextlib.contextmanager
def _memoised_is_ignored(repo_root: Path) -> Iterator[None]:
    """Memoise ``ign.is_ignored`` for the duration of the ``with`` block.

    The diff command's evaluation re-applies the same ``is_ignored`` test
    against thousands of co-change candidates per file. Each call resolves
    fnmatch over ~83 patterns; uncached, that is ~100 CPU-seconds across
    a 1,927-file diff on django.

    A path's verdict is fully determined by the path string and the
    repo's effective ignore-pattern tuple, so we cache by ``(path,
    patterns)`` for the duration of the diff and restore the original
    function on exit. The cache is process-local; the rest of the CLI
    (``why``, ``scan``, …) sees the un-memoised function. ``ign`` itself
    is unchanged.
    """
    patterns = ign.effective_patterns(repo_root)
    cache: dict[str, bool] = {}
    original = ign.is_ignored

    def memoised(path: str, patterns_arg: object = patterns) -> bool:
        if patterns_arg is patterns:
            cached = cache.get(path)
            if cached is None:
                cached = original(path, patterns)
                cache[path] = cached
            return cached
        return original(path, patterns_arg)  # type: ignore[arg-type]

    ign.is_ignored = memoised  # type: ignore[assignment]
    try:
        yield
    finally:
        ign.is_ignored = original


# --- shared: band threshold parsing ----------------------------------------

_BAND_THRESHOLDS_BY_KEY: dict[str, int] = {
    "handle": 75,
    "handle-with-care": 75,
    "history": 50,
    "read": 50,
    "read-history-first": 50,
    "look": 25,
    "worth-a-look": 25,
}


def _parse_fail_on(value: str) -> int:
    threshold = _BAND_THRESHOLDS_BY_KEY.get(value.lower().strip())
    if threshold is None:
        raise typer.BadParameter(
            f"unknown band: {value!r}. "
            f"Use one of: handle | history | look (or full names with hyphens)."
        )
    return threshold


_SECURITY_HINT_RE = re.compile(r"\b(?:CVE-\d{4}-\d+|GHSA-[a-z0-9-]+)\b", re.IGNORECASE)


def _security_touched(card: rc.RiskCard) -> bool:
    """True if any signal references a CVE or GHSA advisory token.

    The 3am SRE persona feedback called for an unmistakable label on files
    whose history mentions a security advisory; the score alone hides it.
    """
    for s in card.signals:
        haystack = " ".join((s.headline, s.detail, *s.evidence))
        if _SECURITY_HINT_RE.search(haystack):
            return True
    return False


def _brief_verdict(card: rc.RiskCard, security: bool) -> str:
    """Map score band + security flag to a single action verb.

    Three personas read ``--brief`` looking for an action ("edit / escalate /
    page owner") rather than a number. The verdict ladder turns the band into
    that verb so a tired reader at 3am gets a directive, not arithmetic.
    """
    if security:
        return "ESCALATE"
    band = card.score.band.value
    if band == "HANDLE WITH CARE":
        return "ESCALATE"
    if band == "READ HISTORY FIRST":
        return "CHECK"
    if band == "WORTH A LOOK":
        return "SCAN"
    return "OK"


def _print_brief(card: rc.RiskCard) -> None:
    """Print a one-line summary suitable for grep/awk and 3am triage.

    Format: ``<path>: <VERDICT> [SECURITY-TOUCHED] (<band>, <score>) — <top>``.
    The verdict (ESCALATE / CHECK / SCAN / OK) is the action verb the reader
    can act on without interpreting the score; the band + score follow as
    supporting evidence; the top headline names the most pressing concern.
    """
    top = card.signals[0].headline if card.signals else "no flags"
    security = _security_touched(card)
    verdict = _brief_verdict(card, security)
    verdict_style = {
        "ESCALATE": "bold red",
        "CHECK": "bold yellow",
        "SCAN": "bold cyan",
        "OK": "bold green",
    }[verdict]
    sec_tag = " [bold red]SECURITY-TOUCHED[/bold red]" if security else ""
    console.print(
        f"{card.path}: [{verdict_style}]{verdict}[/{verdict_style}]{sec_tag} "
        f"([bold]{card.score.band.value}[/bold], {card.score.value}) — {top}"
    )


# --- shared: bucket grouping for diff and tour -----------------------------
#
# The diff and tour commands present a flat list of files sorted by score.
# That made a 50-file PR a wall of identical-looking rows. Grouping them
# under a heading per band gives a reviewer the cluster of HANDLE WITH CARE
# files at a glance and demotes everything else below it. The order is
# fixed: HANDLE WITH CARE → READ HISTORY FIRST → WORTH A LOOK → CLEAR.
# CLEAR collapses NEWBORN-only and signal-less files together, since both
# mean "nothing to flag".

# Bucket order is fixed by the brief; not configurable.
_BUCKET_ORDER: tuple[str, ...] = (
    "HANDLE WITH CARE",
    "READ HISTORY FIRST",
    "WORTH A LOOK",
    "CLEAR",
)


def _bucket_for(card: rc.RiskCard) -> str:
    """Return the bucket label for a single card.

    Files in the three risky bands keep their band as the bucket label.
    NEWBORN-only files, signal-less files, and (rarely) files with one weak
    signal still in the NO_FLAGS band collapse into a single CLEAR bucket;
    they're "we have nothing to flag", not actionable risk.
    """
    from whycode.scorer import Band

    actionable = any(s.kind is not sig.SignalKind.NEWBORN for s in card.signals)
    if not actionable:
        return "CLEAR"
    if card.score.band is Band.NO_FLAGS:
        return "CLEAR"
    return card.score.band.value


def _group_into_buckets(cards: list[rc.RiskCard]) -> dict[str, list[rc.RiskCard]]:
    """Partition cards into the four fixed buckets.

    Each bucket preserves the input order, so callers that pre-sort by
    ``(-score, path)`` keep that stable tie-break inside each bucket.
    Empty buckets are present in the dict (with an empty list value); the
    renderer chooses whether to skip them.
    """
    buckets: dict[str, list[rc.RiskCard]] = {b: [] for b in _BUCKET_ORDER}
    for c in cards:
        buckets[_bucket_for(c)].append(c)
    return buckets


def _bucket_header_style(bucket: str) -> str:
    """Return the rich style for a bucket header.

    The first three buckets reuse the same band colours that ``risk_card``
    uses so a reader scanning the diff picks up the same colour mapping
    they will see on the per-file Risk Card.  CLEAR uses the green NO_FLAGS
    style — that's what a flat ``no flags`` row would have looked like.
    """
    from whycode.scorer import Band

    if bucket == "CLEAR":
        return rc.BAND_STYLE[Band.NO_FLAGS]
    return rc.BAND_STYLE[Band(bucket)]


@app.command()
def why(
    path: str = typer.Argument(..., help="File path to inspect."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a card."
    ),
    brief: bool = typer.Option(
        False, "--brief", "-b", help="One-line summary (for triage and scripts)."
    ),
    at: str | None = typer.Option(
        None,
        "--at",
        help="Show the Risk Card as of this commit / ref (postmortem queries).",
    ),
    mute: list[str] = typer.Option(
        [],
        "--mute",
        help=(
            "Suppress a signal kind for this path going forward "
            "(stored in .whycode/suppressed.json). One of: "
            "revert, incident, high_churn, coupling, silence, ghost, "
            "invariant, newborn. Example: --mute coupling. "
            "(Use the `kind` value shown in --explain output / JSON.)"
        ),
    ),
    no_mutes: bool = typer.Option(
        False,
        "--no-mutes",
        help="Bypass the local suppression list — show all signals.",
    ),
    llm: bool = typer.Option(
        False,
        "--llm",
        help=(
            "Enrich the card with LLM-extracted structured decisions "
            "(L3, opt-in, requires WHYCODE_LLM_API_KEY + WHYCODE_LLM_MODEL). "
            "Sends only commits already filtered by L2 — see --llm-dry-run."
        ),
    ),
    llm_dry_run: bool = typer.Option(
        False,
        "--llm-dry-run",
        help="Show exactly what would be sent to the LLM without making the call.",
    ),
    max_commits: int | None = typer.Option(
        None, "--max-commits", help="Cap the number of commits scanned (debug)."
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local SQLite cache at .whycode/cache.db.",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help=(
            "Below each signal, print the precise rule that fired: the literal "
            "matched tokens, threshold values, and the source location of the "
            "ladder branch. L1+L2 only — L3 (--llm) decisions are not annotated."
        ),
    ),
) -> None:
    """Print the Risk Card for ``path``."""
    repo_root, rel = _require_tracked(path)
    if not json_out and ign.is_ignored(rel, ign.effective_patterns(repo_root)):
        err.print(
            f"[yellow]heads up:[/yellow] [bold]{rel}[/bold] matches the default "
            f"ignore list (changelogs, lockfiles, generated code, vendored trees, "
            f"docs build output, etc.). Risk scoring on metadata files is mostly "
            f"noise; treat the card below as advisory."
        )
    resolved_ref: str | None = None
    if at is not None:
        try:
            resolved_ref = gf.run_git(
                repo_root, "rev-parse", "--verify", f"{at}^{{commit}}"
            ).strip()
        except gf.GitError:
            err.print(f"[red]error:[/red] unknown commit / ref: {at!r}")
            raise typer.Exit(2) from None
    cache = _open_cache(repo_root, no_cache)
    if mute:
        sl = supp.load(repo_root)
        added: list[str] = []
        for token in mute:
            try:
                kind = supp.resolve_kind(token)
            except ValueError as exc:
                err.print(f"[red]error:[/red] {exc}")
                raise typer.Exit(2) from None
            if sl.add(rel, kind):
                added.append(kind.value)
        if added:
            supp.save(repo_root, sl)
            if not json_out:
                err.print(
                    f"[dim]muted on {rel}: {', '.join(added)}  "
                    f"(undo: pass [bold]--no-mutes[/bold] to preview unmuted, "
                    f"or delete the line in .whycode/suppressed.json)[/dim]"
                )
    try:
        card = rc.build(
            repo_root,
            rel,
            max_commits=max_commits,
            ref=resolved_ref,
            apply_suppressions=not no_mutes,
            cache=cache,
        )

        if llm or llm_dry_run:
            from whycode import decisions as dec

            # Pick high-signal commits for L3: incidents take priority, plus
            # any commit with a substantial body. Cap to keep the prompt small.
            facts = gf.gather(
                repo_root, rel, max_commits=max_commits, ref=resolved_ref, cache=cache
            )
            candidates = list(facts.incident_commits)
            for c in facts.commits:
                if c not in candidates and len(c.body) >= 100:
                    candidates.append(c)
                if len(candidates) >= dec.DEFAULT_MAX_COMMITS:
                    break
            candidates = candidates[: dec.DEFAULT_MAX_COMMITS]
            n_commits, prompt_chars = dec.estimate_payload(candidates)

            if llm_dry_run:
                err.print(
                    f"[bold]LLM dry-run:[/bold] would send "
                    f"[bold]{n_commits}[/bold] commit(s), "
                    f"[bold]~{prompt_chars}[/bold] chars to the configured LLM provider.\n"
                    f"  [dim]Provider, model, and key all read from "
                    f"WHYCODE_LLM_* environment variables.[/dim]"
                )
                if not json_out:
                    console.print(rc.render_text(card))
                else:
                    console.print_json(json.dumps(card.to_dict()))
                return

            if n_commits == 0:
                err.print(
                    "[yellow]--llm:[/yellow] no high-signal commits to enrich on this file."
                )
            else:
                try:
                    decisions = dec.extract_decisions(candidates)
                except dec.LLMConfigError as exc:
                    err.print(f"[red]--llm config error:[/red] {exc}")
                    raise typer.Exit(2) from exc
                except dec.LLMCallError as exc:
                    err.print(f"[red]--llm call failed:[/red] {exc}")
                    raise typer.Exit(2) from exc
                card = card.with_decisions(tuple(decisions))

        if json_out:
            console.print_json(json.dumps(card.to_dict(explain=explain)))
            return
        if brief:
            _print_brief(card)
            return
        console.print(rc.render_text(card, explain=explain))
    finally:
        if cache is not None:
            cache.close()


def _resolve_base_ref(repo_root: Path, requested: str | None) -> str:
    """Pick a base ref for ``whycode diff``.

    Order: explicit --base, origin/main, origin/master, main, master, HEAD~1.
    """
    if requested:
        return requested
    candidates = ("origin/main", "origin/master", "main", "master", "HEAD~1")
    for ref in candidates:
        try:
            gf.run_git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            return ref
        except gf.GitError:
            continue
    raise gf.GitError(
        "could not pick a base ref (tried origin/main, main, HEAD~1). "
        "Use --base <ref> to specify one."
    )


@app.command()
def diff(
    base: str | None = typer.Option(
        None, "--base", help="Base ref (default: origin/main → main → HEAD~1)."
    ),
    staged: bool = typer.Option(
        False,
        "--staged",
        help="Score files staged for commit instead (for pre-commit hooks).",
    ),
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
    top: int = typer.Option(20, "--top", help="Show at most this many files."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help=(
            "Emit GitHub-flavoured markdown suitable for posting as a PR comment. "
            "Pipe into a workflow step that calls `gh pr comment`."
        ),
    ),
    fail_on: str | None = typer.Option(
        None,
        "--fail-on",
        help=(
            "Exit non-zero if any file reaches this band: "
            "handle (≥75) / history (≥50) / look (≥25). "
            "Use in CI: `whycode diff --fail-on history`."
        ),
    ),
    show_clear: bool = typer.Option(
        False,
        "--show-clear",
        help=(
            "Expand the CLEAR bucket (files with no risk signals). Default "
            "behaviour collapses it to a single count line."
        ),
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local SQLite cache at .whycode/cache.db.",
    ),
) -> None:
    """Risk-rank files that changed against a base ref. The 'pre-PR' command."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
        if staged:
            raw = gf.run_git(
                repo_root, "diff", "--cached", "--name-only", "--diff-filter=ACMR"
            )
            actual_base = "(staged changes)"
        else:
            actual_base = _resolve_base_ref(repo_root, base)
            raw = gf.run_git(repo_root, "diff", "--name-only", f"{actual_base}...HEAD")
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    threshold: int | None = None
    if fail_on is not None:
        threshold = _parse_fail_on(fail_on)

    # Apply the same ignore-pattern filter that `scan` uses so changelogs,
    # lockfiles, generated stubs, vendored trees etc. don't dominate the
    # ranking. The 0.4.1 quality pass added the filter to scan and to the
    # coupling detector but the diff command's own file list was not gated.
    diff_patterns = ign.effective_patterns(repo_root)
    files = [
        line
        for line in raw.splitlines()
        if line.strip() and not ign.is_ignored(line.strip(), diff_patterns)
    ]
    if not files:
        if json_out:
            console.print_json(json.dumps({"base": actual_base, "files": []}))
        else:
            scope = "staged" if staged else f"vs {actual_base}"
            console.print(f"[green]no changes {scope}[/green]")
        return

    cache = _open_cache(repo_root, no_cache)
    try:
        # One git log walk feeds every changed file's scoring. Without this
        # batched load, diff against an old base on a large repo runs N
        # `git log --follow` calls (one per changed file): on django at 1,927
        # changed files the legacy path measured 6+ minutes, with the
        # 12+ minute variant timing out outright. ``load_diff_facts`` parses
        # one un-pathed walk into a path -> [Commit] map; per-file scoring
        # then does dict lookups instead of re-shelling-out.
        try:
            diff_facts = gf.load_diff_facts(repo_root, cache=cache)
        except gf.GitError as exc:
            err.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(2) from exc
        # Pre-compute the ignore-pattern set ONCE and a verdict-per-path
        # memo. ``signals.detect_coupling`` (re-introduced in 0.4.1 as F10)
        # filters every coupling candidate through ``ign.is_ignored`` —
        # without memoisation that's 83 patterns x 700 candidates x 1,927
        # files = ~100 CPU-seconds across the diff. The memo cache turns
        # each path's verdict into a dict lookup after the first hit.
        with _memoised_is_ignored(repo_root):
            # First pass: every changed file is scored without the
            # ghost-keeper detector, which would otherwise fire ``git
            # blame`` per file. With 1,927 changed files on django this
            # single deferral saves ~5 minutes. We then sort and
            # re-evaluate only the top-N with full signals — at most
            # ``top`` blame calls instead of ``len(files)``.
            prelim: list[rc.RiskCard] = []
            for f in files:
                try:
                    prelim.append(
                        rc.build_from_diff_facts(diff_facts, f, skip_ghost_keeper=True)
                    )
                except gf.GitError:
                    continue
            prelim.sort(key=lambda c: (-c.score.value, c.path))
            # Second pass: re-score the top-N with the full detector ladder
            # so the rendered table includes ghost-keeper findings where
            # they apply. Files outside the top-N keep their first-pass
            # score; they were not going to appear in the user's view
            # anyway.
            refined_top: list[rc.RiskCard] = []
            for prelim_card in prelim[:top]:
                try:
                    refined_top.append(
                        rc.build_from_diff_facts(diff_facts, prelim_card.path)
                    )
                except gf.GitError:
                    refined_top.append(prelim_card)
            cards = refined_top
            cards.sort(key=lambda c: (-c.score.value, c.path))
    finally:
        if cache is not None:
            cache.close()

    # Bucket the cards by band (with NEWBORN-only / signal-less rolled into
    # CLEAR). Each bucket preserves the (-score, path) tie-break already
    # applied to ``cards``, so callers that walk a bucket get a stable order
    # within it. ``--top N`` was applied above against the global sorted list,
    # so the bucketed view contains exactly N rows distributed across buckets
    # in fixed order.
    buckets = _group_into_buckets(cards)
    flagged_total = sum(len(buckets[b]) for b in _BUCKET_ORDER if b != "CLEAR")
    clear_n = len(buckets["CLEAR"])

    if json_out:
        console.print_json(
            json.dumps(
                {
                    "base": actual_base,
                    "buckets": {
                        band: [c.to_dict() for c in buckets[band]]
                        for band in _BUCKET_ORDER
                    },
                }
            )
        )
        if threshold is not None and any(c.score.value >= threshold for c in cards):
            raise typer.Exit(1)
        return

    scope_md = "files staged for commit" if staged else f"files changed vs `{actual_base}`"
    if markdown:
        # Stable marker so a follow-up workflow step can find-and-update the
        # same comment on subsequent pushes instead of stacking new ones.
        print("<!-- whycode-comment -->")
        print("## WhyCode risk briefing")
        print()
        print(f"**{len(files)} {scope_md}**")
        print()
        if flagged_total == 0 and not show_clear:
            print("Nothing flagged. Read the diff anyway.")
            if clear_n:
                print()
                print(
                    f"_+ {clear_n} file(s) with no risk signals — "
                    "pass `--show-clear` to list._"
                )
        else:
            for band in _BUCKET_ORDER:
                rows = buckets[band]
                if not rows:
                    continue
                if band == "CLEAR" and not show_clear:
                    continue
                print(f"### {band} ({len(rows)})")
                print()
                print("| File | Top signal |")
                print("| ---- | ---------- |")
                for c in rows:
                    if c.signals:
                        top_signal = c.signals[0].headline.replace("|", "\\|")
                    else:
                        top_signal = "no flags"
                    print(f"| `{c.path}` | {top_signal} |")
                print()
            if clear_n and not show_clear:
                print(
                    f"_+ {clear_n} file(s) with no risk signals — "
                    "pass `--show-clear` to list._"
                )
                print()
        if threshold is not None and any(c.score.value >= threshold for c in cards):
            raise typer.Exit(1)
        return

    scope = "staged for commit" if staged else f"changed vs {actual_base}"
    console.print(f"[bold]{len(files)} file(s) {scope}[/bold]")
    console.print()
    if flagged_total == 0 and not show_clear:
        console.print("[green]nothing flagged[/green] — but read the diff anyway.")
        if clear_n:
            console.print(
                f"[dim]+ {clear_n} file(s) with no risk signals — "
                "pass --show-clear to list[/dim]"
            )
    else:
        for band in _BUCKET_ORDER:
            rows = buckets[band]
            if not rows:
                continue
            if band == "CLEAR" and not show_clear:
                continue
            style = _bucket_header_style(band)
            console.print(f"[{style}] {band} [/{style}]  [dim]({len(rows)})[/dim]")
            for c in rows:
                top_signal = c.signals[0].headline if c.signals else "no flags"
                console.print(f"  [cyan]{c.path}[/cyan]   [dim]{top_signal}[/dim]")
            console.print()
        if clear_n and not show_clear:
            console.print(
                f"[dim]+ {clear_n} file(s) with no risk signals — "
                "pass --show-clear to list[/dim]"
            )

    if threshold is not None:
        breaches = [c for c in cards if c.score.value >= threshold]
        if breaches:
            err.print(
                f"[red]fail-on:[/red] {len(breaches)} file(s) at or above "
                f"[bold]{fail_on}[/bold] (≥{threshold})."
            )
            raise typer.Exit(1)


@app.command()
def highlights(
    invariants: int = typer.Option(
        5, "--invariants", help="How many invariant lines to surface."
    ),
    incidents: int = typer.Option(
        5, "--incidents", help="How many incident commits to surface."
    ),
    max_commits: int | None = typer.Option(
        None,
        "--max-commits",
        help="Cap on commits scanned (defaults to no cap; tune for very large repos).",
    ),
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a card."
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local SQLite cache at .whycode/cache.db.",
    ),
) -> None:
    """Repo-wide treasure map: top invariants + recent incidents.

    Use after the first run when ``whycode tour`` is too verbose. Same
    invariant + incident sections as ``tour``, without the slim risk scan
    or the MCP setup snippet.
    """
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    cache = _open_cache(repo_root, no_cache)
    try:
        with console.status("Reading repo history…", spinner="dots"):
            commits = gf.all_commits(repo_root, max_count=max_commits, cache=cache)
    finally:
        if cache is not None:
            cache.close()
    if not commits:
        console.print("[yellow]no commits in this repo[/yellow]")
        return

    inv_pairs = gf.extract_invariant_quotes(commits)
    sha_to_commit = {c.sha: c for c in commits}
    deduped = gf.dedupe_invariant_lines(inv_pairs, sha_to_commit)
    inv_records: list[tuple[str, str, gf.Commit]] = []
    for sha, line in deduped:
        commit = sha_to_commit.get(sha)
        if commit is None:
            continue
        inv_records.append((line, sha, commit))
    # Sort newest first; on identical timestamps fall back to lexicographically
    # smallest sha so cache and --no-cache emit byte-identical output.
    inv_records.sort(key=lambda t: t[1])  # secondary: sha asc
    inv_records.sort(key=lambda t: t[2].authored_at, reverse=True)  # primary
    inv_records = inv_records[:invariants]

    incident_records = gf.find_incidents(commits)[:incidents]

    if json_out:
        console.print_json(
            json.dumps(
                {
                    "repo": str(repo_root),
                    "scanned_commits": len(commits),
                    "invariants": [
                        {
                            "sha": c.sha[:12],
                            "subject": c.subject,
                            "author": c.author_name,
                            "authored_at": c.authored_at.isoformat(),
                            "line": line,
                        }
                        for line, _, c in inv_records
                    ],
                    "incidents": [
                        {
                            "sha": c.sha[:12],
                            "subject": c.subject,
                            "author": c.author_name,
                            "authored_at": c.authored_at.isoformat(),
                        }
                        for c in incident_records
                    ],
                }
            )
        )
        return

    console.print(
        f"[bold]WhyCode highlights[/bold]   "
        f"[dim]{len(commits)} commits scanned in this repo[/dim]\n"
    )
    if inv_records:
        console.print(
            f"[bold yellow]INVARIANTS[/bold yellow] "
            f"[dim]({len(inv_records)} most recent stated by past authors)[/dim]"
        )
        for i, (line, sha, commit) in enumerate(inv_records, 1):
            short = sha[:7]
            date = str(commit.authored_at.date())
            console.print(
                f"  {i}. [dim]{short}  {date}  {commit.author_name}[/dim]"
            )
            console.print(f"     [italic]{line}[/italic]")
        console.print()
    else:
        console.print(
            "[dim]INVARIANTS: none found. The repo's commit bodies don't use "
            "cautionary language like 'do not', 'must not', 'important:', etc.[/dim]\n"
        )

    if incident_records:
        console.print(
            f"[bold red]INCIDENTS[/bold red] "
            f"[dim]({len(incident_records)} most recent incident-flavoured commits)[/dim]"
        )
        for i, c in enumerate(incident_records, 1):
            short = c.sha[:7]
            date = str(c.authored_at.date())
            subj = c.subject if len(c.subject) <= 70 else c.subject[:69] + "…"
            console.print(
                f"  {i}. [dim]{short}  {date}  {c.author_name}[/dim]\n"
                f"     {subj}"
            )
        console.print()
    else:
        console.print(
            "[dim]INCIDENTS: none found. No commit subject contains 'hotfix', "
            "'incident', 'regression', etc.[/dim]\n"
        )

    console.print(
        "[dim]→ whycode why <path>   to dig into the file behind any of these.[/dim]"
    )


def _sample_indices(total: int, max_samples: int) -> list[int]:
    """Pick at most ``max_samples`` indices spread across [0, total).

    Always includes both endpoints when there are at least two items, so the
    timeline shows the file's first and most recent state.
    """
    if total <= max_samples:
        return list(range(total))
    if max_samples < 2:
        return [total - 1]
    step = (total - 1) / (max_samples - 1)
    return sorted({round(i * step) for i in range(max_samples)})


@app.command()
def timeline(
    path: str = typer.Argument(..., help="File path to inspect."),
    samples: int = typer.Option(
        15, "--samples", help="Maximum number of points sampled across history."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Show how this file's risk score evolved over its history."""
    repo_root, rel = _require_tracked(path)

    commits = gf.commits_for_path(repo_root, rel)
    if not commits:
        console.print(f"[yellow]no history for {rel}[/yellow]")
        return

    # Order chronologically (oldest first) so the table reads left-to-right.
    chronological = list(reversed(commits))
    indices = _sample_indices(len(chronological), samples)
    sampled = [chronological[i] for i in indices]

    rows: list[tuple[str, str, int, str, str]] = []
    with console.status(
        f"Computing risk at {len(sampled)} points…", spinner="dots"
    ):
        for c in sampled:
            try:
                card = rc.build(repo_root, rel, ref=c.sha)
            except gf.GitError:
                continue
            top = card.signals[0].headline if card.signals else "—"
            rows.append(
                (
                    str(c.authored_at.date()),
                    c.sha[:7],
                    card.score.value,
                    card.score.band.value,
                    top,
                )
            )
    # Field-test report F14: ``timeline`` used to render rows in whatever
    # non-monotonic order ``_sample_indices`` produced (uniform-across-index
    # selection on a list whose ordering is git's parent traversal).  Sort
    # by date ascending before rendering so a reader can scan left-to-right
    # without misreading the trajectory.
    rows.sort(key=lambda r: r[0])

    if json_out:
        console.print_json(
            json.dumps(
                {
                    "path": rel,
                    "samples": [
                        {
                            "date": r[0],
                            "sha": r[1],
                            "score": r[2],
                            "band": r[3],
                            "top_signal": r[4],
                        }
                        for r in rows
                    ],
                }
            )
        )
        return

    table = Table(title=f"Risk timeline for {rel}")
    table.add_column("date")
    table.add_column("sha")
    table.add_column("score", justify="right", style="bold")
    table.add_column("band")
    table.add_column("top signal")
    for date_s, sha_s, score_v, band_s, top_s in rows:
        table.add_row(date_s, sha_s, str(score_v), band_s, top_s)
    console.print(table)
    console.print(
        f"[dim]{len(commits)} commit(s) total; sampled {len(rows)}. "
        f"Use --samples N to change.[/dim]"
    )


def _resolve_since(repo_root: Path, since: str) -> datetime:
    """Translate a ``--since`` argument into a timezone-aware datetime.

    The user can pass either an ISO date (``2024-01-01``, ``2024-01-01T00:00``,
    or fully qualified) or a git ref (``v1.0.0``, ``HEAD~50``, a SHA…). ISO
    parsing is tried first because pure-date strings are the common case;
    on failure we ask git to resolve the ref and read its authored timestamp.
    """
    from datetime import UTC

    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    commit = gf.read_commit(repo_root, since)
    if commit is None:
        raise typer.BadParameter(
            f"could not parse {since!r} as an ISO date or resolve it to a git ref."
        )
    return commit.authored_at


@app.command()
def story(
    path: str = typer.Argument(..., help="File path to render the story for."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead."
    ),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Emit GitHub-flavoured markdown for PR comments.",
    ),
    limit: int = typer.Option(
        12, "--top", help="Cap returned chapters (default 12)."
    ),
    all_chapters: bool = typer.Option(
        False, "--all", help="Show every chapter (escape hatch)."
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Drop chapters older than this ISO date (2024-01-01) or git ref.",
    ),
    roles: str | None = typer.Option(
        None,
        "--roles",
        help="Comma-separated role allowlist (e.g. 'revert,incident').",
    ),
    no_collapse: bool = typer.Option(
        False, "--no-collapse", help="Disable run-folding."
    ),
    body_max_chars: int = typer.Option(
        600, "--body-chars", help="Body excerpt max length."
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local SQLite cache at .whycode/cache.db.",
    ),
    oldest_first: bool = typer.Option(
        False,
        "--oldest-first",
        help="Render chapters oldest first (causality reads forward).",
    ),
    decisions_only: bool = typer.Option(
        False,
        "--decisions",
        help=(
            "Show only decision-grade chapters: revert, incident, deprecate, "
            "reconciliation, pivot, invariant. Equivalent to "
            "--roles revert,incident,deprecate,reconciliation,pivot,invariant."
        ),
    ),
) -> None:
    """Render the file's mindflow — chapters in chronological order."""
    repo_root, rel = _require_tracked(path)

    since_dt: datetime | None = None
    if since is not None:
        since_dt = _resolve_since(repo_root, since)

    role_tuple: tuple[str, ...] | None = None
    if roles is not None:
        role_tuple = tuple(r.strip() for r in roles.split(",") if r.strip())
        if not role_tuple:
            role_tuple = None
    # ``--decisions`` is a shortcut for the canonical decision-grade
    # allowlist; explicit ``--roles`` always wins so a user can narrow the
    # set further (e.g. ``--decisions --roles invariant``).
    if decisions_only and role_tuple is None:
        role_tuple = ("revert", "incident", "deprecate", "reconciliation", "pivot", "invariant")

    cache = _open_cache(repo_root, no_cache)
    try:
        facts = gf.gather(repo_root, rel, cache=cache)
    finally:
        if cache is not None:
            cache.close()

    s = st.build_story(
        facts,
        limit=None if all_chapters else limit,
        roles=role_tuple,
        since=since_dt,
        no_collapse=no_collapse,
        body_max_chars=body_max_chars,
        all_chapters=all_chapters,
    )

    if json_out:
        payload = st.to_dict(s, body_max_chars=body_max_chars)
        # ``index`` on each chapter retains its newest-first meaning so
        # cross-references (``cited_prior_indices`` / ``cited_by_indices``)
        # still point at the same chapter regardless of array order.
        if oldest_first:
            payload["chapters"] = list(reversed(payload["chapters"]))
        console.print_json(json.dumps(payload))
        return
    if oldest_first:
        s = dc_replace(s, chapters=tuple(reversed(s.chapters)))
    if markdown:
        # Use plain ``print`` rather than ``console.print`` so the markdown
        # body is not rich-rewrapped — a pasted PR comment must keep its
        # ``## headings`` and ``> blockquotes`` byte-for-byte.
        print(st.render_markdown(s), end="")
        return
    console.print(st.render_text(s))


@app.command()
def scan(
    top: int = typer.Option(10, "--top", help="How many files to list."),
    sample: int = typer.Option(
        500,
        "--sample",
        help="Cap on tracked files to evaluate (for very large repos).",
    ),
    scan_depth: int = typer.Option(
        200,
        "--scan-depth",
        help=(
            "Cap commits-per-file scanned (controls scan speed). "
            "Use 0 for no cap (slow on large repos)."
        ),
    ),
    no_ignore: bool = typer.Option(
        False,
        "--no-ignore",
        help="Bypass the default-ignore list and scan everything (CHANGELOGs, lockfiles, vendored).",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help=(
            "Bypass the local SQLite cache at .whycode/cache.db. Mostly "
            "useful for benchmarking and for forcing a fresh git read."
        ),
    ),
    repo: Path = typer.Option(
        Path("."), "--repo", help="Path inside the repo (defaults to cwd)."
    ),
) -> None:
    """List the top-N files with the highest risk scores in the repo."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    raw = gf.run_git(repo_root, "ls-files")
    all_paths = [line for line in raw.splitlines() if line.strip()]
    patterns = () if no_ignore else ign.effective_patterns(repo_root)
    paths = [p for p in all_paths if not ign.is_ignored(p, patterns)][:sample]
    if not paths:
        console.print("[yellow]no tracked files found[/yellow]")
        raise typer.Exit(0)

    depth_cap = scan_depth if scan_depth > 0 else None
    cards: list[rc.RiskCard] = []
    cache = _open_cache(repo_root, no_cache)
    try:
        with console.status(f"Scanning {len(paths)} files…", spinner="dots"):
            for p in paths:
                try:
                    card = rc.build(repo_root, p, max_commits=depth_cap, cache=cache)
                except gf.GitError:
                    continue
                # Skip files whose only signal is NEWBORN — that's "not enough
                # history yet", not real risk. `scan` is for surfacing risk;
                # informational signals don't belong here.
                useful = [
                    s for s in card.signals if s.kind is not sig.SignalKind.NEWBORN
                ]
                if useful:
                    cards.append(card)
    finally:
        if cache is not None:
            cache.close()

    cards.sort(key=lambda c: (-c.score.value, c.path))
    top_cards = cards[:top]
    if not top_cards:
        # Be honest about what "no flagged files" actually means. A user who
        # just installed WhyCode and sees a one-line "nothing fired" walks away
        # thinking the tool is broken. Spell out the two real possibilities.
        console.print(
            f"[green]No flagged files among {len(paths)} scanned.[/green]\n\n"
            "WhyCode reads commit messages, reverts and authorship to find risk.\n"
            "A clean output means [bold]one of:[/bold]\n"
            "  • Your repo's history is genuinely quiet, or\n"
            "  • Commits are too terse for WhyCode to learn from "
            "(e.g. only \"fix\", \"update\", \"wip\")."
        )
        return

    table = Table(title=f"Top {len(top_cards)} flagged files")
    table.add_column("score", justify="right", style="bold")
    table.add_column("band")
    table.add_column("path")
    table.add_column("top signal")
    for c in top_cards:
        top_signal = c.signals[0].headline if c.signals else "—"
        table.add_row(str(c.score.value), c.score.band.value, c.path, top_signal)
    console.print(table)


@app.command()
def honest(
    path: str = typer.Argument(..., help="File path to inspect."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of prose."),
) -> None:
    """Print every invariant line in this file's history, verbatim and untruncated.

    Use when the Risk Card's first-sentence truncation is hiding important
    context — e.g., a commit whose constraint is stated across two lines.
    """
    repo_root, rel = _require_tracked(path)
    if not json_out and ign.is_ignored(rel, ign.effective_patterns(repo_root)):
        err.print(
            f"[yellow]heads up:[/yellow] [bold]{rel}[/bold] matches the default "
            f"ignore list. Invariant lines extracted from a metadata file "
            f"(changelogs, lockfiles, generated code, vendored trees, docs build "
            f"output) are usually noise; treat the output below as advisory."
        )
    facts = gf.gather(repo_root, rel)
    if not facts.invariant_quotes:
        if json_out:
            console.print_json(json.dumps({"path": rel, "invariants": []}))
        else:
            console.print(
                f"[dim]No invariants found in the history of {rel}.[/dim]"
            )
        return

    sha_to_commit: dict[str, gf.Commit] = {c.sha: c for c in facts.commits}
    grouped: dict[str, list[str]] = {}
    sha_order: list[str] = []
    for sha, line in facts.invariant_quotes:
        if sha not in grouped:
            grouped[sha] = []
            sha_order.append(sha)
        grouped[sha].append(line)

    if json_out:
        invariants = []
        for sha in sha_order:
            entry: dict[str, Any] = {"sha": sha[:12], "lines": grouped[sha]}
            commit = sha_to_commit.get(sha)
            if commit is not None:
                entry["subject"] = commit.subject
                entry["author"] = commit.author_name
                entry["authored_at"] = commit.authored_at.isoformat()
            invariants.append(entry)
        console.print_json(json.dumps({"path": rel, "invariants": invariants}))
        return

    total = sum(len(v) for v in grouped.values())
    console.print(
        f"[bold]{total} invariant line(s) across {len(sha_order)} commit(s) "
        f"in {rel}:[/bold]\n"
    )
    for sha in sha_order:
        commit = sha_to_commit.get(sha)
        if commit is not None:
            header = (
                f"[bold]{sha[:7]}[/bold]  {commit.authored_at.date()}  "
                f"{commit.author_name}  ·  {commit.subject}"
            )
        else:
            header = f"[bold]{sha[:7]}[/bold]"
        console.print(header)
        for line in grouped[sha]:
            console.print(f"  > {line}")
        console.print()


@app.command()
def show(
    sha: str = typer.Argument(..., help="Commit SHA (full or short) to inspect."),
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a card."),
    full: bool = typer.Option(
        False,
        "--full",
        help="Show the entire commit body and the full diff instead of truncating.",
    ),
    show_diff: bool = typer.Option(
        False,
        "--diff",
        help="Include the unified diff (truncated unless --full is also set).",
    ),
) -> None:
    """Risk-flavored summary for a single commit: classification + per-file risk."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    commit = gf.read_commit(repo_root, sha)
    if commit is None:
        err.print(f"[red]error:[/red] could not read commit {sha!r}")
        raise typer.Exit(2)
    full_sha = commit.sha

    # Walk a recent window of commits so the cross-reference check on this
    # commit's body finds prior SHAs that actually exist in the repo. 5000
    # commits covers most "narrative bridge" references; on tiny repos it
    # walks them all in milliseconds.
    known_repo_commits = gf.all_commits(repo_root, max_count=5000)
    known_shas = {c.sha for c in known_repo_commits}
    file_changes = gf.files_changed_in(repo_root, full_sha)
    # Look up the file's previous commit so the silence_break chapter check
    # can compute the gap. We reuse the first changed (tracked) file as the
    # representative — for the bulk of "single-file commit" cases that's
    # exact; for multi-file pivots it's an approximation that still answers
    # the question "did this file's history just wake up after a long
    # quiet?". A commit that touches no file at all (rare; pure --allow-empty)
    # leaves ``prior_commit_at`` as None.
    prior_commit_at: datetime | None = None
    for change in file_changes:
        try:
            history = gf.commits_for_path(repo_root, change.path)
        except gf.GitError:
            continue
        prior: gf.Commit | None = None
        seen_target = False
        for past in history:
            if past.sha == full_sha:
                seen_target = True
                continue
            if seen_target:
                prior = past
                break
        if prior is not None:
            prior_commit_at = prior.authored_at
            break
    classification = gf.classify_commit(
        commit,
        known_shas=known_shas,
        prior_commit_at=prior_commit_at,
    )
    is_incident = classification.incident_flavoured
    invariants = gf.extract_invariant_quotes([commit])

    show_patterns = ign.effective_patterns(repo_root)
    cards: list[rc.RiskCard] = []
    for change in file_changes:
        if ign.is_ignored(change.path, show_patterns):
            continue
        try:
            cards.append(rc.build(repo_root, change.path))
        except gf.GitError:
            continue
    cards.sort(key=lambda c: (-c.score.value, c.path))

    if json_out:
        console.print_json(
            json.dumps(
                {
                    "sha": full_sha[:12],
                    "subject": commit.subject,
                    "author": commit.author_name,
                    "authored_at": commit.authored_at.isoformat(),
                    "incident_flavored": is_incident,
                    "invariants_stated": len(invariants),
                    "is_revert": classification.is_revert,
                    "cites_prior_sha": classification.cites_prior_sha,
                    "breaks_silence": classification.breaks_silence,
                    "is_pivot": classification.is_pivot,
                    "pivot_kind": classification.pivot_kind,
                    "chapter_role": classification.chapter_role,
                    "files_changed": len(file_changes),
                    "files": [c.to_dict() for c in cards],
                }
            )
        )
        return

    console.print(
        f"[bold]{full_sha[:12]}[/bold]  {commit.author_name}  "
        f"{commit.authored_at.date()}"
    )
    console.print(f"  {commit.subject}")
    console.print()
    role = classification.chapter_role
    role_style = {
        "revert": "bold bright_red",
        "incident": "bold red",
        "deprecate": "bold yellow",
        "reconciliation": "bold magenta",
        "pivot": "bold magenta",
        "invariant": "bold yellow",
        "silence_break": "bold cyan",
        "edit": "dim",
    }.get(role, "dim")
    console.print(f"  [bold]Role:[/bold] [{role_style}]{role.upper()}[/{role_style}]")
    badges: list[str] = []
    if is_incident:
        badges.append("[bold red]incident-flavored[/bold red]")
    if invariants:
        badges.append(f"[yellow]states {len(invariants)} invariant(s)[/yellow]")
    if classification.is_revert:
        badges.append("[bright_red]revert[/bright_red]")
    if classification.cites_prior_sha:
        badges.append("[magenta]cites prior SHA[/magenta]")
    if badges:
        console.print("  " + "   ".join(badges))
    console.print(f"  [dim]{len(file_changes)} files changed[/dim]")

    body = (commit.body or "").strip("\n")
    if body:
        console.print()
        body_lines = body.splitlines()
        if not full and (len(body_lines) > 12 or len(body) > 600):
            visible = body_lines[:12]
            shown = "\n".join(visible)
            if len(shown) > 600:
                shown = shown[:600].rstrip() + "…"
            console.print("  [bold]Body:[/bold]")
            for line in shown.splitlines():
                console.print(f"    {line}")
            remaining_lines = len(body_lines) - len(visible)
            if remaining_lines > 0:
                console.print(
                    f"    [dim]…{remaining_lines} more line(s) — pass --full to see them.[/dim]"
                )
        else:
            console.print("  [bold]Body:[/bold]")
            for line in body.splitlines():
                console.print(f"    {line}")

    if cards:
        console.print()
        table = Table(title="Files in this commit, by current risk")
        table.add_column("score", justify="right", style="bold")
        table.add_column("band")
        table.add_column("path")
        table.add_column("+/-", justify="right")
        table.add_column("top signal")
        change_by_path = {fc.path: fc for fc in file_changes}
        for c in cards[:20]:
            top = c.signals[0].headline if c.signals else "—"
            ch = change_by_path.get(c.path)
            churn = (
                f"[green]+{ch.insertions}[/green]/[red]-{ch.deletions}[/red]"
                if ch is not None
                else "—"
            )
            table.add_row(str(c.score.value), c.score.band.value, c.path, churn, top)
        console.print(table)

    if show_diff:
        console.print()
        try:
            diff_text = gf.run_git(repo_root, "show", "--no-color", "--format=", full_sha)
        except gf.GitError as exc:
            err.print(f"[dim]could not read diff: {exc}[/dim]")
        else:
            diff_lines = diff_text.splitlines()
            if not full and len(diff_lines) > 60:
                console.print("[bold]Diff (first 60 lines):[/bold]")
                for line in diff_lines[:60]:
                    console.print(f"  {line}")
                console.print(
                    f"  [dim]…{len(diff_lines) - 60} more line(s) — pass --full to see them.[/dim]"
                )
            else:
                console.print("[bold]Diff:[/bold]")
                for line in diff_lines:
                    console.print(f"  {line}")


def _install_template(
    template_name: str,
    dst: Path,
    repo_root: Path,
    *,
    force: bool,
    executable: bool,
) -> str:
    """Copy a packaged template to ``dst``. Returns a one-line status."""
    from importlib.resources import files

    rel_label = str(dst.relative_to(repo_root)) if dst.is_relative_to(repo_root) else str(dst)
    if dst.exists() and not force:
        return f"[dim]skipped:[/dim] {rel_label}  (exists; use --force to overwrite)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload = (files("whycode") / "templates" / template_name).read_text()
    dst.write_text(payload)
    if executable:
        dst.chmod(0o755)
    return f"[green]wrote:[/green]   {rel_label}"


_MCP_SNIPPET = '''    {
      "mcpServers": {
        "whycode": {"command": "whycode", "args": ["mcp"]}
      }
    }'''


@app.command()
def tour(
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local SQLite cache at .whycode/cache.db.",
    ),
) -> None:
    """First-run walkthrough: highlights + top risky files + MCP setup snippet.

    The single command to run after installing WhyCode. Skips straight to
    the most concrete things in the repo (verbatim invariants and
    incident-flagged commits) and ends with the one snippet you'll need to
    wire WhyCode into an MCP-aware editor.
    """
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    console.print("[bold]Welcome to WhyCode.[/bold]")
    console.print(f"[dim]Reading the history of {repo_root.name}…[/dim]\n")

    cache = _open_cache(repo_root, no_cache)
    try:
        # Section 1 — invariants and incidents (cheap; one git log call).
        with console.status("Looking for stated decisions…", spinner="dots"):
            commits = gf.all_commits(repo_root, max_count=2000, cache=cache)
        if not commits:
            console.print(
                "[yellow]This repo has no commits yet — nothing to learn from.[/yellow]"
            )
            return

        inv_pairs = gf.extract_invariant_quotes(commits)
        sha_to_commit = {c.sha: c for c in commits}
        deduped = gf.dedupe_invariant_lines(inv_pairs, sha_to_commit)
        # Sort newest first with sha-asc tie-break so cache and --no-cache
        # surface the same three lines in the same order.
        deduped_sorted = sorted(
            (p for p in deduped if p[0] in sha_to_commit),
            key=lambda p: p[0],
        )
        deduped_sorted.sort(
            key=lambda p: sha_to_commit[p[0]].authored_at, reverse=True
        )
        invariants_top = [
            (line, sha_to_commit[sha]) for sha, line in deduped_sorted
        ][:3]
        incidents_top = gf.find_incidents(commits)[:3]

        if invariants_top or incidents_top:
            # Field-test report F16: the original tour rendered both classes
            # under one ``Decisions and incidents`` header, so a parenthetical
            # invariant prose line was visually indistinguishable from a real
            # incident commit. Render two subheads matching the layout
            # ``highlights`` already uses.
            if invariants_top:
                console.print(
                    f"[bold yellow]Stated invariants[/bold yellow] "
                    f"[dim]({len(invariants_top)} most recent)[/dim]"
                )
                for line, c in invariants_top:
                    console.print(f"  [italic]{line}[/italic]")
                    console.print(
                        f"  [dim]{c.sha[:7]}  {c.authored_at.date()}  "
                        f"{c.author_name}[/dim]\n"
                    )
            if incidents_top:
                console.print(
                    f"[bold red]Recent incidents[/bold red] "
                    f"[dim]({len(incidents_top)} most recent)[/dim]"
                )
                for c in incidents_top:
                    subj = c.subject if len(c.subject) <= 70 else c.subject[:69] + "…"
                    console.print(f"  [red]{subj}[/red]")
                    console.print(
                        f"  [dim]{c.sha[:7]}  {c.authored_at.date()}  "
                        f"{c.author_name}[/dim]\n"
                    )
        else:
            console.print(
                "[dim]No headline decisions or incidents in recent history.[/dim]"
            )
            console.print(
                "[dim]Commit messages may be too terse — describing 'why' in commit "
                "bodies (or using `hotfix:` / `BREAKING CHANGE:` prefixes) makes WhyCode "
                "much more useful.[/dim]\n"
            )

        # Section 2 — top risky files. Slimmer scan: 100 files, depth 50 commits.
        raw = gf.run_git(repo_root, "ls-files")
        patterns = ign.effective_patterns(repo_root)
        paths = [
            p for p in raw.splitlines() if p.strip() and not ign.is_ignored(p, patterns)
        ][:100]
        cards: list[rc.RiskCard] = []
        if paths:
            with console.status(
                f"Risk-ranking {len(paths)} files (slim scan)…", spinner="dots"
            ):
                for p in paths:
                    try:
                        card = rc.build(repo_root, p, max_commits=50, cache=cache)
                    except gf.GitError:
                        continue
                    useful = [
                        s for s in card.signals if s.kind is not sig.SignalKind.NEWBORN
                    ]
                    if useful:
                        cards.append(card)
            cards.sort(key=lambda c: (-c.score.value, c.path))

        if cards:
            top_cards = cards[:3]
            # Group the top 3 under their band headings — same shape the
            # diff command uses, applied to a much shorter list. Three rows
            # is small enough that the bucket headers don't feel
            # bureaucratic; the visual consistency with ``whycode diff`` is
            # what's worth the extra line.
            console.print("[bold red]Top 3 risky files[/bold red]")
            top_buckets = _group_into_buckets(list(top_cards))
            for band in _BUCKET_ORDER:
                rows = top_buckets[band]
                if not rows or band == "CLEAR":
                    continue
                style = _bucket_header_style(band)
                console.print(
                    f"  [{style}] {band} [/{style}]  [dim]({len(rows)})[/dim]"
                )
                for top in rows:
                    console.print(f"    [cyan]{top.path}[/cyan]")
                    console.print(f"      [dim]{top.signals[0].headline}[/dim]")
            console.print()

        # Section 3 — MCP setup snippet (vendor-neutral phrasing).
        console.print("[bold magenta]Wire WhyCode into your AI editor[/bold magenta]")
        console.print(
            "  WhyCode ships an MCP server. Any MCP-aware editor or assistant\n"
            "  can call it — just add this snippet to your editor's MCP config:\n"
        )
        console.print(_MCP_SNIPPET)
        console.print(
            "\n  [dim]The config file is typically named ``mcp.json`` (or "
            "the editor's main settings JSON). See your editor's MCP docs "
            "for the exact location.[/dim]\n"
        )

        # Section 4 — what to do next. The first suggestion substitutes
        # the actual top-risk path the tour just identified so a user can
        # paste the next command verbatim instead of hunting for one of
        # the names in the table above. When the slim scan found nothing,
        # we degrade to a generic ``<path>`` placeholder rather than
        # omitting the line — every fresh tour ends with the same shape.
        console.print("[bold]Next:[/bold]")
        if cards:
            console.print(
                f"  [dim]·[/dim] [bold]whycode why {cards[0].path}[/bold]"
                "   the full Risk Card"
            )
        else:
            console.print(
                "  [dim]·[/dim] [bold]whycode why <path>[/bold]"
                "                the Risk Card for any tracked file"
            )
        console.print(
            "  [dim]·[/dim] [bold]whycode init[/bold]                     install CI + pre-commit"
        )
        console.print(
            "  [dim]·[/dim] [bold]whycode highlights[/bold]                more invariants and incidents"
        )
    finally:
        if cache is not None:
            cache.close()


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing files instead of skipping."
    ),
    repo: Path = typer.Option(
        Path("."), "--repo", help="Path inside the repo (defaults to cwd)."
    ),
) -> None:
    """One-command setup: install CI risk gate + local pre-commit hook."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    workflow_status = _install_template(
        "github-workflow.yml",
        repo_root / ".github" / "workflows" / "whycode.yml",
        repo_root,
        force=force,
        executable=False,
    )
    hook_status = _install_template(
        "pre-commit",
        repo_root / ".git" / "hooks" / "pre-commit",
        repo_root,
        force=force,
        executable=True,
    )

    console.print(workflow_status)
    console.print(hook_status)
    console.print()
    console.print("[bold]WhyCode is wired into this repo.[/bold]")
    console.print(
        "[dim]→ Run [bold]whycode diff --staged[/bold] to preview what the "
        "pre-commit hook will check.[/dim]"
    )
    console.print(
        "  [dim]local[/dim]  pre-commit blocks HANDLE WITH CARE commits "
        "(`git commit --no-verify` to bypass)"
    )
    console.print(
        "  [dim]ci[/dim]     .github/workflows/whycode.yml gates PRs "
        "(commit + push the workflow file to enable)"
    )


@app.command()
def mcp(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Log every tool call to stderr so you can verify the AI uses it.",
    ),
) -> None:
    """Start the MCP stdio server."""
    try:
        from whycode.mcp_server import serve
    except ImportError as exc:
        err.print(
            "[red]error:[/red] MCP support is not installed. "
            "Run [bold]pip install 'whycode[mcp]'[/bold]."
        )
        raise typer.Exit(2) from exc
    err.print(
        f"[dim]whycode mcp: stdio server ready (v{__version__}) — "
        f"connect from your editor. Pass [bold]-v[/bold] to log every tool call.[/dim]"
    )
    serve(verbose=verbose)


cache_app = typer.Typer(
    add_completion=False,
    help="Inspect and clear the local WhyCode cache (advanced).",
    no_args_is_help=True,
    hidden=True,
)
app.add_typer(cache_app, name="cache")


def _format_size(n: int) -> str:
    """Render a byte count for human eyeballs (KB / MB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@cache_app.command("stats")
def cache_stats(
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
) -> None:
    """Print the size, last seen HEAD, and commit count of the local cache."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    db_path = ch.cache_path_for(repo_root)
    if not db_path.exists():
        console.print(
            f"[dim]no cache yet at {db_path}.[/dim] "
            f"Run [bold]whycode scan[/bold] or [bold]highlights[/bold] to populate one."
        )
        return
    with ch.open_for(repo_root) as store:
        s = store.stats()
    console.print(f"[bold]cache:[/bold] {s.path}")
    console.print(f"  schema_version  {s.schema_version}")
    head = s.head_sha[:12] if s.head_sha else "[dim]not set[/dim]"
    console.print(f"  last seen HEAD  {head}")
    console.print(f"  commit rows     {s.commit_count}")
    console.print(f"  diffstat rows   {s.file_row_count}")
    console.print(f"  size on disk    {_format_size(s.size_bytes)}")


@cache_app.command("clear")
def cache_clear(
    repo: Path = typer.Option(Path("."), "--repo", help="Path inside the repo."),
) -> None:
    """Delete the local cache database. Next run rebuilds it from scratch."""
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    if ch.remove(repo_root):
        console.print(f"[green]cleared:[/green] {ch.cache_path_for(repo_root)}")
    else:
        console.print(
            f"[dim]nothing to clear — no cache at {ch.cache_path_for(repo_root)}[/dim]"
        )


@app.command()
def version() -> None:
    """Print the installed WhyCode version."""
    console.print(__version__)


def main() -> None:
    """Entry-point used by ``python -m whycode`` and tests."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
