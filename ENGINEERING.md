# WhyCode — Engineering Charter

This file is the durable working contract for this repo. It is the source of
truth for scope, architecture, and commit policy. **Read it. Follow it. Update
it when reality changes.**

---

## 1. The product, in one sentence

> **WhyCode tells you — and the AI editing your code — what you should be afraid
> of before touching a file, by mining what your repo already remembers.**

Not history. Not blame. Not a graph nobody reads. A **risk card**: the
shortest possible briefing that a careful engineer would want before changing
this file.

## 2. The single product question (MVP scope guard)

Every feature must justify itself by answering:

> "Does this make `whycode why <file>` more trustworthy or more useful **the
> first time** a stranger runs it on a real repo?"

If the answer is no, it does not belong in MVP. Park it in `docs/IDEAS.md`.

## 3. The three-layer architecture (non-negotiable)

| Layer | Name             | Tech                | Role                                         |
| ----- | ---------------- | ------------------- | -------------------------------------------- |
| L1    | Deterministic    | git plumbing + sqlite | Facts. No interpretation. Never hallucinates. |
| L2    | Heuristic        | pure Python rules   | Signal extraction. Filters noise to gold.    |
| L3    | LLM (optional)   | configurable LLM    | Decision summarisation. Off by default.      |

**Hard rules:**
- L1 must run with zero network and zero API key. Always.
- L2 must run with zero network. Rules must be auditable in plain Python.
- L3 is opt-in. The CLI must produce a useful Risk Card with **L1+L2 only**.
- Every L3 output must carry a `confidence` field and an `evidence` list of
  commit SHAs / PR URLs. No evidence → do not show.

## 4. Definition of "useful Risk Card"

A Risk Card is **useful** if, on a real mid-sized repo (≥ 500 commits), it
produces at least one of these without LLM help:

1. A revert/hotfix chain pointing at this file.
2. A "knowledge keeper" assignment (or a "no keeper" warning).
3. A coupling alert (files that change together ≥ N times).
4. A silence flag (untouched > 6 months but in production).
5. A counterfactual hit (something that was tried and reverted here).

If none of those fire, the card must say so honestly. **Empty is allowed.
Lying is not.**

## 5. Work boundaries (what NOT to build, ever, in MVP)

- ❌ No SaaS. No accounts. No login. No cloud DB.
- ❌ No web app beyond a single static HTML viewer (post-MVP).
- ❌ No Slack / Jira / Linear integrations.
- ❌ No "AI suggests a fix". WhyCode reports, never prescribes.
- ❌ No language-specific AST parsing. Text + git metadata only.
- ❌ No telemetry of any kind.
- ❌ No auto-pushing of analysis to remotes.

## 6. Privacy contract (the user must trust this)

- All data stays in `.whycode/` next to the repo, gitignored by default.
- The CLI must print **exactly which network calls it would make** before any
  L3 invocation, and require an explicit `--llm` flag to enable them.
- No commit content, file content, PR text leaves the machine without `--llm`.
- We never read `.env`, secrets, or anything outside the git history.

## 7. Tech choices (locked for MVP)

- **Language**: Python 3.11+. Reason: ship speed, MCP SDK, sqlite stdlib.
- **CLI**: Typer (Click under the hood, prettier API).
- **Storage**: SQLite via stdlib `sqlite3`. No ORM.
- **Tests**: pytest. Synthetic git repos built in fixtures.
- **Lint/format**: ruff (one tool, both jobs). No black, no isort.
- **Type check**: mypy strict on `src/whycode/`.
- **MCP**: `mcp` Python SDK (stdio transport).
- **No build system beyond `pip install -e .`**.

## 8. Repository layout

```
WhyCode/
├── ENGINEERING.md             # this file
├── README.md                  # public pitch + quickstart
├── LICENSE                    # MIT, (c) Kevin
├── pyproject.toml             # project + tool config
├── .gitignore
├── .whycode/                  # local cache, gitignored
├── src/whycode/
│   ├── __init__.py
│   ├── __main__.py            # python -m whycode
│   ├── cli.py                 # `whycode` CLI entrypoint
│   ├── git_facts.py           # L1: deterministic git plumbing
│   ├── signals.py             # L2: heuristic signal extraction
│   ├── risk_card.py           # rendering the card (text + JSON)
│   ├── scorer.py              # risk score calculation
│   ├── db.py                  # sqlite cache (idempotent)
│   ├── mcp_server.py          # MCP stdio server
│   └── _terminal.py           # ANSI rendering helpers
└── tests/
    ├── conftest.py            # synthetic repo fixtures
    ├── test_git_facts.py
    ├── test_signals.py
    ├── test_scorer.py
    └── test_cli.py
```

## 9. Commit policy

- **Author = Kevin.** Solo project; no external trailers in commit messages.
- **Branch**: feature work happens on a short-named branch (e.g. `feature/<name>`);
  `main` is the canonical history.
- **Granularity**: one logical change per commit. A commit is the smallest
  unit that compiles, tests-green, and tells a coherent story.
- **Message shape**:
  ```
  <area>: <imperative summary, ≤ 72 chars>

  <optional why-paragraph: what changed, why now, what was rejected>
  ```
  Examples of `<area>`: `cli`, `l1`, `l2`, `mcp`, `tests`, `docs`, `infra`.
- **No `--no-verify`**. Fix the hook failure or fix the code.
- **No amend** of pushed commits.

## 10. Definition of Done for the MVP

The MVP is **done** when all of the following are true on a clean checkout:

1. `pip install -e .` succeeds with no errors.
2. `whycode why <any-file-in-this-repo>` returns a Risk Card in < 5 seconds
   (cold) on this repo's history.
3. `whycode mcp` starts an MCP server that exposes at least:
   - `get_risk_profile(path: str) -> RiskCard`
   - `get_file_decisions(path: str, limit: int = 5) -> list[Decision]`
   - and is reachable from any MCP-aware client as configured by a one-line
     snippet in the README.
4. `pytest` passes with ≥ 80% coverage on `src/whycode/{git_facts,signals,scorer}.py`.
5. `ruff check .` and `mypy src/whycode/` both pass with no errors.
6. README contains a 30-second pitch, a quickstart, and a "what this is NOT"
   section copied from §5 above.
7. Running `whycode why src/whycode/git_facts.py` (dogfood) returns at least
   ONE non-trivial signal.

## 11. Working loop (every session)

1. Read this file. Confirm scope.
2. Pick the next pending todo. **One in_progress at a time.**
3. Write the test or the spec first when feasible. Otherwise write the code,
   then a regression test.
4. Run `ruff check . && mypy src/whycode/ && pytest -q` before each commit.
5. Commit with a descriptive message. Push when a meaningful unit lands.
6. Update ENGINEERING.md if the rules of the world changed (rare). Keep
   PR-style discipline: small, reviewable, reversible.

## 12. Anti-patterns specifically banned in this repo

- "Just make it work" code with `# TODO: clean up later`. We don't.
- Speculative abstractions. No `BaseSignalProvider` until we have **three**
  concrete ones.
- Vendoring or wrapping code we could `import` from stdlib.
- LLM calls without a corresponding fixture/recording-based test.
- Pretty-printing logic mixed into business logic. Keep `risk_card.py`
  rendering-only.
- Comments that explain WHAT (the code shows that). Comments only for
  surprising WHY.

---

_Last updated when this file changes via a real commit._
