# Changelog

All notable changes to WhyCode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.4] — 2026-05-06

### Internal — tighter public API boundary

A code-audit pass found 12 sites where ``cli.py`` reached into ``git_facts``
private members (``_run_git``, ``_log_format``, ``_parse_log_records``,
``_INCIDENT_RE``, ``_BREAKING_CC_RE``). Three commands also repeated the
same five-line "resolve path + verify it's tracked" boilerplate.

- Promoted ``run_git()`` to public API (``_run_git`` kept as a back-compat
  alias). Callers that need to invoke an arbitrary git command go through
  this rather than the underscore name.
- Added ``read_commit(repo_root, ref) -> Commit | None`` — single-commit
  resolver that wraps the log-format dance in one place. ``whycode show``
  now uses it instead of the three-line `_log_format` + `_parse_log_records`
  sequence.
- Added ``classify_commit(commit) -> CommitClassification`` — public
  re-use of the same ladder ``find_incidents`` and ``extract_invariant_quotes``
  apply, so callers don't reach for the underscore regex constants.
- Combined the resolve-and-verify-path boilerplate into a single
  ``_require_tracked()`` helper inside ``cli.py``; ``why``, ``timeline``,
  and ``honest`` each lost five lines of duplication.

No user-visible behaviour change; no perf change. 99 tests still passing.

## [0.2.3] — 2026-05-06

This release is the result of running WhyCode against a real moderately-sized
OSS repo (`pallets/click`, ~3000 commits, 149 files) and fixing what hurt.

### Performance
- `co_changes` (the engine behind the coupling signal) now uses a single
  `git log --no-walk --numstat` over the file's pre-fetched SHA list
  instead of one `git show` per commit. **`whycode why` on a real
  100-commit core file: 3.1s → 0.79s (4×). `whycode scan --top 5` on a
  3000-commit repo: 25.7s → 12.7s (2×).**
- `whycode scan` gains `--scan-depth N` (default 200) capping the
  per-file commit history scanned. `--scan-depth 0` for no cap (full
  history; slow on large repos).

### Changed
- `whycode scan` now skips a built-in ignore list of "always-noisy"
  paths: changelogs, lockfiles, generated stubs (`*_pb2.py`,
  `*.pb.go`, `*.generated.*`), vendored dirs (`node_modules/`,
  `vendor/`, `third_party/`), built docs (`_build/`, `site/`), and
  static assets. **`CHANGELOG.rst` and other release-touched files
  no longer dominate the top-N risk list.** `--no-ignore` opts back
  in to scanning everything. A `.whycodeignore` file at repo root
  (one fnmatch pattern per line, `#` for comments) extends the list.

### Added
- `whycode/ignore.py` module + `tests/test_ignore.py` (8 tests).
- Three integration tests for `scan` covering ignore patterns and
  the user-extension file.

## [0.2.2] — 2026-05-06

### Added
- `whycode highlights` — the first-run "treasure map". Scans the whole
  repo and surfaces the most recent invariant lines (verbatim, from
  commit bodies) and the most recent incident-flavoured commits.
  This is what a new contributor wants to read on day one — concrete
  decisions, not aggregate risk scores. Use `--invariants N`, `--incidents
  N`, or `--max-commits N` to tune scope; `--json` for tooling.
- `whycode why <path> --mute <kind>` — local feedback loop: mark a
  signal as "this is wrong, hide it" and never see it on this file
  again. The suppression list lives at `.whycode/suppressed.json`
  (gitignored, per-developer; no telemetry, no cloud, no cross-team
  sharing). Kind names accept unique prefixes (`incident`, `revert`,
  `ghost`, …). `--no-mutes` temporarily bypasses the list.
- `whycode why` (and every other surface) now applies the suppression
  list automatically.

### Changed
- `risk_card.build()` gains an `apply_suppressions=True` keyword
  argument. The MCP server inherits this for free — no changes there.

## [0.2.1] — 2026-05-06

### Changed
- The GitHub Action template that `whycode init` writes is now **advisory
  by default**: it prints the risk-ranked table to the job log but does
  not fail the build. Users opt into hard gating by appending
  `--fail-on <band>` to the diff line. Rationale: a brand-new repo's
  history is usually too thin for `READ HISTORY FIRST` to be meaningful,
  and a tool whose first contact blocks merging is a tool that gets
  uninstalled the same day. Advisory first; gate on opt-in.
- README's "Wire it into git, CI" section reflects the new default.

## [0.2.0] — 2026-05-06

Published to PyPI as **`whycode-cli`** (the bare name `whycode` was already
taken by an unrelated project; the installed command is still `whycode`).

### Added
- `whycode timeline <path>` — show the file's risk score evolution sampled
  across its history; useful for spotting "when did this become a load-bearing
  wall?" and for postmortem timelines.
- `whycode honest <path>` — print every invariant line stated in the file's
  history, verbatim and untruncated, grouped by commit. Use when the Risk
  Card's first-sentence truncation is hiding context.
- `whycode why <path> --at <ref>` — render the Risk Card as it would have
  looked as of any past commit / tag. The header surfaces "as of <sha>" so
  historical views are not mistaken for current ones.
- `whycode init` — one-command setup that drops a CI workflow and a local
  pre-commit risk gate into your repo (idempotent; `--force` to overwrite).
- `whycode show <sha>` — risk-flavoured summary for a single commit:
  classification (incident-flavoured? states invariants?) plus a per-file
  risk ranking of the files it touched.
- MCP server: `summary` field added to both `get_risk_profile` and
  `get_file_decisions` responses — a one-paragraph prose digest LLM
  consumers can quote verbatim.
- CI: drop-in GitHub Action template at `.github/workflows/whycode.yml`
  that gates PRs at `--fail-on history` by default.

### Changed
- Ghost-keeper detection now uses `git blame` line ownership (with a
  commit-count fallback) so a single big initial commit by an absent
  author is still recognised as the keeper, even when later activity
  by others outnumbers them by commit count.
- Incident detection precision: a body keyword now requires a corroborating
  issue identifier (`#1234`, `INC-447`, `SEV-1`, `P0`, Jira-style
  `ABC-123`) to fire. Subject keywords and structured `BREAKING CHANGE:`
  footers still fire on their own.
- Severity for `revert_chain` and `invariant_quotes` decays with age
  (full weight < 2 years; one step lower at 2–5 years; two steps lower
  beyond 5 years). Headlines now show the most-recent date inline.
- `whycode diff` and `whycode scan` no longer surface NEWBORN-only files
  as risk; they appear under the "no flags" tally instead.

### Fixed
- Quoted invariant tokens (`"do not"`, `"warning:"`) inside commit bodies
  are no longer treated as invariant statements — only un-quoted
  occurrences fire.
- Subject-only mentions of words like `invariant` no longer self-flag for
  files whose commits document the matcher itself.

## [0.1.0] — 2026-05-06

### Added
- Initial release: `whycode why`, `whycode diff`, `whycode scan`,
  `whycode mcp`, plus eight independent risk detectors (revert chain,
  incident history, ghost keeper, invariant quotes, coupling, high
  churn, silence, newborn) feeding a 0..100 score and a four-band label.
