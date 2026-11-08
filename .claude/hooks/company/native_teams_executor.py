#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
WS-115: Native Agent Teams Executor.

Executes tasks using Claude Code's native Agent Teams feature, which spawns
actual separate Claude processes that work in parallel with inter-agent messaging.

Unlike role-projection (single process with multi-identity prompt), native teams:
- Run multiple Claude processes in parallel
- Have built-in inter-agent messaging (mailbox)
- Let Claude decide team composition dynamically

Usage:
    from native_teams_executor import (
        should_use_native_teams,
        execute_with_native_teams,
    )

    if should_use_native_teams(task, config):
        result = execute_with_native_teams(task, lead_employee_id, lead_context, config)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

try:
    from . import agent_providers as _agent_providers
except ImportError:  # direct script execution
    import agent_providers as _agent_providers

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Lazy imports for sibling modules
_employee_activator = None
_company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global _employee_activator, _company_resolver
    if _employee_activator is not None:
        return

    try:
        from . import company_resolver as cr
        from . import employee_activator as ea

        _employee_activator = ea
        _company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import employee_activator as ea  # type: ignore[no-redef]

        _employee_activator = ea
        _company_resolver = cr


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Native teams timeout: 2x normal team timeout (they do parallel work)
DEFAULT_NATIVE_TIMEOUT = 3600  # 1 hour

# Effort mapping for complexity
EFFORT_BY_COMPLEXITY = {
    "trivial": "low",
    "standard": "medium",
    "complex": "high",
    "epic": "max",
}


# -----------------------------------------------------------------------------
# Result dataclass
# -----------------------------------------------------------------------------


@dataclass
class NativeTeamResult:
    """Result from native Agent Teams execution."""

    success: bool
    output: str
    exit_code: int
    duration_seconds: float
    strategy: str = "native-teams"
    lead_employee_id: str | None = None
    teammates_defined: list[str] | None = None
    error: str | None = None


# -----------------------------------------------------------------------------
# Decision: Should we use native teams?
# -----------------------------------------------------------------------------


def should_use_native_teams(task: dict, config: dict) -> bool:
    """
    Determine if a task should use native Agent Teams (multi-process).

    Triggers when:
    1. Epic tasks (highest complexity)
    2. Stuck tasks with 3+ retries (rescue strategy)
    3. Task explicitly requests native teams

    Args:
        task: Task dictionary.
        config: agentTeams config from gatesmith.json.

    Returns:
        True if native teams should be used.
    """
    native_config = config.get("nativeTeamsConfig", {})

    # Gate: Native teams must be enabled
    if not native_config.get("enabled", False):
        return False

    conditions = native_config.get("triggerConditions", {})
    complexity = task.get("complexity", task.get("estimated_complexity", "standard"))
    retry_count = task.get("retry_count", 0)

    # Trigger 1: Epic tasks
    if conditions.get("epicTasks", True) and complexity == "epic":
        return True

    # Trigger 2: Stuck tasks after N retries
    stuck_threshold = conditions.get("stuckTasksAfterRetries", 3)
    if retry_count >= stuck_threshold:
        return True

    # Trigger 3: Explicit request via task metadata
    if conditions.get("explicitRequest", True):
        if task.get("use_native_teams", False):
            return True
        if task.get("execution_strategy") == "native-teams":
            return True

    return False


# -----------------------------------------------------------------------------
# Teammate Definition Builder
# -----------------------------------------------------------------------------


def build_teammate_definitions(
    task: dict,
    lead_employee_id: str,
    config: dict,
) -> dict[str, dict]:
    """
    Build teammate definitions for --agents flag.

    Creates agent definitions based on task needs, excluding the lead employee.
    Uses actual employee agent definitions when available.

    Args:
        task: Task dictionary.
        lead_employee_id: ID of the lead employee (excluded from teammates).
        config: agentTeams config.

    Returns:
        Dict of agent name -> agent definition for --agents JSON.
    """
    _ensure_imports()

    teammates = {}
    native_config = config.get("nativeTeamsConfig", {})
    max_teammates = native_config.get("maxTeammates", 3)
    teammate_model = native_config.get("teammateModel", "claude-sonnet-5-5")

    # Load employees
    org = _employee_activator.load_org()
    employees = org.get("employees", [])

    # Determine needed roles based on task
    needed_roles = _determine_needed_roles(task)

    # Match roles to employees
    for role in needed_roles[:max_teammates]:
        best_match = _find_best_employee_for_role(
            role, employees, exclude_ids=[lead_employee_id]
        )

        if best_match:
            # Use employee's agent definition
            context = _employee_activator.load_employee_context(best_match["id"])
            teammates[f"{role}-agent"] = {
                "description": f"{role.replace('-', ' ').title()} specialist",
                "model": teammate_model,
                "prompt": _build_teammate_prompt(best_match, context, role),
            }
        else:
            # Generate ephemeral specialist
            teammates[f"{role}-agent"] = {
                "description": f"Ephemeral {role.replace('-', ' ')} specialist",
                "model": teammate_model,
                "prompt": _build_ephemeral_prompt(role),
            }

    return teammates


