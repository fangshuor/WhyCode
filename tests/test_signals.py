"""Tests for Layer 2 — heuristic signals."""

from __future__ import annotations

from whycode import git_facts as gf
from whycode import signals as sig


def _facts_for(repo, path: str):  # type: ignore[no-untyped-def]
    return gf.gather(repo.root, path)


def test_no_signals_for_quiet_recent_file(repo, now) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init: add a", {"a.txt": "1"}, when=now)
    facts = _facts_for(repo, "a.txt")
    signals = sig.all_signals(facts)
    # The only signal should be NEWBORN (very young file).
    kinds = {s.kind for s in signals}
    assert kinds.issubset({sig.SignalKind.NEWBORN})


def test_revert_chain_fires(repo, now, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit(
        "feat: payment refactor",
        {"refund.py": "v1"},
        when=days_ago(60),
    )
    repo.revert(sha, when=days_ago(55))
    facts = _facts_for(repo, "refund.py")
    signals = sig.all_signals(facts)
    kinds = {s.kind for s in signals}
    assert sig.SignalKind.REVERT_CHAIN in kinds


def test_revert_chain_recent_keeps_full_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(60))
    repo.revert(sha, when=days_ago(50))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    # 1 revert → base severity 3, < 2yr → no decay → 3
    assert signal.severity == 3
    assert "weeks ago" in signal.headline or "month" in signal.headline


def test_revert_chain_old_decays_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # A revert from > 5 years ago should weigh at least one step less than a fresh one.
    sha = repo.commit("feat: A", {"x.py": "1"}, when=days_ago(2200))
    repo.revert(sha, when=days_ago(2100))
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_revert_chain(facts)
    assert signal is not None
    # 1 revert → base 3, > 5yr → decay 2 → 1
    assert signal.severity == 1
    assert "year" in signal.headline


def test_invariant_quote_old_decays_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.\nImportant: legacy header.",
        when=days_ago(800),  # ~2.2 years ago
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    # 2 invariants → base 3, 2-5yr → decay 1 → 2
    assert signal.severity == 2
    assert "year" in signal.headline or "month" in signal.headline


def test_incident_history_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("feat: refunds", {"refund.py": "1"}, when=days_ago(120))
    repo.commit(
        "hotfix: refund double-charge",
        {"refund.py": "2"},
        body="See incident #INC-447",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "refund.py")
    signal = sig.detect_incident_history(facts)
    assert signal is not None
    assert signal.severity >= 3


def test_invariant_quotes_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"x.py": "1"},
        body="Do not switch to async — v1 clients break.\nImportant: legacy header must stay.",
        when=days_ago(30),
    )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert "Do not switch to async" in signal.detail or "legacy header" in signal.detail.lower()


def test_silence_fires_for_old_files(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"old.py": "1"}, when=days_ago(400))
    facts = _facts_for(repo, "old.py")
    signal = sig.detect_silence(facts)
    assert signal is not None
    assert signal.severity >= 2


