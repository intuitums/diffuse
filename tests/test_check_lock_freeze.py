"""The freeze-vs-lock gate that keeps test-runner on the shipped dependency set."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_lock_freeze import compare_freeze_to_lock, main, parse_freeze, parse_lock

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts" / "check_lock_freeze.py"


def test_parse_freeze_normalizes_name():
    assert parse_freeze("Foo_Bar==1.2.3\n") == {"foo-bar": "1.2.3"}


def test_parse_lock_skips_comments_and_blank_lines():
    text = "# comment\n\nlitellm==1.93.0\nfastapi==0.139.2\n"
    assert parse_lock(text) == [("litellm", "1.93.0"), ("fastapi", "0.139.2")]


def test_exact_match_is_not_drift():
    freeze = "litellm==1.93.0\nfastapi==0.139.2\n"
    lock = "litellm==1.93.0\nfastapi==0.139.2\n"
    assert compare_freeze_to_lock(freeze, lock) == []


def test_version_mismatch_is_drift():
    freeze = "litellm==1.95.0\n"
    lock = "litellm==1.93.0\n"
    drifted = compare_freeze_to_lock(freeze, lock)
    assert len(drifted) == 1
    assert "litellm" in drifted[0]
    assert "1.95.0" in drifted[0]
    assert "1.93.0" in drifted[0]


def test_missing_package_is_drift():
    """The bug the previous inline check had: missing was skipped, not reported."""

    freeze = "fastapi==0.139.2\n"
    lock = "litellm==1.93.0\nfastapi==0.139.2\n"
    drifted = compare_freeze_to_lock(freeze, lock)
    assert any("litellm" in line and "missing" in line for line in drifted)
    assert not any("fastapi" in line for line in drifted)


def test_empty_lock_is_a_parse_failure_not_a_green_pass():
    with pytest.raises(ValueError, match="Compared nothing"):
        compare_freeze_to_lock("litellm==1.93.0\n", "# nothing pinned\n")


def test_cli_exits_1_when_a_locked_package_is_missing(tmp_path):
    freeze = tmp_path / "freeze.txt"
    lock = tmp_path / "requirements.lock"
    freeze.write_text("fastapi==0.139.2\n")
    lock.write_text("litellm==1.93.0\nfastapi==0.139.2\n")
    assert main([str(freeze), str(lock)]) == 1


def test_cli_exits_0_on_exact_match(tmp_path):
    freeze = tmp_path / "freeze.txt"
    lock = tmp_path / "requirements.lock"
    freeze.write_text("litellm==1.93.0\n")
    lock.write_text("litellm==1.93.0\n")
    assert main([str(freeze), str(lock)]) == 0


def test_script_is_runnable_as_a_module_path():
    """verify.yml invokes this file; a missing shebang or bad import fails CI."""

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "requirements.lock" in completed.stdout


def test_verify_workflow_calls_the_script_not_an_inline_copy():
    """An inline reimplementation would rot the way the previous one did."""

    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "verify.yml").read_text()
    assert "scripts/check_lock_freeze.py" in workflow
    # The old skip-on-missing pattern must not return.
    assert "if actual is None:\n                  continue" not in workflow
