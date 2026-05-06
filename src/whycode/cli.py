"""The ``whycode`` CLI.

Commands
--------
- ``whycode tour``              — first-run walkthrough: highlights + top risk + MCP setup.
- ``whycode why <path>``        — print the Risk Card for a single file.
- ``whycode why <path> --at SHA`` — risk card as of a past commit.
- ``whycode why <path> --mute KIND`` — locally suppress a noisy signal kind.
- ``whycode highlights``        — repo-wide treasure map of decisions and incidents.
- ``whycode diff [--base REF]`` — risk-rank files changed against a base ref.
- ``whycode show <sha>``        — risk-flavored summary for one commit.
- ``whycode timeline <path>``   — risk score evolution across the file's history.
- ``whycode honest <path>``     — every invariant line, verbatim, untruncated.
- ``whycode scan [--top N]``    — print the top-N riskiest files in the repo.
- ``whycode init``              — install CI workflow + pre-commit risk gate.
- ``whycode mcp``               — start the MCP stdio server.
- ``whycode version``           — print version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from whycode import __version__
from whycode import git_facts as gf
from whycode import ignore as ign
from whycode import risk_card as rc
from whycode import signals as sig
from whycode import suppressions as supp

app = typer.Typer(
    add_completion=False,
    help="WhyCode — tells you what to be afraid of before touching a file.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)


def _resolve_repo_and_path(path_arg: str) -> tuple[Path, str]:
    """Translate a user-provided path into (repo_root, repo-relative path)."""
    p = Path(path_arg).resolve()
    start = p if p.is_dir() else p.parent if p.exists() else Path.cwd()
    try:
        repo_root = gf.discover_repo_root(start)
    except gf.GitError as exc:
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
        err.print(
            f"[yellow]warning:[/yellow] [bold]{rel}[/bold] is not tracked by git "
            f"and has no history in this repo. Nothing to learn from."
        )
        raise typer.Exit(1)
    return repo_root, rel


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


def _print_brief(card: rc.RiskCard) -> None:
    """Print a one-line summary suitable for grep/awk and 3am triage."""
    top = card.signals[0].headline if card.signals else "no flags"
    console.print(
        f"{card.path}: [bold]{card.score.band.value}[/bold] "
        f"({card.score.value}/100) — {top}"
    )


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
            "(stored in .whycode/suppressed.json). Accepts kind name or "
            "unique prefix: incident, revert, ghost, coupling, silence, …"
        ),
    ),
    no_mutes: bool = typer.Option(
        False,
        "--no-mutes",
        help="Bypass the local suppression list — show all signals.",
    ),
    max_commits: int | None = typer.Option(
        None, "--max-commits", help="Cap the number of commits scanned (debug)."
    ),
) -> None:
    """Print the Risk Card for ``path``."""
    repo_root, rel = _require_tracked(path)
    resolved_ref: str | None = None
    if at is not None:
        try:
            resolved_ref = gf.run_git(
                repo_root, "rev-parse", "--verify", f"{at}^{{commit}}"
            ).strip()
        except gf.GitError:
            err.print(f"[red]error:[/red] unknown commit / ref: {at!r}")
            raise typer.Exit(2) from None
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
                    f"(stored in .whycode/suppressed.json — edit to undo)[/dim]"
                )
    card = rc.build(
        repo_root,
        rel,
        max_commits=max_commits,
        ref=resolved_ref,
        apply_suppressions=not no_mutes,
    )
    if json_out:
        console.print_json(json.dumps(card.to_dict()))
        return
    if brief:
        _print_brief(card)
        return
    console.print(rc.render_text(card))


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

    files = [line for line in raw.splitlines() if line.strip()]
    if not files:
        if json_out:
            console.print_json(json.dumps({"base": actual_base, "files": []}))
        else:
            scope = "staged" if staged else f"vs {actual_base}"
            console.print(f"[green]no changes {scope}[/green]")
        return

    cards: list[rc.RiskCard] = []
    for f in files:
        try:
            cards.append(rc.build(repo_root, f))
        except gf.GitError:
            continue
    cards.sort(key=lambda c: -c.score.value)
    cards = cards[:top]

    if json_out:
        console.print_json(
            json.dumps(
                {
                    "base": actual_base,
                    "files": [c.to_dict() for c in cards],
                }
            )
        )
        if threshold is not None and any(c.score.value >= threshold for c in cards):
            raise typer.Exit(1)
        return

    # NEWBORN-only files have no real risk signal — they're "we don't know yet".
    # Push them into the quiet bucket so the table only shows actionable risk.
    def _is_actionable(c: rc.RiskCard) -> bool:
        return any(s.kind is not sig.SignalKind.NEWBORN for s in c.signals)

    flagged = [c for c in cards if _is_actionable(c)]
    quiet_n = len(cards) - len(flagged)
    scope_md = "files staged for commit" if staged else f"files changed vs `{actual_base}`"
    if markdown:
        # Stable marker so a follow-up workflow step can find-and-update the
        # same comment on subsequent pushes instead of stacking new ones.
        print("<!-- whycode-comment -->")
        print("## WhyCode risk briefing")
        print()
        print(f"**{len(files)} {scope_md}**")
        print()
        if not flagged:
            print("Nothing flagged. Read the diff anyway.")
        else:
            print("| Score | Band | File | Top signal |")
            print("| ----: | ---- | ---- | ---------- |")
            for c in flagged:
                top_signal = c.signals[0].headline.replace("|", "\\|")
                print(
                    f"| {c.score.value} | {c.score.band.value} | "
                    f"`{c.path}` | {top_signal} |"
                )
            if quiet_n:
                print()
                print(f"_+ {quiet_n} file(s) changed with no flags._")
            print()
            print(
                "_Run `whycode why <path>` for the full Risk Card on any of the above._"
            )
        if threshold is not None and any(c.score.value >= threshold for c in cards):
            raise typer.Exit(1)
        return

    scope = "staged for commit" if staged else f"changed vs {actual_base}"
    console.print(f"[bold]{len(files)} file(s) {scope}[/bold]")
    if not flagged:
        console.print("[green]nothing flagged[/green] — but read the diff anyway.")
        return
    table = Table(title="Risk-ranked changes")
    table.add_column("score", justify="right", style="bold")
    table.add_column("band")
    table.add_column("path")
    table.add_column("top signal")
    for c in flagged:
        table.add_row(
            str(c.score.value),
            c.score.band.value,
            c.path,
            c.signals[0].headline,
        )
    console.print(table)
    if quiet_n:
        console.print(f"[dim]+ {quiet_n} file(s) changed with no flags[/dim]")
    console.print(
        "[dim]→ whycode why <path>   for the full Risk Card on any of the above[/dim]"
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
) -> None:
    """The first-run treasure map: top decisions and incidents across the repo.

    Surfaces the highest-value commit-message lines (invariants stated by past
    authors) and the most recent incident-flavoured commits — the things a
    reader most wants to know about the codebase before touching anything.
    """
    try:
        repo_root = gf.discover_repo_root(repo.resolve())
    except gf.GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    with console.status("Reading repo history…", spinner="dots"):
        commits = gf.all_commits(repo_root, max_count=max_commits)
    if not commits:
        console.print("[yellow]no commits in this repo[/yellow]")
        return

    inv_pairs = gf.extract_invariant_quotes(commits)
    sha_to_commit = {c.sha: c for c in commits}
    seen_lines: dict[str, str] = {}
    for sha, line in inv_pairs:
        seen_lines.setdefault(line, sha)
    inv_records: list[tuple[str, str, gf.Commit]] = []
    for line, sha in seen_lines.items():
        commit = sha_to_commit.get(sha)
        if commit is None:
            continue
        inv_records.append((line, sha, commit))
    inv_records.sort(key=lambda t: t[2].authored_at, reverse=True)
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
            "cautionary language like 'do not', 'must not', 'warning:', etc.[/dim]\n"
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
    with console.status(f"Scanning {len(paths)} files…", spinner="dots"):
        for p in paths:
            try:
                card = rc.build(repo_root, p, max_commits=depth_cap)
            except gf.GitError:
                continue
            # Skip files whose only signal is NEWBORN — that's "not enough
            # history yet", not real risk. `scan` is for surfacing risk;
            # informational signals don't belong here.
            useful = [s for s in card.signals if s.kind is not sig.SignalKind.NEWBORN]
            if useful:
                cards.append(card)

    cards.sort(key=lambda c: -c.score.value)
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

    classification = gf.classify_commit(commit)
    is_incident = classification.incident_flavoured
    invariants = gf.extract_invariant_quotes([commit])
    file_changes = gf.files_changed_in(repo_root, full_sha)

    cards: list[rc.RiskCard] = []
    for change in file_changes:
        try:
            cards.append(rc.build(repo_root, change.path))
        except gf.GitError:
            continue
    cards.sort(key=lambda c: -c.score.value)

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
    badges: list[str] = []
    if is_incident:
        badges.append("[bold red]incident-flavored[/bold red]")
    if invariants:
        badges.append(f"[yellow]states {len(invariants)} invariant(s)[/yellow]")
    if not badges:
        badges.append("[dim]no special classification[/dim]")
    console.print("  " + "   ".join(badges))
    console.print(f"  [dim]{len(file_changes)} files changed[/dim]")

    if not cards:
        return
    table = Table(title="Files in this commit, by current risk")
    table.add_column("score", justify="right", style="bold")
    table.add_column("band")
    table.add_column("path")
    table.add_column("top signal")
    for c in cards[:20]:
        top = c.signals[0].headline if c.signals else "—"
        table.add_row(str(c.score.value), c.score.band.value, c.path, top)
    console.print(table)


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

    # Section 1 — invariants and incidents (cheap; one git log call).
    with console.status("Looking for stated decisions…", spinner="dots"):
        commits = gf.all_commits(repo_root, max_count=2000)
    if not commits:
        console.print("[yellow]This repo has no commits yet — nothing to learn from.[/yellow]")
        return

    inv_pairs = gf.extract_invariant_quotes(commits)
    sha_to_commit = {c.sha: c for c in commits}
    seen_lines: dict[str, str] = {}
    for sha, line in inv_pairs:
        seen_lines.setdefault(line, sha)
    invariants_top = [
        (line, sha_to_commit[sha])
        for line, sha in seen_lines.items()
        if sha in sha_to_commit
    ][:3]
    incidents_top = gf.find_incidents(commits)[:3]

    if invariants_top or incidents_top:
        console.print("[bold yellow]Decisions and incidents[/bold yellow]")
        for line, c in invariants_top:
            console.print(f"  [italic]{line}[/italic]")
            console.print(
                f"  [dim]{c.sha[:7]}  {c.authored_at.date()}  {c.author_name}[/dim]\n"
            )
        for c in incidents_top:
            subj = c.subject if len(c.subject) <= 70 else c.subject[:69] + "…"
            console.print(f"  [red]{subj}[/red]")
            console.print(
                f"  [dim]{c.sha[:7]}  {c.authored_at.date()}  {c.author_name}[/dim]\n"
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
    paths = [p for p in raw.splitlines() if p.strip() and not ign.is_ignored(p, patterns)][
        :100
    ]
    cards: list[rc.RiskCard] = []
    if paths:
        with console.status(
            f"Risk-ranking {len(paths)} files (slim scan)…", spinner="dots"
        ):
            for p in paths:
                try:
                    card = rc.build(repo_root, p, max_commits=50)
                except gf.GitError:
                    continue
                useful = [s for s in card.signals if s.kind is not sig.SignalKind.NEWBORN]
                if useful:
                    cards.append(card)
        cards.sort(key=lambda c: -c.score.value)

    if cards:
        console.print("[bold red]Top 3 risky files[/bold red]")
        for top in cards[:3]:
            console.print(
                f"  [bold]{top.score.value:>3}[/bold]  "
                f"{top.score.band.value:<20}  [cyan]{top.path}[/cyan]"
            )
            console.print(f"       [dim]{top.signals[0].headline}[/dim]")
        console.print()

    # Section 3 — MCP setup snippet (vendor-neutral phrasing).
    console.print("[bold magenta]Wire WhyCode into your AI editor[/bold magenta]")
    console.print(
        "  WhyCode ships an MCP server. Any MCP-aware editor or assistant\n"
        "  can call it — just add this snippet to your editor's MCP config:\n"
    )
    console.print(_MCP_SNIPPET)
    console.print(
        "\n  [dim](See your editor's docs for the exact config-file location.)[/dim]\n"
    )

    # Section 4 — what to do next.
    console.print("[bold]Next:[/bold]")
    if cards:
        console.print(
            f"  [dim]·[/dim] [bold]whycode why {cards[0].path}[/bold]   the full Risk Card"
        )
    console.print(
        "  [dim]·[/dim] [bold]whycode init[/bold]                     install CI + pre-commit"
    )
    console.print(
        "  [dim]·[/dim] [bold]whycode highlights[/bold]                more invariants and incidents"
    )


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
    serve(verbose=verbose)


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