def _determine_needed_roles(task: dict) -> list[str]:
    """Determine which specialist roles are needed for a task."""
    roles = []
    complexity = task.get("complexity", "standard")
    capabilities = task.get("required_capabilities", [])
    description = task.get("description", "").lower()

    # Always include a reviewer for epic tasks
    if complexity == "epic":
        roles.append("code-reviewer")

    # Security reviewer if security-related
    if "security" in description or "security" in capabilities:
        roles.append("security-reviewer")

    # Test specialist if test-related
    if "test" in description or "testing" in capabilities:
        roles.append("test-specialist")

    # Architecture reviewer for complex/epic
    if complexity in ("complex", "epic"):
        roles.append("architecture-reviewer")

    # Default to code-reviewer if no specific roles
    if not roles:
        roles.append("code-reviewer")

    return roles


def _find_best_employee_for_role(
    role: str,
    employees: list[dict],
    exclude_ids: list[str],
) -> dict | None:
    """Find the best employee match for a role."""
    role_capabilities = {
        "code-reviewer": ["python", "backend", "code-review"],
        "security-reviewer": ["security", "owasp", "secrets-scanning"],
        "test-specialist": ["testing", "pytest", "tdd"],
        "architecture-reviewer": ["architecture", "design-decisions", "patterns"],
    }

    target_caps = role_capabilities.get(role, [])
    best_match = None
    best_score = 0

    for emp in employees:
        if emp["id"] in exclude_ids:
            continue
        if emp.get("status") != "available":
            continue

        emp_caps = emp.get("capabilities", [])
        score = len(set(emp_caps) & set(target_caps))

        if score > best_score:
            best_score = score
            best_match = emp

    return best_match


def _build_teammate_prompt(
    employee: dict,
    context: Any,
    role: str,
) -> str:
    """Build a teammate prompt from employee context."""
    base = f"""You are {employee.get("name", "a specialist")} acting as a {role.replace("-", " ")}.

Your role on this team: Provide expert {role.replace("-", " ")} perspective.

Focus areas:
- Review code/decisions from your {role.replace("-", " ")} perspective
- Identify issues the lead might miss
- Provide actionable recommendations

"""

    # Add agent definition if available
    if context and hasattr(context, "agent_definition") and context.agent_definition:
        base += f"Background:\n{context.agent_definition[:1500]}\n\n"

    return base


def _build_ephemeral_prompt(role: str) -> str:
    """Build a prompt for an ephemeral specialist."""
    prompts = {
        "code-reviewer": """You are a senior code reviewer.
Review for: correctness, edge cases, error handling, readability.
Be specific and actionable in your feedback.""",
        "security-reviewer": """You are a security specialist.
Review for: OWASP Top 10, input validation, auth issues, secrets exposure.
Flag security concerns with severity levels.""",
        "test-specialist": """You are a testing specialist.
Review for: test coverage, edge cases, test quality, missing scenarios.
Suggest specific test cases to add.""",
        "architecture-reviewer": """You are a software architect.
Review for: design patterns, scalability, maintainability, coupling.
Consider long-term implications of changes.""",
    }
    return prompts.get(role, f"You are a {role.replace('-', ' ')} specialist.")


# -----------------------------------------------------------------------------
# Execution
# -----------------------------------------------------------------------------


