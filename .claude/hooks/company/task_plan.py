# /// script
# requires-python = ">=3.10"
# ///
"""
Task Plan Manager (Checklist-as-State)

Manages plan files that serve as both task descriptions AND progress state.
This eliminates the need for separate state tracking files and provides
real-time visibility into task progress.

Plan File Format:
```markdown
# Task: <title>

**ID**: <task-id>
**Started**: <ISO timestamp>
**Status**: [ ] In Progress | [x] Completed | [!] Failed | [?] Blocked

## Description
<task description>

## Progress
- [ ] Step 1 (not started)
- [x] Step 2 (completed)
- [!] Step 3 (failed)
- [?] Step 4 (blocked/question)

## Notes
<worker updates>
```

Based on patterns from github.com/bassimeledath/dispatch
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class CheckboxState(Enum):
    """Checkbox states in plan files."""

    PENDING = "[ ]"
    COMPLETED = "[x]"
    FAILED = "[!]"
    BLOCKED = "[?]"

    @classmethod
    def from_string(cls, s: str) -> "CheckboxState":
        """Parse checkbox state from string."""
        s = s.strip().lower()
        if s in ("[ ]", ""):
            return cls.PENDING
        elif s in ("[x]", "[X]"):
            return cls.COMPLETED
        elif s in ("[!]",):
            return cls.FAILED
        elif s in ("[?]",):
            return cls.BLOCKED
        return cls.PENDING


class TaskStatus(Enum):
    """Overall task status derived from plan file."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class PlanStep:
    """A single step/subtask in a plan."""

    text: str
    state: CheckboxState = CheckboxState.PENDING
    line_number: int = 0

    @property
    def is_done(self) -> bool:
        return self.state == CheckboxState.COMPLETED

    @property
    def is_blocked(self) -> bool:
        return self.state in (CheckboxState.BLOCKED, CheckboxState.FAILED)


@dataclass
class TaskPlan:
    """
    Represents a task plan file.

    The plan file is the single source of truth for task state.
    """

    task_id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    steps: list[PlanStep] = field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    employee_id: str | None = None
    notes: str = ""
    file_path: Path | None = None

    @property
    def progress_percent(self) -> float:
        """Calculate completion percentage."""
        if not self.steps:
            return 0.0
        completed = sum(1 for s in self.steps if s.is_done)
        return (completed / len(self.steps)) * 100

    @property
    def completed_steps(self) -> int:
        """Count of completed steps."""
        return sum(1 for s in self.steps if s.is_done)

    @property
    def total_steps(self) -> int:
        """Total number of steps."""
        return len(self.steps)

    @property
    def blocked_steps(self) -> list[PlanStep]:
        """Get all blocked or failed steps."""
        return [s for s in self.steps if s.is_blocked]


