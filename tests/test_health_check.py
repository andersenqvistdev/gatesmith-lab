"""Tests for scripts/health-check.sh."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "health-check.sh"


def run_script():
    return subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=False)


def test_exits_zero():
    assert run_script().returncode == 0


def test_prints_exactly_ok():
    assert run_script().stdout == "OK\n"


def test_prints_nothing_to_stderr():
    assert run_script().stderr == ""


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"script not executable: {SCRIPT}"


def test_fails_when_stdout_is_not_writable():
    with open(os.devnull) as read_only:
        result = subprocess.run(
            [str(SCRIPT)],
            stdout=read_only,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    assert result.returncode != 0
    assert "failed to write" in result.stderr
