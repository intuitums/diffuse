"""File-based progress heartbeat for the review worker.

`restart: unless-stopped` only recovers a worker process that exits. A worker
that is wedged — spinning inside a pathological regex, blocked on a lock, or
retrying an unreachable SCM forever — stays "up" while draining no jobs, so
process existence is the wrong liveness signal. The worker touches this file
every time it makes observable progress (a poll turn, or a lease heartbeat
during a long job); a container healthcheck fails once the file goes stale,
which lets the orchestrator restart it.

The signal deliberately lives on the filesystem rather than in a heartbeat
column: it costs no database round trip on the sick path, and it stays honest
when the reason the worker is wedged is PostgreSQL itself.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# A path inside the container, not a shared volume: the probe must describe the
# container it runs in, and `docker compose` healthchecks exec there.
DEFAULT_LIVENESS_PATH = "/tmp/diffuse-worker-alive"

# The worker heartbeats its lease while a job runs, so a gap this long means it
# is not progressing rather than merely busy. The ceiling keeps a misconfigured
# value from disabling the probe entirely, and the floor keeps a healthy worker
# on a slow poll interval from being restarted mid-review.
DEFAULT_MAX_AGE_SECONDS = 300
MIN_MAX_AGE_SECONDS = 30
MAX_MAX_AGE_SECONDS = 3600


def liveness_path() -> Path:
    raw = os.environ.get("DIFFUSE_WORKER_LIVENESS_FILE", "").strip()
    return Path(raw or DEFAULT_LIVENESS_PATH)


def max_age_seconds() -> int:
    raw = os.environ.get("DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise ValueError(
            "DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS must be a positive number "
            "of seconds"
        )
    seconds = int(raw)
    if not MIN_MAX_AGE_SECONDS <= seconds <= MAX_MAX_AGE_SECONDS:
        raise ValueError(
            "DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS must be between "
            f"{MIN_MAX_AGE_SECONDS} and {MAX_MAX_AGE_SECONDS} seconds"
        )
    return seconds


def touch_liveness(path: Path | None = None) -> None:
    """Record forward progress. Never raises: liveness must not fail a job."""
    target = liveness_path() if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    except OSError:
        # A read-only or full filesystem should degrade to "no probe", not take
        # down a worker that is otherwise healthy.
        pass


def liveness_age_seconds(path: Path | None = None) -> float | None:
    """Seconds since the last recorded progress, or None if never recorded."""
    target = liveness_path() if path is None else path
    try:
        modified_at = target.stat().st_mtime
    except OSError:
        return None
    return max(0.0, time.time() - modified_at)


def liveness_is_fresh(
    path: Path | None = None,
    *,
    maximum_age_seconds: int | None = None,
) -> bool:
    age = liveness_age_seconds(path)
    if age is None:
        return False
    limit = max_age_seconds() if maximum_age_seconds is None else maximum_age_seconds
    return age <= limit


def main() -> int:
    """Container healthcheck entry point: `python -m service.worker_liveness`."""
    age = liveness_age_seconds()
    if age is None:
        print("worker liveness file has never been written", file=sys.stderr)
        return 1
    limit = max_age_seconds()
    if age > limit:
        print(
            f"worker made no progress for {age:.0f}s (limit {limit}s)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
