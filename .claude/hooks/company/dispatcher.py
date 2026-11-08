# /// script
# requires-python = ">=3.10"
# ///
"""
Parallel Dispatcher

Coordinates parallel worker execution, replacing sequential task processing.
Claims multiple tasks, spawns workers, monitors progress, and handles IPC.

This is the main entry point for the new parallel execution model.

Based on patterns from github.com/bassimeledath/dispatch
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Local imports
from ipc_handler import IPCHandler
from parallel_worker import Worker, WorkerState, create_pool
from task_plan import PlanManager


@dataclass
class DispatchResult:
    """Result of a dispatch cycle."""

    workers_spawned: int
    workers_completed: int
    workers_failed: int
    tasks_processed: list[str]
    questions_pending: int
    duration_seconds: float


@dataclass
class DispatcherConfig:
    """Configuration for the dispatcher."""

    max_workers: int = 3
    poll_interval_seconds: float = 5.0
    worker_timeout_seconds: int = (
        1800  # WS-105: Increased to allow longer task execution
    )
    auto_answer_timeout: int = 300
    enable_ipc: bool = True


class Dispatcher:
    """
    Parallel task dispatcher.

    Manages the lifecycle of parallel workers:
    1. Claims tasks from the work queue
    2. Spawns workers with fresh contexts
    3. Monitors progress via plan files
    4. Handles IPC (questions/answers)
    5. Reports completion back to the queue
    """

    def __init__(
        self,
        project_root: Path | None = None,
        config: DispatcherConfig | None = None,
    ):
        self.project_root = project_root or Path.cwd()
        self.config = config or self._load_config()

        # Initialize components
        self.pool = create_pool(self.project_root)
        self.pool.max_workers = self.config.max_workers

        self.plans = PlanManager(self.project_root)
        self.ipc = IPCHandler(self.project_root) if self.config.enable_ipc else None

        # Callbacks
        self.on_worker_complete: Callable[[Worker], None] | None = None
        self.on_question_asked: Callable[[str, str], None] | None = None

    def _load_config(self) -> DispatcherConfig:
        """Load config from gatesmith.json."""
        config_path = self.project_root / "gatesmith.json"
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                daemon = data.get("daemon", {})
                return DispatcherConfig(
                    max_workers=daemon.get("maxWorkers", 3),
                    poll_interval_seconds=daemon.get("pollInterval", 5.0),
                    worker_timeout_seconds=daemon.get("workerTimeout", 600),
                )
            except Exception:
                pass
        return DispatcherConfig()

    def dispatch_cycle(self) -> DispatchResult:
        """
        Execute one dispatch cycle.

        1. Get pending tasks
        2. Spawn workers for available slots
        3. Check completed workers
        4. Handle IPC questions
        5. Return results

        Returns:
            DispatchResult with cycle statistics
        """
        start_time = time.time()
        tasks_processed = []
        workers_spawned = 0
        workers_completed = 0
        workers_failed = 0

        # Get pending tasks
        pending_tasks = self._get_pending_tasks()

        # Spawn workers for available slots
        while self.pool.can_spawn and pending_tasks:
            task = pending_tasks.pop(0)
            worker = self._spawn_worker_for_task(task)
            if worker:
                workers_spawned += 1
                tasks_processed.append(task.get("task_id", "unknown"))

        # Check completed workers
        for worker in self.pool.get_all_workers():
            if worker.state in (
                WorkerState.COMPLETED,
                WorkerState.FAILED,
                WorkerState.TIMEOUT,
            ):
                if worker.state == WorkerState.COMPLETED:
                    workers_completed += 1
                    self._handle_worker_success(worker)
                else:
                    workers_failed += 1
                    self._handle_worker_failure(worker)

        # Count pending questions
        questions_pending = 0
        if self.ipc:
            questions_pending = len(self.ipc.get_pending_questions())

        return DispatchResult(
            workers_spawned=workers_spawned,
            workers_completed=workers_completed,
            workers_failed=workers_failed,
            tasks_processed=tasks_processed,
            questions_pending=questions_pending,
            duration_seconds=time.time() - start_time,
        )

    def _get_pending_tasks(self) -> list[dict]:
        """Get pending tasks from work queue."""
        queue_path = self.project_root / ".company" / "state" / "work_queue.json"
        if not queue_path.exists():
            return []

        try:
            queue = json.loads(queue_path.read_text())
            return queue.get("pending", [])[: self.config.max_workers]
        except Exception:
            return []

    def _spawn_worker_for_task(self, task: dict) -> Worker | None:
        """Spawn a worker for a task."""
        task_id = task.get("task_id", f"task-{int(time.time())}")
        title = task.get("title", "Untitled Task")
        description = task.get("description", title)

        # Get employee info
        employee_id = task.get("assigned_to", "default-employee")
        employee_prompt = self._get_employee_prompt(employee_id)

        # Create plan file
        self.plans.create_plan(
            task_id=task_id,
            title=title,
            description=description,
            employee_id=employee_id,
        )

        # Claim task in queue
        self._claim_task(task_id)

        # Spawn worker
        worker = self.pool.spawn_worker(
            task_id=task_id,
            task_title=title,
            task_description=description,
            employee_id=employee_id,
            employee_prompt=employee_prompt,
            timeout_seconds=self.config.worker_timeout_seconds,
            on_complete=self._on_worker_complete,
        )

        return worker

    def _get_employee_prompt(self, employee_id: str) -> str:
        """Get the system prompt for an employee."""
        # Try to load from employee file
        employee_file = (
            self.project_root / ".company" / "employees" / f"{employee_id}.md"
        )
        if employee_file.exists():
            return employee_file.read_text()

        # Fall back to org.json
        org_path = self.project_root / ".company" / "org.json"
        if org_path.exists():
            try:
                org = json.loads(org_path.read_text())
                for emp in org.get("employees", org.get("agents", [])):
                    if emp.get("id") == employee_id:
                        return self._build_prompt_from_employee(emp)
            except Exception:
                pass

        # Default prompt
        return f"""You are {employee_id}, an AI employee.
