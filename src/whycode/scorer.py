"""Risk scoring: signals -> a single 0..100 score and a human-readable band."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from whycode.signals import Signal, SignalKind

# Per-signal contribution at severity 1. Severity multiplies linearly up to 5.
# Coupling weight was 5 in 0.6.1 — the recon pass on click/flask/requests showed
# coupling-only files (CHANGES.rst, large test fixtures) topping out at 100 while
# files with 14 reverts only reached ~70. Coupling fires liberally on monorepos
# where every test file co-changes with every fixture; demoting the base weight
# rebalances the band cap without affecting kind ordering.
_BASE_WEIGHT: dict[SignalKind, int] = {
    SignalKind.REVERT_CHAIN: 9,
    SignalKind.INCIDENT_HISTORY: 8,
    SignalKind.GHOST_KEEPER: 6,
    SignalKind.INVARIANT_QUOTE: 6,
    SignalKind.HIGH_CHURN: 5,
    SignalKind.SUBJECT_BLIND_PIVOT: 5,
    SignalKind.BODY_REFERENCE: 4,
    SignalKind.COUPLING: 3,
    SignalKind.SILENCE: 3,
    SignalKind.NEWBORN: 1,
}

_BAND_THRESHOLDS = (
    (75, "HANDLE WITH CARE"),
    (50, "READ HISTORY FIRST"),
    (25, "WORTH A LOOK"),
    (0, "NO FLAGS"),
)


class Band(StrEnum):
    HANDLE_WITH_CARE = "HANDLE WITH CARE"
    READ_HISTORY_FIRST = "READ HISTORY FIRST"
    WORTH_A_LOOK = "WORTH A LOOK"
    NO_FLAGS = "NO FLAGS"


@dataclass(frozen=True)
class Score:
    value: int
    band: Band


def score(signals: Sequence[Signal]) -> Score:
    """Combine signals into a 0..100 risk score with a band label."""
    raw = 0
    for s in signals:
        weight = _BASE_WEIGHT.get(s.kind, 1)
        raw += weight * s.severity
    bounded = max(0, min(100, raw))
    label = next(label for thresh, label in _BAND_THRESHOLDS if bounded >= thresh)
    return Score(value=bounded, band=Band(label))


__all__ = ["Band", "Score", "score"]
