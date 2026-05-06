# Changelog

All notable changes to WhyCode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-06

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
