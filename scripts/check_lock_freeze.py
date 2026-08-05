#!/usr/bin/env python3
"""Compare a `pip freeze` dump to `requirements.lock`; exit non-zero on drift.

Used by verify.yml's `test-container` job so the test image is proven to
contain the shipped dependency set rather than trusted to. Missing packages
count as drift — skipping them would let an incomplete install pass as long
as whatever did install matched its pin.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_FREEZE_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(.+)$")
_LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def parse_freeze(text: str) -> dict[str, str]:
    installed: dict[str, str] = {}
    for line in text.splitlines():
        match = _FREEZE_LINE.match(line.strip())
        if match:
            installed[_normalize(match.group(1))] = match.group(2)
    return installed


def parse_lock(text: str) -> list[tuple[str, str]]:
    pinned: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _LOCK_LINE.match(line)
        if match:
            pinned.append((_normalize(match.group(1)), match.group(2)))
    return pinned


def compare_freeze_to_lock(freeze_text: str, lock_text: str) -> list[str]:
    """Return human-readable drift lines; empty means exact match.

    Raises ValueError when the lock parses to nothing, which means the
    caller's lock parse is broken rather than the image being empty.
    """

    installed = parse_freeze(freeze_text)
    pinned = parse_lock(lock_text)
    if not pinned:
        raise ValueError("Compared nothing -- the lock parse is broken, not the image.")

    drifted: list[str] = []
    for name, expected in pinned:
        actual = installed.get(name)
        if actual is None:
            drifted.append(f"  {name}: missing from image, lock pins {expected}")
        elif actual != expected:
            drifted.append(f"  {name}: image has {actual}, lock pins {expected}")
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "freeze_file",
        type=Path,
        help="Path to `pip freeze` output from the image under test",
    )
    parser.add_argument(
        "lock_file",
        type=Path,
        help="Path to requirements.lock",
    )
    args = parser.parse_args(argv)

    freeze_text = args.freeze_file.read_text()
    lock_text = args.lock_file.read_text()
    try:
        drifted = compare_freeze_to_lock(freeze_text, lock_text)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    checked = len(parse_lock(lock_text))
    if drifted:
        print(
            f"The test image does not contain the shipped dependency set "
            f"({len(drifted)} of {checked} differ):",
            file=sys.stderr,
        )
        print("\n".join(drifted), file=sys.stderr)
        return 1

    print(f"{checked} locked packages match the image exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
