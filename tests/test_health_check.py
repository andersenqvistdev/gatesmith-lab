import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"


def test_health_check_prints_ok_and_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout == "OK\n"
    assert result.stderr == ""
