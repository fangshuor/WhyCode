"""Tests for the L3 decision-extraction layer.

LLM calls are mocked; no real network is touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from whycode.decisions import (
    Decision,
    _parse_decisions,
    estimate_payload,
    extract_decisions,
)
from whycode.git_facts import Commit
from whycode.llm import LLMCallError, LLMConfigError


def _commit(
    sha: str = "a" * 40,
    subject: str = "compat: keep sync path",
    body: str = "Do not switch to async — v1 clients break.",
    author: str = "Mei",
) -> Commit:
    return Commit(
        sha=sha,
        author_name=author,
        author_email=f"{author.lower()}@example.com",
        authored_at=datetime(2025, 9, 14, tzinfo=UTC),
        subject=subject,
        body=body,
    )


def test_estimate_payload_zero_for_empty() -> None:
    assert estimate_payload([]) == (0, 0)


def test_estimate_payload_grows_with_commits() -> None:
    one = estimate_payload([_commit()])
    two = estimate_payload([_commit(), _commit(sha="b" * 40)])
    assert one[0] == 1
    assert two[0] == 2
    assert two[1] > one[1]


def test_parse_decisions_well_formed() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "compat_workaround",
                "what_changed": "Kept synchronous HTTP for refund flow.",
                "why": "v1 clients break under async.",
                "do_not": "Don't switch this to async.",
                "evidence": ["a" * 12],
                "confidence": 0.9,
            }
        ]
    )
    out = _parse_decisions(raw, ["a" * 40])
    assert len(out) == 1
    d = out[0]
    assert d.decision_type == "compat_workaround"
    assert d.confidence == 0.9
    assert d.evidence == ("a" * 40,)
    assert d.do_not is not None and "async" in d.do_not


def test_parse_decisions_unknown_type_normalised_to_other() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "made-up-category",
                "what_changed": "x",
                "why": "y",
                "do_not": None,
                "evidence": ["a" * 12],
                "confidence": 0.7,
            }
        ]
    )
    out = _parse_decisions(raw, ["a" * 40])
    assert len(out) == 1
    assert out[0].decision_type == "other"


def test_parse_decisions_strips_code_fence() -> None:
    raw = "```json\n[]\n```"
    assert _parse_decisions(raw, ["a" * 40]) == []
    raw_filled = '```json\n[{"decision_type":"other","what_changed":"x","why":"y","confidence":0.6,"evidence":[]}]\n```'
    out = _parse_decisions(raw_filled, ["a" * 40])
    assert len(out) == 1


def test_parse_decisions_garbage_returns_empty() -> None:
    assert _parse_decisions("not json", ["a" * 40]) == []
    assert _parse_decisions("", ["a" * 40]) == []
    assert _parse_decisions("{}", ["a" * 40]) == []  # not a list
    assert _parse_decisions("null", ["a" * 40]) == []


def test_parse_decisions_drops_invalid_evidence() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "constraint",
                "what_changed": "x",
                "why": "y",
                "evidence": ["zz" * 6, "a" * 12],  # first invalid, second valid
                "confidence": 0.8,
            }
        ]
    )
    out = _parse_decisions(raw, ["a" * 40])
    assert len(out) == 1
    assert out[0].evidence == ("a" * 40,)


def test_parse_decisions_drops_when_required_fields_empty() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "constraint",
                "what_changed": "",  # empty
                "why": "y",
                "confidence": 0.9,
                "evidence": ["a" * 12],
            }
        ]
    )
    assert _parse_decisions(raw, ["a" * 40]) == []


def test_extract_filters_below_min_confidence() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "constraint",
                "what_changed": "x",
                "why": "y",
                "confidence": 0.3,  # below default 0.5
                "evidence": ["a" * 12],
            }
        ]
    )
    with patch("whycode.decisions.call_llm", return_value=raw):
        out = extract_decisions([_commit()])
    assert out == []


def test_extract_keeps_above_min_confidence() -> None:
    raw = json.dumps(
        [
            {
                "decision_type": "compat_workaround",
                "what_changed": "Kept sync HTTP",
                "why": "v1 clients break",
                "confidence": 0.85,
                "evidence": ["a" * 12],
            }
        ]
    )
    with patch("whycode.decisions.call_llm", return_value=raw):
        out = extract_decisions([_commit()])
    assert len(out) == 1
    assert out[0].confidence == 0.85


def test_extract_returns_empty_on_no_commits() -> None:
    out = extract_decisions([])
    assert out == []


def test_extract_propagates_llm_config_error() -> None:
    def boom(*_a, **_kw) -> str:
        raise LLMConfigError("not configured")

    with (
        patch("whycode.decisions.call_llm", side_effect=boom),
        pytest.raises(LLMConfigError),
    ):
        extract_decisions([_commit()])


def test_extract_propagates_llm_call_error() -> None:
    def boom(*_a, **_kw) -> str:
        raise LLMCallError("network down")

    with (
        patch("whycode.decisions.call_llm", side_effect=boom),
        pytest.raises(LLMCallError),
    ):
        extract_decisions([_commit()])


def test_decision_to_dict_round_trips() -> None:
    d = Decision(
        decision_type="rollback",
        what_changed="Reverted async refactor",
        why="broke v1",
        do_not="don't try async again",
        evidence=("a" * 40,),
        confidence=0.92,
    )
    payload = d.to_dict()
    assert payload["decision_type"] == "rollback"
    assert payload["confidence"] == 0.92
    assert payload["do_not"] == "don't try async again"
    assert payload["evidence"] == ["a" * 40]
