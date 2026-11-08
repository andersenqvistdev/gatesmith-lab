# /// script
# requires-python = ">=3.11"
# ///
"""
Centralized path definitions for .company/ directory structure.

P73: Reorganization of .company/ directory for better maintainability.

Structure:
  .company/
  ├── config/           # Configuration files (rarely change)
  ├── state/            # Runtime state files (change frequently)
  ├── runtime/          # Daemon runtime files (pid, heartbeat, lock)
  ├── org.json          # Root org definition (stays at root)
  ├── vision.md         # Company vision (stays at root)
  │
  │ # Deliverable directories (task outputs)
  ├── business/
  ├── sales/
  ├── research/
  ├── knowledge/
  ├── reports/
  ├── employee-ideas/
  │
  │ # Infrastructure directories
  ├── agents/
  ├── employees/
  ├── assignments/
  ├── escalations/
  ├── logs/
  ├── schemas/
  └── ...
"""

from pathlib import Path as _Path


# Resolve company root
def _find_company_root() -> _Path:
    """Find .company directory, searching upward from cwd."""
    cwd = _Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / ".company"
        if candidate.is_dir():
            return candidate
    # Fallback to cwd/.company
    return cwd / ".company"


COMPANY_ROOT = _find_company_root()

# === Core files (stay at root) ===
ORG_JSON = COMPANY_ROOT / "org.json"
VISION_MD = COMPANY_ROOT / "vision.md"

# === Config directory ===
CONFIG_DIR = COMPANY_ROOT / "config"
CONFIG_JSON = CONFIG_DIR / "config.json"
LOOP_CONFIG_JSON = CONFIG_DIR / "loop_config.json"
MANIFEST_JSON = CONFIG_DIR / "manifest.json"

# === State directory ===
STATE_DIR = COMPANY_ROOT / "state"
ADAPTIVE_SCHEDULER_STATE = STATE_DIR / "adaptive_scheduler_state.json"
APPROVAL_HISTORY = STATE_DIR / "approval_history.json"
AUTO_MERGE_SKIP_TRACKER = STATE_DIR / "auto_merge_skip_tracker.json"
AUTONOMY_VALIDATION = STATE_DIR / "autonomy_validation.json"
CIRCUIT_BREAKER = STATE_DIR / "circuit_breaker.json"
DAEMON_METRICS = STATE_DIR / "daemon_metrics.json"
DOCUMENT_APPROVALS = STATE_DIR / "document_approvals.json"
EFFICIENCY_DATA = STATE_DIR / "efficiency_data.json"
EMPLOYEE_IDEAS_JSON = STATE_DIR / "employee_ideas.json"
FEEDBACK_STATE = STATE_DIR / "feedback_state.json"
IMPROVEMENT_CYCLES = STATE_DIR / "improvement_cycles.json"
LEARNING_OUTCOMES = STATE_DIR / "learning_outcomes.json"
LOOP_MONITOR = STATE_DIR / "loop_monitor.json"
NEW_PROPOSALS_CSL = STATE_DIR / "new_proposals_csl.json"
NEW_PROPOSALS_EMPLOYEES = STATE_DIR / "new_proposals_employees.json"
ORCHESTRATOR_METRICS = STATE_DIR / "orchestrator_metrics.json"
PENDING_APPROVALS = STATE_DIR / "pending_approvals.json"
PLANNING_APPROVALS = STATE_DIR / "planning_approvals.json"
REORG_STATE = STATE_DIR / "reorg_state.json"
ROADMAP_STATE = STATE_DIR / "roadmap_state.json"
ROUTING_STATE = STATE_DIR / "routing_state.json"
SESSION_STATE = STATE_DIR / "session_state.json"
STRATEGIC_STATE = STATE_DIR / "strategic_state.json"
VALIDATION_TRACKER = STATE_DIR / "validation_tracker.json"
VENTURE_STATE = STATE_DIR / "venture_state.json"
WORK_ITEMS = STATE_DIR / "work_items.json"
WORK_QUEUE = STATE_DIR / "work_queue.json"
HEALTH_CHECK_LOG = STATE_DIR / "health_check_log.jsonl"

# === Runtime directory (daemon files) ===
RUNTIME_DIR = COMPANY_ROOT / "runtime"
DAEMON_PID = RUNTIME_DIR / "daemon.pid"
DAEMON_HEARTBEAT = RUNTIME_DIR / "daemon.heartbeat"
DASHBOARD_PID = RUNTIME_DIR / "dashboard.pid"
QUEUE_LOCK = RUNTIME_DIR / "queue.lock"