def execute_with_native_teams(
    task: dict,
    lead_employee_id: str,
    lead_context: Any,
    config: dict,
    project_dir: Path | None = None,
) -> NativeTeamResult:
    """
    Execute a task using Claude Code's native Agent Teams.

    Spawns actual separate Claude processes that work in parallel.

    Args:
        task: Task dictionary.
        lead_employee_id: ID of the lead employee.
        lead_context: EmployeeContext for the lead.
        config: agentTeams config.
        project_dir: Project directory for subprocess cwd.

    Returns:
        NativeTeamResult with execution details.
    """
    _ensure_imports()

    # Worktree-isolation invariant (defense-in-depth; the team_executor wrapper
    # guards too): native teams spawns multiple write-capable Claude agents in
    # ``cwd=project_dir``. It MUST be an isolated worktree, never the main repo
    # root — otherwise the agents mutate the working tree directly. Refuse if no
    # isolated dir was provided.
    _main_root = _employee_activator.get_project_root()

    def _resolves_equal(a: Path | str, b: Path | str) -> bool:
        try:
            return Path(a).resolve() == Path(b).resolve()
        except (OSError, ValueError, TypeError):
            return False

    if project_dir is None or _resolves_equal(project_dir, _main_root):
        return NativeTeamResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=0.0,
            lead_employee_id=lead_employee_id,
            error=(
                "native teams requires an isolated worktree; refusing to run in "
                "the main repo root (no isolated project_dir was provided)"
            ),
        )

    native_config = config.get("nativeTeamsConfig", {})

    # Build teammate definitions
    teammates = build_teammate_definitions(task, lead_employee_id, config)
    teammate_names = list(teammates.keys())

    # Build lead prompt
    lead_prompt = _build_lead_prompt(task, lead_context, teammate_names)

    # Get model and effort settings
    activation_config = _employee_activator.get_activation_config()
    complexity = task.get("complexity", task.get("estimated_complexity", "standard"))
    model = activation_config["modelByComplexity"].get(
        complexity, activation_config["model"]
    )
    effort_level = EFFORT_BY_COMPLEXITY.get(complexity, "medium")

    # Build command
    cmd = [
        "uv",
        "run",
        "claude",
        "--model",
        model,
        "--print",
        "--dangerously-skip-permissions",
        "--setting-sources",
        "user",
        # WS-114 optimizations
        "--no-session-persistence",
        "--effort",
        effort_level,
    ]

    # Add teammates via --agents flag
    if teammates:
        cmd.extend(["--agents", json.dumps(teammates)])

    # Add tool restrictions
    agentic_config = activation_config["agenticMode"]
    if agentic_config["enabled"]:
        allowed = agentic_config["allowedTools"]
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        disallowed = agentic_config["disallowedTools"]
        if disallowed:
            cmd.extend(["--disallowedTools", ",".join(disallowed)])

    # Build environment. SCOUT-20260726-3: this spawn path was still doing
    # dict(os.environ) + denylist strip after the agent_providers.prepare_
    # environment fix, leaking daemon-ambient secrets (GitHub tokens, cloud
    # credentials, DB URLs) into native-team workers running inside worktrees
    # that may process untrusted repo content. Delegate to prepare_environment
    # (not a re-inlined allowlist copy) so this path also gets the
    # auth-mode-conditional ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN strip —
    # this CLI always runs claude in subscription-style
    # --dangerously-skip-permissions mode, so a stray API key in the
    # daemon's ambient shell must not override the OAuth login.
    child_env = _agent_providers.prepare_environment(
        _agent_providers.ProviderSpec(
            name="claude-subscription",
            provider_type="claude",
            auth="subscription",
            model=model,
        )
    )

    # Clean PATH
    original_path = child_env.get("PATH", "")
    cleaned_parts = [
        p
        for p in original_path.split(os.pathsep)
        if ".venv" not in p and "/uv/" not in p
    ]
    child_env["PATH"] = os.pathsep.join(cleaned_parts)

    # Enable Agent Teams
    child_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

    # Daemon-context markers (PR 265 review: this spawn path was missed by
    # the agent_providers.prepare_environment fix; note this executor runs
    # with --setting-sources user so project hooks never load — the markers
    # still matter for any env-inheriting subprocess and for parity, but
    # enforcement for this path rests on the Layer B PR-time diff check).
    child_env["FORGE_DAEMON"] = "1"
    child_env.setdefault("FORGE_EMPLOYEE_ID", "native-team")

    # Set terminal defaults
    child_env["TERM"] = "xterm-256color"
    child_env["LANG"] = "en_US.UTF-8"
    child_env = {k: v for k, v in child_env.items() if v}

    # Calculate timeout
    timeout_multiplier = native_config.get("timeoutMultiplier", 2.0)
    timeout = int(DEFAULT_NATIVE_TIMEOUT * timeout_multiplier)

    # Log execution
    task_id = task.get("task_id", "unknown")
    print(
        f"[WS-115] NATIVE TEAMS: task={task_id} lead={lead_employee_id} "
        f"teammates={teammate_names} complexity={complexity}",
        file=sys.stderr,
    )

    # Execute
    start_time = time.time()

    # 2026-07-06 fork-bomb guard: never launch a real native-teams lead (which
    # spawns further teammate sessions) from a test context.
    assert_spawn_allowed("native_teams_executor.execute_native_team", subprocess.run)

    try:
        result = subprocess.run(
            cmd,
            input=lead_prompt,
            capture_output=True,
            text=True,
            env=child_env,
            cwd=str(project_dir),
            timeout=timeout,
        )

        duration = time.time() - start_time
        output = result.stdout or ""
        stderr = result.stderr or ""

        # Detect success
        success = _detect_native_success(output, result.returncode)

        error_msg = None
        if not success:
            if stderr:
                error_msg = stderr[:500]
            elif result.returncode != 0:
                error_msg = f"Native teams failed with exit_code={result.returncode}"
            else:
                error_msg = "Native teams execution failed"

        return NativeTeamResult(
            success=success,
            output=output,
            exit_code=result.returncode,
            duration_seconds=duration,
            lead_employee_id=lead_employee_id,
            teammates_defined=teammate_names,
            error=error_msg,
        )

    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return NativeTeamResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            lead_employee_id=lead_employee_id,
            teammates_defined=teammate_names,
            error=f"Native teams timed out after {timeout}s",
        )

    except Exception as e:
        duration = time.time() - start_time
        return NativeTeamResult(
            success=False,
            output="",
            exit_code=-1,
            duration_seconds=duration,
            lead_employee_id=lead_employee_id,
            teammates_defined=teammate_names,
            error=str(e),
        )


