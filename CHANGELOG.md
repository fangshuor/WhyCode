# Changelog

All notable changes to WhyCode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-05-06

### Added — L3 LLM-enriched decision extraction (the missing 40%)

The original three-layer design always specified an opt-in L3 layer for
LLM-enriched decision summarisation. L1 + L2 give keyword-level
fragments ("do not switch to async"); L3 turns them into structured
decisions with the *why* drawn from the surrounding commit body.

```
$ whycode why src/payment/refund.py --llm

╭─ HANDLE WITH CARE  score 78/100 ─────────────╮
│ src/payment/refund.py   (24 commits)         │
│ Latest: hotfix: idempotency token regression │
╰──────────────────────────────────────────────╯
   HIGH    3 reverts touched this file (most recent: 4 months ago)
   MED     2 invariants stated by past authors

╭─ DECISIONS (L3) ───────────────────────────────────────────────╮
│ COMPAT WORKAROUND   confidence 88%                              │
│ Kept synchronous HTTP for the refund flow.                      │
│ Why: v1 clients need synchronous responses to honour the SLA we │
│      signed with Acme Corp; an async refactor in 2024 broke    │
│      that and was rolled back the same week.                   │
│ Don't: switch this to async without revisiting the SLA.        │
│ evidence: a3f4b2c, 7e22a04                                     │
╰────────────────────────────────────────────────────────────────╯
```

Privacy contract honoured:

- **Off by default.** `--llm` is required to opt in. L1 and L2 still run
  with zero network and zero API key, exactly as before.
- **No vendor names in source.** Configuration is entirely via
  environment variables — `WHYCODE_LLM_API_KEY` and `WHYCODE_LLM_MODEL`
  must both be set explicitly. The source tree itself does not embed
  any provider's product or model identifier.
- **Lazy import.** The provider SDK is only imported when L3 is actually
  invoked. Users who never use `--llm` pay no import cost and need no
  AI dependency installed.
- **`--llm-dry-run`** prints exactly how many commits and how many
  characters *would* be sent to the LLM, without making the call.
  Use it before paying any cost or letting any data leave the machine.
- **Filtered input only.** L3 receives at most ten high-signal commits
  per `--llm` invocation: the L2 incident-flagged commits plus any
  commit body of substantial length. Trivial commits (`fix: typo`)
  are never sent.

Install the optional extras:

```bash
pip install 'whycode-cli[llm]'
export WHYCODE_LLM_API_KEY=…
export WHYCODE_LLM_MODEL=…
whycode why src/some-file.py --llm
```

The decision schema:

```json
{
  "decision_type": "incident_fix" | "compat_workaround" | "perf_rewrite"
                   | "rollback" | "constraint" | "other",
  "what_changed":  "one-sentence summary",
  "why":           "one paragraph from the body, quoted where possible",
  "do_not":        "actionable constraint or null",
  "evidence":      ["sha1", "sha2", ...],
  "confidence":    0.0 - 1.0
}
```

Confidence below `0.5` is filtered out by default; the LLM is instructed
to skip rather than invent. A malformed model response degrades to
"no decisions" rather than crashing the card.

### Internal
- `src/whycode/llm.py` — provider-neutral client wrapper, lazy-imported.
- `src/whycode/decisions.py` — `Decision` dataclass + extractor + parser.
- `tests/test_decisions.py` — 14 tests against mocked LLM responses;
  no real network in CI.
- `RiskCard` gained an optional `decisions` field (empty by default);
  `RiskCard.with_decisions()` returns an enriched copy.
- The MCP server inherits the `decisions` field through `card.to_dict()`
  for free — but L3 enrichment is not auto-triggered for MCP calls;
  it stays opt-in.

132 tests passing (118 prior + 14 L3). ruff + mypy strict clean.

## [0.2.6] — 2026-05-06

### Added
- `whycode tour` — first-run walkthrough that runs in seconds: surfaces
  the most-recent invariants and incident commits, slim-scans the repo
  for top-3 risky files, and prints the one MCP-config snippet you'll
  need to wire WhyCode into an MCP-aware editor. The single command to
  run after `pip install whycode-cli` to find out what this tool can
  see in your repo. Quiet repos get an honest empty-state explanation
  ("commit messages may be too terse — describing 'why' in commit bodies
  makes WhyCode much more useful").

### Changed
- The CLI module docstring lists `tour` as the first command.

## [0.2.5] — 2026-05-06

### Added
- `whycode diff --markdown` emits a GitHub-flavoured markdown table
  designed for posting as a PR comment. Includes a hidden HTML marker
  (`<!-- whycode-comment -->`) so subsequent runs find and update the
  same comment instead of stacking new ones.
- The packaged GitHub Action workflow (and this repo's dogfood copy)
  now post the risk briefing as a sticky PR comment in addition to
  printing it to the job log. Most PR reviewers never read CI logs;
  inline comments are read every time.

### Changed
- The workflow now requires `pull-requests: write` permission so the
  comment-posting step works. CI logs alone still work for read-only
  setups — just remove the comment steps.

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
