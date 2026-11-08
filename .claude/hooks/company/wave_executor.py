#!/usr/bin/env python3
# P20: Wave Executor for Daemon Wave Execution
# /// script
# requires-python = ">=3.10"
# ///
"""
Wave Executor — Execute task plans in waves for daemon-based autonomous work.

This module provides wave-based execution for GSD/BMAD task plans:
1. Execute subtasks within a wave (potentially parallel)
2. Track success/failure per subtask
3. Create atomic commits after each subtask completion
4. Execute waves in dependency order
5. Stop execution cleanly on wave failure

Part of P20: GSD/BMAD + Daemon Unification.

Usage as module:
    from wave_executor import execute_wave, execute_plan
    result = execute_wave(wave, plan, employee_id)
    plan_result = execute_plan(plan, employee_id)

Usage as script:
    uv run wave_executor.py execute-wave --plan-json '{"task_id":"123",...}' --wave-number 1 --employee-id senior-python-developer
    uv run wave_executor.py execute-plan --plan-json '{"task_id":"123",...}' --employee-id senior-python-developer
    uv run wave_executor.py status --plan-json '{"task_id":"123",...}'
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Lazy imports for sibling modules
task_planner = None
company_resolver = None
employee_activator = None
orchestrator = None
orchestrator_metrics = None

# Lazy import for atomic_commit (in parent hooks directory)
atomic_commit = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global task_planner, company_resolver, employee_activator
    if task_planner is not None:
        return

    try:
        from . import company_resolver as cr
        from . import employee_activator as ea
        from . import task_planner as tp

        task_planner = tp
        company_resolver = cr
        employee_activator = ea
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import employee_activator as ea  # type: ignore[no-redef]
        import task_planner as tp  # type: ignore[no-redef]

        task_planner = tp
        company_resolver = cr
        employee_activator = ea


def _ensure_orchestrator():
    """Lazily import orchestrator module for gate checks."""
    global orchestrator, orchestrator_metrics
    if orchestrator is not None:
        return True

    try:
        from . import orchestrator as orch
        from . import orchestrator_metrics as orch_metrics

        orchestrator = orch
        orchestrator_metrics = orch_metrics
        return True
    except ImportError:
        try:
            import orchestrator as orch  # type: ignore[no-redef]
            import orchestrator_metrics as orch_metrics  # type: ignore[no-redef]

            orchestrator = orch
            orchestrator_metrics = orch_metrics
            return True
        except ImportError:
            # Orchestrator not available - proceed with existing behavior
            return False


def _ensure_atomic_commit():
    """Lazily import atomic_commit from parent hooks directory."""
    global atomic_commit
    if atomic_commit is not None:
        return

    try:
        # Try importing from parent hooks directory
        hooks_dir = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(hooks_dir))
        import atomic_commit as ac

        atomic_commit = ac
    except ImportError:
        atomic_commit = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_SUBTASK_TIMEOUT = 300  # 5 minutes per subtask
COMMIT_MESSAGE_PREFIX = "feat(daemon)"


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class SubtaskResult:
    """Result of executing a single subtask."""

    subtask_id: str
    success: bool
    output: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    commit_hash: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "subtask_id": self.subtask_id,
            "success": self.success,
            "output": self.output[:1000] if self.output else "",  # Truncate long output
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "commit_hash": self.commit_hash,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class WaveResult:
    """Result of executing a wave of subtasks."""

    wave_number: int
    subtasks_completed: int = 0
    subtasks_failed: int = 0
    subtasks_total: int = 0
    commits: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    subtask_results: list[SubtaskResult] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        """Wave is successful if all subtasks completed."""
        return (
            self.subtasks_failed == 0 and self.subtasks_completed == self.subtasks_total
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "wave_number": self.wave_number,
            "success": self.success,
            "subtasks_completed": self.subtasks_completed,
            "subtasks_failed": self.subtasks_failed,
            "subtasks_total": self.subtasks_total,
            "commits": self.commits,
            "errors": self.errors,
            "subtask_results": [sr.to_dict() for sr in self.subtask_results],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class PlanResult:
    """Result of executing a complete task plan."""

    plan_id: str
    waves_completed: int = 0
    waves_failed: int = 0
    waves_total: int = 0
    subtasks_completed: int = 0
    subtasks_failed: int = 0
    commits: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wave_results: list[WaveResult] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    stopped_at_wave: int | None = (
        None  # Wave number where execution stopped (on failure)
    )

    @property
    def success(self) -> bool:
        """Plan is successful if all waves completed."""
        return self.waves_failed == 0 and self.waves_completed == self.waves_total

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "plan_id": self.plan_id,
            "success": self.success,
            "waves_completed": self.waves_completed,
            "waves_failed": self.waves_failed,
            "waves_total": self.waves_total,
            "subtasks_completed": self.subtasks_completed,
            "subtasks_failed": self.subtasks_failed,
            "commits": self.commits,
            "errors": self.errors,
            "wave_results": [wr.to_dict() for wr in self.wave_results],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "stopped_at_wave": self.stopped_at_wave,
        }


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------


def _get_project_root() -> Path:
    """Get the project root directory."""
    _ensure_imports()
    company_root = company_resolver.find_company_root()
    return company_root if company_root else Path.cwd()


def _create_atomic_commit(
    task_id: str,
    subtask_id: str,
    description: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Create an atomic commit for a completed subtask.

    Commit message format: feat(daemon): {description} [{task_id}.{subtask_id}]

    Args:
        task_id: Parent task ID
        subtask_id: Subtask ID that was completed
        description: Subtask description for commit message
        dry_run: If True, don't actually commit

    Returns:
        Dict with commit result including commit hash if successful
    """
    _ensure_atomic_commit()

    # Clean up the description for commit message
    clean_desc = description.replace("\n", " ").strip()
    if len(clean_desc) > 72:
        clean_desc = clean_desc[:69] + "..."

    # Use atomic_commit module if available
    if atomic_commit is not None:
        # The module runs real `git add`/`git commit` in the live checkout.
        # An unmocked test reaching this path committed the operator's
        # uncommitted work as a junk commit (2026-07-11) — the clean-tree
        # tripwire cannot see it because the tree ends up clean.
        assert_spawn_allowed(
            "wave_executor._create_atomic_commit",
            getattr(atomic_commit, "subprocess", subprocess).run,
        )
        try:
            result = atomic_commit.atomic_commit(
                phase="daemon",
                task_id=f"{task_id}.{subtask_id}",
                task_name=clean_desc,
                dry_run=dry_run,
            )
            return result
        except Exception as e:
            return {"committed": False, "error": str(e)}

    # Fallback: manual git commit
    if dry_run:
        return {
            "committed": False,
            "dry_run": True,
            "message": f"{COMMIT_MESSAGE_PREFIX}: {clean_desc} [{task_id}.{subtask_id}]",
        }

    # Same hazard as the module path above: `git add -A` + commit in the
    # live checkout. Tests must mock subprocess (dry_run returns earlier).
    assert_spawn_allowed("wave_executor._create_atomic_commit", subprocess.run)

    try:
        project_root = _get_project_root()

        # Check for changes to commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
        )

        if not status_result.stdout.strip():
            return {"committed": False, "reason": "No changes to commit"}

        # Stage all changes
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
        )

        # Create commit
        commit_msg = f"{COMMIT_MESSAGE_PREFIX}: {clean_desc} [{task_id}.{subtask_id}]"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(project_root),
        )

        if commit_result.returncode != 0:
            return {
                "committed": False,
                "error": commit_result.stderr.strip(),
                "message": commit_msg,
            }

        # Get commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(project_root),
        )
        commit_hash = (
            hash_result.stdout.strip()[:8] if hash_result.returncode == 0 else ""
        )

        return {
            "committed": True,
            "message": commit_msg,
            "hash": commit_hash,
        }

    except subprocess.TimeoutExpired:
        return {"committed": False, "error": "Git command timed out"}
    except Exception as e:
        return {"committed": False, "error": str(e)}


