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
pip install git+https://github.com/fangshuor/WhyCode.git
```

(Until WhyCode lands on PyPI. Requires Python 3.11+.)

## 60-second tour

```bash
cd /path/to/your/repo

whycode init                        # one-command setup: CI workflow + pre-commit gate
whycode why src/some/file.py        # the Risk Card for one file
whycode why src/some/file.py -b     # one-line summary (for triage / scripts)
whycode diff                        # rank everything you changed vs origin/main
whycode diff --staged               # ditto, for files staged for commit
whycode diff --fail-on history      # CI gate: exit 1 if any file is ≥ READ HISTORY FIRST
whycode show <sha>                  # classification + per-file risk for one commit
whycode scan --top 10               # the riskiest files in the whole repo
whycode mcp -v                      # MCP server with tool-call logging
```

That's it. No config file, no daemon, no account, no upload.

### What a Risk Card looks like

```
╭─  READ HISTORY FIRST  score 57/100 ──────────────────────────────╮
│ src/payment/refund.py   (24 commits)                             │
│ Latest: hotfix: idempotency token regression                     │
│         a3f4b2c1   Mei Chen   2025-09-14                         │
╰──────────────────────────────────────────────────────────────────╯
   HIGH    3 reverts touched this file
           9d2e7a1 reverts c5b81fe; bbf441c reverts 4d29ab0; …

   MED     2 incident-flagged changes in history
           2 commits matched incident keywords (latest 12 days ago:
           'hotfix: idempotency token regression').
           evidence: a3f4b2c, 7e22a04

   MED     2 invariants stated by past authors
             > Do not switch to async — v1 clients break.  (4d29ab0)
             > Important: keep the legacy header in place. (c5b81fe)

  → git show 9d2e7a1   to read the most relevant commit in full
```

Score interpretation:

| Score   | Band                | What to do                              |
| ------- | ------------------- | --------------------------------------- |
| 75–100  | HANDLE WITH CARE    | Stop. Read the linked commits first.    |
| 50–74   | READ HISTORY FIRST  | At least skim the top signal.           |
| 25–49   | WORTH A LOOK        | One thing might bite you. Glance.       |
| 0–24    | NO FLAGS            | Quiet history — but read the diff anyway. |

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
- **`.github/workflows/whycode.yml`** — a GitHub Action that risk-ranks every
  PR's files and fails the build at `--fail-on history` (≥ READ HISTORY FIRST).

Tune the `--fail-on` thresholds inside those two files for your repo. Re-run
with `whycode init --force` to overwrite.

**MCP server** — see the next section.

## Architecture (three layers, by design)

| Layer | What                                                                     | Network? | API key? |
| ----- | ------------------------------------------------------------------------ | -------- | -------- |
| 1     | Deterministic git facts (log, diffstat, revert pairs, author activity)   | no       | no       |
| 2     | Heuristic signals (reverts, incidents, silence, ghost keeper, coupling, invariants, churn, newborn) | no | no |
| 3     | LLM polish (optional, opt-in, never on by default)                       | yes      | yes      |

**Layer 1 + Layer 2 produce the Risk Card you saw above. No model calls, no
data leaving your machine.** Layer 3 is reserved for natural-language
summarisation of decisions and is strictly opt-in.

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