def _build_lead_prompt(
    task: dict,
    lead_context: Any,
    teammate_names: list[str],
) -> str:
    """Build the lead's prompt including task and teammate info."""
    prompt_parts = []

    # Task description
    title = task.get("title", task.get("description", "Task"))
    description = task.get("description", "")

    prompt_parts.append(f"# Task: {title}")
    prompt_parts.append(f"\n{description}")

    # Lead context
    if lead_context and hasattr(lead_context, "agent_definition"):
        prompt_parts.append(f"\n## Your Role\n{lead_context.agent_definition[:2000]}")

    # Teammate info
    if teammate_names:
        prompt_parts.append("\n## Team Members Available")
        prompt_parts.append("You have access to specialist teammates who can help:")
        for name in teammate_names:
            prompt_parts.append(f"- {name}")
        prompt_parts.append("\nCoordinate with them as needed for their expertise.")

    # Acceptance criteria if available
    criteria = task.get("acceptance_criteria", [])
    if criteria:
        prompt_parts.append("\n## Acceptance Criteria")
        for c in criteria:
            prompt_parts.append(f"- {c}")

    prompt_parts.append("\n## Instructions")
    prompt_parts.append(
        "Complete this task thoroughly. Use your teammates for review and specialized input."
    )

    return "\n".join(prompt_parts)


def _detect_native_success(output: str, exit_code: int) -> bool:
    """Detect whether native teams execution succeeded."""
    if exit_code != 0:
        return False

    # Check for failure markers
    failure_patterns = [
        "FAILED test_",
        "FAILED ::",
        "AssertionError:",
        "Traceback (most recent call last)",
        "Error:",
    ]
    for pattern in failure_patterns:
        if pattern in output:
            return False

    # Check for success indicators
    success_patterns = [
        "completed",
        "successfully",
        "done",
        "finished",
        "implemented",
        "fixed",
    ]
    output_lower = output.lower()
    for pattern in success_patterns:
        if pattern in output_lower:
            return True

    # If exit_code=0 and no failure markers, assume success
    return True


# -----------------------------------------------------------------------------
# CLI for testing
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test native teams executor")
    parser.add_argument("--task-id", default="test-001")
    parser.add_argument("--complexity", default="epic")
    parser.add_argument("--description", default="Test native teams execution")
    args = parser.parse_args()

    _ensure_imports()

    # Load config
    config_path = Path("gatesmith.json")
    if config_path.exists():
        config = json.loads(config_path.read_text()).get("agentTeams", {})
    else:
        config = {"nativeTeamsConfig": {"enabled": True}}

    task = {
        "task_id": args.task_id,
        "complexity": args.complexity,
        "description": args.description,
    }

    if should_use_native_teams(task, config):
        print(f"Task {args.task_id} qualifies for native teams")
        # Would execute here with proper context
    else:
        print(f"Task {args.task_id} does not qualify for native teams")