def _check_orchestrator_gate(
    task_id: str,
    stage: str,
    stage_result: str,
) -> dict[str, Any]:
    """
    Check orchestrator gate for pipeline progression.

    Uses orchestrator.can_proceed() to determine if we can advance to the next
    pipeline stage. If orchestrator is unavailable, returns proceed=True to
    allow existing behavior.

    Args:
        task_id: Task identifier
        stage: Current pipeline stage (e.g., "implement", "review", "test")
        stage_result: "pass", "fail", or "needs_review"

    Returns:
        Dict with:
            - proceed: bool - Can we advance?
            - next_stage: str|None - Next stage if proceeding
            - reason: str - Explanation
            - requires_human: bool - Needs human escalation?
            - gate_available: bool - Was orchestrator available?
    """
    if not _ensure_orchestrator():
        # Orchestrator unavailable - proceed with existing behavior
        return {
            "proceed": stage_result == "pass",
            "next_stage": None,
            "reason": "Orchestrator unavailable, using fallback behavior",
            "requires_human": False,
            "gate_available": False,
        }

    try:
        # Convert stage string to PipelineStage enum
        stage_enum = orchestrator.PipelineStage(stage)

        # Call orchestrator gate check
        result = orchestrator.Orchestrator().can_proceed(
            task_id=task_id,
            current_stage=stage_enum,
            stage_result=stage_result,
        )

        # Track gate result for metrics
        gate_passed = result.get("proceed", False)
        orchestrator_metrics.track_gate_result(passed=gate_passed)

        return {
            "proceed": result.get("proceed", False),
            "next_stage": result.get("next_stage"),
            "reason": result.get("reason", ""),
            "requires_human": result.get("requires_human", False),
            "gate_available": True,
        }

    except (ValueError, AttributeError) as e:
        # Invalid stage or orchestrator error - fallback to simple pass/fail
        return {
            "proceed": stage_result == "pass",
            "next_stage": None,
            "reason": f"Gate check fallback: {e}",
            "requires_human": False,
            "gate_available": False,
        }
    except Exception as e:
        # Unexpected error - log and proceed to avoid blocking
        return {
            "proceed": stage_result == "pass",
            "next_stage": None,
            "reason": f"Gate check error: {e}",
            "requires_human": False,
            "gate_available": False,
        }


