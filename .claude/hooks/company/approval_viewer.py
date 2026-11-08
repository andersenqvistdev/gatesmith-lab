#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Approval Viewer — list and approve pending strategic ideas.

Usage:
    uv run .claude/hooks/company/approval_viewer.py               # List pending approvals
    uv run .claude/hooks/company/approval_viewer.py approve <id>  # Approve by ID
    uv run .claude/hooks/company/approval_viewer.py approve --all # Approve all pending
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_company_dir(start: Path | None = None) -> Path:
    # start lets callers (and tests) anchor the search explicitly; the default
    # keeps the __file__-then-cwd walk for CLI use from anywhere in the repo.
    # Without the anchor a test can only observe the walk's FIRST branch, which
    # resolves against this file's own checkout — so any assertion about the cwd
    # branch silently tested the developer's working tree instead of the code.
    starts = (
        [start] if start is not None else [Path(__file__).resolve().parent, Path.cwd()]
    )
    for s in starts:
        for parent in [s] + list(s.parents):
            candidate = parent / ".company"
            if candidate.is_dir() and (candidate / "org.json").exists():
                return candidate
    return Path(".company")


def relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 0:
            return "future"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return "?"


def trunc(s: str, width: int) -> str:
    return (s[: width - 1] + "…") if len(s) > width else s


def load_pending_approvals(company_dir: Path) -> list[dict]:
    """Load proposals from pending_approvals.json. Returns [] when missing."""
    path = company_dir / "state" / "pending_approvals.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("proposals", data.get("pending", []))
    except (json.JSONDecodeError, OSError):
        return []


def render_approvals(proposals: list[dict]) -> str:
    """Render pending approvals as human-readable text."""
    w = 78
    lines: list[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    pending = [p for p in proposals if p.get("status") == "pending"]

    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("\033[1m  PENDING APPROVALS\033[0m")
    lines.append(f"  {now_str}")
    lines.append("\033[1m" + "=" * w + "\033[0m")
    lines.append("")

    if not pending:
        lines.append("  \033[32mNo pending approvals.\033[0m")
        lines.append("")
        lines.append("\033[2m" + "─" * w + "\033[0m")
        return "\n".join(lines)

    lines.append(f"  {len(pending)} pending  (gatesmith-queue approve <id> | --all)")
    lines.append("")

    for p in pending:
        pid = p.get("proposal_id", "?")
        title = p.get("title", "Untitled")
        value = p.get("estimated_value", "?")
        created_at = p.get("created_at")
        age = relative_time(created_at)
        tier = p.get("approval_tier", "?")
        approvers = ", ".join(p.get("approval_required", []))

        lines.append(f"  \033[1m{pid}\033[0m")
        lines.append(f"    Title:    {trunc(title, 60)}")
        if isinstance(value, float):
            lines.append(f"    Value:    {value:.2f}")
        else:
            lines.append(f"    Value:    {value}")
        lines.append(f"    Age:      {age}")
        lines.append(f"    Tier:     {tier}")
        if approvers:
            lines.append(f"    Needs:    {approvers}")
        lines.append("")

    lines.append("\033[2m" + "─" * w + "\033[0m")
    return "\n".join(lines)


def _load_ideation_module(company_dir: Path):
    """Lazily import employee_ideation from the hooks directory."""
    hooks_dir = company_dir.parent / ".claude" / "hooks" / "company"
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))
    try:
        import employee_ideation

        return employee_ideation
    except ImportError:
        return None


def approve_idea(idea_id: str, company_dir: Path) -> dict:
    """Approve a single idea via employee_ideation.approve_idea."""
    module = _load_ideation_module(company_dir)
    if module is None:
        return {"success": False, "error": "employee_ideation module not available"}
    try:
        return module.approve_idea(idea_id, approved_by="human")
    except Exception as e:
        return {"success": False, "error": str(e)}


def approve_all(company_dir: Path) -> dict:
    """Approve all pending ideas in pending_approvals.json."""
    proposals = load_pending_approvals(company_dir)
    pending = [p for p in proposals if p.get("status") == "pending"]

    if not pending:
        return {"success": True, "approved": [], "errors": []}

    approved = []
    errors = []
    for p in pending:
        idea_id = p.get("proposal_id")
        if not idea_id:
            continue
        result = approve_idea(idea_id, company_dir)
        if result.get("success"):
            approved.append(idea_id)
        else:
            errors.append({"id": idea_id, "error": result.get("error", "unknown")})

    return {"success": True, "approved": approved, "errors": errors}


def main() -> None:
    args = sys.argv[1:]
    company_dir = find_company_dir()

    if not args or args[0] == "list":
        proposals = load_pending_approvals(company_dir)
        print(render_approvals(proposals))
        return

    if args[0] == "approve":
        rest = args[1:]
        if not rest:
            print("Error: provide an idea ID or --all", file=sys.stderr)
            sys.exit(2)

        if rest[0] == "--all":
            result = approve_all(company_dir)
            approved = result.get("approved", [])
            errors = result.get("errors", [])
            if not approved and not errors:
                print("No pending approvals.")
                return
            print(f"Approved {len(approved)} idea(s):")
            for eid in approved:
                print(f"  ✓ {eid}")
            for err in errors:
                print(f"  ✗ {err['id']}: {err['error']}", file=sys.stderr)
            if errors:
                sys.exit(1)
        else:
            idea_id = rest[0]
            result = approve_idea(idea_id, company_dir)
            if result.get("success"):
                print(f"Approved idea {idea_id}")
            else:
                print(f"Error: {result.get('error')}", file=sys.stderr)
                sys.exit(1)
        return

    if args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return

    print(f"Unknown subcommand: {args[0]}", file=sys.stderr)
    print("Usage: approval_viewer.py [approve <id|--all>]", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
