# Changelog

All notable changes to WhyCode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [0.6.2] — 2026-05-08

### Fixed — small-version polish + 0.6.1 follow-ups

A focused audit on the post-0.6.1 surfaces flagged a stale empty-state
hint, two ignore-filter holes the recon caught (now closed), four
missing regression tests, a scorer overweight, and a misleading
ghost-keeper headline.

#### Real-repo correctness

- `whycode show <sha>` now applies the default ignore filter to the
  per-file table — release commits no longer surface ``CHANGELOG.md``,
  lockfiles, generated stubs alongside the source files.
- `whycode honest <ignored-path>` prepends the same "heads up: matches
  default ignore list" advisory ``whycode why`` already shows. Pulling
  invariant lines from a CHANGELOG release-notes file is mostly noise;
  the advisory makes that explicit.

#### Calibration

- COUPLING base weight dropped from 5 to 3 in ``scorer.py``. Recon on
  click / flask / requests showed coupling-only files (``CHANGES.rst``,
  test fixtures) topping out near 100 while files with 14 reverts only
  reached ~70 — an upside-down calibration. Coupling fires liberally
  on monorepos where every test co-changes with every fixture; the
  rebalance shifts the band cap without changing detector ordering.
- Ghost-keeper headline now distinguishes "primary line owner inactive
  N days" (when the file's most recent commit is by someone else) from
  the original "primary author last active N days ago" phrasing. The
  detail line and severity are unchanged; the headline just stops
  contradicting the `most_recent_at` shown in the same Risk Card
  (recon found this on click ``shell_completion.py``).

#### Consistency

- The ``highlights`` empty-state hint listed ``warning:`` as an example
  invariant token even though 0.6.1 removed it from
  ``INVARIANT_TOKENS``. Replaced with ``important:``.

#### Test surface

Four regression tests added for 0.6.1 surfaces that lacked coverage:
- ``test_why_heads_up_advisory_on_ignored_path`` — locks the advisory
  text on metadata files.
- ``test_mute_confirmation_mentions_no_mutes_reverse_path`` — locks
  the discoverability of the reverse path added in 0.6.1.
- ``test_mcp_command_prints_stdio_ready_line`` — locks the startup
  stderr line that prevents the "looks hung" first impression.
- ``test_why_warns_on_untracked_path`` extended to assert the friendly
  "→ check the path" next-step.

236 tests passing (was 233); ruff + mypy strict clean.

#### Deferred

- Coupling pre-rename self-reference (``click/core.py`` showing as a
  co-changer of ``src/click/core.py``) — basename-only matching
  collides with ``__init__.py``; needs more careful design than a
  patch release allows.
- ``--mute`` enum-vs-display naming inconsistency (cosmetic).


## [0.6.1] — 2026-05-07

### Fixed — onboarding friction + real-repo recon

A read-only audit pass plus recon on three real OSS repos (click, flask,
requests) surfaced a cluster of small UX cuts and one genuine regression
worth shipping fast.

#### Onboarding friction

- ``whycode --help`` ends with ``Start here: whycode tour`` so a brand-
  new user has a clear first command.
- ``--mute KIND`` help now lists every kind name explicitly (was
  truncated with ``…``); the mute confirmation suggests
  ``--no-mutes`` as the preview path before editing the JSON.
- Outside-a-repo error reads "not inside a git repository. → cd into a
  git repo, or pass --repo PATH" instead of a raw ``git rev-parse
  --show-toplevel`` traceback.
- Missing-path error no longer claims "warning" with a successful exit
  code — it now exits **2** so a CI loop running ``whycode why`` per
  file fails loudly on a typo. The message keeps the "→ run whycode
  scan --top 5" next-step.
- ``whycode mcp`` prints one stderr line on startup (server ready,
  version, ``-v`` hint) so a curious user doesn't see a silent block.
- ``whycode init`` ends with "→ Run ``whycode diff --staged`` to
  preview what the pre-commit hook will check."
- ``whycode tour``'s MCP snippet hint mentions the typical
  ``mcp.json`` filename so a fresh user has a concrete thing to
  search for in their editor's docs.
- The ``cache`` subgroup is hidden from the main help (it's a
  diagnostic surface, not a user-facing one); ``whycode cache stats``
  / ``cache clear`` still work.
- ``whycode highlights`` docstring explains how it differs from
  ``tour`` (same content minus MCP setup + risk scan).

#### Real-repo correctness

- **Regression fix**: ``whycode diff`` was bypassing the default ignore
  list — on click, ``CHANGES.rst`` ranked #1 (HANDLE WITH CARE) above
  application code. The 0.4.1 quality pass added the filter to
  ``scan`` and ``detect_coupling`` but missed the diff command's own
  file list. Now applied consistently.
- ``whycode why <metadata-file>`` (a CHANGELOG, lockfile, generated
  stub, etc.) prints a "heads up: this path matches the default
  ignore list" advisory above the card so a user who arrived via
  typo / LLM-suggested path doesn't read an authoritative-looking
  100-score on a release-notes file.