def _handle_human_escalation(
    task_id: str,
    stage: str,
    reason: str,
) -> dict[str, Any]:
    """
    Handle human escalation when a gate requires human review.

    Args:
        task_id: Task identifier
        stage: Current pipeline stage
        reason: Reason for escalation

    Returns:
        Dict with escalation result
    """
    # Try to use escalation module if available
    try:
        from . import escalation as esc
    except ImportError:
        try:
            import escalation as esc  # type: ignore[no-redef]
        except ImportError:
            # No escalation module - return simple result
            return {
                "escalated": False,
                "reason": f"Escalation module unavailable. Stage {stage} requires human review: {reason}",
                "task_id": task_id,
            }

    try:
        # Create escalation for human review
        result = esc.create_escalation(
            title=f"Gate check requires human review: {task_id}",
            description=f"Stage '{stage}' for task {task_id} requires human review.\nReason: {reason}",
            severity="medium",
            source="wave_executor",
            task_id=task_id,
        )
        return {
            "escalated": True,
            "escalation_id": result.get("escalation_id"),
            "reason": reason,
            "task_id": task_id,
        }
    except Exception as e:
        return {
            "escalated": False,
            "reason": f"Failed to escalate: {e}",
            "task_id": task_id,
        }


def _execute_subtask(
    subtask: "task_planner.Subtask",
    plan: "task_planner.TaskPlan",
    employee_id: str,
    timeout: int = DEFAULT_SUBTASK_TIMEOUT,
) -> SubtaskResult:
    """
    Execute a single subtask using the employee activator.

    Args:
        subtask: Subtask to execute
        plan: Parent plan (for context)
        employee_id: Employee ID to execute the task
        timeout: Execution timeout in seconds

    Returns:
        SubtaskResult with execution details
    """
    _ensure_imports()

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    # Build task dict for employee activator
    task_dict = {
        "task_id": subtask.id,
        "title": subtask.description,
        "description": subtask.description,
        "acceptance_criteria": [subtask.acceptance] if subtask.acceptance else [],
        "required_capabilities": [],  # Subtasks inherit from parent plan
        "estimated_complexity": plan.complexity,
        "tags": [],
        "files": subtask.files,
        "source": "wave_executor",
    }

    try:
        # Use employee activator for execution
        result = employee_activator.activate_employee_for_task(task_dict, None)

        duration = time.time() - start_time
        completed_at = datetime.now(timezone.utc).isoformat()

        if result.get("success"):
            # Create atomic commit for successful subtask
            commit_result = _create_atomic_commit(
                task_id=plan.task_id,
                subtask_id=subtask.id,
                description=subtask.description,
            )

            commit_hash = commit_result.get("hash", "")

            return SubtaskResult(
                subtask_id=subtask.id,
                success=True,
                output=result.get("execution_result", {}).get("output", ""),
                duration_seconds=duration,
                commit_hash=commit_hash,
                started_at=started_at,
                completed_at=completed_at,
            )
        else:
            return SubtaskResult(
                subtask_id=subtask.id,
                success=False,
                error=result.get("message", "Unknown error"),
                duration_seconds=duration,
                started_at=started_at,
                completed_at=completed_at,
            )

    except Exception as e:
        duration = time.time() - start_time
        completed_at = datetime.now(timezone.utc).isoformat()

        return SubtaskResult(
            subtask_id=subtask.id,
            success=False,
            error=str(e),
            duration_seconds=duration,
            started_at=started_at,
            completed_at=completed_at,
        )


