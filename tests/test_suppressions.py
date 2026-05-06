"""Tests for the local suppression list."""

from __future__ import annotations

import json

import pytest

from whycode import suppressions as sup
from whycode.signals import SignalKind


def test_empty_when_no_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sl = sup.load(tmp_path)
    assert len(sl) == 0
    assert not sl.is_suppressed("foo.py", SignalKind.NEWBORN)


def test_add_save_load_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sl = sup.load(tmp_path)
    assert sl.add("src/a.py", SignalKind.INCIDENT_HISTORY) is True
    assert sl.add("src/a.py", SignalKind.INCIDENT_HISTORY) is False  # idempotent
    sl.add("src/b.py", SignalKind.GHOST_KEEPER)
    sup.save(tmp_path, sl)

    reloaded = sup.load(tmp_path)
    assert reloaded.is_suppressed("src/a.py", SignalKind.INCIDENT_HISTORY)
    assert reloaded.is_suppressed("src/b.py", SignalKind.GHOST_KEEPER)
    assert not reloaded.is_suppressed("src/a.py", SignalKind.GHOST_KEEPER)
    assert not reloaded.is_suppressed("src/c.py", SignalKind.INCIDENT_HISTORY)


def test_corrupt_file_treated_as_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / ".whycode" / "suppressed.json"
    target.parent.mkdir()
    target.write_text("not json {")
    sl = sup.load(tmp_path)
    assert len(sl) == 0  # graceful degradation


def test_save_creates_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sl = sup.SuppressionList()
    sl.add("a.py", SignalKind.SILENCE)
    sup.save(tmp_path, sl)
    assert (tmp_path / ".whycode" / "suppressed.json").exists()
    payload = json.loads((tmp_path / ".whycode" / "suppressed.json").read_text())
    assert payload == {"suppressed": [["a.py", "silence"]]}


def test_resolve_kind_exact_match() -> None:
    assert sup.resolve_kind("incident_history") is SignalKind.INCIDENT_HISTORY


def test_resolve_kind_prefix_match() -> None:
    assert sup.resolve_kind("revert") is SignalKind.REVERT_CHAIN
    assert sup.resolve_kind("ghost") is SignalKind.GHOST_KEEPER
    assert sup.resolve_kind("invariant") is SignalKind.INVARIANT_QUOTE


def test_resolve_kind_dash_normalised() -> None:
    assert sup.resolve_kind("incident-history") is SignalKind.INCIDENT_HISTORY


def test_resolve_kind_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown signal kind"):
        sup.resolve_kind("nonsense")


def test_resolve_kind_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        sup.resolve_kind("")


def test_filter_signals_drops_suppressed() -> None:
    from whycode.signals import Signal

    sigs = [
        Signal(kind=SignalKind.INCIDENT_HISTORY, severity=3, headline="x", detail="x"),
        Signal(kind=SignalKind.SILENCE, severity=2, headline="y", detail="y"),
    ]
    sl = sup.SuppressionList()
    sl.add("foo.py", SignalKind.INCIDENT_HISTORY)
    out = sup.filter_signals(sigs, sl, "foo.py")
    assert len(out) == 1
    assert out[0].kind is SignalKind.SILENCE


def test_filter_signals_path_specific() -> None:
    from whycode.signals import Signal

    sigs = [Signal(kind=SignalKind.INCIDENT_HISTORY, severity=3, headline="x", detail="x")]
    sl = sup.SuppressionList()
    sl.add("other.py", SignalKind.INCIDENT_HISTORY)
    # Suppression is for other.py — must not affect foo.py.
    out = sup.filter_signals(sigs, sl, "foo.py")
    assert len(out) == 1
