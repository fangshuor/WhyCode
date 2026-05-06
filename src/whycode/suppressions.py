"""Local suppression list — the "this signal is wrong, hide it" feedback loop.

Stored at ``<repo>/.whycode/suppressed.json``.  ``.whycode/`` is gitignored,
so the list is per-developer and never travels with the repo.  This honours
the privacy contract: no central DB, no telemetry, no cross-team sharing of
"these signals are bogus" data.

Schema (intentionally tiny — easy to hand-edit):

    {
      "suppressed": [
        ["src/foo.py", "incident_history"],
        ["src/bar.py", "ghost_keeper"]
      ]
    }

Match is exact ``(path, kind)`` — no globs.  We can add patterns if real
users ask for them; for now precision beats convenience.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from whycode.signals import SignalKind

if TYPE_CHECKING:
    from whycode.signals import Signal

_FILENAME = "suppressed.json"
_DIRNAME = ".whycode"


@dataclass
class SuppressionList:
    entries: list[tuple[str, str]] = field(default_factory=list)

    def is_suppressed(self, path: str, kind: SignalKind) -> bool:
        return (path, kind.value) in self.entries

    def add(self, path: str, kind: SignalKind) -> bool:
        """Idempotent. Returns True if a new entry was added."""
        pair = (path, kind.value)
        if pair in self.entries:
            return False
        self.entries.append(pair)
        return True

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def _path(repo_root: Path) -> Path:
    return repo_root / _DIRNAME / _FILENAME


def load(repo_root: Path) -> SuppressionList:
    """Return the suppression list for ``repo_root``. Empty if no file exists."""
    target = _path(repo_root)
    if not target.exists():
        return SuppressionList()
    try:
        raw = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        # A corrupt file should not stop ``whycode why`` from running.
        # Treat as empty and let the user see what happened on next save.
        return SuppressionList()
    items = raw.get("suppressed", [])
    out: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, list) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return SuppressionList(entries=out)


def save(repo_root: Path, sl: SuppressionList) -> None:
    target = _path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"suppressed": [list(pair) for pair in sl.entries]}
    target.write_text(json.dumps(payload, indent=2) + "\n")


def resolve_kind(token: str) -> SignalKind:
    """Resolve a user-supplied kind name (or unique prefix) to a SignalKind.

    Raises ValueError when the token is unknown or matches multiple kinds.
    """
    needle = token.lower().replace("-", "_").strip()
    if not needle:
        raise ValueError("empty signal kind")
    matches = [k for k in SignalKind if needle in k.value]
    if not matches:
        raise ValueError(
            f"unknown signal kind: {token!r}. "
            f"Known: {', '.join(k.value for k in SignalKind)}"
        )
    if len(matches) > 1:
        names = ", ".join(k.value for k in matches)
        raise ValueError(f"ambiguous: {token!r} matches multiple kinds: {names}")
    return matches[0]


def filter_signals(
    signals: Iterable[Signal], suppressions: SuppressionList, path: str
) -> list[Signal]:
    """Drop signals whose ``(path, kind)`` is in the suppression list."""
    return [s for s in signals if not suppressions.is_suppressed(path, s.kind)]


__all__ = [
    "SuppressionList",
    "filter_signals",
    "load",
    "resolve_kind",
    "save",
]
