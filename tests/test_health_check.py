"""Tests for scripts/health-check.sh."""

import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "health-check.sh"


def run_script(*args):
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_exits_zero():
    assert run_script().returncode == 0


def test_prints_exactly_ok():
    assert run_script().stdout == "OK\n"


def test_prints_nothing_to_stderr():
    assert run_script().stderr == ""


def test_json_exits_zero():
    assert run_script("--json").returncode == 0


def test_json_prints_status_ok():
    result = run_script("--json")
    assert result.stdout == '{"status":"ok"}\n'
    assert json.loads(result.stdout) == {"status": "ok"}


def test_json_prints_nothing_to_stderr():
    assert run_script("--json").stderr == ""
