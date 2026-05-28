"""Shared test helpers.

The snapshot helper: tests compare rendered output against committed snapshot
files under tests/snapshots/. To regenerate snapshots after an intentional
rendering change, run with `UPDATE_SNAPSHOTS=1 pytest`.
"""

from __future__ import annotations

import os
from pathlib import Path

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


def assert_matches_snapshot(actual: str, snapshot_name: str) -> None:
    """Compare `actual` against tests/snapshots/{snapshot_name}.

    If UPDATE_SNAPSHOTS=1 in env, write the snapshot file instead. The file's
    contents become the new expectation on subsequent runs.
    """
    path = SNAPSHOTS_DIR / snapshot_name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    if not path.exists():
        raise AssertionError(
            f"Snapshot {path} does not exist. Run with UPDATE_SNAPSHOTS=1 to create it."
        )
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        # Helpful diff on first ~20 differing chars
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(actual, expected, strict=False)) if a != b),
            min(len(actual), len(expected)),
        )
        raise AssertionError(
            f"Snapshot {snapshot_name} mismatch.\n"
            f"First difference at offset {first_diff}.\n"
            f"Expected (around diff): {expected[max(0, first_diff - 40) : first_diff + 40]!r}\n"
            f"Actual   (around diff): {actual[max(0, first_diff - 40) : first_diff + 40]!r}"
        )
