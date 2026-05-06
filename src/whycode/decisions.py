"""L3 — LLM-enriched decision extraction.

What L1+L2 give: a regex-level harvest of single lines like
``"Do not switch to async"``. What L3 adds: structured decisions with
the full *why* drawn from the surrounding commit body.

Structured decision schema (one ``Decision`` per finding):

    {
      "decision_type": "incident_fix" | "compat_workaround" | "perf_rewrite"
                       | "rollback" | "constraint" | "other",
      "what_changed":  "one sentence summary",
      "why":           "one paragraph; quotes from the body where possible",
      "do_not":        "actionable constraint, or null",
      "evidence":      ["<sha1>", "<sha2>", …],
      "confidence":    0.0 - 1.0
    }

Confidence < ``min_confidence`` is filtered out before return — better to
emit nothing than emit a dressed-up guess. Privacy: this module makes a
network call only if ``call_llm`` is invoked, which only happens when the
caller passed commits in. Layer 1 and Layer 2 never reach this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from whycode.git_facts import Commit
from whycode.llm import LLMCallError, LLMConfigError, call_llm

DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_MAX_COMMITS = 10

_SYSTEM = (
    "You are a careful code-history archaeologist. You read commit messages "
    "and surface the engineering decisions that future readers will need to "
    "respect. You never invent facts; if a commit body does not state a "
    "decision worth carrying forward, you emit nothing for that commit. "
    "All quotes you produce must be drawn from the commit body itself; "
    "summarise rather than paraphrase when you cannot quote."
)

_PROMPT_TEMPLATE = """Below are commits from a Git repository. For each commit, extract a structured Decision **only when the commit body genuinely states one**. Otherwise emit nothing for that commit.

A Decision has this shape:

  {{
    "decision_type": one of
        "incident_fix" | "compat_workaround" | "perf_rewrite" |
        "rollback" | "constraint" | "other",
    "what_changed":  one-sentence summary of the change itself,
    "why":           one paragraph drawn from the body (quote where possible),
    "do_not":        the actionable constraint a future editor must respect,
                     or null if none stated,
    "evidence":      array of commit SHAs supporting this decision,
    "confidence":    a float in [0, 1] reflecting how clearly the body
                     states this decision (use < 0.5 if you are unsure)
  }}

Rules:
  - Reply with a JSON array of Decision objects, no prose, no code fences.
  - Empty array if nothing qualifies.
  - Quote rather than rephrase when stating "why".
  - Do not infer constraints that are not in the body.
  - Skip commits whose body is just a release note, dependency bump, or
    one-line fix without explanation.

COMMITS:

{commits}
"""


@dataclass(frozen=True)
class Decision:
    decision_type: str
    what_changed: str
    why: str
    do_not: str | None
    evidence: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_type": self.decision_type,
            "what_changed": self.what_changed,
            "why": self.why,
            "do_not": self.do_not,
            "evidence": list(self.evidence),
            "confidence": round(self.confidence, 2),
        }


def _format_commits_for_prompt(commits: Sequence[Commit]) -> str:
    parts: list[str] = []
    for c in commits:
        parts.append(f"COMMIT {c.sha[:12]}  ({c.author_name}, {c.authored_at.date()})")
        parts.append(f"Subject: {c.subject}")
        if c.body:
            parts.append(f"Body:\n{c.body}")
        parts.append("---")
    return "\n".join(parts)


_VALID_TYPES = frozenset(
    {
        "incident_fix",
        "compat_workaround",
        "perf_rewrite",
        "rollback",
        "constraint",
        "other",
    }
)


def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    return raw.strip()


def _parse_decisions(raw: str, valid_shas: Sequence[str]) -> list[Decision]:
    """Lenient parser. Bad JSON → empty list (we do not crash on a bad model
    response). Missing fields default to empty/zero. Invalid evidence SHAs
    are dropped silently."""
    text = _strip_code_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    short_lookup = {s[:12]: s for s in valid_shas}
    out: list[Decision] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            decision_type = str(item.get("decision_type", "other"))
            if decision_type not in _VALID_TYPES:
                decision_type = "other"
            what_changed = str(item.get("what_changed", "")).strip()
            why = str(item.get("why", "")).strip()
            do_not_raw = item.get("do_not")
            do_not = str(do_not_raw).strip() if do_not_raw else None
            raw_evidence = item.get("evidence", []) or []
            evidence: list[str] = []
            for token in raw_evidence:
                t = str(token).strip()
                # Accept full or 12-char prefix SHAs that match what we sent.
                if t in short_lookup:
                    evidence.append(short_lookup[t])
                elif len(t) >= 12 and t[:12] in short_lookup:
                    evidence.append(short_lookup[t[:12]])
            if not evidence and valid_shas:
                evidence = [valid_shas[0]]
            confidence = float(item.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            continue
        if not what_changed or not why:
            continue
        out.append(
            Decision(
                decision_type=decision_type,
                what_changed=what_changed,
                why=why,
                do_not=do_not,
                evidence=tuple(evidence),
                confidence=confidence,
            )
        )
    return out


def estimate_payload(commits: Sequence[Commit]) -> tuple[int, int]:
    """Return ``(commit_count, prompt_char_count)`` so callers can show the
    user the exact size of what would be sent before invoking the network.
    """
    if not commits:
        return 0, 0
    prompt = _PROMPT_TEMPLATE.format(commits=_format_commits_for_prompt(commits))
    return len(commits), len(prompt) + len(_SYSTEM)


def extract_decisions(
    commits: Sequence[Commit],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Decision]:
    """Send ``commits`` to the configured LLM and parse structured decisions.

    Raises ``LLMConfigError`` when the environment is not set up; raises
    ``LLMCallError`` on transport / API failure. Returns ``[]`` on empty
    input or a malformed model response.
    """
    if not commits:
        return []
    prompt = _PROMPT_TEMPLATE.format(commits=_format_commits_for_prompt(commits))
    raw = call_llm(prompt, _SYSTEM)
    decisions = _parse_decisions(raw, [c.sha for c in commits])
    return [d for d in decisions if d.confidence >= min_confidence]


__all__ = [
    "DEFAULT_MAX_COMMITS",
    "DEFAULT_MIN_CONFIDENCE",
    "Decision",
    "LLMCallError",
    "LLMConfigError",
    "estimate_payload",
    "extract_decisions",
]
