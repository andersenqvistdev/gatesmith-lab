# /// script
# requires-python = ">=3.10"
# ///
"""
SessionStart Hook: Load development context and display project state.
This runs once when Claude Code starts, setting up the environment.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CLAUDE_MD_SIZE_THRESHOLD_KB = 20


def check_claude_md_size() -> dict:
    """
    Check if CLAUDE.md exceeds the size threshold that causes empty Claude output.
    Past incident: 74KB CLAUDE.md produced blank responses (documented in memory).
    Warning only — never blocks.
    """
    threshold_bytes = CLAUDE_MD_SIZE_THRESHOLD_KB * 1024
    result = {
        "oversized": False,
        "size_bytes": 0,
        "size_kb": 0.0,
        "threshold_kb": CLAUDE_MD_SIZE_THRESHOLD_KB,
        "exists": False,
    }
    claude_md_path = os.path.join(os.getcwd(), "CLAUDE.md")
    try:
        # Skip symlinks (avoids disclosing size of unrelated target files)
        # and non-regular files (e.g. a directory named CLAUDE.md)
        if os.path.islink(claude_md_path) or not os.path.isfile(claude_md_path):
            return result
        size_bytes = os.path.getsize(claude_md_path)
        result["exists"] = True
        result["size_bytes"] = size_bytes
        result["size_kb"] = size_bytes / 1024
        result["oversized"] = size_bytes > threshold_bytes
    except OSError:
        pass
    return result


def check_uv_available() -> dict:
    """Check if uv (Python package manager) is available."""
    result = {"available": False, "version": None, "error": None}
    try:
        proc = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            result["available"] = True
            result["version"] = proc.stdout.strip()
        else:
            result["error"] = "uv command failed"
    except FileNotFoundError:
        result["error"] = (
            "uv not found in PATH - hooks will fail! Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    except subprocess.TimeoutExpired:
        result["error"] = "uv command timed out"
    return result


def check_hooks_health() -> dict:
    """
    Verify all configured hooks are accessible from current working directory.
    Returns health status and any issues found.
    """
    result = {
        "healthy": True,
        "cwd": os.getcwd(),
        "hooks_dir_exists": False,
        "missing_hooks": [],
        "inaccessible_hooks": [],
        "working_hooks": [],
        "recommendation": None,
    }

    hooks_dir = os.path.join(os.getcwd(), ".claude", "hooks")
    settings_path = os.path.join(os.getcwd(), ".claude", "settings.json")

    # Check if hooks directory exists
    if not os.path.isdir(hooks_dir):
        result["healthy"] = False
        result["recommendation"] = (
            f"Hooks directory not found at {hooks_dir}. Run install.sh --upgrade from Gatesmith source."
        )
        return result

    result["hooks_dir_exists"] = True

    # Get list of hooks from settings.json
    configured_hooks = set()
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f)

            hooks_config = settings.get("hooks", {})
            for event_type, hook_list in hooks_config.items():
                for hook_entry in hook_list:
                    for hook in hook_entry.get("hooks", []):
                        cmd = hook.get("command", "")
                        # Extract hook path from "uv run .claude/hooks/xxx.py"
                        if ".claude/hooks/" in cmd:
                            hook_path = cmd.split(".claude/hooks/")[-1].split()[0]
                            configured_hooks.add(hook_path)
        except (json.JSONDecodeError, OSError):
            pass

    # Check each configured hook
    for hook_name in configured_hooks:
        hook_path = os.path.join(hooks_dir, hook_name)
        if not os.path.exists(hook_path):
            result["missing_hooks"].append(hook_name)
            result["healthy"] = False
        elif not os.access(hook_path, os.X_OK):
            result["inaccessible_hooks"].append(hook_name)
            # Not blocking, just warning
        else:
            result["working_hooks"].append(hook_name)

    if result["missing_hooks"]:
        result["recommendation"] = (
            f"Missing hooks: {', '.join(result['missing_hooks'])}. "
            f"Run: ./install.sh --upgrade <project-path> from Gatesmith source directory."
        )

    return result


def get_git_info() -> dict:
    """Gather current git state."""
    info = {}
    try:
        info["branch"] = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        info["status"] = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5
        ).stdout.strip()

        info["last_commit"] = subprocess.run(
            ["git", "log", "-1", "--oneline"], capture_output=True, text=True, timeout=5
        ).stdout.strip()

        dirty_count = len([line for line in info["status"].split("\n") if line.strip()])
        info["dirty_files"] = dirty_count
    except (FileNotFoundError, subprocess.TimeoutExpired):
        info["error"] = "Not a git repository or git not available"

    return info


def get_project_info() -> dict:
    """Detect project type and key files."""
    info = {"type": "unknown", "key_files": []}

    markers = {
        "package.json": "node",
        "pyproject.toml": "python",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java-maven",
        "build.gradle": "java-gradle",
    }

    for marker, ptype in markers.items():
        if os.path.exists(marker):
            info["type"] = ptype
            info["key_files"].append(marker)

    return info


def get_company_info() -> dict | None:
    """Get company/organization status if .company/ exists."""
    company_dir = os.path.join(os.getcwd(), ".company")
    org_file = os.path.join(company_dir, "org.json")
    work_queue_file = os.path.join(company_dir, "work_queue.json")

    if not os.path.isdir(company_dir):
        return None

    info = {"active": True, "departments": 0, "agents": 0, "active_work": 0}

    if os.path.exists(org_file):
        try:
            with open(org_file) as f:
                org_data = json.load(f)
                # departments is a list in org.json schema, not a dict
                departments = org_data.get("departments", [])
                info["departments"] = (
                    len(departments) if isinstance(departments, list) else 0
                )

                # Count employees from top-level employees array (v2.x schema)
                # Also support legacy 'agents' field for backward compatibility
                employees = org_data.get("employees", org_data.get("agents", []))
                info["agents"] = len(employees) if isinstance(employees, list) else 0
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Count active work items from work_queue.json (separate file, not in org.json)
    if os.path.exists(work_queue_file):
        try:
            with open(work_queue_file) as f:
                work_queue_data = json.load(f)
                # work_queue.json has "pending", "in_progress", and "blocked" arrays
                pending = work_queue_data.get("pending", [])
                in_progress = work_queue_data.get("in_progress", [])
                blocked = work_queue_data.get("blocked", [])
                # Count all active items (pending + in_progress + blocked)
                info["active_work"] = (
                    (len(pending) if isinstance(pending, list) else 0)
                    + (len(in_progress) if isinstance(in_progress, list) else 0)
                    + (len(blocked) if isinstance(blocked, list) else 0)
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return info


def get_company_config() -> dict | None:
    """Load company config from .company/config.json."""
    company_dir = os.path.join(os.getcwd(), ".company")
    config_file = os.path.join(company_dir, "config.json")

    if not os.path.exists(config_file):
        return None

    try:
        with open(config_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_current_health() -> int:
    """
    Get current health score from dashboard_aggregator.
    Returns a default of 75 if unavailable.
    """
    try:
        # Add hooks/company to path for import
        hooks_company_dir = os.path.join(os.getcwd(), ".claude", "hooks", "company")
        if hooks_company_dir not in sys.path:
            sys.path.insert(0, hooks_company_dir)

        from dashboard_aggregator import aggregate_health

        health_data = aggregate_health()
        return health_data.get("health_score", 75)
    except (ImportError, Exception):
        return 75


def save_session_state(session_start: str, start_health: int) -> None:
    """
    Save session state to .company/session_state.json for stop_validator to use.

    Args:
        session_start: ISO format session start timestamp
        start_health: Health score at session start
    """
    company_dir = os.path.join(os.getcwd(), ".company")

    # Create .company dir if it doesn't exist
    if not os.path.isdir(company_dir):
        try:
            os.makedirs(company_dir, exist_ok=True)
        except OSError:
            return

    state_file = os.path.join(company_dir, "session_state.json")
    state = {
        "start_time": session_start,
        "start_health": start_health,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def auto_archive_idle_consultants() -> dict:
    """
    Check for and archive idle auto-consultants at session start.

    Respects config flags:
    - agents.autoArchiveConsultants: whether to run auto-archival
    - agents.consultantIdleTimeout: idle threshold in hours

    Returns:
        Dict with archival results
    """
    result = {"checked": False, "archived": [], "errors": []}

    # Load config to check if auto-archive is enabled
    config = get_company_config()
    if config is None:
        return result

    agents_config = config.get("agents", {})

    # Check if auto-archive is enabled (default: True if not specified)
    if not agents_config.get("autoArchiveConsultants", True):
        result["checked"] = True
        result["skipped"] = "autoArchiveConsultants disabled"
        return result

    # Get idle timeout from config (default: 24 hours)
    idle_timeout = agents_config.get("consultantIdleTimeout", 24)

    # Import consultant lifecycle functions
    try:
        # Add hooks/company to path for import
        hooks_company_dir = os.path.join(os.getcwd(), ".claude", "hooks", "company")
        if hooks_company_dir not in sys.path:
            sys.path.insert(0, hooks_company_dir)

        from consultant_lifecycle import archive_consultant, check_idle_consultants
    except ImportError:
        # Company extension not installed - silently skip
        return result

    result["checked"] = True

    # Find idle consultants
    try:
        idle = check_idle_consultants(timeout_hours=idle_timeout)
    except Exception as e:
        result["errors"].append(f"check_idle_consultants failed: {e}")
        return result

    # Archive each idle consultant
    for consultant_id in idle:
        try:
            archive_result = archive_consultant(consultant_id, reason="idle_timeout")
            if archive_result.get("success"):
                result["archived"].append(consultant_id)
            else:
                result["errors"].append(
                    f"{consultant_id}: {archive_result.get('message', 'unknown error')}"
                )
        except Exception as e:
            result["errors"].append(f"{consultant_id}: {e}")

    return result


def main():
    # Check uv availability first - critical for hooks
    uv_status = check_uv_available()

    # Check hooks health - detect missing or inaccessible hooks
    hooks_health = check_hooks_health()

    # Check CLAUDE.md size - oversized files cause empty Claude output
    claude_md_info = check_claude_md_size()

    git = get_git_info()
    project = get_project_info()

    session_start = datetime.now(timezone.utc).isoformat()

    context = {
        "session_start": session_start,
        "cwd": os.getcwd(),
        "git": git,
        "project": project,
        "uv": uv_status,
        "hooks_health": hooks_health,
    }

    # Get initial health score and save session state for stop_validator
    start_health = get_current_health()
    save_session_state(session_start, start_health)

    # Auto-archive idle consultants at session start
    archive_result = auto_archive_idle_consultants()
    context["auto_archive"] = archive_result

    # Check when last security audit was run
    audit_reminder = ""
    log_dir = os.path.join(os.getcwd(), "logs")
    activity_log = os.path.join(log_dir, "activity.jsonl")
    if os.path.exists(activity_log):
        try:
            last_audit = None
            with open(activity_log) as f:
                for line in f:
                    entry = json.loads(line)
                    if "security" in entry.get("input_summary", "").lower():
                        last_audit = entry.get("timestamp", "")
            if last_audit:
                from datetime import datetime as dt

                audit_date = dt.fromisoformat(last_audit.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - audit_date).days
                if days_ago > 7:
                    audit_reminder = f"Security audit: {days_ago} days ago. Consider running /security-audit"
            else:
                audit_reminder = (
                    "No security audit on record. Consider running /security-audit"
                )
        except Exception:
            pass

    # Count active hooks and agents
    hooks_dir = os.path.join(os.getcwd(), ".claude", "hooks")
    agents_dir = os.path.join(os.getcwd(), ".claude", "agents")
    hook_count = (
        len([f for f in os.listdir(hooks_dir) if f.endswith(".py")])
        if os.path.isdir(hooks_dir)
        else 0
    )
    agent_count = (
        len([f for f in os.listdir(agents_dir) if f.endswith(".md")])
        if os.path.isdir(agents_dir)
        else 0
    )

    # Get company info if available
    company = get_company_info()

    # Output context summary for the agent
    lines = [
        "=== Session Initialized (Gatesmith) ===",
        f"Project: {project['type']} | Dir: {os.path.basename(os.getcwd())}",
        f"Gatesmith: {hook_count} hooks active | {agent_count} agents available",
    ]

    # Critical warning if uv is not available
    if not uv_status["available"]:
        lines.insert(1, f"⚠️  WARNING: {uv_status['error']}")
        lines.insert(2, "   All hooks use 'uv run' and will fail without uv installed!")

    # Critical warning if hooks are unhealthy
    if not hooks_health["healthy"]:
        lines.insert(1, "⚠️  WARNING: Hook configuration issues detected!")
        if hooks_health["missing_hooks"]:
            lines.insert(
                2, f"   Missing hooks: {', '.join(hooks_health['missing_hooks'])}"
            )
        if hooks_health["recommendation"]:
            lines.insert(3, f"   Fix: {hooks_health['recommendation']}")

    # Warning if CLAUDE.md is oversized (causes empty Claude output)
    if claude_md_info["oversized"]:
        lines.insert(
            1,
            f"⚠️  WARNING: CLAUDE.md is {claude_md_info['size_kb']:.1f}KB "
            f"(>{claude_md_info['threshold_kb']}KB) — oversized files cause empty output. "
            f"See docs/CLAUDE-full.md",
        )

    if company:
        lines.append(
            f"Company: active | {company['departments']} departments | {company['agents']} agents | {company['active_work']} active work items"
        )

    # Report auto-archived consultants
    if archive_result.get("archived"):
        archived_ids = archive_result["archived"]
        lines.append(
            f"Auto-archived {len(archived_ids)} idle consultant(s): {', '.join(archived_ids)}"
        )

    if "branch" in git:
        lines.append(
            f"Git: {git['branch']} | Last: {git.get('last_commit', 'N/A')} | Dirty: {git.get('dirty_files', 0)} files"
        )

    if audit_reminder:
        lines.append(audit_reminder)

    print("\n".join(lines))

    # Log session start
    log_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "sessions.jsonl"), "a") as f:
        f.write(json.dumps(context) + "\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
