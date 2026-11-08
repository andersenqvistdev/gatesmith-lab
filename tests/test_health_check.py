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


def run_script_with_closed_stdout():
    # `>&-` closes stdout, so writing OK fails.
    return subprocess.run(
        f'"{SCRIPT}" >&-', shell=True, capture_output=True, text=True, check=False
    )


def test_fails_when_stdout_cannot_be_written():
    assert run_script_with_closed_stdout().returncode == 1


def test_reports_write_failure_on_stderr():
    result = run_script_with_closed_stdout()
    assert "failed to write OK to stdout" in result.stderr
