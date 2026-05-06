"""Tests for the risk scorer."""

from __future__ import annotations

from whycode.scorer import Band, score
from whycode.signals import Signal, SignalKind


def _sig(kind: SignalKind, severity: int) -> Signal:
    return Signal(kind=kind, severity=severity, headline="x", detail="x")


def test_no_signals_yields_no_flags() -> None:
    s = score([])
    assert s.value == 0
    assert s.band is Band.NO_FLAGS


def test_single_low_signal_below_threshold() -> None:
    s = score([_sig(SignalKind.NEWBORN, 1)])
    assert s.band in (Band.NO_FLAGS, Band.WORTH_A_LOOK)


def test_revert_plus_incident_pushes_into_handle_with_care() -> None:
    s = score([
        _sig(SignalKind.REVERT_CHAIN, 5),
        _sig(SignalKind.INCIDENT_HISTORY, 4),
    ])
    assert s.value >= 75
    assert s.band is Band.HANDLE_WITH_CARE


def test_score_capped_at_100() -> None:
    s = score(
        [_sig(k, 5) for k in SignalKind] * 3,
    )
    assert s.value == 100
