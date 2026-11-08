"""Tests for scripts/health-check.sh."""

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
