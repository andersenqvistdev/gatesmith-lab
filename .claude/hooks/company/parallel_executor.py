# /// script
# requires-python = ">=3.10"
# ///
"""
Parallel Executor — Daemon Integration Bridge

Provides a simple interface for the daemon to use parallel execution.
Replaces sequential task processing when parallelExecution is enabled.

Usage:
    from parallel_executor import execute_parallel_cycle, is_parallel_enabled

    if is_parallel_enabled():
        result = execute_parallel_cycle()
    else:
        # Fall back to sequential execution
        ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def is_parallel_enabled(project_root: Path | None = None) -> bool:
    """
    Check if parallel execution is enabled in config.

    Returns:
        True if parallelExecution is enabled in gatesmith.json
    """
    project_root = project_root or Path.cwd()
    config_path = project_root / "gatesmith.json"

    if not config_path.exists():
        return False

    try:
        config = json.loads(config_path.read_text())
        return config.get("daemon", {}).get("parallelExecution", False)
    except Exception:
        return False


def execute_parallel_cycle(project_root: Path | None = None) -> dict:
    """
    Execute one parallel dispatch cycle.

    This is the main entry point for the daemon to use parallel execution.

    Returns:
        Dict with cycle results:
        {
            "success": bool,
            "workers_spawned": int,
            "workers_completed": int,
            "workers_failed": int,
            "tasks_processed": list[str],
            "questions_pending": int,
            "active_workers": int,
            "duration_seconds": float,
            "error": str | None
        }
    """
    project_root = project_root or Path.cwd()

    try:
        # Import here to avoid circular imports
        from dispatcher import Dispatcher, DispatcherConfig

        # Load config
        config = _load_config(project_root)
        dispatcher_config = DispatcherConfig(
            max_workers=config.get("maxWorkers", 3),
            poll_interval_seconds=config.get("pollInterval", 5.0),
            worker_timeout_seconds=config.get("workerTimeout", 600),
        )

        # Create dispatcher and run one cycle
        dispatcher = Dispatcher(
            project_root=project_root,
            config=dispatcher_config,
        )

        result = dispatcher.dispatch_cycle()

        return {
            "success": True,
            "workers_spawned": result.workers_spawned,
            "workers_completed": result.workers_completed,
            "workers_failed": result.workers_failed,
            "tasks_processed": result.tasks_processed,
            "questions_pending": result.questions_pending,
            "active_workers": len(dispatcher.pool.get_active_workers()),
            "duration_seconds": result.duration_seconds,
            "error": None,
        }

    except Exception as e:
        return {
            "success": False,
            "workers_spawned": 0,
            "workers_completed": 0,
            "workers_failed": 0,
            "tasks_processed": [],
            "questions_pending": 0,
            "active_workers": 0,
            "duration_seconds": 0,
            "error": str(e),
        }


def get_parallel_status(project_root: Path | None = None) -> dict:
    """
    Get current parallel execution status.

    Returns:
        Status dict with workers, plans, and questions info
    """
    project_root = project_root or Path.cwd()

    try:
        from dispatcher import create_dispatcher

        dispatcher = create_dispatcher(project_root)
        return dispatcher.get_status()
    except Exception as e:
        return {
            "error": str(e),
            "active_workers": 0,
            "max_workers": 0,
            "workers": [],
            "plans": {},
            "questions_pending": 0,
        }


def get_progress_display(project_root: Path | None = None) -> str:
    """
    Get a formatted progress display for the dashboard.

    Returns:
        Formatted string showing worker progress
    """
    project_root = project_root or Path.cwd()

    try:
        from task_plan import PlanManager, TaskStatus

        manager = PlanManager(project_root)
        plans = manager.list_plans()

        if not plans:
            return "No active plans"

        lines = []
        lines.append("=" * 60)
        lines.append(" PARALLEL EXECUTION PROGRESS")
        lines.append("=" * 60)

        # Group by status
        in_progress = [p for p in plans if p.status == TaskStatus.IN_PROGRESS]
        completed = [p for p in plans if p.status == TaskStatus.COMPLETED]
        failed = [p for p in plans if p.status == TaskStatus.FAILED]

        # Summary
        lines.append(
            f"\n In Progress: {len(in_progress)} | Completed: {len(completed)} | Failed: {len(failed)}"
        )
        lines.append("")

        # Show in-progress tasks with progress bars
        if in_progress:
            lines.append(" ACTIVE WORKERS:")
            for plan in in_progress[:5]:  # Limit to 5
                progress = plan.progress_percent
                bar_width = 20
                filled = int(progress / 100 * bar_width)
                bar = "█" * filled + "░" * (bar_width - filled)
                lines.append(f"   [{bar}] {progress:3.0f}% {plan.title[:35]}")
                # Show current step
                for step in plan.steps:
                    if step.state.value == "[ ]":
                        lines.append(f"        → {step.text[:40]}")
                        break
            lines.append("")

        # Show recent completions
        if completed:
            lines.append(" RECENTLY COMPLETED:")
            for plan in completed[:3]:
                lines.append(f"   ✓ {plan.title[:50]}")
            lines.append("")

        # Show failures
        if failed:
            lines.append(" FAILED:")
            for plan in failed[:3]:
                lines.append(f"   ✗ {plan.title[:50]}")

        lines.append("=" * 60)
        return "\n".join(lines)

    except Exception as e:
        return f"Error getting progress: {e}"


def answer_worker_question(
    worker_id: str,
    answer: str,
    project_root: Path | None = None,
) -> bool:
    """
    Answer a pending question from a worker.

    Args:
        worker_id: The worker asking the question
        answer: The answer text

    Returns:
        True if successful
    """
    project_root = project_root or Path.cwd()

    try:
        from ipc_handler import IPCHandler

        handler = IPCHandler(project_root)
        return handler.answer_question(worker_id, answer)
    except Exception:
        return False


def get_pending_questions(project_root: Path | None = None) -> list[dict]:
    """
    Get all pending questions from workers.

    Returns:
        List of question dicts with worker_id, task_id, text, options
    """
    project_root = project_root or Path.cwd()

    try:
        from ipc_handler import IPCHandler

        handler = IPCHandler(project_root)
        questions = handler.get_pending_questions()

        return [
            {
                "worker_id": q.worker_id,
                "task_id": q.task_id,
                "text": q.text,
                "options": q.options,
                "asked_at": q.asked_at.isoformat(),
            }
            for q in questions
        ]
    except Exception:
        return []


def _load_config(project_root: Path) -> dict:
    """Load daemon config from gatesmith.json."""
    config_path = project_root / "gatesmith.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            return config.get("daemon", {})
        except Exception:
            pass
    return {}


# CLI interface
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: parallel_executor.py <command>")
        print("Commands: enabled, cycle, status, progress, questions")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "enabled":
        enabled = is_parallel_enabled()
        print(f"Parallel execution: {'ENABLED' if enabled else 'DISABLED'}")

    elif cmd == "cycle":
        print("Executing parallel cycle...")
        result = execute_parallel_cycle()
        print(f"Success: {result['success']}")
        print(f"Spawned: {result['workers_spawned']}")
        print(f"Completed: {result['workers_completed']}")
        print(f"Failed: {result['workers_failed']}")
        print(f"Active: {result['active_workers']}")
        if result["error"]:
            print(f"Error: {result['error']}")

    elif cmd == "status":
        status = get_parallel_status()
        print(json.dumps(status, indent=2, default=str))

    elif cmd == "progress":
        print(get_progress_display())

    elif cmd == "questions":
        questions = get_pending_questions()
        if not questions:
            print("No pending questions")
        else:
            for q in questions:
                print(f"[{q['worker_id']}] {q['text']}")
                if q["options"]:
                    print(f"  Options: {', '.join(q['options'])}")