# -----------------------------------------------------------------------------
# Main Execution Functions
# -----------------------------------------------------------------------------


def execute_wave(
    wave: "task_planner.Wave",
    plan: "task_planner.TaskPlan",
    employee_id: str,
    stop_on_first_failure: bool = True,
) -> WaveResult:
    """
    Execute all subtasks in a wave.

    Subtasks within a wave can be executed in parallel (they have no dependencies
    on each other). However, the current implementation executes sequentially
    for simplicity and to avoid resource contention.

    Args:
        wave: Wave to execute
        plan: Parent task plan
        employee_id: Employee ID to execute the tasks
        stop_on_first_failure: If True, stop execution on first subtask failure

    Returns:
        WaveResult with execution details
    """
    _ensure_imports()

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    result = WaveResult(
        wave_number=wave.number,
        subtasks_total=len(wave.subtask_ids),
        started_at=started_at,
    )

    # Get subtask objects from plan
    subtask_map = {st.id: st for st in plan.subtasks}

    for subtask_id in wave.subtask_ids:
        subtask = subtask_map.get(subtask_id)
        if not subtask:
            result.subtasks_failed += 1
            result.errors.append(f"Subtask {subtask_id} not found in plan")
            if stop_on_first_failure:
                break
            continue

        # Execute the subtask
        subtask_result = _execute_subtask(subtask, plan, employee_id)
        result.subtask_results.append(subtask_result)

        if subtask_result.success:
            result.subtasks_completed += 1
            if subtask_result.commit_hash:
                result.commits.append(subtask_result.commit_hash)

            # Check orchestrator gate for pipeline progression
            gate_result = _check_orchestrator_gate(
                task_id=f"{plan.task_id}.{subtask_id}",
                stage="implement",  # Subtask execution is "implement" stage
                stage_result="pass",
            )

            if not gate_result.get("proceed", True):
                if gate_result.get("requires_human", False):
                    # Human escalation required
                    _handle_human_escalation(
                        task_id=f"{plan.task_id}.{subtask_id}",
                        stage="implement",
                        reason=gate_result.get("reason", "Gate check failed"),
                    )
                    reason = gate_result.get("reason", "")
                    result.errors.append(
                        f"Subtask {subtask_id} requires human review: {reason}"
                    )
                    if stop_on_first_failure:
                        break
                else:
                    # Gate failed but no human needed - treat as failure
                    reason = gate_result.get("reason", "")
                    result.errors.append(
                        f"Subtask {subtask_id} gate check failed: {reason}"
                    )
                    if stop_on_first_failure:
                        break
        else:
            result.subtasks_failed += 1
            result.errors.append(f"Subtask {subtask_id} failed: {subtask_result.error}")

            # Track failed gate result for metrics
            _check_orchestrator_gate(
                task_id=f"{plan.task_id}.{subtask_id}",
                stage="implement",
                stage_result="fail",
            )

            if stop_on_first_failure:
                break

    # Finalize result
    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.duration_seconds = time.time() - start_time

    return result