Complete the assigned task thoroughly and professionally.
Report your progress by updating the plan file.
If you need clarification, ask a question via the IPC system.
"""

    def _build_prompt_from_employee(self, emp: dict) -> str:
        """Build prompt from employee data."""
        name = emp.get("name", emp.get("id", "Employee"))
        role = emp.get("role", "team member")
        capabilities = emp.get("capabilities", [])

        prompt = f"""You are {name}, a {role}.

Your capabilities: {", ".join(capabilities[:5]) if capabilities else "general development"}

Instructions:
- Complete the assigned task thoroughly
- Update the plan file with your progress
- If blocked, ask a question via IPC
- Write clean, tested code
- Follow project conventions
"""
        return prompt

    def _claim_task(self, task_id: str) -> None:
        """Move task from pending to in_progress."""
        queue_path = self.project_root / ".company" / "state" / "work_queue.json"
        if not queue_path.exists():
            return

        try:
            queue = json.loads(queue_path.read_text())

            # Find and move task
            pending = queue.get("pending", [])
            in_progress = queue.get("in_progress", [])

            task = None
            for i, t in enumerate(pending):
                if t.get("task_id") == task_id:
                    task = pending.pop(i)
                    break

            if task:
                task["claimed_at"] = datetime.now(timezone.utc).isoformat()
                task["status"] = "in_progress"
                in_progress.append(task)

                queue["pending"] = pending
                queue["in_progress"] = in_progress

                self._atomic_write(queue_path, json.dumps(queue, indent=2))
        except Exception:
            pass

    def _on_worker_complete(self, worker: Worker) -> None:
        """Callback when a worker completes."""
        if self.on_worker_complete:
            self.on_worker_complete(worker)

        # Clean up IPC
        if self.ipc:
            self.ipc.cleanup_worker(worker.worker_id)

    def _handle_worker_success(self, worker: Worker) -> None:
        """Handle successful worker completion."""
        # Mark task complete in queue
        self._complete_task(worker.task_id)

        # Update plan
        self.plans.complete_plan(worker.task_id)

    def _handle_worker_failure(self, worker: Worker) -> None:
        """Handle worker failure."""
        # Mark task failed in queue
        self._fail_task(worker.task_id, worker.error or "Unknown error")

        # Update plan
        self.plans.fail_plan(worker.task_id, worker.error)

    def _complete_task(self, task_id: str) -> None:
        """Move task to completed."""
        queue_path = self.project_root / ".company" / "state" / "work_queue.json"
        if not queue_path.exists():
            return

        try:
            queue = json.loads(queue_path.read_text())
            in_progress = queue.get("in_progress", [])
            completed = queue.get("completed", [])

            task = None
            for i, t in enumerate(in_progress):
                if t.get("task_id") == task_id:
                    task = in_progress.pop(i)
                    break

            if task:
                task["completed_at"] = datetime.now(timezone.utc).isoformat()
                task["status"] = "completed"
                completed.append(task)

                queue["in_progress"] = in_progress
                queue["completed"] = completed

                self._atomic_write(queue_path, json.dumps(queue, indent=2))
        except Exception:
            pass

    def _fail_task(self, task_id: str, reason: str) -> None:
        """Move task to failed."""
        queue_path = self.project_root / ".company" / "state" / "work_queue.json"
        if not queue_path.exists():
            return

        try:
            queue = json.loads(queue_path.read_text())
            in_progress = queue.get("in_progress", [])
            failed = queue.get("failed", [])

            task = None
            for i, t in enumerate(in_progress):
                if t.get("task_id") == task_id:
                    task = in_progress.pop(i)
                    break

            if task:
                task["failed_at"] = datetime.now(timezone.utc).isoformat()
                task["status"] = "failed"
                task["failure_reason"] = reason
                failed.append(task)

                queue["in_progress"] = in_progress
                queue["failed"] = failed

                self._atomic_write(queue_path, json.dumps(queue, indent=2))
        except Exception:
            pass

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write file atomically."""
        import os
        import tempfile

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
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def run(self, max_cycles: int | None = None) -> None:
        """
        Run the dispatcher continuously.

        Args:
            max_cycles: Stop after N cycles (None = run forever)
        """
        cycles = 0
        print(f"[Dispatcher] Starting with max_workers={self.config.max_workers}")

        try:
            while max_cycles is None or cycles < max_cycles:
                result = self.dispatch_cycle()

                # Log cycle
                print(
                    f"[Dispatcher] Cycle {cycles}: "
                    f"spawned={result.workers_spawned} "
                    f"completed={result.workers_completed} "
                    f"failed={result.workers_failed} "
                    f"questions={result.questions_pending} "
                    f"({result.duration_seconds:.2f}s)"
                )

                # Handle pending questions
                if self.ipc and result.questions_pending > 0:
                    self._notify_pending_questions()

                cycles += 1
                time.sleep(self.config.poll_interval_seconds)

        except KeyboardInterrupt:
            print("\n[Dispatcher] Shutting down...")
            self.shutdown()

    def _notify_pending_questions(self) -> None:
        """Notify about pending questions."""
        if not self.ipc:
            return

        questions = self.ipc.get_pending_questions()
        for q in questions:
            print(f"[?] Worker {q.worker_id} asks: {q.text}")
            if self.on_question_asked:
                self.on_question_asked(q.worker_id, q.text)

    def shutdown(self, force: bool = False) -> None:
        """Shutdown the dispatcher and all workers."""
        self.pool.shutdown(force=force)
        if self.ipc:
            self.ipc.cleanup_expired(max_age_seconds=0)

    def get_status(self) -> dict:
        """Get current dispatcher status."""
        active = self.pool.get_active_workers()
        plans_summary = self.plans.get_progress_summary()

        return {
            "active_workers": len(active),
            "max_workers": self.config.max_workers,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "task_id": w.task_id,
                    "employee_id": w.employee_id,
                    "state": w.state.value,
                    "started_at": w.started_at.isoformat() if w.started_at else None,
                }
                for w in active
            ],
            "plans": plans_summary,
            "questions_pending": len(self.ipc.get_pending_questions())
            if self.ipc
            else 0,
        }


def create_dispatcher(project_root: Path | None = None) -> Dispatcher:
    """Factory function to create dispatcher."""
    return Dispatcher(project_root)


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dispatcher.py <command>")
        print("Commands: run, status, cycle")
        sys.exit(1)

    cmd = sys.argv[1]
    dispatcher = create_dispatcher()

    if cmd == "run":
        max_cycles = int(sys.argv[2]) if len(sys.argv) > 2 else None
        dispatcher.run(max_cycles=max_cycles)

    elif cmd == "status":
        status = dispatcher.get_status()
        print(json.dumps(status, indent=2))

    elif cmd == "cycle":
        result = dispatcher.dispatch_cycle()
        print(f"Spawned: {result.workers_spawned}")
        print(f"Completed: {result.workers_completed}")
        print(f"Failed: {result.workers_failed}")
        print(f"Questions: {result.questions_pending}")
        print(f"Duration: {result.duration_seconds:.2f}s")
