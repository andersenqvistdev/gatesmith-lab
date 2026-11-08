# /// script
# requires-python = ">=3.10"
# ///
"""
Parallel Worker Manager

Spawns and manages multiple Claude worker processes for parallel task execution.
Each worker runs in a fresh context with its own process, preventing context
window exhaustion and enabling true parallel execution.

Architecture:
    Dispatcher (main) spawns N workers, each executing one task.
    Workers report progress via plan files (checklist-as-state pattern).
    Workers communicate questions via IPC directory.

Based on patterns from github.com/bassimeledath/dispatch
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed


class WorkerState(Enum):
    """Worker lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class Worker:
    """Represents a parallel worker process."""

    worker_id: str
    task_id: str
    task_title: str
    employee_id: str
    process: subprocess.Popen | None = None
    state: WorkerState = WorkerState.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    output: str = ""
    error: str = ""
    plan_file: Path | None = None


@dataclass
class WorkerPool:
    """Manages a pool of parallel workers."""

    max_workers: int = 3
    workers: dict[str, Worker] = field(default_factory=dict)
    project_root: Path = field(default_factory=lambda: Path.cwd())
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        """Ensure directories exist."""
        (self.project_root / ".company" / "workers").mkdir(parents=True, exist_ok=True)
        (self.project_root / ".company" / "plans").mkdir(parents=True, exist_ok=True)
        (self.project_root / ".company" / "ipc").mkdir(parents=True, exist_ok=True)

    @property
    def active_count(self) -> int:
        """Count of currently running workers."""
        with self._lock:
            return sum(
                1 for w in self.workers.values() if w.state == WorkerState.RUNNING
            )

    @property
    def can_spawn(self) -> bool:
        """Check if we can spawn more workers."""
        return self.active_count < self.max_workers

    def spawn_worker(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
        employee_id: str,
        employee_prompt: str,
        timeout_seconds: int = 600,
        on_complete: Callable[[Worker], None] | None = None,
    ) -> Worker | None:
        """
        Spawn a new worker process for a task.

        Args:
            task_id: Unique task identifier
            task_title: Human-readable task title
            task_description: Full task description
            employee_id: Employee executing the task
            employee_prompt: System prompt for the employee
            timeout_seconds: Maximum execution time
            on_complete: Callback when worker completes

        Returns:
            Worker instance or None if pool is full
        """
        if not self.can_spawn:
            return None

        worker_id = f"worker-{task_id[:8]}-{int(time.time())}"
        worker = Worker(
            worker_id=worker_id,
            task_id=task_id,
            task_title=task_title,
            employee_id=employee_id,
        )

        # Create plan file for this task
        plan_file = self._create_plan_file(task_id, task_title, task_description)
        worker.plan_file = plan_file

        # Build the prompt for Claude
        full_prompt = self._build_worker_prompt(
            task_title=task_title,
            task_description=task_description,
            employee_prompt=employee_prompt,
            plan_file=plan_file,
        )

        # Prepare environment (strip problematic vars)
        env = self._prepare_worker_env()

        # Spawn process
        # 2026-07-06 fork-bomb guard: never launch a real claude worker from tests.
        assert_spawn_allowed("parallel_worker.spawn_worker", subprocess.Popen)
        try:
            process = subprocess.Popen(
                [
                    "claude",
                    "--print",
                    "--dangerously-skip-permissions",
                    "--setting-sources",
                    "user",  # Skip CLAUDE.md (P79 fix)
                    "-p",
                    full_prompt,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(self.project_root),
            )
            worker.process = process
            worker.state = WorkerState.RUNNING
            worker.started_at = datetime.now(timezone.utc)

            # Save worker state
            self._save_worker_state(worker)

            with self._lock:
                self.workers[worker_id] = worker

            # Start monitor thread
            monitor = threading.Thread(
                target=self._monitor_worker,
                args=(worker, timeout_seconds, on_complete),
                daemon=True,
            )
            monitor.start()

            return worker

        except Exception as e:
            worker.state = WorkerState.FAILED
            worker.error = str(e)
            return worker

    def _create_plan_file(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
    ) -> Path:
        """Create a checklist-as-state plan file for the task."""
        plan_dir = self.project_root / ".company" / "plans"
        plan_file = plan_dir / f"{task_id}.plan.md"

        content = f"""# Task: {task_title}

**ID**: {task_id}
**Started**: {datetime.now(timezone.utc).isoformat()}
**Status**: [ ] In Progress

## Description

{task_description}

## Progress

- [ ] Analyze task requirements
- [ ] Implement solution
- [ ] Verify implementation
- [ ] Report completion

## Notes

_Worker will update this file as progress is made._
"""
        # Atomic write
        self._atomic_write(plan_file, content)
        return plan_file

    def _build_worker_prompt(
        self,
        task_title: str,
        task_description: str,
        employee_prompt: str,
        plan_file: Path,
    ) -> str:
        """Build the full prompt for the worker."""
        return f"""{employee_prompt}

## Your Task

**Title**: {task_title}

**Description**:
{task_description}

## Instructions

1. Execute this task to completion
2. Update the plan file at `{plan_file}` as you make progress
3. Mark checklist items with [x] when complete, [!] if error, [?] if blocked
4. If you need human input, write your question to the IPC directory
5. Focus on delivering working, tested code

## Progress Tracking

Update `{plan_file}` with your progress. Use checkbox notation:
- [ ] Not started
- [x] Completed
- [!] Error/failed
- [?] Blocked/need input

Begin working on this task now.
"""

    def _prepare_worker_env(self) -> dict[str, str]:
        """Prepare clean environment for worker process.

        Applies all required environment filtering for Claude CLI subprocess:
        - Strips CLAUDECODE to prevent nested session error
        - Strips CLAUDE_CODE_ prefixed vars for session detection
        - Strips UV_ prefixed vars to prevent package manager interference
        - Cleans PATH of virtualenv/UV-managed entries
        - Sets TERM and LANG for proper output handling
        - Filters empty values
        """
        env = os.environ.copy()

        # Strip CLAUDECODE (prevents nested session error)
        env.pop("CLAUDECODE", None)

        # Strip CLAUDE_CODE_ prefixed vars (session detection)
        for key in list(env.keys()):
            if key.startswith("CLAUDE_CODE_"):
                env.pop(key, None)

        # Strip UV_ prefixed vars (package manager interference)
        for key in list(env.keys()):
            if key.startswith("UV_"):
                env.pop(key, None)

        # Strip other problematic vars
        for var in ["VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME"]:
            env.pop(var, None)

        # Clean PATH of virtualenv/UV-managed entries
        if "PATH" in env:
            path_parts = env["PATH"].split(os.pathsep)
            clean_parts = [
                p
                for p in path_parts
                if not any(x in p for x in [".venv", "virtualenv", ".uv", "UV_"])
            ]
            env["PATH"] = os.pathsep.join(clean_parts)

        # Ensure clean terminal
        env["TERM"] = "xterm-256color"
        env["LANG"] = "en_US.UTF-8"

        # Filter empty values
        return {k: v for k, v in env.items() if v}

    def _monitor_worker(
        self,
        worker: Worker,
        timeout_seconds: int,
        on_complete: Callable[[Worker], None] | None,
    ) -> None:
        """Monitor worker process until completion or timeout."""
        try:
            stdout, stderr = worker.process.communicate(timeout=timeout_seconds)
            worker.output = stdout.decode("utf-8", errors="replace")
            worker.error = stderr.decode("utf-8", errors="replace")
            worker.exit_code = worker.process.returncode
            worker.state = (
                WorkerState.COMPLETED if worker.exit_code == 0 else WorkerState.FAILED
            )
        except subprocess.TimeoutExpired:
            worker.process.kill()
            worker.state = WorkerState.TIMEOUT
            worker.error = f"Worker timed out after {timeout_seconds}s"
        except Exception as e:
            worker.state = WorkerState.FAILED
            worker.error = str(e)
        finally:
            worker.completed_at = datetime.now(timezone.utc)
            self._save_worker_state(worker)
            self._update_plan_completion(worker)

            if on_complete:
                try:
                    on_complete(worker)
                except Exception:
                    pass  # Don't let callback errors crash monitor

    def _save_worker_state(self, worker: Worker) -> None:
        """Save worker state to file (atomic)."""
        state_dir = self.project_root / ".company" / "workers"
        state_file = state_dir / f"{worker.worker_id}.json"

        state = {
            "worker_id": worker.worker_id,
            "task_id": worker.task_id,
            "task_title": worker.task_title,
            "employee_id": worker.employee_id,
            "state": worker.state.value,
            "started_at": worker.started_at.isoformat() if worker.started_at else None,
            "completed_at": worker.completed_at.isoformat()
            if worker.completed_at
            else None,
            "exit_code": worker.exit_code,
            "plan_file": str(worker.plan_file) if worker.plan_file else None,
        }

        self._atomic_write(state_file, json.dumps(state, indent=2))

    def _update_plan_completion(self, worker: Worker) -> None:
        """Update plan file with final status."""
        if not worker.plan_file or not worker.plan_file.exists():
            return

        try:
            content = worker.plan_file.read_text()

            # Update status line
            if worker.state == WorkerState.COMPLETED:
                content = content.replace(
                    "**Status**: [ ] In Progress", "**Status**: [x] Completed"
                )
            elif worker.state == WorkerState.FAILED:
                content = content.replace(
                    "**Status**: [ ] In Progress",
                    f"**Status**: [!] Failed (exit={worker.exit_code})",
                )
            elif worker.state == WorkerState.TIMEOUT:
                content = content.replace(
                    "**Status**: [ ] In Progress", "**Status**: [!] Timeout"
                )

            # Add completion timestamp
            content += f"\n\n**Completed**: {worker.completed_at.isoformat()}\n"

            self._atomic_write(worker.plan_file, content)
        except Exception:
            pass  # Best effort

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write file atomically using tempfile + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, path)
        except Exception:
            os.close(fd)
            os.unlink(tmp_path)
            raise

    def get_worker(self, worker_id: str) -> Worker | None:
        """Get worker by ID."""
        with self._lock:
            return self.workers.get(worker_id)

    def get_active_workers(self) -> list[Worker]:
        """Get all currently running workers."""
        with self._lock:
            return [w for w in self.workers.values() if w.state == WorkerState.RUNNING]

    def get_all_workers(self) -> list[Worker]:
        """Get all workers."""
        with self._lock:
            return list(self.workers.values())

    def wait_all(self, timeout: float | None = None) -> bool:
        """
        Wait for all active workers to complete.

        Args:
            timeout: Maximum seconds to wait (None = wait forever)

        Returns:
            True if all completed, False if timeout
        """
        start = time.time()
        while self.active_count > 0:
            if timeout and (time.time() - start) > timeout:
                return False
            time.sleep(0.5)
        return True

    def shutdown(self, force: bool = False) -> None:
        """
        Shutdown all workers.

        Args:
            force: If True, kill immediately. Otherwise, wait for completion.
        """
        with self._lock:
            for worker in self.workers.values():
                if worker.process and worker.state == WorkerState.RUNNING:
                    if force:
                        worker.process.kill()
                    else:
                        worker.process.terminate()