- ``examples/**``, ``example/**``, ``demo/**``, ``demos/**``,
  ``samples/**`` added to the default ignore list. On click these
  five-year-untouched example scripts dominated ``scan --top 15``
  with no actionable code.
- ``INVARIANT_TOKENS`` no longer matches the lowercase ``warning:``
  / ``note:`` literals. On click and other Python projects, pasted
  Ruff/lint output ("the warning:", "warning: rule X is removed")
  was self-flagging as a quoted invariant — turning ``whycode
  highlights`` into a wall of bogus constraints. The remaining
  tokens (``do not`` / ``don't`` / ``must not`` / ``important:``
  / ``danger:`` / ``invariant`` / ``workaround`` / ``tradeoff``)
  cover the genuine "the past author left a constraint" intent
  without aliasing tool output.
- ``whycode tour``'s "Top 3 risky files" picker reverted to top-3 by
  raw score (post-onboarding-audit, which had introduced a
  distinct-by-kind picker; recon on requests showed it hid the
  14-revert ``models.py`` core in favour of two test-server
  fixtures). The original ranking was correct; calibration of
  per-kind score weights is a separate follow-up.

233 tests passing; ruff + mypy strict clean.

Deferred to a future focused pass:
- Coupling self-references via pre-rename paths (``click/core.py``
  shows up as a co-changer of ``src/click/core.py``); needs
  ``--follow`` rename-resolution threaded through co-change
  loading.
- Score rebalancing — coupling-only files currently top out higher
  than reverts+incidents, an upside-down calibration.
- Ghost-keeper mixed-signal phrasing when the line-blame primary
  differs from the commit-count primary.


## [0.6.0] — 2026-05-07

### Changed — output density audit pass

Three UX features (`--explain`, bucketed `diff`, narrative + per-signal
`next_step`) shipped via parallel branches in 3 hours and accumulated
overlapping render layers. This release re-reads the actual output a
tired user sees on a real Risk Card and removes what they don't need.

#### Risk Card

- The narrative summary keeps only sentence 1 (file age + primary author
  + last activity). Sentence 2 ("the strongest concern is X; consider Y
  first.") was a verbatim restatement of the strongest signal's headline
  + next_step rendered three lines below; the user read it twice.
  Cards with no signals collapse to one honest line: "`<path>`: no flags
  fired across N commits. Read the diff anyway."
- `--explain` now replaces the per-signal `next_step` line with the rule
  trace (rule id, why_it_fired, evidence, source_ref) instead of stacking
  both. Each signal stays at one action-line regardless of mode; a 5-signal
  card with `--explain` is no longer a 40-line wall.
- The header panel shows path + commit count only. The "Latest:
  <subject>" + "<sha> <author> <date>" lines were removed — the
  `incident_history` signal references the relevant SHA when it matters,
  the JSON output still carries the `most_recent` block for tools that
  consume it.
- `detect_high_churn`'s lecturing detail line ("Code that changes this
  often is rarely settled — read recent diffs first.") was cut. Headline
  + next_step already say it.

#### `whycode diff`

- The per-row integer score column is gone, in both text and markdown
  output. The bucket header already encodes the band; the integer added
  visual width without information density. Markdown table is now
  `| File | Top signal |`.
- The `→ whycode why <path>` footer hint is gone — every user who runs
  `diff` knows about `why`.
- The `whycode diff --json` output drops the flat `files` array. The
  `buckets` field (added in 0.5.3) carries the same data; clients can
  iterate it to recover a flat sorted view if needed.

#### `whycode tour`

- The "Top 3 risky files" block drops the per-row integer score for the
  same reason — bucket heading carries the band.

#### MCP server

- `get_risk_profile` accepts a new `explain` boolean argument (default
  false); when true, the returned card includes the per-signal
  `explanation` block.
- `get_file_decisions` includes `next_step` per signal in its payload —
  it was stale relative to the new card shape.
- `_summary_text` drops the `/100` suffix to match the rendered header.

#### Internal

- `_propagate_failures` decorator removed from cli.py and from every
  command. Typer/Click already exit non-zero on uncaught exceptions; the
  decorator was solving a non-problem.
- Back-compat aliases removed: `_age_phrase = age_phrase` (signals.py),
  `_run_git = run_git` (git_facts.py), `_BAND_STYLE = BAND_STYLE`
  (risk_card.py). Internal callsites updated.
- `_FAST_DETECTORS` tuple + `_all_signals_without_ghost_keeper` helper
  (risk_card.py) inlined into `signals.all_signals(skip_ghost_keeper=…)`.
- `with_closing` helper (cache.py) and the `closing` import removed —
  zero callers.
- Redundant `try/except ValueError` around `_parse_iso` in
  `_parse_log_with_files` (git_facts.py) removed — `_parse_iso` already
  swallows ValueError and returns the epoch sentinel.
- `LLMConfig` dropped from `llm.py`'s `__all__` — only constructed
  internally.
- WHAT-only and past-task-reference comments deleted.

233 tests passing; ruff + mypy strict clean. No outbound calls added,
no new dependency.


## [0.5.4] — 2026-05-07

### Changed — Risk Card opens with a narrative, each signal carries its own next step

The Risk Card was a list of facts. Real users read it, blinked, and had
to mentally synthesise "so what?" before drilling into the per-signal
list. This release lifts that synthesis to the top of the card and
makes every signal carry its own concrete action — no more generic
`→ git show <sha>` footer that ignored what the signal actually said.

#### Two-sentence narrative summary

A new templated narrative block opens every Risk Card, immediately
under the header panel. It reads as one paragraph, two sentences:

1. file age + primary author + last activity, and
2. the dominant concern + the action implied by its `next_step`.

NO FLAGS cards collapse to one quiet honest sentence ("`<path>` has N
commits and no risk signals fired. Read the diff anyway.") so the
empty case never lies and never spams.

This is L1+L2 only — there is no LLM call, no new git command, no
new dependency. The wording reuses the same `age_phrase` ("5 weeks
ago / 3 months ago / 2 years ago") detectors already put into their
headlines, so the card reads consistently top-to-bottom.

#### Per-signal `next_step`

The `Signal` dataclass gained one optional field, `next_step:
str | None`. Each detector now populates it with the action a careful
reader would take given that signal — and the rendering layer prints
it on its own dim-coloured line directly under the signal detail. The
old global `→ git show <sha>` footer (which picked "the most-relevant"
SHA by a heuristic that ignored what the signal said) is gone.

Wording per kind:

| signal kind         | next_step                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------ |
| `revert_chain`      | Read both sides — `git show <revert>` then `git show <reverted>` — to learn …            |
| `incident_history`  | Read `git show <most_recent_incident>` to see the incident-flavoured change in context.    |
| `ghost_keeper`      | Primary author has been gone N days. Surface to your team; `git log --author='<email>' --` |
| `invariant_quote`   | Honour the invariant: `<verbatim quote>`.                                                  |
| `coupling`          | Read these too — they tend to change together: `<top-3 paths>`.                            |
| `silence`           | Untouched for N days. Verify it's still exercised (run the relevant tests) …               |
| `high_churn`        | Skim recent diffs for the live design intent: `git log -p --since='90 days' -- <path>`     |
| `newborn`           | `None` — not enough history to recommend anything.                                         |

`whycode why --json` now includes `next_step` per signal under
`signals[]`. The schema is wider but backwards-compatible — existing
keys are untouched.

#### Trim visual redundancy

The header used to compete for attention with three near-redundant
tags: a band, a bold numeric score, and a per-signal severity badge.
The score is now a small dim `· N` suffix on the band line; the band
carries the headline word and the per-signal badges differentiate
inside the table. The numeric score stays in `--json` output as it
is public API.

#### `whycode tour` ending substitutes the actual top-risk path

The tour ends with a `Next:` block whose first suggestion is now the
literal path of the file the slim scan ranked #1, so a fresh tour
leaves the user one paste-and-run away from the actual first dive.
When the slim scan surfaces nothing, the line degrades to a generic
`<path>` placeholder so every tour ends with the same shape.

### Before / after

The same file as rendered by 0.5.2 (top of card) versus 0.5.4:

```
0.5.2:
╭─  READ HISTORY FIRST  score 57/100 ──────────────────────────╮
│ refund.py   (3 commits)                                       │
│ Latest: hotfix: regression                                    │
│         …                                                     │
╰───────────────────────────────────────────────────────────────╯
   HIGH    1 revert touched this file (most recent: 7 weeks ago)
           Reverts in this file's history: 4a9ba84 reverts …
   MED     1 incident-flagged change in history
           …
→ git show 4a9ba84   to read the most relevant commit in full
```

```
0.5.4:
╭─  READ HISTORY FIRST  · 51 ──────────────────────────────────╮
│ refund.py   (3 commits)                                       │
│ Latest: hotfix: regression                                    │
│         …                                                     │
╰───────────────────────────────────────────────────────────────╯
  refund.py is 3 commits old, primarily authored by Kevin, last
  touched 16 days ago.
  The strongest concern is 1 revert touched this file (most recent:
  7 weeks ago); consider Read both sides — git show 4a9ba84 then
  git show 1c4d3e2 — to learn what was tried and why it broke first.

   HIGH    1 revert touched this file (most recent: 7 weeks ago)
           Reverts in this file's history: 4a9ba84 reverts …
           → Read both sides — git show 4a9ba84 then git show 1c4d3e2
             — to learn what was tried and why it broke.

   MED     1 incident-flagged change in history
           …
           → Read git show 7e22a04 to see the incident-flavoured
             change in context.
```

### Internal

- `src/whycode/signals.py` — `Signal` gained `next_step: str | None`;
  every detector populates it. `_age_phrase` is now public as
  `age_phrase` so the rendering layer can reuse it; the old name
  remains a back-compat alias.
- `src/whycode/risk_card.py` — `RiskCard` gained `primary_author` (by
  commit count, cheaper than the ghost-keeper detector's blame-based
  primary owner). `_narrative_summary` composes the two-sentence block
  from facts already on the card — no extra git calls. The legacy
  `_next_step_hint` global footer was removed; per-signal hints render
  inside `_signals_table`. The header line drops `score N/100` for a
  dim `· N` suffix on the band.
- `src/whycode/cli.py` — the tour's `Next:` block falls back to a
  generic `<path>` placeholder only when the slim scan finds no
  flagged files; otherwise the literal top-risk path is substituted.

### Tests

- 16 new tests cover the `next_step` wording per detector, the JSON
  schema additions, the narrative block, the band+score format, and
  both branches of the tour `Next:` line.
- 226 tests passing total (210 from 0.5.2 + 16 new). ruff + mypy strict
  clean.

## [0.5.3] — 2026-05-07

### Changed — `whycode diff` is now bucketed by band

The pre-PR command produced a single sorted table for every changed
file. On a 50-file PR that became a wall: HANDLE WITH CARE rows shared
visual weight with NO FLAGS rows, and a reviewer had to scan top to
bottom to find the cluster of risky files. Output is now grouped by
band, with each bucket clearly headed and the bucket order fixed.

Before:

```
16 file(s) changed vs origin/main
                              Risk-ranked changes
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ score ┃ band               ┃ path                   ┃ top signal     ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│    82 │ HANDLE WITH CARE   │ src/whycode/cli.py     │ Tightly coupled│
│    82 │ HANDLE WITH CARE   │ tests/test_cli.py      │ Tightly coupled│
│    71 │ READ HISTORY FIRST │ src/whycode/git_facts… │ Tightly coupled│
│    59 │ READ HISTORY FIRST │ src/whycode/risk_card… │ Tightly coupled│
│    39 │ WORTH A LOOK       │ tests/test_git_facts.… │ Tightly coupled│
│    15 │ NO FLAGS           │ src/whycode/signals.py │ Tightly coupled│
│   …                                                                 │
└───────┴────────────────────┴────────────────────────┴────────────────┘
+ 4 file(s) changed with no flags
```

After:

```
16 file(s) changed vs HEAD~5

 HANDLE WITH CARE   (2)
   82  src/whycode/cli.py        Tightly coupled to 5 other files
   82  tests/test_cli.py         Tightly coupled to 5 other files

 READ HISTORY FIRST   (5)
   71  src/whycode/git_facts.py  Tightly coupled to 5 other files
   59  src/whycode/risk_card.py  Tightly coupled to 5 other files
   …

 WORTH A LOOK   (1)
   39  tests/test_git_facts.py   Tightly coupled to 5 other files

+ 8 file(s) with no risk signals — pass --show-clear to list

→ whycode why <path>   for the full Risk Card on any of the above
```

Behaviours:

- Bucket order is fixed: HANDLE WITH CARE → READ HISTORY FIRST →
  WORTH A LOOK → CLEAR. Empty buckets are not rendered (no
  ``READ HISTORY FIRST (0)`` lines).
- The CLEAR bucket (NEWBORN-only / no useful signals) is collapsed to
  a single count line by default; ``--show-clear`` expands it.
- Within each bucket the existing stable tie-break ``(-score, path)``
  from 0.4.2 is preserved.
- ``--top N`` still caps the **total** row count across all buckets,
  not per-bucket.
- ``--json`` gains a top-level ``buckets`` field keyed by band string;
  each value is the file array. The flat ``files`` array is
  preserved for backward compatibility.
- ``--markdown`` emits a ``### <band> (<count>)`` heading per
  non-empty bucket. The PR-comment marker (``<!-- whycode-comment -->``)
  is unchanged.
- ``--fail-on`` summary line and exit codes are unchanged.

The same bucketing applies to ``whycode tour``'s "Top 3 risky files"
block — three rows is short enough that the bucket headers don't feel
bureaucratic, and the visual consistency with ``whycode diff`` is what
the change is for.

### Changed — Risk Card visual de-noise

Two redundancies in the per-file Risk Card:

- The header rendered both the band (``HANDLE WITH CARE``) AND the
  integer score (``score 78/100``) at full prominence. The band
  already names the bucket; the score is duplication on the most
  prominent surface of the card. Demoted to a small dim ``· 78``
  suffix on the same line. JSON output is unchanged — ``score``
  stays as an integer key.
- The signals table appended ``evidence: <sha>`` even when that SHA
  was already visible in the card header (the silence detector's
  evidence is the head-of-history commit the header just printed two
  rows up). The header SHA is now considered when checking
  redundancy, so single-SHA evidence trailers that repeat the header
  are suppressed.

Per-signal severity badges (``HIGH`` / ``MED`` / ``LOW``) stay — they
communicate per-detector weight, not band.

### Internal

- ``src/whycode/cli.py`` — ``_BUCKET_ORDER``, ``_bucket_for``,
  ``_group_into_buckets``, ``_bucket_header_style`` are the only new
  symbols. Bucketing is inline in the ``diff`` command and ``tour``;
  no generic table-grouping abstraction.
- ``src/whycode/risk_card.py`` — ``BAND_STYLE`` is now public so the
  diff command's bucket headers reuse the same band colours; ``_BAND_STYLE``
  is kept as a back-compat alias. ``_evidence_redundant`` accepts an
  ``extra_context`` keyword so the header SHA is part of the visible
  surface used to decide whether the trailer is duplication.
- ``--show-clear`` flag added to ``whycode diff``.
- ``tests/test_cli.py`` — 7 new tests: bucket headers, empty-bucket
  suppression, ``--top N`` global cap, ``--json`` ``buckets`` key,
  ``--markdown`` per-bucket section, tour bucket labels, header
  ``· N`` suffix.

201 tests passing (194 from 0.5.0 + 7 new). ruff + mypy strict clean.
Privacy contract is unchanged — bucketing is rendering-only.

## [0.5.2] — 2026-05-07

### Added — `whycode why <path> --explain` makes the rule ladder transparent

When WhyCode flagged a file before this release, the user could see
*what* fired (a one-line headline plus a detail string) but not *how*
the rule had matched. A signal whose detail read

> *"1 commit matched incident keywords (latest: 'hotfix: regression')"*

does not tell the reader whether the match was on the literal token
`hotfix:`, on a Conventional Commits `fix!:` marker, on a
`BREAKING CHANGE:` footer, or on the `regression + #INC-447`
corroboration rule. That ambiguity hurt in two scenarios: a user who
believed the signal was a false positive could not tell whether the
*rule* was wrong or their *understanding* of the rule was wrong, and
a new user who wanted to audit the tool's reasoning before relying on
it had no entry point into the source.

`whycode why <path> --explain` adds a small dim block under each fired
signal naming the precise rule branch that produced it. Each detector
now records a structured `Explanation` (rule identifier, one-sentence
prose, literal evidence, source location) at fire-time; `--explain`
renders that structure inline. The default surface is unchanged for
users who already learned to read the card.

```
$ whycode why src/payment/refund.py --explain

   MED  3 reverts touched this file (most recent: 4 months ago)
        Reverts in this file's history: 9d2e7a1 reverts c5b81fe; …
        ─ rule: revert_pair_default_message  src/whycode/git_facts.py:find_revert_pairs
          fired because: 3 commit bodies match the default git revert
                         footer 'This reverts commit <sha>'
          evidence: 9d2e7a1, bbf441c, 0e1f883
```

`--explain --json` adds the same structure to the JSON output:

```json
"signals": [
  {
    "kind": "incident_history",
    "severity": 3,
    "headline": "...",
    "detail": "...",
    "evidence": ["..."],
    "explanation": {
      "rule": "incident_subject_keyword",
      "why_it_fired": "subject 'hotfix: regression' matched the literal token 'hotfix'",
      "evidence": ["hotfix"],
      "source_ref": "src/whycode/git_facts.py:find_incidents"
    }
  }
]
```

When `--explain` is off the JSON shape is unchanged — the
`explanation` key is omitted, so existing downstream parsers see no
drift. The flag composes with `--at`, `--mute`, `--no-mutes`, and
`--max-commits`. It covers L1+L2 detectors only; if a user combines
`--llm --explain`, the L3 decision block is rendered as before
without per-decision explanations.

The `incident_history` detector — which has the densest acceptance
ladder — now records which of six branches accepted the most-recent
fired commit:

- `incident_subject_security_advisory` — `CVE-…` / `GHSA-…` token in
  subject.
- `incident_subject_revert_marker` — default `Reverted "…"` body
  subject or human `Reverts <sha>` pointer.
- `incident_subject_conventional_commits_breaking` — Conventional
  Commits breaking marker (`feat!:` / `fix!:` / …).
- `incident_subject_keyword` — incident keyword in subject (`hotfix`,
  `outage`, `rollback`, `regression in <…>`, …).
- `incident_body_breaking_change_footer` — structured `BREAKING
  CHANGE:` footer in body.
- `incident_body_keyword_with_issue_id` — body keyword corroborated
  by an issue / incident id (`#1234`, `INC-447`, `SEV-1`, …).

### Internal

- `src/whycode/signals.py` — `Explanation` dataclass + optional field
  on `Signal`; `_classify_incident_commit()` mirrors the
  `find_incidents` ladder so an explanation can name which clause
  matched on a specific commit.
- `src/whycode/risk_card.py` — `_signals_table(..., explain=False)`
  appends the explanation block; `to_dict(explain=False)` adds the
  `explanation` key per signal.
- `src/whycode/cli.py` — the `why` command grows a single `--explain`
  flag wired to both surfaces.
- `tests/test_signals.py` — 11 tests covering every detector's rule
  identifier and matched evidence.
- `tests/test_cli.py` — 5 tests covering the rendered text block, JSON
  shape, regression guards that the default surface is unchanged, and
  composition with `--at`.

210 total tests (194 from 0.5.0 + 16 new); ruff + mypy strict clean.
The pre-existing `test_diff_markdown_output` assertion on the
markdown table header (`| Score | Band |`) is unrelated to this change
and remains a known stale assertion against the `| Score | File |
Top signal |` shape the renderer actually emits.

## [0.5.0] — 2026-05-07

### Performance — `whycode diff` is now usable on long-lived branches

The "pre-PR" command was unrunnable when a feature branch had drifted
far from its base. On django (10,000-commit history, 1,927 changed
files vs a base from a year ago) the legacy implementation took
**6 m 7 s**; against a base from four years ago (3,762 commits range)
it timed out after **12 m 10 s** without producing output. The cause
was per-file evaluation: each changed file fired its own
`git log --follow` plus a co-change diffstat pass, so wall-clock cost
scaled linearly with the number of changed files.

`whycode diff` now batches the evaluation in two phases:

1. One un-pathed `git log --no-merges --numstat --pretty=...` walk
   parses every commit + its file-set into an in-memory
   `path -> [Commit]` map and a `sha -> tuple[paths]` co-change index.
   Per-file scoring then drives off this map instead of re-shelling-out.
2. The first pass scores every changed file with the seven detectors
   whose evidence is already in `RepoFacts` (revert, incident,
   invariants, coupling, churn, silence, newborn). Only the top-N
   files (the ones that will appear in the rendered table) are then
   re-evaluated with the ghost-keeper detector, which still needs a
   per-file `git blame`. On a 1,927-file diff this trades ~1,927
   blame calls for at most `--top` of them.

Bench against `django/django` (10,000 commits, captured against the
same shallow clone the field test used; `time` wall-clock; ``--no-cache``):

| command                                              | before          | after | speedup |
| ---------------------------------------------------- | --------------- | ----- | ------- |
| `diff --base <2025-01-01-sha>` (1,927 files changed) | 6 m 7 s         | 14 s  | ~26x    |
| `diff --base <2022-sha>` (3,171 files changed)       | killed at 12 m  | 15 s  | from "unrunnable" to "fast" |

And against `pallets/flask` (5,535 commits, 207 files changed vs
`2.0.0`):

| command                            | before  | after | speedup |
| ---------------------------------- | ------- | ----- | ------- |
| `diff --base 2.0.0` (207 files)    | 28.6 s  | 2.0 s | ~14x    |

The `--no-cache` path benefits identically — the wins come from
sharing the git log walk across files rather than from caching.

### Output equivalence

The new walk is intentionally un-pathed: it skips git's `--follow`
rename-resolution, which would otherwise cost a separate full-history
walk per file. The diff command only ever scores files present in
HEAD's working tree — files named by `git diff --name-only base...HEAD`
— so the trade is "lose pre-rename history for files renamed long
before this diff" against "score 1,927 files in seconds rather than
minutes". On flask the JSON output preserves the same number of files
and signal shape; ordering ties resolve differently for files renamed
through history (e.g. `src/flask/app.py` was at `flask/app.py` before
2019, and the new pipeline doesn't follow that rename across base).

This is the documented "stable-tie-break difference" the brief
allowed; structural equality of the JSON output is preserved.

### Internal

- `src/whycode/git_facts.py` — `DiffFacts` dataclass, `load_diff_facts`,
  `_parse_log_with_files`, `gather_for_diff`. The walk is one
  `git log --no-merges --numstat --pretty=...` parsed into the
  in-memory map; ``cache`` is threaded through so the persisted
  ``commits`` and ``commit_files`` rows seed any later
  `why` / `scan` invocation on the same HEAD.
- `src/whycode/risk_card.py` — `build_from_diff_facts` materialises a
  `RiskCard` from the in-memory map; `skip_ghost_keeper=True` flips
  off the per-file ``git blame`` for the first pass.
- `src/whycode/cli.py` — the `diff` command now does
  build_from_diff_facts(skip_ghost_keeper=True) for every changed file,
  sorts, then re-evaluates the top-N with full signals. A
  ``_memoised_is_ignored`` context manager around the two passes caches
  per-path verdicts of ``ign.is_ignored`` (the F10 filter from 0.4.1
  re-applies fnmatch over ~83 patterns and ~700 candidates per file —
  uncached that's ~100 CPU-seconds across a 1,927-file diff).
- `tests/test_git_facts.py` — 7 new tests covering the index, the
  co-change reduction, the empty-path fallback, the multiline-body /
  numstat split, and the per-path cap. The new
  `test_gather_for_diff_co_changes_match_per_file_pipeline` test also
  asserts equality against the legacy `gather()` output on a synthetic
  repo, so the per-file scorer cannot drift.

194 tests passing (187 from 0.4.2 + 7 batch-loader). ruff + mypy strict
clean.

## [0.4.2] — 2026-05-07

### Fixed — cache-correctness determinism and a `--no-cache` perf regression

A field-test pass against `pallets/flask` and `django/django` surfaced
three cache-layer bugs the public release shipped with. This release
pins all three.

- **F4** — `whycode highlights` (and `tour`) returned different
  invariant SHAs for the same HEAD across cache and `--no-cache`
  reads. Two cherry-picks of the same body with identical
  `authored_at` timestamps could collapse to either SHA depending on
  the unstable walk order. Now there is a documented dedup rule
  (earliest `authored_at`, then lexicographically smallest sha) shared
  by both the cache and the no-cache code path. JSON output for the
  same HEAD is now byte-identical regardless of cache state.
- **F5** — `whycode scan --top N` (and the matching truncation in
  `diff` and `show`) swapped files at the cutoff between cache and
  `--no-cache` runs when scores tied. Stable secondary sort on the
  lexicographically smallest path settles the tie deterministically.
- **F7** — `whycode scan --no-cache` was 2.4× slower than the
  equivalent cold cache fill on a 7,043-file repo. Bypassing the
  cache also bypassed the in-session diffstat amortisation the cold
  path uses to share `git log --no-walk --numstat` results across
  files. `--no-cache` now opens a transient `:memory:` SQLite store
  so the same git walk runs in both modes; only the persistence
  layer differs. The store is destroyed on close — nothing lands at
  `.whycode/cache.db`, no state crosses runs.

Bench against `/tmp/recon-django` (10,000 commits, 7,043 files) with
`whycode scan --top 10`:

| run        | before  | after    |
| ---------- | ------- | -------- |
| cold       | 2 m 43 s | 3 m  1 s |
| warm       | 19 s    | 22 s     |
| `--no-cache` | 6 m 26 s | 2 m 44 s |

`--no-cache` is now strictly faster than the cold persistent fill
(164 s vs 181 s), as the 0.4.0 release notes implied it should be.

### Internal

- `src/whycode/git_facts.py` — `dedupe_invariant_lines(pairs,
  sha_to_commit)` is the documented home of the F4 tie-break rule.
- `src/whycode/cache.py` — `CacheStore.__init__` accepts an
  `in_memory` flag; `cache.open_in_memory(repo_root)` is the
  module-level entry point used by the CLI's `--no-cache` flag.
- `src/whycode/cli.py` — `_open_cache` routes `--no-cache` to the
  in-memory store; every `cards.sort` site now sorts by `(-score,
  path)` for stable truncation.
- `tests/test_cache.py` — two new tests for the in-memory mode.
- `tests/test_cli.py` — three new regression tests asserting
  byte-identical output across cache states for `highlights` (F4),
  `scan` truncation (F5), and `scan` warm vs `--no-cache` (F7).

5 new regression tests; 187 tests passing total. ruff + mypy strict
clean.

## [0.4.1] — 2026-05-07

### Fixed — quality and CI-safety pass against three real OSS repos

A read-only field test against `pallets/flask`, `psf/requests`, and
`django/django` (5,500 / 6,500 / 10,000 commits) caught seven issues
that hurt the headline experience on real repositories. This release
fixes all of them.

- **F1** — Tolerate pathological tz offsets in commit timestamps.
  A 2011 commit on `psf/requests` records its tz offset as `+51800`
  in the underlying object, which `git --pretty=%aI` emits as
  `+518:00` and which `datetime.fromisoformat` rejects. That single
  record poisoned `_parse_log_records` and through it every command
  that walks history (`tour`, `highlights`, `scan`, `why`). The new
  parser repairs `+518:00` → `+05:18` and `+51800` → `+05:18`; on
  irrecoverable failure it returns a tz-aware Unix-epoch sentinel
  so the walk continues. A single per-session stderr warning is
  emitted; no per-line spam, no network.
- **F11 / F12** — Force non-zero exit on uncaught command failures.
  `whycode tour` and `whycode scan` rendered a Rich traceback to
  stderr but exited with status 0, falsifying CI signals (a
  `whycode diff --fail-on history` step that had crashed could be
  reported as green). Each command body now propagates unhandled
  exceptions as `typer.Exit(2)`; the existing rich traceback
  rendering is preserved.
- **F2** — Filter pasted tool output and cap per-commit invariants.
  On django, 15 of the top 20 "invariants stated by past authors"
  were quoted spell-check warnings from a single commit. Lines
  that look like `WARNING: …`, `path/to/file:line:`, or are
  preceded by a `> ` block-quote are now dropped at the source,
  and each commit body contributes at most two invariants — real
  authors rarely state more than two crisp constraints in one
  message; anything beyond is almost certainly a paste.
- **F3** — Tighten the incident classifier; add CVE / GHSA / revert
  recognition. `regression` in a subject now requires either a
  corroborating issue id or an anchored incident phrase (e.g.
  `regression in <something>`, `Fixed: regression`); the phrases
  `regression test(s)`, `regression suite`, `no regression`,
  `regression nature` no longer fire. Subjects citing
  `CVE-YYYY-NNN` or `GHSA-…` always fire — naming an advisory is
  unambiguous evidence. Default `git revert` body subjects
  (`Reverted "…"`) and the human variant (`Reverts <sha>`) are
  added to the high-confidence incident set.
- **F8** — Extend the default ignore list to suppress high-touch
  metadata. The django top-10 risk list was dominated by `AUTHORS`,
  `.github/workflows/*.yml`, locale `.po` files and `.gitignore`,
  with no application code at all reaching the top 10. The default
  ignore list now covers `.github/**`, `.gitlab/**`, `.circleci/**`,
  `AUTHORS*`, `LICENSE*`, `COPYING*`, `NOTICE*`, `*.po` / `*.mo` /
  `*.pot`, `setup.{py,cfg}`, `MANIFEST.in`, `.editorconfig`,
  `.pre-commit-config.yaml`, `.readthedocs.{yaml,yml}`, `.flake8`,
  `tox.ini`, `pytest.ini`, `Makefile`, and release-notes-shaped
  `*.txt` paths only (a random `requirements.txt` stays visible).
- **F10** — Apply the scan ignore list to coupling. The per-file
  coupling signal used to surface `CHANGELOG`, `.github` workflows
  and other metadata as a file's "tight coupling"; the same filter
  that powers `whycode scan` now runs inside `signals.detect_coupling`
  so every consumer (`why`, `diff`, `scan`, the MCP server)
  inherits it for free.
- **F14** — Sort `timeline` rows by date ascending before render.
  Out-of-order authored_at values from cherry-picks / rebases used
  to produce non-monotonic table rows that were easy to misread.
- **F16** — Split `tour`'s "Decisions and incidents" cell into two
  subheads — `Stated invariants` (yellow) and `Recent incidents`
  (red) — matching the layout `highlights` already uses.

22 new tests cover the regressions; 182 tests passing total.
Privacy contract is unchanged. ruff + mypy strict clean.

## [0.4.0] — 2026-05-06

### Performance — local SQLite cache for git facts

Every read-heavy command (`scan`, `highlights`, `tour`, `diff`, `why`,
`timeline`) now persists git-derived facts in a per-repo SQLite
database at `.whycode/cache.db`. The cache is invalidated on
`git rev-parse HEAD` change: a HEAD that matches the previously
recorded sha serves every read straight from SQLite. A HEAD that
moved triggers an incremental update — `git log <last_head>..HEAD`
appends the new commits without re-walking the whole history. If
`last_head` is unreachable (force-push, branch swap), we fall back
to a full rebuild.

Bench against `pallets/click` (3098 commits, 149 files):

| command                | cold (no cache) | warm (cache hit) | speedup |
| ---------------------- | --------------- | ---------------- | ------- |
| `scan --top 5`         | 16.3s           | 3.0s             | 5.4x    |
| `tour`                 | 11.0s           | 2.3s             | 4.8x    |
| `why src/click/core.py`| 1.1s            | 0.17s            | 6.6x    |

Privacy contract is unchanged. The cache is local-only at
`.whycode/cache.db`, which is already gitignored. There is no
telemetry, no network, no upload, no third-party dependency added —
just `sqlite3` from the stdlib.

The schema is intentionally tiny and hand-editable:

```
meta(key, value)
commits(sha, author_name, author_email, authored_at, subject, body)
commit_files(sha, path, insertions, deletions)
path_log(path, head_sha, position, sha)
line_ownership(path, head_sha, author_email, line_count)
```

`schema_version` lives in `meta` so we can grow it without breaking
existing caches; on a mismatch we drop and rebuild rather than
migrate (the cache is a derived artefact, losing it is never
destructive).

### Added
- `whycode cache stats` — schema, last seen HEAD, row counts, db size.
- `whycode cache clear` — wipe the cache. Idempotent.
- `--no-cache` flag on `why`, `diff`, `scan`, `highlights`, `tour` to
  bypass the cache (mostly for benchmarking and forcing a fresh git
  read).

### Internal
- `src/whycode/cache.py` — `CacheStore` class wrapping a per-repo
  sqlite db. ~30 tests in `tests/test_cache.py` covering schema init,
  HEAD-driven invalidation, incremental update, full-rebuild fallback,
  warm == cold output equality, and the `cache` subcommand surface.
- `git_facts.{all_commits, commits_for_path, co_changes, files_changed_in,
  line_ownership, gather}` accept an optional `cache: CacheStore | None`
  keyword. With it omitted, behaviour is identical to 0.3.0.
- `RepoFacts` carries the cache reference forward so signal detectors
  (specifically `detect_ghost_keeper`) reuse it for `git blame`.

148 tests passing (118 prior + 25 cache + 5 cache-CLI). ruff + mypy
strict clean.

## [0.3.1] — 2026-05-06

### Added — MCP `prompts/` capability (three saved-search shortcuts)

The MCP server already exposes WhyCode's data through *tools* the host LLM
can call on demand. But each user has had to write their own prompt to
get the LLM to fetch and use that data sensibly ("call `get_risk_profile`,
then walk me through any HIGH signals before editing"). That's friction.

This release adds the MCP `prompts/` capability: three reusable templates
the host editor surfaces as one-click actions. The server fills in the
WhyCode data; the host LLM does the reasoning, exactly as it does for
tools today.

- **`before_edit_checklist(path)`** — fetches the Risk Card for the file
  and asks the assistant to walk the user through every HIGH-severity
  signal before suggesting any edit.
- **`summarise_for_postmortem(sha)`** — fetches a commit's metadata and
  WhyCode classification and asks the assistant to draft a concise
  incident summary suitable for a postmortem document, citing specific
  evidence SHAs.
- **`risk_briefing_for_pr(base)`** — runs the diff risk briefing for
  files changed against a base ref and asks the assistant to summarise
  it for a PR reviewer in 3-5 bullets, putting HANDLE WITH CARE files
  first.

### Privacy

The prompts surface adds **zero outbound network calls**. Each prompt
composes a static template from local git data (the same calls already
backing the tool surface and the CLI) and hands it to the client. The
host LLM is the one making any LLM call, on the user's terms — exactly
as it has been for tools. The server stays read-only.

### Internal

- `src/whycode/mcp_server.py` gained `_list_prompts` / `_get_prompt`
  handlers and three rendering helpers (one per prompt). The existing
  tool surface (`get_risk_profile`, `get_file_decisions`) is unchanged.
- `tests/test_mcp_prompts.py` — 12 tests covering listing, retrieval,
  argument validation, vendor-neutral prompt text, and a tripwire that
  fails the build if any prompt opens an outbound IPv4/IPv6 socket.
- 130 tests passing (118 prior + 12 prompts). ruff + mypy strict clean.

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