def test_coupling_fires_when_files_co_change_often(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit("init", {"a.py": "1", "b.py": "1"}, when=days_ago(60))
    repo.commit("change", {"a.py": "2", "b.py": "2"}, when=days_ago(50))
    repo.commit("change", {"a.py": "3", "b.py": "3"}, when=days_ago(40))
    repo.commit("change", {"a.py": "4", "b.py": "4"}, when=days_ago(30))
    facts = _facts_for(repo, "a.py")
    signal = sig.detect_coupling(facts)
    assert signal is not None
    assert "b.py" in signal.detail


def test_high_churn_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    for i in range(7):
        repo.commit(f"tweak {i}", {"hot.py": str(i)}, when=days_ago(80 - i * 5))
    facts = _facts_for(repo, "hot.py")
    signal = sig.detect_high_churn(facts)
    assert signal is not None


def test_ghost_keeper_fires(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Ghost author committed > GHOST_KEEPER_DAYS ago, never since.
    repo.commit(
        "init", {"legacy.py": "1"},
        when=days_ago(800),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    repo.commit(
        "tweak", {"legacy.py": "2"},
        when=days_ago(750),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    facts = _facts_for(repo, "legacy.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    assert signal.severity >= 3


def test_ghost_keeper_uses_line_ownership_to_promote_severity(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Ghost wrote almost all the lines; severity should reflect dominant ownership.
    body = "\n".join(f"line {i}" for i in range(20)) + "\n"
    repo.commit(
        "feat: big initial commit", {"core.py": body},
        when=days_ago(900),
        author_name="Old Owner",
        author_email="ghost@example.com",
    )
    # One small drive-by commit by an active author shouldn't take ownership away.
    repo.commit(
        "fix: typo", {"core.py": body + "# tiny comment\n"},
        when=days_ago(5),
        author_name="Active Bob",
        author_email="bob@example.com",
    )
    facts = _facts_for(repo, "core.py")
    signal = sig.detect_ghost_keeper(facts)
    assert signal is not None
    # Old Owner still owns ~20 of 21 lines → ghost keeper still fires.
    assert "ghost@example.com" not in signal.detail  # email should NOT leak; name should
    assert "Old Owner" in signal.detail
    assert "lines" in signal.detail.lower()


def test_ghost_keeper_silent_when_line_owner_still_active(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Old contributor wrote lines and then left; but a CURRENT author rewrote them.
    repo.commit(
        "feat: original", {"x.py": "old line\n"},
        when=days_ago(900),
        author_name="Old",
        author_email="old@example.com",
    )
    # Active author rewrites the whole file recently.
    new_body = "\n".join(f"new line {i}" for i in range(15)) + "\n"
    repo.commit(
        "refactor: rewrite", {"x.py": new_body},
        when=days_ago(10),
        author_name="Active",
        author_email="active@example.com",
    )
    facts = _facts_for(repo, "x.py")
    # Although `Old` has more time-since-activity, they don't own current lines.
    # The active author owns them. So no ghost keeper.
    signal = sig.detect_ghost_keeper(facts)
    assert signal is None


def test_newborn_suppressed_when_other_signals_fire(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    repo.commit(
        "compat: keep sync path",
        {"y.py": "1"},
        body="Do not switch to async.",
        when=days_ago(2),
    )
    facts = _facts_for(repo, "y.py")
    signals = sig.all_signals(facts)
    kinds = {s.kind for s in signals}
    assert sig.SignalKind.INVARIANT_QUOTE in kinds
    assert sig.SignalKind.NEWBORN not in kinds


def test_invariant_subject_alone_does_not_match(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    """A commit subject mentioning 'invariant' is meta, not a stated invariant."""
    repo.commit(
        "fix: tighten invariant matcher",
        {"z.py": "1"},
        body="Just a refactor — no behaviour change.",
        when=days_ago(2),
    )
    facts = _facts_for(repo, "z.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is None


def test_first_sentence_does_not_split_at_short_prefix() -> None:
    # "1." should NOT be treated as a sentence break — too short to be useful.
    out = sig._first_sentence("1. The first item is the one to remember.", 80)
    assert out.startswith("1.")
    assert "first item" in out


def test_first_sentence_splits_at_real_boundary() -> None:
    out = sig._first_sentence(
        "Important: do not call this from threads. There is more text after.",
        120,
    )
    assert out == "Important: do not call this from threads."
    assert "more text" not in out


def test_invariant_signal_caps_at_three_bullets(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    # Spread the five invariants across five separate commits so the F2
    # per-commit cap of two does not apply (it only filters within a single
    # commit body — commits each contribute up to two genuine constraints).
    bodies = [
        "Do not switch to async — v1 clients break.",
        "Important: keep the legacy header in place.",
        "Warning: idempotency token must be unique.",
        "Workaround for upstream bug #5: noop when input is empty.",
        "Tradeoff: we accept ~10ms latency to keep correctness.",
    ]
    for i, body in enumerate(bodies):
        repo.commit(
            f"compat: constraint {i}", {"x.py": str(i)}, body=body, when=days_ago(20 + i)
        )
    facts = _facts_for(repo, "x.py")
    signal = sig.detect_invariant_quotes(facts)
    assert signal is not None
    assert "5 invariants stated" in signal.headline
    # Detail shows 3 bullets + a "...and 2 more" line.
    bullets = [line for line in signal.detail.splitlines() if line.strip().startswith("> ")]
    assert len(bullets) == 4
    assert "and 2 more" in bullets[-1]


def test_signals_sorted_by_severity_desc(repo, days_ago) -> None:  # type: ignore[no-untyped-def]
    sha = repo.commit("feature", {"x.py": "1"}, when=days_ago(40))
    repo.revert(sha, when=days_ago(35))
    repo.commit(
        "hotfix: corner case",
        {"x.py": "2"},
        body="incident #1",
        when=days_ago(10),
    )
    facts = _facts_for(repo, "x.py")
    signals = sig.all_signals(facts)
    severities = [s.severity for s in signals]
    assert severities == sorted(severities, reverse=True)