def load_config() -> dict:
    """Load parallel execution config from gatesmith.json."""
    config_path = Path("gatesmith.json")
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            return {
                "max_workers": config.get("daemon", {}).get("maxWorkers", 3),
                "worker_timeout": config.get("daemon", {}).get("workerTimeout", 600),
            }
        except Exception:
            pass
    return {"max_workers": 3, "worker_timeout": 600}


def create_pool(project_root: Path | None = None) -> WorkerPool:
    """Create a worker pool with config from gatesmith.json."""
    config = load_config()
    return WorkerPool(
        max_workers=config["max_workers"],
        project_root=project_root or Path.cwd(),
    )


# CLI for testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: parallel_worker.py <command>")
        print("Commands: status, test")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        pool = create_pool()
        workers = pool.get_all_workers()
        print(f"Active workers: {pool.active_count}/{pool.max_workers}")
        for w in workers:
            print(f"  {w.worker_id}: {w.state.value} ({w.task_title[:40]})")

    elif cmd == "test":
        print("Testing worker spawn...")
        pool = create_pool()
        worker = pool.spawn_worker(
            task_id="test-001",
            task_title="Test Task",
            task_description="This is a test task.",
            employee_id="test-employee",
            employee_prompt="You are a test employee.",
            timeout_seconds=30,
        )
        if worker:
            print(f"Spawned: {worker.worker_id}")
            print(f"Plan: {worker.plan_file}")
        else:
            print("Failed to spawn (pool full?)")