def execute_plan(
    plan: "task_planner.TaskPlan",
    employee_id: str,
    stop_on_wave_failure: bool = True,
) -> PlanResult:
    """
    Execute a complete task plan in wave order.

    Waves are executed sequentially in dependency order (Wave 1, then Wave 2, etc.).
    Execution stops on the first wave failure if stop_on_wave_failure is True.

    Args:
        plan: TaskPlan to execute
        employee_id: Employee ID to execute the tasks
        stop_on_wave_failure: If True, stop execution on first wave failure

    Returns:
        PlanResult with complete execution details
    """
    _ensure_imports()

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    # Decompose plan into waves
    waves = task_planner.decompose_to_waves(plan)

    result = PlanResult(
        plan_id=plan.task_id,
        waves_total=len(waves),
        started_at=started_at,
    )

    for wave in waves:
        # Execute the wave
        wave_result = execute_wave(wave, plan, employee_id)
        result.wave_results.append(wave_result)

        # Aggregate results
        result.subtasks_completed += wave_result.subtasks_completed
        result.subtasks_failed += wave_result.subtasks_failed
        result.commits.extend(wave_result.commits)
        result.errors.extend(wave_result.errors)

        if wave_result.success:
            result.waves_completed += 1

            # Check orchestrator gate after wave completion
            # This is the "review" or "test" stage depending on pipeline
            gate_result = _check_orchestrator_gate(
                task_id=f"{plan.task_id}.wave{wave.number}",
                stage="review",  # Wave completion triggers review gate
                stage_result="pass",
            )

            if not gate_result.get("proceed", True):
                if gate_result.get("requires_human", False):
                    # Human escalation required after wave
                    _handle_human_escalation(
                        task_id=f"{plan.task_id}.wave{wave.number}",
                        stage="review",
                        reason=gate_result.get("reason", "Wave review required"),
                    )
                    reason = gate_result.get("reason", "")
                    result.errors.append(
                        f"Wave {wave.number} requires human review: {reason}"
                    )
                    result.stopped_at_wave = wave.number
                    if stop_on_wave_failure:
                        break
                else:
                    # Gate failed - treat as wave failure
                    result.waves_failed += 1
                    result.waves_completed -= 1  # Correct the count
                    reason = gate_result.get("reason", "")
                    result.errors.append(
                        f"Wave {wave.number} gate check failed: {reason}"
                    )
                    result.stopped_at_wave = wave.number
                    if stop_on_wave_failure:
                        break
        else:
            result.waves_failed += 1
            result.stopped_at_wave = wave.number

            # Track failed gate result for wave
            _check_orchestrator_gate(
                task_id=f"{plan.task_id}.wave{wave.number}",
                stage="review",
                stage_result="fail",
            )

            if stop_on_wave_failure:
                break

    # Finalize result
    result.completed_at = datetime.now(timezone.utc).isoformat()
    result.duration_seconds = time.time() - start_time

    return result