# === Infrastructure directories ===
AGENTS_DIR = COMPANY_ROOT / "agents"
EMPLOYEES_DIR = COMPANY_ROOT / "employees"
ASSIGNMENTS_DIR = COMPANY_ROOT / "assignments"
ESCALATIONS_DIR = COMPANY_ROOT / "escalations"
LOGS_DIR = COMPANY_ROOT / "logs"
SCHEMAS_DIR = COMPANY_ROOT / "schemas"
DAEMON_SNAPSHOTS_DIR = COMPANY_ROOT / "daemon_snapshots"
ANALYTICS_DIR = COMPANY_ROOT / "analytics"
ARCHIVE_DIR = COMPANY_ROOT / "archive"
SOCIAL_DIR = COMPANY_ROOT / "social"
TEMPLATES_DIR = COMPANY_ROOT / "templates"
WORKSHOPS_DIR = COMPANY_ROOT / "workshops"
BRIEFS_DIR = COMPANY_ROOT / "briefs"
JOBS_DIR = COMPANY_ROOT / "jobs"
SCRIPTS_DIR = COMPANY_ROOT / "scripts"

# === Deliverable directories ===
BUSINESS_DIR = COMPANY_ROOT / "business"
SALES_DIR = COMPANY_ROOT / "sales"
RESEARCH_DIR = COMPANY_ROOT / "research"
KNOWLEDGE_DIR = COMPANY_ROOT / "knowledge"
REPORTS_DIR = COMPANY_ROOT / "reports"
EMPLOYEE_IDEAS_DIR = COMPANY_ROOT / "employee-ideas"


# === Backward compatibility: path migration helper ===
def ensure_dirs_exist():
    """Create new directory structure if it doesn't exist."""
    for d in [CONFIG_DIR, STATE_DIR, RUNTIME_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# === Legacy path mapping (old -> new) ===
MIGRATION_MAP = {
    # Config files
    "config.json": CONFIG_JSON,
    "loop_config.json": LOOP_CONFIG_JSON,
    "manifest.json": MANIFEST_JSON,
    # State files
    "adaptive_scheduler_state.json": ADAPTIVE_SCHEDULER_STATE,
    "approval_history.json": APPROVAL_HISTORY,
    "auto_merge_skip_tracker.json": AUTO_MERGE_SKIP_TRACKER,
    "autonomy_validation.json": AUTONOMY_VALIDATION,
    "circuit_breaker.json": CIRCUIT_BREAKER,
    "daemon_metrics.json": DAEMON_METRICS,
    "document_approvals.json": DOCUMENT_APPROVALS,
    "state/efficiency_data.json": EFFICIENCY_DATA,
    "employee_ideas.json": EMPLOYEE_IDEAS_JSON,
    "state/feedback_state.json": FEEDBACK_STATE,
    "improvement_cycles.json": IMPROVEMENT_CYCLES,
    "learning_outcomes.json": LEARNING_OUTCOMES,
    "loop_monitor.json": LOOP_MONITOR,
    "new_proposals_csl.json": NEW_PROPOSALS_CSL,
    "new_proposals_employees.json": NEW_PROPOSALS_EMPLOYEES,
    "state/orchestrator_metrics.json": ORCHESTRATOR_METRICS,
    "pending_approvals.json": PENDING_APPROVALS,
    "planning_approvals.json": PLANNING_APPROVALS,
    "reorg_state.json": REORG_STATE,
    "roadmap_state.json": ROADMAP_STATE,
    "routing_state.json": ROUTING_STATE,
    "session_state.json": SESSION_STATE,
    "strategic_state.json": STRATEGIC_STATE,
    "validation_tracker.json": VALIDATION_TRACKER,
    "venture_state.json": VENTURE_STATE,
    "work_items.json": WORK_ITEMS,
    "state/work_queue.json": WORK_QUEUE,
    # Runtime files
    "daemon.pid": DAEMON_PID,
    "daemon.heartbeat": DAEMON_HEARTBEAT,
    "dashboard.pid": DASHBOARD_PID,
    "queue.lock": QUEUE_LOCK,
}


if __name__ == "__main__":
    print(f"Company root: {COMPANY_ROOT}")
    print(f"Config dir: {CONFIG_DIR}")
    print(f"State dir: {STATE_DIR}")
    print(f"Runtime dir: {RUNTIME_DIR}")