class PlanManager:
    """
    Manages plan files for tasks.

    Provides CRUD operations with atomic file writes.
    """

    CHECKBOX_PATTERN = re.compile(r"^(\s*)-\s*\[([ xX!?])\]\s*(.+)$")
    STATUS_PATTERN = re.compile(r"\*\*Status\*\*:\s*\[([ xX!?])\]\s*(.+)")
    ID_PATTERN = re.compile(r"\*\*ID\*\*:\s*(.+)")
    STARTED_PATTERN = re.compile(r"\*\*Started\*\*:\s*(.+)")
    COMPLETED_PATTERN = re.compile(r"\*\*Completed\*\*:\s*(.+)")
    ASSIGNED_PATTERN = re.compile(r"\*\*Assigned\*\*:\s*(.+)")

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or Path.cwd()
        self.plans_dir = self.project_root / ".company" / "plans"
        self.plans_dir.mkdir(parents=True, exist_ok=True)

    def create_plan(
        self,
        task_id: str,
        title: str,
        description: str,
        steps: list[str] | None = None,
        employee_id: str | None = None,
    ) -> TaskPlan:
        """
        Create a new plan file for a task.

        Args:
            task_id: Unique task identifier
            title: Task title
            description: Full task description
            steps: Optional list of step descriptions
            employee_id: Assigned employee

        Returns:
            TaskPlan instance
        """
        # Generate default steps if none provided
        if steps is None:
            steps = self._generate_default_steps(title, description)

        plan = TaskPlan(
            task_id=task_id,
            title=title,
            description=description,
            status=TaskStatus.IN_PROGRESS,
            steps=[PlanStep(text=s, line_number=i) for i, s in enumerate(steps)],
            started_at=datetime.now(timezone.utc),
            employee_id=employee_id,
        )

        plan.file_path = self.plans_dir / f"{task_id}.plan.md"
        self._write_plan(plan)
        return plan

    def _generate_default_steps(self, title: str, description: str) -> list[str]:
        """Generate default steps based on task type."""
        # Detect task type from keywords
        title_lower = title.lower()

        if any(k in title_lower for k in ["test", "coverage"]):
            return [
                "Analyze existing test coverage",
                "Identify gaps in test coverage",
                "Write unit tests for uncovered code",
                "Run tests and verify passing",
                "Update documentation if needed",
            ]
        elif any(k in title_lower for k in ["fix", "bug", "issue"]):
            return [
                "Reproduce the issue",
                "Identify root cause",
                "Implement fix",
                "Write regression test",
                "Verify fix resolves issue",
            ]
        elif any(k in title_lower for k in ["implement", "add", "create", "build"]):
            return [
                "Analyze requirements",
                "Design solution approach",
                "Implement core functionality",
                "Add error handling",
                "Write tests",
                "Update documentation",
            ]
        elif any(k in title_lower for k in ["refactor", "improve", "optimize"]):
            return [
                "Analyze current implementation",
                "Identify improvement areas",
                "Implement changes incrementally",
                "Ensure tests still pass",
                "Review for side effects",
            ]
        elif any(k in title_lower for k in ["document", "docs", "readme"]):
            return [
                "Review existing documentation",
                "Identify gaps or outdated sections",
                "Write new documentation",
                "Add examples where helpful",
                "Verify accuracy",
            ]
        else:
            return [
                "Understand task requirements",
                "Plan implementation approach",
                "Execute implementation",
                "Verify completion",
                "Document any changes",
            ]

    def load_plan(self, task_id: str) -> TaskPlan | None:
        """
        Load a plan from file.

        Args:
            task_id: Task identifier

        Returns:
            TaskPlan or None if not found
        """
        plan_file = self.plans_dir / f"{task_id}.plan.md"
        if not plan_file.exists():
            return None

        return self._parse_plan_file(plan_file)

    def _parse_plan_file(self, plan_file: Path) -> TaskPlan:
        """Parse a plan file into TaskPlan object."""
        content = plan_file.read_text()
        lines = content.split("\n")

        # Extract title from first heading
        title = ""
        for line in lines:
            if line.startswith("# Task:"):
                title = line.replace("# Task:", "").strip()
                break

        # Extract metadata
        task_id = ""
        started_at = None
        completed_at = None
        employee_id = None
        status = TaskStatus.IN_PROGRESS

        for line in lines:
            if match := self.ID_PATTERN.search(line):
                task_id = match.group(1).strip()
            elif match := self.STARTED_PATTERN.search(line):
                try:
                    started_at = datetime.fromisoformat(
                        match.group(1).strip().replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            elif match := self.COMPLETED_PATTERN.search(line):
                try:
                    completed_at = datetime.fromisoformat(
                        match.group(1).strip().replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            elif match := self.ASSIGNED_PATTERN.search(line):
                employee_id = match.group(1).strip()
            elif match := self.STATUS_PATTERN.search(line):
                checkbox = match.group(1)
                status_text = match.group(2).strip().lower()
                if checkbox == "x" or "completed" in status_text:
                    status = TaskStatus.COMPLETED
                elif checkbox == "!" or "failed" in status_text:
                    status = TaskStatus.FAILED
                elif checkbox == "?" or "blocked" in status_text:
                    status = TaskStatus.BLOCKED

        # Extract description (between ## Description and ## Progress)
        description = ""
        in_description = False
        for line in lines:
            if "## Description" in line:
                in_description = True
                continue
            if in_description and line.startswith("##"):
                break
            if in_description:
                description += line + "\n"
        description = description.strip()

        # Extract steps (checkbox items under ## Progress)
        steps = []
        in_progress = False
        for line_num, line in enumerate(lines):
            if "## Progress" in line:
                in_progress = True
                continue
            if in_progress and line.startswith("##"):
                break
            if in_progress:
                if match := self.CHECKBOX_PATTERN.match(line):
                    checkbox = match.group(2)
                    text = match.group(3).strip()
                    state = CheckboxState.from_string(f"[{checkbox}]")
                    steps.append(PlanStep(text=text, state=state, line_number=line_num))

        # Extract notes (after ## Notes)
        notes = ""
        in_notes = False
        for line in lines:
            if "## Notes" in line:
                in_notes = True
                continue
            if in_notes and line.startswith("##"):
                break
            if in_notes:
                notes += line + "\n"
        notes = notes.strip()

        return TaskPlan(
            task_id=task_id,
            title=title,
            description=description,
            status=status,
            steps=steps,
            started_at=started_at,
            completed_at=completed_at,
            employee_id=employee_id,
            notes=notes,
            file_path=plan_file,
        )

    def update_step(
        self,
        task_id: str,
        step_index: int,
        new_state: CheckboxState,
    ) -> bool:
        """
        Update a specific step's state.

        Args:
            task_id: Task identifier
            step_index: Index of step to update
            new_state: New checkbox state

        Returns:
            True if successful
        """
        plan = self.load_plan(task_id)
        if not plan or step_index >= len(plan.steps):
            return False

        plan.steps[step_index].state = new_state

        # Auto-update overall status
        if all(s.is_done for s in plan.steps):
            plan.status = TaskStatus.COMPLETED
            plan.completed_at = datetime.now(timezone.utc)
        elif any(s.state == CheckboxState.FAILED for s in plan.steps):
            plan.status = TaskStatus.FAILED
        elif any(s.state == CheckboxState.BLOCKED for s in plan.steps):
            plan.status = TaskStatus.BLOCKED

        self._write_plan(plan)
        return True

    def mark_step_complete(self, task_id: str, step_index: int) -> bool:
        """Mark a step as completed."""
        return self.update_step(task_id, step_index, CheckboxState.COMPLETED)

    def mark_step_failed(self, task_id: str, step_index: int) -> bool:
        """Mark a step as failed."""
        return self.update_step(task_id, step_index, CheckboxState.FAILED)

    def mark_step_blocked(self, task_id: str, step_index: int) -> bool:
        """Mark a step as blocked."""
        return self.update_step(task_id, step_index, CheckboxState.BLOCKED)

    def complete_plan(self, task_id: str) -> bool:
        """Mark entire plan as completed."""
        plan = self.load_plan(task_id)
        if not plan:
            return False

        plan.status = TaskStatus.COMPLETED
        plan.completed_at = datetime.now(timezone.utc)

        # Mark all pending steps as complete
        for step in plan.steps:
            if step.state == CheckboxState.PENDING:
                step.state = CheckboxState.COMPLETED

        self._write_plan(plan)
        return True

    def fail_plan(self, task_id: str, reason: str = "") -> bool:
        """Mark entire plan as failed."""
        plan = self.load_plan(task_id)
        if not plan:
            return False

        plan.status = TaskStatus.FAILED
        plan.completed_at = datetime.now(timezone.utc)
        if reason:
            plan.notes += f"\n\n**Failure Reason**: {reason}"

        self._write_plan(plan)
        return True

    def add_note(self, task_id: str, note: str) -> bool:
        """Add a note to the plan."""
        plan = self.load_plan(task_id)
        if not plan:
            return False

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        plan.notes += f"\n- [{timestamp}] {note}"

        self._write_plan(plan)
        return True

    def _write_plan(self, plan: TaskPlan) -> None:
        """Write plan to file atomically."""
        if not plan.file_path:
            plan.file_path = self.plans_dir / f"{plan.task_id}.plan.md"

        # Generate markdown content
        content = self._render_plan(plan)
        self._atomic_write(plan.file_path, content)

    def _render_plan(self, plan: TaskPlan) -> str:
        """Render plan to markdown format."""
        # Status checkbox
        status_checkbox = {
            TaskStatus.PENDING: "[ ]",
            TaskStatus.IN_PROGRESS: "[ ]",
            TaskStatus.COMPLETED: "[x]",
            TaskStatus.FAILED: "[!]",
            TaskStatus.BLOCKED: "[?]",
        }.get(plan.status, "[ ]")

        status_text = {
            TaskStatus.PENDING: "Pending",
            TaskStatus.IN_PROGRESS: "In Progress",
            TaskStatus.COMPLETED: "Completed",
            TaskStatus.FAILED: "Failed",
            TaskStatus.BLOCKED: "Blocked",
        }.get(plan.status, "Unknown")

        lines = [
            f"# Task: {plan.title}",
            "",
            f"**ID**: {plan.task_id}",
            f"**Started**: {plan.started_at.isoformat() if plan.started_at else 'N/A'}",
            f"**Status**: {status_checkbox} {status_text}",
        ]

        if plan.completed_at:
            lines.append(f"**Completed**: {plan.completed_at.isoformat()}")

        if plan.employee_id:
            lines.append(f"**Assigned**: {plan.employee_id}")

        lines.extend(
            [
                "",
                "## Description",
                "",
                plan.description,
                "",
                "## Progress",
                "",
            ]
        )

        # Render steps
        for step in plan.steps:
            lines.append(f"- {step.state.value} {step.text}")

        lines.extend(
            [
                "",
                "## Notes",
                "",
                plan.notes if plan.notes else "_No notes yet._",
            ]
        )

        return "\n".join(lines)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write file atomically."""
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

    def list_plans(self, status: TaskStatus | None = None) -> list[TaskPlan]:
        """
        List all plans, optionally filtered by status.

        Args:
            status: Filter by status (None = all)

        Returns:
            List of TaskPlan objects
        """
        plans = []
        for plan_file in self.plans_dir.glob("*.plan.md"):
            try:
                plan = self._parse_plan_file(plan_file)
                if status is None or plan.status == status:
                    plans.append(plan)
            except Exception:
                continue
        return sorted(
            plans,
            key=lambda p: p.started_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    def get_progress_summary(self) -> dict:
        """
        Get aggregate progress across all active plans.

        Returns:
            Summary dict with counts and percentages
        """
        plans = self.list_plans()

        total = len(plans)
        completed = sum(1 for p in plans if p.status == TaskStatus.COMPLETED)
        failed = sum(1 for p in plans if p.status == TaskStatus.FAILED)
        blocked = sum(1 for p in plans if p.status == TaskStatus.BLOCKED)
        in_progress = total - completed - failed - blocked

        total_steps = sum(p.total_steps for p in plans)
        completed_steps = sum(p.completed_steps for p in plans)

        return {
            "total_plans": total,
            "completed": completed,
            "failed": failed,
            "blocked": blocked,
            "in_progress": in_progress,
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "overall_progress": (completed_steps / total_steps * 100)
            if total_steps > 0
            else 0,
        }

    def delete_plan(self, task_id: str) -> bool:
        """Delete a plan file."""
        plan_file = self.plans_dir / f"{task_id}.plan.md"
        if plan_file.exists():
            plan_file.unlink()
            return True
        return False


# CLI interface
if __name__ == "__main__":
    import sys

    manager = PlanManager()

    if len(sys.argv) < 2:
        print("Usage: task_plan.py <command> [args]")
        print("Commands: create, load, list, progress, complete, fail")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        plans = manager.list_plans()
        for p in plans:
            print(
                f"{p.task_id}: {p.title} [{p.status.value}] {p.progress_percent:.0f}%"
            )

    elif cmd == "progress":
        summary = manager.get_progress_summary()
        print(json.dumps(summary, indent=2))

    elif cmd == "create" and len(sys.argv) >= 4:
        task_id = sys.argv[2]
        title = sys.argv[3]
        desc = sys.argv[4] if len(sys.argv) > 4 else "No description"
        plan = manager.create_plan(task_id, title, desc)
        print(f"Created: {plan.file_path}")

    elif cmd == "load" and len(sys.argv) >= 3:
        task_id = sys.argv[2]
        plan = manager.load_plan(task_id)
        if plan:
            print(f"Title: {plan.title}")
            print(f"Status: {plan.status.value}")
            print(f"Progress: {plan.progress_percent:.0f}%")
            for i, step in enumerate(plan.steps):
                print(f"  {i}: {step.state.value} {step.text}")
        else:
            print(f"Plan not found: {task_id}")
