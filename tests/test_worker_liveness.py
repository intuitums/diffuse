import os

import pytest

from service import worker_liveness


@pytest.fixture(autouse=True)
def _isolated_liveness_file(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "DIFFUSE_WORKER_LIVENESS_FILE",
        str(tmp_path / "nested" / "diffuse-worker-alive"),
    )
    monkeypatch.delenv("DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS", raising=False)


def test_a_worker_that_never_reported_progress_is_not_alive():
    # The probe must fail before the first poll completes rather than treat a
    # missing file as healthy.
    assert worker_liveness.liveness_age_seconds() is None
    assert not worker_liveness.liveness_is_fresh()
    assert worker_liveness.main() == 1


def test_recorded_progress_makes_the_worker_fresh():
    worker_liveness.touch_liveness()

    assert worker_liveness.liveness_age_seconds() < 5
    assert worker_liveness.liveness_is_fresh()
    assert worker_liveness.main() == 0


def test_a_worker_that_stopped_progressing_fails_the_probe():
    # This is the case `restart: unless-stopped` cannot see: the process is up,
    # but nothing has advanced since the stale heartbeat.
    worker_liveness.touch_liveness()
    path = worker_liveness.liveness_path()
    stale = path.stat().st_mtime - (worker_liveness.DEFAULT_MAX_AGE_SECONDS + 60)
    os.utime(path, (stale, stale))

    assert not worker_liveness.liveness_is_fresh()
    assert worker_liveness.main() == 1


def test_touching_liveness_never_raises_on_an_unwritable_path(monkeypatch, tmp_path):
    unwritable = tmp_path / "blocked"
    unwritable.write_text("not a directory")
    monkeypatch.setenv("DIFFUSE_WORKER_LIVENESS_FILE", str(unwritable / "alive"))

    worker_liveness.touch_liveness()

    assert not worker_liveness.liveness_is_fresh()


def test_probe_window_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS", "60")
    assert worker_liveness.max_age_seconds() == 60

    for invalid in ("0", "29", "3601", "-1", "1.5", "sixty"):
        monkeypatch.setenv("DIFFUSE_WORKER_LIVENESS_MAX_AGE_SECONDS", invalid)
        with pytest.raises(ValueError, match="LIVENESS_MAX_AGE_SECONDS"):
            worker_liveness.max_age_seconds()
