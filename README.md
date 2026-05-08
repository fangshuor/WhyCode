# WhyCode

> **Tells you what to be afraid of before touching a file.**

`git blame` answers "who". `git log` answers "what". WhyCode answers the
question your senior engineer asks before any change:

> *"Should I be careful here?"*

It mines what your repository already remembers — reverts, hotfixes,
incident-tagged commits, ghost authors, tightly coupled files, long silences,
verbatim warnings from past authors — and condenses them into a single **Risk
Card**. Then it hands that card to you, your CI, or the AI that's about to edit
the code.

## Who is this for?

WhyCode is most valuable in moments when *the wrong edit hurts.* If you
recognise yourself in any of these, it'll pay rent:

- **Solo dev returning to a 3-month-old side project** — `whycode why <file>`
  to remember why the weird bit is weird before the AI "fixes" it.
- **Senior engineer joining an unfamiliar codebase** — `whycode scan` to map
  the load-bearing walls; `whycode why` before each first edit.
- **AI-paired developer** — install the MCP server and your editor's
  assistant can read invariants and incident history *before* it refactors.
- **Tech lead reviewing PRs** — `whycode diff` ranks the PR's files by risk
  so attention goes to the scary changes first.
- **CI / pre-commit gating** — `whycode diff --fail-on history` blocks the
  build when high-risk files change without explanation.
- **SRE doing a 3am hotfix** — `whycode why <file> --brief` for a one-line
  "what should I be afraid of?" sanity check.
- **Junior dev wanting to ship without breaking things** — `whycode scan`
  shows where to step lightly.

What WhyCode is **not** for: replacing `git blame`, telling you *what*
changed, or suggesting fixes. It tells you the *why* and the *risk*. You
decide what to do.

## Install

```bash
pip install whycode-cli
```

The PyPI distribution is `whycode-cli` (the bare name `whycode` was already
taken by an unrelated project); the installed command is still `whycode`.

From source, if you want to track `main`:

```bash
pip install git+https://github.com/fangshuor/WhyCode.git
```

Requires Python 3.11+.

## 60-second tour

```bash
cd /path/to/your/repo

whycode tour                        # the one command to run first
whycode init                        # one-command setup: CI workflow + pre-commit gate
whycode highlights                  # repo-wide treasure map: top decisions + incidents
whycode why src/some/file.py        # the Risk Card for one file
whycode why src/some/file.py -b     # one-line summary (for triage / scripts)
whycode why src/some/file.py --at <sha>     # risk as of a past commit
whycode why src/some/file.py --mute <kind>  # local "this signal is wrong, hide it" feedback
whycode diff                        # rank everything you changed vs origin/main
whycode diff --staged               # ditto, for files staged for commit
whycode diff --fail-on history      # CI gate: exit 1 if any file is ≥ READ HISTORY FIRST
whycode show <sha>                  # classification + per-file risk for one commit
whycode timeline src/some/file.py   # risk score evolution across the file's history
whycode honest src/some/file.py     # every invariant line, verbatim, untruncated
whycode scan --top 10               # the riskiest files in the whole repo
whycode mcp -v                      # MCP server with tool-call logging
```

That's it. No config file, no daemon, no account, no upload.

### What a Risk Card looks like

```
╭─  READ HISTORY FIRST  · 57 ──────────────────────────────────────╮
│ src/payment/refund.py   (24 commits)                             │
╰──────────────────────────────────────────────────────────────────╯
  src/payment/refund.py is 24 commits old, primarily authored by
  Mei Chen, last touched 12 days ago.

   HIGH    3 reverts touched this file
           9d2e7a1 reverts c5b81fe; bbf441c reverts 4d29ab0; …
           → Read both sides — git show 9d2e7a1 then git show c5b81fe
             — to learn what was tried and why it broke.

   MED     2 incident-flagged changes in history
           2 commits matched incident keywords (latest 12 days ago:
           'hotfix: idempotency token regression').
           evidence: a3f4b2c, 7e22a04
           → Read git show a3f4b2c to see the incident-flavoured
             change in context.

   MED     2 invariants stated by past authors
             > Do not switch to async — v1 clients break.  (4d29ab0)
             > Important: keep the legacy header in place. (c5b81fe)
           → Honour the invariant: Do not switch to async — v1 clients break.
```

### What a `whycode diff` briefing looks like

```
8 file(s) changed vs origin/main

 HANDLE WITH CARE   (2)
   src/payment/refund.py     3 reverts touched this file
   src/payment/charge.py     primary author last active 1100 days ago

 READ HISTORY FIRST   (2)
   src/api/auth.py           tightly coupled to 4 other files
   src/foo.py                2 invariants stated by past authors

 WORTH A LOOK   (1)
   src/bar.py                tightly coupled to 3 other files

+ 3 file(s) with no risk signals — pass --show-clear to list
```

The output groups files by band so a reviewer scanning a 50-file PR sees
the cluster of HANDLE WITH CARE files first instead of having to read
top-to-bottom. The CLEAR bucket (no risk signals) collapses to a count
line by default; pass `--show-clear` to expand it.

Score interpretation:

| Score   | Band                | What to do                              |
| ------- | ------------------- | --------------------------------------- |
| 75–100  | HANDLE WITH CARE    | Stop. Read the linked commits first.    |
| 50–74   | READ HISTORY FIRST  | At least skim the top signal.           |
| 25–49   | WORTH A LOOK        | One thing might bite you. Glance.       |
| 0–24    | NO FLAGS            | Quiet history — but read the diff anyway. |

### "But why exactly did this fire?" — `--explain`

When a signal looks wrong (or you just want to understand the reasoning
before trusting the tool), pass `--explain`. The per-signal next_step line
is replaced by the rule trace: which branch of the ladder fired, the
literal evidence it looked at, and the source location of the ladder
branch:

```
$ whycode why src/payment/refund.py --explain

   MED     1 incident-flagged change in history
           1 commit matched incident keywords (latest 12 days ago:
           'hotfix: idempotency token regression').
           evidence: a3f4b2c
           ─ rule: incident_subject_keyword  src/whycode/git_facts.py:find_incidents
             fired because: subject 'hotfix: idempotency token regression'
                            matched the literal token 'hotfix'
             evidence: hotfix
```

Default output is unchanged. `--explain --json` adds an `explanation`
key per signal. The flag covers L1+L2 detectors only; if you also pass
`--llm`, the L3 decision block is unaffected.

## The killer use case: hand it to your AI editor

WhyCode is also an MCP server. Configure it in any MCP-aware editor or
assistant, and the host LLM can pull a Risk Card before it edits any file.

```json
{
  "mcpServers": {
    "whycode": { "command": "whycode", "args": ["mcp"] }
  }
}
```

Drop that snippet into your editor's MCP configuration file (location varies
by editor — check your editor's MCP docs). Then in any chat:

> *"Refactor the refund flow."*

A well-configured assistant will call `get_risk_profile("src/payment/refund.py")`
first and read the warnings before it changes a line. Run `whycode mcp -v`
during development to log every tool call to stderr so you can verify the
integration is actually live.

Tools exposed:

- `get_risk_profile(path)` — full Risk Card.
- `get_file_decisions(path, limit=5)` — only the decision-flavoured signals
  (reverts, incidents, ghost keepers, invariant quotes).

## Wire it into git, CI, and your editor

WhyCode is most useful when it shows up automatically in the moments you'd
otherwise forget to look. The fast path:

```bash
whycode init
```

That installs two things:

- **`.git/hooks/pre-commit`** — runs `whycode diff --staged --fail-on handle`
  before every commit. HANDLE WITH CARE files can't be touched without an
  explicit `git commit --no-verify`.
- **`.github/workflows/whycode.yml`** — a GitHub Action that prints a
  risk-ranked table for every PR. Advisory by default (never blocks merging);
  append `--fail-on history` (or `handle` / `look`) to the diff line to turn
  it into a hard gate.

Tune the thresholds inside those two files for your repo. Re-run with
`whycode init --force` to overwrite.

**MCP server** — see the next section.

## Architecture (three layers, by design)

| Layer | What                                                                     | Network? | API key? |
| ----- | ------------------------------------------------------------------------ | -------- | -------- |
| 1     | Deterministic git facts (log, diffstat, revert pairs, author activity)   | no       | no       |
| 2     | Heuristic signals (reverts, incidents, silence, ghost keeper, coupling, invariants, churn, body-references, newborn) | no | no |
| 3     | LLM-extracted structured decisions (optional, opt-in, never on by default) | yes      | yes      |

**Layer 1 + Layer 2 produce the Risk Card by default. No model calls, no
data leaving your machine.** Layer 3 lifts the keyword fragments L1 + L2
extract ("do not switch to async") into structured decisions with the
*why* drawn from the surrounding commit body — but only when you ask for
it with `--llm`.

### Optional L3 — LLM-enriched decisions

Install the optional extras and configure the env vars:

```bash
pip install 'whycode-cli[llm]'
export WHYCODE_LLM_API_KEY="…"
export WHYCODE_LLM_MODEL="<your-provider's-model-identifier>"

whycode why src/some/file.py --llm        # full card + structured decisions
whycode why src/some/file.py --llm-dry-run  # see exactly what would be sent
```

Privacy contract: configuration is entirely environment-driven (no
hardcoded provider in the source tree); the SDK is lazy-imported (no
import cost unless you opt in); only L2-filtered high-signal commits
are sent (capped at 10 per call); a malformed model response degrades
to "no decisions" rather than crashing.

## What this is NOT

- ❌ Not a SaaS. No accounts, no cloud, no telemetry.
- ❌ Not a code review bot. WhyCode reports — never prescribes.
- ❌ Not a "what changed" tool. Plenty of those exist already.
- ❌ Not language-specific. We read git history, not your AST.
- ❌ Not a replacement for `git blame`. It's the briefing your senior would
  give you *before* you opened blame.

## Developing

```bash
git clone https://github.com/fangshuor/WhyCode.git
cd WhyCode
pip install -e '.[dev,mcp]'
ruff check . && mypy src/whycode/ && pytest -q
```

See [`ENGINEERING.md`](./ENGINEERING.md) for the engineering charter — the
durable rules this repo is built under.

## License

MIT. © 2026 Kevin.