def get_plan_status(
    plan: "task_planner.TaskPlan", completed: set[str]
) -> dict[str, Any]:
    """
    Get status of a plan execution in progress.

    Args:
        plan: TaskPlan being executed
        completed: Set of completed subtask IDs

    Returns:
        Dict with status information
    """
    _ensure_imports()

    total_subtasks = len(plan.subtasks)
    completed_count = len(completed)
    remaining_count = total_subtasks - completed_count

    # Get ready subtasks
    ready = task_planner.get_ready_subtasks(plan, completed)
    ready_count = len(ready)

    # Estimate remaining time
    remaining_minutes = task_planner.estimate_remaining_time(plan, completed)

    # Get wave progress
    waves = task_planner.decompose_to_waves(plan)
    current_wave = 1
    for wave in waves:
        wave_complete = all(st_id in completed for st_id in wave.subtask_ids)
        if wave_complete:
            current_wave = wave.number + 1
        else:
            break

    return {
        "plan_id": plan.task_id,
        "complexity": plan.complexity,
        "total_subtasks": total_subtasks,
        "completed_subtasks": completed_count,
        "remaining_subtasks": remaining_count,
        "ready_subtasks": ready_count,
        "ready_subtask_ids": [st.id for st in ready],
        "total_waves": len(waves),
        "current_wave": min(current_wave, len(waves)),
        "estimated_remaining_minutes": remaining_minutes,
        "percent_complete": round(completed_count / total_subtasks * 100, 1)
        if total_subtasks > 0
        else 0,
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print usage help."""
    help_text = """
Wave Executor — Execute task plans in waves for daemon-based autonomous work.

Commands:
    execute-wave    Execute a single wave from a plan
    execute-plan    Execute a complete plan (all waves)
    status          Get status of a plan execution
    help            Show this help message

execute-wave options:
    --plan-json JSON        JSON representation of the TaskPlan
    --wave-number N         Wave number to execute (1-based)
    --employee-id ID        Employee ID to execute the tasks
    --stop-on-failure       Stop on first subtask failure (default: true)

execute-plan options:
    --plan-json JSON        JSON representation of the TaskPlan
    --employee-id ID        Employee ID to execute the tasks
    --stop-on-wave-fail     Stop on first wave failure (default: true)

status options:
    --plan-json JSON        JSON representation of the TaskPlan
    --completed IDS         Comma-separated list of completed subtask IDs

Examples:
    # Execute a single wave
    uv run wave_executor.py execute-wave \\
        --plan-json '{"task_id":"P20.4",...}' \\
        --wave-number 1 \\
        --employee-id senior-python-developer

    # Execute a complete plan
    uv run wave_executor.py execute-plan \\
        --plan-json '{"task_id":"P20.4",...}' \\
        --employee-id senior-python-developer

    # Check plan status
    uv run wave_executor.py status \\
        --plan-json '{"task_id":"P20.4",...}' \\
        --completed "subtask1,subtask2"
"""
    print(help_text)


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def _reconstruct_plan(plan_data: dict[str, Any]) -> "task_planner.TaskPlan":
    """Reconstruct a TaskPlan from JSON data."""
    _ensure_imports()

    subtasks = [
        task_planner.Subtask(
            id=st["id"],
            description=st["description"],
            files=st.get("files", []),
            action=task_planner.SubtaskAction(st.get("action", "modify")),
            acceptance=st.get("acceptance", ""),
            estimated_minutes=st.get("estimated_minutes", 30),
            depends_on=st.get("depends_on", []),
        )
        for st in plan_data.get("subtasks", [])
    ]

    return task_planner.TaskPlan(
        task_id=plan_data.get("task_id", "unknown"),
        complexity=plan_data.get("complexity", "standard"),
        subtasks=subtasks,
        dependencies=plan_data.get("dependencies", {}),
        total_estimated_minutes=plan_data.get("total_estimated_minutes", 0),
        wave_assignment=plan_data.get("wave_assignment", {}),
        created_at=plan_data.get("created_at", ""),
        errors=plan_data.get("errors", []),
    )


def main() -> int:
    """Main entry point for CLI usage."""
    if len(sys.argv) < 2:
        print_help()
        return 0

    command = sys.argv[1].lower()
    args = parse_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        print_help()
        return 0

    try:
        _ensure_imports()

        if command == "execute-wave":
            plan_json = args.get("plan_json", "")
            wave_number = int(args.get("wave_number", 1))
            employee_id = args.get("employee_id", "")
            stop_on_failure = args.get("stop_on_failure", True)

            if not plan_json:
                print(json.dumps({"success": False, "error": "--plan-json required"}))
                return 1

            if not employee_id:
                print(json.dumps({"success": False, "error": "--employee-id required"}))
                return 1

            plan_data = json.loads(plan_json)
            plan = _reconstruct_plan(plan_data)

            # Get the requested wave
            waves = task_planner.decompose_to_waves(plan)
            wave = None
            for w in waves:
                if w.number == wave_number:
                    wave = w
                    break

            if not wave:
                print(
                    json.dumps(
                        {
                            "success": False,
                            "error": f"Wave {wave_number} not found. Plan has {len(waves)} waves.",
                        }
                    )
                )
                return 1

            result = execute_wave(wave, plan, employee_id, stop_on_failure)
            print(json.dumps(result.to_dict(), indent=2))

        elif command == "execute-plan":
            plan_json = args.get("plan_json", "")
            employee_id = args.get("employee_id", "")
            stop_on_wave_fail = args.get("stop_on_wave_fail", True)

            if not plan_json:
                print(json.dumps({"success": False, "error": "--plan-json required"}))
                return 1

            if not employee_id:
                print(json.dumps({"success": False, "error": "--employee-id required"}))
                return 1

            plan_data = json.loads(plan_json)
            plan = _reconstruct_plan(plan_data)

            result = execute_plan(plan, employee_id, stop_on_wave_fail)
            print(json.dumps(result.to_dict(), indent=2))

        elif command == "status":
            plan_json = args.get("plan_json", "")
            completed_str = args.get("completed", "")

            if not plan_json:
                print(json.dumps({"success": False, "error": "--plan-json required"}))
                return 1

            plan_data = json.loads(plan_json)
            plan = _reconstruct_plan(plan_data)
            completed = set(completed_str.split(",")) if completed_str else set()

            status = get_plan_status(plan, completed)
            print(json.dumps(status, indent=2))

        else:
            print(f"Unknown command: {command}")
            print_help()
            return 1

    except json.JSONDecodeError as e:
        print(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}))
        return 1
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
