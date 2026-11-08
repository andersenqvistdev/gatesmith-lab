#!/usr/bin/env python3
"""Machine-level guard against real Claude CLI spawns from test contexts.

2026-07-06 incident: running pytest on test_operation_loop.py reached the real
Agent Teams path through a function-local import that module-attribute mocks
cannot see. Each affected test spawned a real team of `claude` workers, and the
spawned workers — allowed to run pytest themselves — re-triggered the leaky
tests recursively. 774 real claude sessions were created in ~65 minutes (368
alive simultaneously, ~160GB RSS); the storm only stopped when the Claude
subscription window was exhausted. No Gatesmith safety applied: worker caps and
circuit breakers only guard the daemon's own spawn paths.

This module is the hard stop for that entire failure class. Every call site
that launches the Claude CLI calls assert_spawn_allowed() with the exact
subprocess primitive it is about to use:

    assert_spawn_allowed("team_executor._run_team_in", subprocess.run)

Behavior:
- If the primitive is not the real subprocess function (a test installed a
  Mock or monkeypatch double), the spawn is allowed — properly mocked tests
  are unaffected and need no changes.
- If the primitive is real and this process is inside a pytest run ("pytest"
  in sys.modules), SpawnBlockedError is raised.
- If the primitive is real and PYTEST_CURRENT_TEST is present in the
  environment, SpawnBlockedError is raised too. pytest sets that variable for
  every test and child processes inherit it (the worker env filtering only
  strips UV_/CLAUDECODE/CLAUDE_CODE_ prefixes), so even a worker leaked BY a
  test cannot spawn further workers — this breaks the recursive amplification.
- FORGE_ALLOW_REAL_SPAWN_IN_TESTS=1 bypasses the guard, for deliberate
  integration tests only.
"""

from __future__ import annotations

import os
import sys
from typing import Any

ALLOW_ENV = "FORGE_ALLOW_REAL_SPAWN_IN_TESTS"


class SpawnBlockedError(RuntimeError):
    """A real Claude CLI spawn was attempted from a test context."""


def detect_test_context() -> str | None:
    """Return why this process counts as a test context, or None if it doesn't.

    Two independent signals, either one suffices:
    - "pytest" in sys.modules: this process IS a pytest run.
    - PYTEST_CURRENT_TEST in the environment: this process DESCENDS from a
      pytest run (the variable is inherited by children spawned during a test).
    """
    if "pytest" in sys.modules:
        return "pytest is loaded in this process"
    current_test = os.environ.get("PYTEST_CURRENT_TEST")
    if current_test:
        return (
            "PYTEST_CURRENT_TEST is set in the environment "
            f"({current_test.split(' ')[0]}) — this process descends from a pytest run"
        )
    return None


def assert_spawn_allowed(context: str, runner: Any = None) -> None:
    """Refuse to launch a real Claude CLI process from a test context.

    Args:
        context: "module.function" of the spawn site, for the error message.
        runner: The exact subprocess primitive about to be used (subprocess.run
            or subprocess.Popen), resolved in the caller's namespace so that
            test doubles installed by mock/monkeypatch are recognized. When the
            primitive is a double, nothing real can spawn and the call is
            allowed. Pass None to enforce the test-context check regardless.

    Raises:
        SpawnBlockedError: real primitive + test context + no explicit opt-in.
    """
    if os.environ.get(ALLOW_ENV) == "1":
        return
    if runner is not None and getattr(runner, "__module__", None) != "subprocess":
        # The subprocess primitive has been replaced by a test double — the
        # test has mocked this path properly, nothing real will spawn.
        return
    reason = detect_test_context()
    if reason is None:
        return
    raise SpawnBlockedError(
        f"Refusing to spawn a real Claude CLI process from {context}: {reason}. "
        f"Mock this call path in the test, or set {ALLOW_ENV}=1 for a deliberate "
        "integration test. Unmocked spawns fork-bomb real claude workers — see "
        "the 2026-07-06 incident (774 sessions / 160GB)."
    )
