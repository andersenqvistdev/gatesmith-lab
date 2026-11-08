#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Gatesmith Daemon — persistent daemon process for continuous autonomous operation.

Phase P12 implementation following ADR-002: Daemon Mode Architecture.

This daemon provides:
1. Persistent process with double-fork daemonization (Unix)
2. Signal handling for graceful shutdown, reload, and status
3. Heartbeat file for health monitoring
4. PID file management with stale detection
5. Integration with operation_loop.py and loop_monitor.py

Usage:
    # Start daemon (daemonize by default)
    python forge_daemon.py start

    # Start in foreground (for debugging/containers)
    python forge_daemon.py start --foreground
    python forge_daemon.py run

    # Stop daemon gracefully
    python forge_daemon.py stop

    # Restart daemon
    python forge_daemon.py restart

    # Check daemon status
    python forge_daemon.py status

Exit codes:
    0 = Success
    1 = Error
    2 = Daemon already running (start) or not running (stop)

Signals:
    SIGTERM -> Graceful shutdown
    SIGHUP  -> Reload configuration
    SIGUSR1 -> Write status to log
"""

from __future__ import annotations

import argparse
import atexit
import fnmatch
import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Daemon uptime/restart metrics tracker (no external dependencies — import directly)
try:
    from daemon_metrics import DaemonMetricsTracker as _DaemonMetricsTracker
except ImportError:
    _DaemonMetricsTracker = None  # type: ignore[assignment,misc]

# -----------------------------------------------------------------------------
# Module-level state for signal handling
# -----------------------------------------------------------------------------

_shutdown_requested: bool = False
_reload_requested: bool = False
_status_requested: bool = False

# Module-level metrics tracker — set by start_daemon(), read by update_heartbeat()
_g_metrics_tracker: Any = None
_g_session_id: str | None = None

# Logger instance (configured in setup_logging)
logger: logging.Logger = logging.getLogger("forge_daemon")


# -----------------------------------------------------------------------------
# Lazy imports for sibling modules
# -----------------------------------------------------------------------------

_imports_ready = False
operation_loop = None
loop_monitor = None
company_resolver = None
initiative_engine = None
project_orchestrator = None
employee_pool = None
employee_initiative = None
employee_ideation = None
manager_review = None
work_allocator = None
autonomy_metrics = None
escalation = None
strategic_planner = None
goal_autofill = None
executive_loop = None
document_approvals = None
roadmap_scheduler = None
pr_output_manager = None
# P20: GSD/BMAD planning modules
task_planner = None
plan_checker = None
# P25: Full Autonomous Operation modules
adaptive_scheduler_mod = None
session_continuity_mod = None
approval_learner_mod = None
goal_scheduler_mod = None
budget_governor_mod = None
learning_loop_mod = None
# P28: Central Orchestrator module
orchestrator_mod = None
# WS-105: Autonomous failure recovery
failure_recovery_mod = None
# P28.2: Orchestrator metrics for routing decisions
orchestrator_metrics_mod = None
# P28.3: Unified state management
unified_state_mod = None
# P43: Cron scheduler module
cron_scheduler_mod = None
# Task result persistence
task_result_writer = None
# Approved-idea auto-conversion
idea_to_task_converter_mod = None


# --- Discovery thread state (WS-121) -----------------------------------------
#
# The discovery thread calls Claude inline, which freezes in AGENT mode (no
# --print), so it was disabled with a bare `return` at the top of its worker.
# That left ten subsystems dormant while startup still logged "Discovery thread
# started (interval=1800s)" -- a healthy-looking signal for machinery that never
# ran. The executive loop last executed 2026-04-06 and nothing said so.
#
# Naming the state makes the disable a single switch instead of an unreachable
# statement, and lets both log lines tell the truth. Flip to True only once the
# AGENT-mode freeze is fixed; everything below the worker's `return` is what has
# to work again at that point.
DISCOVERY_ENABLED = False
DISCOVERY_DISABLED_REASON = "WS-121: inline Claude calls freeze in AGENT mode"
DISCOVERY_DORMANT_SUBSYSTEMS = (
    "proactive scan",
    "strategic planning",
    "weekly planning",
    "daily planning",
    "executive loop",
    "self-improvement",
    "feedback monitor",
    "escalation cleanup",
    "artifact generation",
    "vision auto-refresh",
)


def _is_daemon_task(task: dict) -> bool:
    """Check if a task is assigned to any daemon process.

    A task is considered a daemon task if either assigned_to or claimed_by
    starts with 'daemon-'.

    Args:
        task: Task dictionary

    Returns:
        True if the task is assigned to a daemon
    """
    assigned_to = task.get("assigned_to") or ""
    claimed_by = task.get("claimed_by") or ""
    return str(assigned_to).startswith("daemon-") or str(claimed_by).startswith(
        "daemon-"
    )


def _is_current_daemon_task(task: dict, daemon_pid: int | str) -> bool:
    """Check if a task belongs to this specific daemon instance.

    Args:
        task: Task dictionary
        daemon_pid: This daemon's PID (int) or full agent ID (str like 'daemon-12345')

    Returns:
        True if the task is assigned to this specific daemon
    """
    # Handle both int PID and string agent ID
    if isinstance(daemon_pid, int):
        daemon_agent_id = f"daemon-{daemon_pid}"
    else:
        daemon_agent_id = str(daemon_pid)

    assigned_to = task.get("assigned_to") or ""
    claimed_by = task.get("claimed_by") or ""
    return assigned_to == daemon_agent_id or claimed_by == daemon_agent_id


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global \
        _imports_ready, \
        operation_loop, \
        loop_monitor, \
        company_resolver, \
        initiative_engine, \
        project_orchestrator, \
        employee_pool, \
        employee_initiative, \
        employee_ideation, \
        manager_review, \
        work_allocator, \
        autonomy_metrics, \
        escalation, \
        strategic_planner, \
        goal_autofill, \
        executive_loop, \
        document_approvals, \
        roadmap_scheduler, \
        pr_output_manager, \
        task_planner, \
        plan_checker, \
        adaptive_scheduler_mod, \
        session_continuity_mod, \
        approval_learner_mod, \
        goal_scheduler_mod, \
        budget_governor_mod, \
        learning_loop_mod, \
        orchestrator_mod, \
        orchestrator_metrics_mod, \
        unified_state_mod, \
        cron_scheduler_mod, \
        task_result_writer, \
        failure_recovery_mod, \
        idea_to_task_converter_mod

    if _imports_ready:
        return

    # Add module directory to path for direct script execution
    module_dir = Path(__file__).parent
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    try:
        from . import autonomy_metrics as am_mod
        from . import company_resolver as cr
        from . import escalation as esc_mod
        from . import goal_autofill as gafill
        from . import goal_scheduler as gsched
        from . import loop_monitor as lm
        from . import operation_loop as ol
        from . import optional_modules as opt
        from . import plan_checker as pc
        from . import pr_output_manager as pom
        from . import roadmap_scheduler as rs
        from . import task_planner as tp
        from . import unified_state as ustate
        from . import work_allocator as wa
    except ImportError:
        import autonomy_metrics as am_mod  # type: ignore[no-redef]
        import company_resolver as cr  # type: ignore[no-redef]
        import escalation as esc_mod  # type: ignore[no-redef]
        import goal_autofill as gafill  # type: ignore[no-redef]
        import goal_scheduler as gsched  # type: ignore[no-redef]
        import loop_monitor as lm  # type: ignore[no-redef]
        import operation_loop as ol  # type: ignore[no-redef]
        import optional_modules as opt  # type: ignore[no-redef]
        import plan_checker as pc  # type: ignore[no-redef]
        import pr_output_manager as pom  # type: ignore[no-redef]
        import roadmap_scheduler as rs  # type: ignore[no-redef]
        import task_planner as tp  # type: ignore[no-redef]
        import unified_state as ustate  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]

    # Task result persistence (optional)
    try:
        from . import task_result_writer as trw
    except ImportError:
        try:
            import task_result_writer as trw  # type: ignore[no-redef]
        except ImportError:
            trw = None  # type: ignore[assignment]

    # WS-105: Autonomous failure recovery (optional)
    try:
        from . import failure_recovery as frecov
    except ImportError:
        try:
            import failure_recovery as frecov  # type: ignore[no-redef]
        except ImportError:
            frecov = None  # type: ignore[assignment]

    operation_loop = ol
    loop_monitor = lm
    company_resolver = cr
    work_allocator = wa
    autonomy_metrics = am_mod
    escalation = esc_mod
    roadmap_scheduler = rs
    pr_output_manager = pom
    task_planner = tp
    plan_checker = pc
    goal_scheduler_mod = gsched
    goal_autofill = gafill
    unified_state_mod = ustate
    task_result_writer = trw
    failure_recovery_mod = frecov

    # Not in the public export (K2a): the ADR-003 discovery thread and the
    # experimental company layer read as None when absent.
    def _optional(name):
        return opt.import_optional(name, __package__)

    initiative_engine = _optional("initiative_engine")
    employee_initiative = _optional("employee_initiative")
    employee_ideation = _optional("employee_ideation")
    executive_loop = _optional("executive_loop")
    strategic_planner = _optional("strategic_planner")
    project_orchestrator = _optional("project_orchestrator")
    employee_pool = _optional("employee_pool")
    manager_review = _optional("manager_review")
    document_approvals = _optional("document_approvals")
    adaptive_scheduler_mod = _optional("adaptive_scheduler")
    session_continuity_mod = _optional("session_continuity")
    approval_learner_mod = _optional("approval_learner")
    budget_governor_mod = _optional("budget_governor")
    learning_loop_mod = _optional("learning_loop")
    orchestrator_mod = _optional("orchestrator")
    orchestrator_metrics_mod = _optional("orchestrator_metrics")
    cron_scheduler_mod = _optional("cron_scheduler")
    idea_to_task_converter_mod = _optional("idea_to_task_converter")

    _imports_ready = True


# -----------------------------------------------------------------------------
# Configuration Dataclass
# -----------------------------------------------------------------------------


def _default_review_paths() -> list:
    """Shared reviewPaths seed, lazily imported (daemon lazy-import style).

    Falls back to a minimal hard floor if the module is unavailable — the
    hold tier must never silently resolve to "no holds".
    """
    try:
        from auto_merge_paths import DEFAULT_REVIEW_PATHS

        return list(DEFAULT_REVIEW_PATHS)
    except ImportError:
        return [
            ".claude/hooks/company/*audit*",
            ".claude/hooks/company/*judge*",
            ".claude/hooks/company/pr_output_manager.py",
            ".claude/hooks/company/forge_daemon.py",
            ".github/workflows/*.yml",
        ]


def _hold_label_names() -> set:
    """Labels that hold a PR for human review; no merge lever may pass them.

    needs-manual-review comes from the pre-merge deliverable gate,
    awaiting-operator-review from the reviewPaths hold and the operator's
    manual brake. Lazily imports the shared seed (daemon lazy-import style)
    with a literal fallback — a broken import must never fail open into
    "no hold".
    """
    try:
        from auto_merge_paths import REVIEW_HOLD_LABEL
    except ImportError:
        REVIEW_HOLD_LABEL = "awaiting-operator-review"
    # CHALLENGER-STAGE: any challenger:* label but challenger:passed holds too.
    try:
        from challenger_stage import HOLD_LABELS as _challenger_holds
    except ImportError:
        _challenger_holds = {
            "challenger:pending",
            "challenger:incomplete",
            "challenger:needs-human",
        }
    return {
        "needs-manual-review",
        REVIEW_HOLD_LABEL,
        _pending_label(),
        *_challenger_holds,
    }


def _verdict_hold():
    """Lazy import of the pending-verdict hold module."""
    hooks_dir = str(Path(__file__).resolve().parent)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import verdict_hold

    return verdict_hold


def _pending_label() -> str:
    """verdict-pending: the head has no positive verdict yet (literal fallback)."""
    try:
        return _verdict_hold().PENDING_LABEL
    except Exception:
        return "verdict-pending"


def _fill_auto_merge_from_template(data: dict, config_path: Path) -> None:
    """Give a root config that does not declare ``autonomy.autoMerge`` the
    install template's block, the rule every merge lever shares
    (governing_config). A fresh install's root (``/company-init`` writes one
    holding ``modelProfile``) otherwise left the daemon on its permissive
    defaults. An untrusted template, a non-object ``autonomy`` or a null
    ``autoMerge`` refuses: ``enabled: false``. Only for a project-root
    ``gatesmith.json``; other paths are taken as given."""
    if config_path.name != "gatesmith.json" or config_path.parent.name == ".claude":
        return
    autonomy = data.get("autonomy")
    if "autonomy" in data and not isinstance(autonomy, dict):
        # null, [] and the like: merge_gate refuses these, so the daemon's own
        # levers do too (challenger on #787).
        logger.warning(
            f"{config_path}: 'autonomy' is {autonomy!r}, not an object — "
            f"auto-merge is off until it is fixed"
        )
        data["autonomy"] = {"autoMerge": {"enabled": False}}
        return
    if isinstance(autonomy, dict) and "autoMerge" in autonomy:
        if autonomy["autoMerge"] is None:
            autonomy["autoMerge"] = {"enabled": False}
        return
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import governing_config

        got = governing_config.resolve("autonomy.autoMerge", config_path.parent)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning(f"config resolver unavailable ({exc}); auto-merge off")
        got = None
    if got is None or got.error:
        block = {"enabled": False}
    elif got.source == governing_config.TEMPLATE_CONFIG:
        block = got.value if isinstance(got.value, dict) else {"enabled": False}
    else:
        return
    data.setdefault("autonomy", {})["autoMerge"] = block


def _warn_merge_policy_shape(data: dict, config_path: Path) -> None:
    """Normalize a null ``autonomy`` and warn when ``autoMerge`` is absent.

    An absent ``autonomy.autoMerge`` keeps the pre-2026-09-26 defaults —
    FULL auto-merge — so a misspelt or misplaced block silently means full
    autonomy. Warn once per load when other autonomy keys exist but
    ``autoMerge`` does not (or is not an object). ``"autonomy": null`` used
    to raise AttributeError here; it is treated as absent, with a warning.
    """
    autonomy = data.get("autonomy")
    if "autonomy" in data and not isinstance(autonomy, dict):
        logger.warning(
            f"{config_path}: 'autonomy' is {autonomy!r}, not an object — "
            f"treating it as absent (auto-merge defaults to ON)"
        )
        data["autonomy"] = {}
        return
    if not isinstance(autonomy, dict) or not autonomy:
        return
    am = autonomy.get("autoMerge")
    if not isinstance(am, dict):
        what = "missing" if am is None else f"{type(am).__name__}, not an object"
        logger.warning(
            f"{config_path}: autonomy.autoMerge is {what} — the daemon will "
            f"AUTO-MERGE its own PRs (enabled/allowMergeToMain default true). "
            f"Add an explicit autonomy.autoMerge block; a misspelt key is "
            f"read as full autonomy."
        )


def _config_bool(section: Any, key: str, *, default: bool, permissive: bool) -> bool:
    """Read a merge-permission bool from a config section.

    Absent key -> ``default``. A real bool is honoured. Anything else (a
    string ``"false"``, a number, null) is malformed and resolves to the value
    that does NOT permit merging (``not permissive``) — ``bool("false")`` is
    True, and a typo must never widen what the daemon may merge.
    """
    if not isinstance(section, dict) or key not in section:
        return default
    value = section[key]
    if isinstance(value, bool):
        return value
    logger.warning(
        f"autonomy.autoMerge.{key}={value!r} is not a boolean — treating it as "
        f"{not permissive} (fail closed)"
    )
    return not permissive


# Per-PR rate limit for the "merge not allowed by config" log line: the
# auto-merge loop runs every few minutes and a held PR can sit for days.
_MERGE_CONFIG_SKIP_LOG_INTERVAL_S = 6 * 3600
_merge_config_skip_logged: dict[tuple[int, str], float] = {}


def _log_merge_config_skip(pr_number: int, reason: str) -> None:
    """Log one INFO line per (PR, reason) per interval; DEBUG otherwise."""
    key = (int(pr_number), reason)
    now = time.monotonic()
    last = _merge_config_skip_logged.get(key)
    if last is None or now - last >= _MERGE_CONFIG_SKIP_LOG_INTERVAL_S:
        _merge_config_skip_logged[key] = now
        logger.info(
            f"[Schedule] PR #{pr_number} skipped — merge not allowed by config: "
            f"{reason}"
        )
    else:
        logger.debug(f"PR #{pr_number} skipped — merge not allowed by config: {reason}")


# Author associations whose approval counts. GitHub reports the reviewer's
# relationship to the repo; a drive-by approval from a non-collaborator
# (CONTRIBUTOR, NONE, ...) is not an operator decision.
_APPROVER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _is_human_approver(review: dict) -> bool:
    """A collaborator-level, non-bot author (bot test shared with pr_output_manager)."""
    if review.get("authorAssociation") not in _APPROVER_ASSOCIATIONS:
        return False
    author = review.get("author")
    if not isinstance(author, dict) or not author.get("login"):
        return False
    try:
        from pr_output_manager import _is_human_reviewer
    except Exception:
        login = str(author.get("login", "")).lower()
        return not (login.endswith("[bot]") or "bot" in login.split("-"))
    try:
        return bool(_is_human_reviewer(author))
    except Exception:
        return False


def _pr_approved_head(pr_number: int) -> str | None:
    """The head SHA a standing human approval covers, or None.

    The approval must be bound to the PR's CURRENT head: a review whose
    ``commit.oid`` is an older head approved code that is no longer what
    would merge (the daemon's own CI healer pushes after the gates). So both
    branches — ``reviewDecision == APPROVED`` (branch protection requires
    reviews) and the no-protection fallback — require at least one APPROVED
    ``latestReviews`` entry on ``headRefOid`` from a collaborator-level human,
    and no CHANGES_REQUESTED. REVIEW_REQUIRED / CHANGES_REQUESTED decisions
    refuse. GitHub never lets a PR's author approve their own PR, so the
    daemon's account cannot self-approve — with a single GitHub account this
    returns None forever. Any gh error or unparseable answer -> None.
    """
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "author,headRefOid,reviewDecision,latestReviews",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    head = data.get("headRefOid")
    if not isinstance(head, str) or not head:
        return None
    pr_author = data.get("author")
    pr_login = pr_author.get("login") if isinstance(pr_author, dict) else None
    if not isinstance(pr_login, str) or not pr_login:
        return None
    decision = data.get("reviewDecision") or ""
    if decision in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        return None
    reviews = data.get("latestReviews") or []
    if not isinstance(reviews, list):
        return None
    reviews = [r for r in reviews if isinstance(r, dict)]
    if any(r.get("state") == "CHANGES_REQUESTED" for r in reviews):
        return None
    for r in reviews:
        commit = r.get("commit")
        oid = commit.get("oid") if isinstance(commit, dict) else None
        author = r.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if isinstance(login, str) and login.casefold() == pr_login.casefold():
            # GitHub forbids self-approval; refuse it here too (logins are
            # case-insensitive on GitHub).
            continue
        if r.get("state") == "APPROVED" and oid == head and _is_human_approver(r):
            return head
    return None


def _pr_head_sha(pr_number: int) -> str | None:
    """The PR's current head SHA (for --match-head-commit), or None on error."""
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    sha = (res.stdout or "").strip() if res.returncode == 0 else ""
    return sha or None


def _pr_has_approving_review(pr_number: int) -> bool:
    """Compatibility wrapper: True when a head-bound human approval stands."""
    return _pr_approved_head(pr_number) is not None


def _merge_policy_check(
    config: "DaemonConfig", pr_number: int
) -> tuple[str | None, str | None]:
    """(reason the config forbids merging this PR or None, approved head SHA).

    Every daemon merge path (full-trust direct merge, the --admin
    pre-existing-failure bypass, the legacy --auto arm) passes through this
    one gate before any ``gh pr merge``. ``allowMergeToMain=false`` refuses
    every merge the daemon would make: its merge targets are the configured
    integration branches (allowedBaseBranches, default main/master), which is
    what the key means. ``requireApproval=true`` refuses until a different
    GitHub account (collaborator, not a bot) has approved the PR's current
    head — the daemon then merges exactly that head through the same gates.
    The approved SHA is returned so the merge can be refused if the head
    moves between this check and the merge.
    """
    if not config.auto_merge_allow_merge_to_main:
        return "allowMergeToMain=false", None
    if config.auto_merge_require_approval:
        approved = _pr_approved_head(pr_number)
        if approved is None:
            return (
                "requireApproval=true and the PR's current head has no approval "
                "from another collaborator",
                None,
            )
        return None, approved
    return None, None


def _merge_blocked_by_config(config: "DaemonConfig", pr_number: int) -> str | None:
    """Reason the operator's config forbids the daemon merging this PR, or None."""
    return _merge_policy_check(config, pr_number)[0]


def _native_auto_merge_permitted(config: "DaemonConfig") -> bool:
    """Whether an ARMED GitHub native auto-merge may stay armed on a daemon PR.

    Native ``--auto`` merges whatever head is green, with no approval binding,
    so it is only acceptable under full autonomy: enabled, allowMergeToMain,
    and no requireApproval (same rule as pr_output_manager's arm gate).
    """
    return (
        config.auto_merge_enabled
        and config.auto_merge_allow_merge_to_main
        and not config.auto_merge_require_approval
        and _merge_gate_policy_can_merge()
    )


def _merge_gate_policy_can_merge() -> bool:
    """Whether merge_gate's policy also allows merging without a human.

    The daemon's own loader reads only the root gatesmith.json; on a fresh
    install (template only) or a root holding just modelProfile it falls back
    to "allowed". merge_gate reads the governing file, so an arm that skips
    may_merge (revert PRs, the disarm sweep) must agree with it too
    (challenger on #778, F2). Fails closed."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import merge_gate

        return bool(merge_gate.load_policy(project_root).can_merge)
    except Exception:  # noqa: BLE001 — fail closed
        return False


_POLICY_DISARM_INTERVAL_S = 15 * 60
# gh pr list defaults to 30 rows; gh paginates internally up to --limit.
_PR_LIST_LIMIT = 500
_policy_disarm_last: dict[str, float] = {}


def _run_policy_disarm_sweep(config: "DaemonConfig", *, force: bool = False) -> int:
    """Disarm native auto-merge on daemon PRs the merge policy forbids.

    Runs from the daemon loop even when the merge loop is off
    (``autoMerge.enabled=false``) — otherwise a PR armed before the operator
    turned merging off would still merge the moment its checks go green.
    Rate-limited to one ``gh pr list`` per interval. Returns PRs disarmed.
    """
    if _native_auto_merge_permitted(config):
        return 0
    now = time.monotonic()
    last = _policy_disarm_last.get("t")
    if not force and last is not None and now - last < _POLICY_DISARM_INTERVAL_S:
        return 0
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--limit",
                str(_PR_LIST_LIMIT),
                "--json",
                "number,headRefName,autoMergeRequest",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0:
            logger.warning(
                f"Policy disarm sweep: gh pr list failed "
                f"(rc={res.returncode}): {(res.stderr or '').strip()[:200]}"
            )
            return 0
        prs = json.loads(res.stdout or "[]")
    except (subprocess.TimeoutExpired, OSError, ValueError) as err:
        logger.warning(f"Policy disarm sweep: gh pr list failed: {err}")
        return 0
    if not isinstance(prs, list):
        logger.warning("Policy disarm sweep: gh pr list returned no list")
        return 0
    # Only a successful listing counts as a sweep for the rate limit — a
    # failed one is retried next cycle.
    _policy_disarm_last["t"] = now
    disarmed = 0
    for pr in prs:
        if not isinstance(pr, dict) or not pr.get("autoMergeRequest"):
            continue
        branch = str(pr.get("headRefName", ""))
        if not branch.startswith(("daemon/", "forge/")):
            continue
        number = pr.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            logger.warning(
                f"Policy disarm sweep: PR entry on {branch!r} has no usable "
                f"number ({number!r}) — skipped"
            )
            continue
        try:
            r = subprocess.run(
                ["gh", "pr", "merge", str(number), "--disable-auto"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as err:
            logger.warning(f"PR #{number}: failed to disarm native auto-merge: {err}")
            continue
        if r.returncode != 0:
            logger.warning(
                f"PR #{number}: failed to disarm native auto-merge "
                f"(rc={r.returncode}): {(r.stderr or '').strip()[:200]}"
            )
        else:
            disarmed += 1
            logger.info(
                f"[Schedule] PR #{number}: disarmed native auto-merge — merge "
                f"policy forbids it (enabled={config.auto_merge_enabled}, "
                f"allowMergeToMain={config.auto_merge_allow_merge_to_main}, "
                f"requireApproval={config.auto_merge_require_approval})"
            )
    return disarmed


# Increment 2a (GAP-7 acceptance ledger, task-20260905052354-32fd19): checks
# that the pre-existing-failure bypass must never wave through via admin
# merge, even once the check stops being advisory. `acceptance` (ci.yml) is
# the judge of the change itself — a PR admin-merging past it as
# "pre-existing" would defeat the point of the ledger.
DEFAULT_BYPASS_EXCLUDED_CHECKS: tuple = ("acceptance",)


def _positive_number(value: Any, default: float) -> float:
    """``value`` when it is a positive int/float (never a bool), else default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value if value > 0 else default


# Keys already warned about as non-boolean, so a SIGHUP reload does not repeat.
_acceptance_config_warned: set[str] = set()


def _apply_acceptance_config(config: "DaemonConfig", block: Any) -> None:
    """LEDGER-5c: read the top-level ``acceptance`` block into ``config``.

    Fail-closed toward OFF: only a literal JSON ``true`` turns the step (or the
    oracle) on — a string, a number or an absent key leaves the code default
    ``False``. Malformed numbers fall back to the code defaults.
    """
    if not isinstance(block, dict):
        return
    oracle_block = block.get("oracle")
    for key, holder, sub in (
        ("acceptance.enabled", block, "enabled"),
        ("acceptance.oracle.enabled", oracle_block, "enabled"),
    ):
        if (
            isinstance(holder, dict)
            and sub in holder
            and not isinstance(holder[sub], bool)
            and key not in _acceptance_config_warned
        ):
            _acceptance_config_warned.add(key)
            logger.warning(
                f"[Acceptance] {key} is {holder[sub]!r}, not a JSON boolean — "
                "treated as false (write true without quotes to turn it on)"
            )
    config.acceptance_enabled = block.get("enabled") is True
    config.acceptance_interval_hours = float(
        _positive_number(block.get("intervalHours"), config.acceptance_interval_hours)
    )
    config.acceptance_step_timeout_seconds = int(
        _positive_number(
            block.get("stepTimeoutSeconds"), config.acceptance_step_timeout_seconds
        )
    )
    config.acceptance_oracle_timeout_seconds = int(
        _positive_number(
            block.get("oracleTimeoutSeconds"), config.acceptance_oracle_timeout_seconds
        )
    )
    artifact = block.get("canaryArtifact")
    if isinstance(artifact, str):
        config.acceptance_canary_artifact = artifact
    oracle = block.get("oracle")
    config.acceptance_oracle_enabled = (
        isinstance(oracle, dict) and oracle.get("enabled") is True
    )


@dataclass
class DaemonConfig:
    """Configuration for the Gatesmith daemon.

    Attributes:
        pid_file: Path to PID file (default: .company/daemon.pid)
        heartbeat_file: Path to heartbeat file (default: .company/daemon.heartbeat)
        log_file: Path to log file (default: .company/logs/daemon.log)
        launchagent_dir: Directory holding the LaunchAgent plist (default:
            ~/Library/LaunchAgents). Overridable so tests can point it at a
            fixture dir and never unload/load the operator's real daemon.
        log_level: Logging level (default: INFO)
        poll_interval_seconds: Seconds between task polls (default: 30)
        max_tasks_per_hour: Rate limit for task execution (default: 20)
        idle_timeout_seconds: Seconds of idle before auto-shutdown (default: 1800)
        daemonize: Whether to fork into background (default: True)
        proactive_enabled: Whether proactive scanning is enabled (default: True)
        proactive_scan_interval_minutes: Minutes between proactive scans (default: 60)
        cross_project_enabled: Whether cross-project routing is enabled (default: True)
        cross_project_auto_routing: Whether auto-routing is enabled (default: True)
        cross_project_confidence_threshold: Min confidence for auto-routing (default: 0.7)
        cross_project_require_approval_for: Task types requiring approval (default: ["high_risk", "complex"])
        rebalance_check_interval: Iterations between rebalance checks (default: 10)
    """

    pid_file: Path = field(default_factory=lambda: Path(".company/runtime/daemon.pid"))
    heartbeat_file: Path = field(
        default_factory=lambda: Path(".company/runtime/daemon.heartbeat")
    )
    log_file: Path = field(default_factory=lambda: Path(".company/logs/daemon.log"))
    # Where uptime windows / restart events go. Threaded into the metrics
    # tracker so a test's DaemonConfig points at tmp_path: until 2026-08-30
    # start_daemon(tmp_config) wrote the tracker's cwd-relative default and
    # the real daemon_metrics.json held 100/100 zero-second pytest sessions —
    # G7's primary uptime source saw 2.5 uptime-days from a daemon running
    # since March.
    metrics_file: Path = field(
        default_factory=lambda: Path(".company/state/daemon_metrics.json")
    )
    # Absolute (home-anchored) so it is NOT the cwd-relative antipattern the other
    # path fields carry; tests override it to a fixture dir for hermeticity.
    launchagent_dir: Path = field(
        default_factory=lambda: Path.home() / "Library" / "LaunchAgents"
    )
    log_level: str = "INFO"
    poll_interval_seconds: int = 30
    discovery_loop_interval_seconds: int = (
        150  # WI-004: 2.5-min cadence for discovery thread (was 300)
    )
    max_tasks_per_hour: int = 20
    idle_timeout_seconds: int = 0  # 0 = disabled; daemon runs until explicitly stopped
    daemonize: bool = True
    proactive_enabled: bool = True
    proactive_scan_interval_minutes: int = 60
    # Cross-project orchestration settings (P14)
    cross_project_enabled: bool = True
    cross_project_auto_routing: bool = True
    cross_project_confidence_threshold: float = 0.7
    cross_project_require_approval_for: list = field(
        default_factory=lambda: ["high_risk", "complex"]
    )
    rebalance_check_interval: int = 10
    # Strategic planning settings (P15)
    strategic_planning_enabled: bool = True
    # Silence sentinel (plan item 4, 2026-07-26): alert on silent failure
    # (dead scout, hours-empty queue, restart storms, persistent brief skips)
    silence_sentinel_enabled: bool = True
    silence_sentinel_interval_minutes: int = 30
    # LEDGER-5c (GAP-7): the scheduled acceptance step. OFF by code default;
    # the operator turns it on with gatesmith.json `acceptance.enabled`.
    # Each sub-step runs as a subprocess in a background thread, bounded by
    # its timeout, so the step can never freeze the loop (ADR-003).
    acceptance_enabled: bool = False
    acceptance_interval_hours: float = 24.0
    acceptance_step_timeout_seconds: int = 900
    # `acceptance.oracle.enabled` — the one sub-step that calls a model (via
    # acceptance_probe.py -> agent_providers); off unless explicitly true.
    acceptance_oracle_enabled: bool = False
    acceptance_oracle_timeout_seconds: int = 3600
    # The canary needs a Python artifact to mutate; empty -> canary skipped.
    acceptance_canary_artifact: str = ""
    # Project root the ledger runs against. None -> the repo root derived from
    # this file (NOT cwd); tests point it at tmp_path.
    acceptance_project_dir: Path | None = None
    strategic_planning_interval_hours: int = 24  # Daily by default
    # P29: Strategic planning cycles (weekly/daily)
    weekly_planning_interval_hours: int = 168  # Weekly (7 * 24 = 168 hours)
    daily_planning_interval_hours: int = 24  # Daily
    # Executive loop settings (P17)
    executive_loop_enabled: bool = True
    executive_loop_interval_hours: int = (
        12  # WS-018: Cost optimization (was 8h, Opus→Sonnet)
    )
    executive_loop_trigger_on_empty_queue: bool = True
    executive_loop_skip_if_queue_above: int = 5  # Skip if queue has > N pending tasks
    # P22 Fix 2: Stale task cleanup settings
    stale_cleanup_enabled: bool = True
    stale_cleanup_interval_hours: int = 6  # Every 6 hours
    stale_task_max_age_hours: float = 24.0  # Archive tasks older than 24h
    stuck_in_progress_max_hours: float = 2.0  # Kill zombie in_progress tasks stuck >2h
    # Autonomy queue reconcile: flip stale completions whose PR ended
    # CLOSED-unmerged so the autonomy proxy stops counting them as shipped.
    autonomy_reconcile_enabled: bool = True
    autonomy_reconcile_interval_hours: int = 6  # Every 6 hours
    autonomy_reconcile_window_days: int = 30  # Bound gh calls to recent cohorts
    # P1-R2: fast pr_open-lane reconcile — a merged PR should flip its task to
    # completed within minutes, not the 6h closed-unmerged cadence. <=0 disables.
    pr_open_reconcile_interval_seconds: int = 300
    # P1-R4: stranded-worktree harvest — a completed worker whose capture made
    # no PR must not strand its committed branch. <=0 disables the sweep.
    stranded_harvest_interval_seconds: int = 600
    # Stale-escalation auto-archive: move escalation records older than N hours to
    # .company/archive/escalations/ so resolved/dead escalations stop dragging the
    # health score (the active-escalations factor).
    escalation_archive_enabled: bool = True
    escalation_archive_interval_hours: int = 6  # Every 6 hours
    escalation_archive_stale_hours: int = 48  # Archive records not updated in 48h
    # P22 Fix 4: Daemon supervision settings
    restart_on_crash: bool = True
    max_restarts: int = 3
    restart_delay_seconds: int = 60
    # P23: Document approval scan settings
    document_scan_enabled: bool = True
    document_scan_interval_minutes: int = 60  # Every hour
    # P26: Employee initiative settings
    employee_initiative_enabled: bool = True
    employee_initiative_interval_minutes: int = 15  # WI-004: Every 15 minutes (was 30)
    employee_initiative_max_per_cycle: int = 2  # Max employees to activate per cycle
    employee_initiative_max_proposals_per_hour: int = 3  # Per employee rate limit
    # Idle-backoff gate: stop fast initiative cadence during sustained empty-queue periods
    employee_initiative_idle_backoff_threshold: int = (
        3  # Idle cycles before slow cadence
    )
    employee_initiative_idle_cadence_minutes: int = (
        120  # Minutes between runs when idle (0=disable)
    )
    # P34: Employee creative ideation settings
    employee_ideation_enabled: bool = True
    employee_ideation_interval_minutes: int = 30  # WI-004: Every 30 minutes (was 60)
    employee_ideation_max_employees: int = 3  # Max employees per cycle
    employee_ideation_max_ideas_per_employee: int = 2  # Max ideas per employee
    # Auto-convert approved ideas to tasks without human CLI invocation (config: employeeIdeation.autoConvertApproved)
    employee_ideation_auto_convert_approved: bool = True
    # P35: Auto-merge settings
    auto_merge_enabled: bool = True
    auto_merge_interval_minutes: int = 5  # Check every 5 minutes
    auto_merge_require_tests: bool = True
    auto_merge_require_lint: bool = True
    auto_merge_method: str = "squash"  # squash, merge, rebase
    auto_merge_delete_branch: bool = True
    # autonomy.autoMerge.{allowMergeToMain, requireApproval}. The installer
    # writes allowMergeToMain=false + requireApproval=true and promises "safe
    # merge defaults"; until 2026-09-26 no daemon merge path read either key,
    # so a fresh install auto-merged (webapp PR #17, --admin). Defaults keep
    # the pre-fix behaviour for a config that omits the keys; a malformed
    # (non-bool) value fails closed — see _config_bool.
    auto_merge_allow_merge_to_main: bool = True
    auto_merge_require_approval: bool = False
    # WS-066-002: Full-trust security gates
    auto_merge_full_trust: bool = True  # Immediate merge after gate check (not --auto)
    # A PR with zero CI checks means the repo has no gate at all — refuse
    # auto-merge unless explicitly allowed (same key pr_output_manager uses).
    auto_merge_allow_gateless_repos: bool = False
    # A daemon PR merges only on a POSITIVE deliverable verdict for its exact
    # head commit. Mirrors autonomy.deliverableGate.enabled: with the gate off
    # nothing ever writes a verdict, so "missing" cannot be required — a
    # recorded negative verdict still refuses either way.
    auto_merge_require_deliverable_verdict: bool = True
    # Auto-merge only lands on these base branches; a fresh repo can end up
    # with a daemon/* branch as GitHub default, redirecting every PR there.
    auto_merge_allowed_base_branches: list = field(
        default_factory=lambda: ["main", "master"]
    )
    auto_merge_blocked_paths: list = field(
        default_factory=lambda: [
            "CLAUDE.md",
            "gatesmith.json",
            ".env",
            ".claude/settings.json",
            ".claude/hooks/*",
            ".github/workflows/*",
            ".company/org.json",
        ]
    )
    # PR 257 (2026-07-17): blockedPaths is a short exact/glob list of files that
    # must never auto-merge. reviewPaths is a broader glob hold tier for whole
    # gate/audit/steering *subsystems* — so new files matching the convention
    # (e.g. "*audit*") are covered without editing an exact list every time.
    # The default is the shared seed from auto_merge_paths (PR 260 review
    # blocker: an empty default made the hold tier inert whenever the config
    # key was absent — which is the normal state, since gatesmith.json is
    # humanProtected and the daemon must not write the seed there itself).
    auto_merge_review_paths: list = field(
        default_factory=lambda: _default_review_paths()
    )
    auto_merge_max_diff_lines: int = 500  # Max added+removed lines
    auto_merge_max_diff_files: int = 20  # Max files changed
    auto_merge_secrets_scan: bool = True  # Run secrets scan on diff
    auto_merge_audit_enabled: bool = True  # Persist audit log
    auto_merge_audit_max_entries: int = 200  # Rolling window
    # WS-066-003: Pre-existing CI failure bypass
    auto_merge_bypass_preexisting_failures: bool = (
        True  # Merge despite CI if failures are pre-existing
    )
    # Increment 2a: checks the bypass must never treat as pre-existing.
    auto_merge_bypass_excluded_checks: list = field(
        default_factory=lambda: list(DEFAULT_BYPASS_EXCLUDED_CHECKS)
    )
    auto_merge_admin_max_per_day: int = 3  # Max admin merges per day (rate limit)
    auto_merge_require_rollback_healthy: bool = (
        True  # Require rollback executor to be healthy
    )
    # WS-066-004: Self-healing CI
    ci_healer_enabled: bool = True  # Auto-fix common CI failures
    ci_healer_max_attempts_per_pr: int = 3  # Max heal attempts per PR
    ci_healer_max_heals_per_day: int = 10  # Daily rate limit
    # E8: CI-fail terminal path (path-D)
    ci_fail_terminal_enabled: bool = True
    ci_fail_terminal_cycles: int = 5
    ci_fail_max_requeues: int = 1
    # E8: Cross-regeneration build ceiling
    cross_regen_ceiling_enabled: bool = True
    cross_regen_max_builds: int = 5
    # P81: Stale PR cleanup
    stale_pr_max_age_hours: int = 24  # Auto-close stale PRs after this
    # P53: Scheduled reports settings
    scheduled_reports_enabled: bool = True
    scheduled_reports: list = field(default_factory=list)  # List of report configs
    # Scheduled tasks (sales/operations activities queued to employees)
    scheduled_tasks_enabled: bool = True
    scheduled_tasks: list = field(default_factory=list)  # List of task configs
    # Recurring product health tasks (test suite, docs freshness, dependency audit)
    recurring_tasks_enabled: bool = True
    recurring_tasks: list = field(default_factory=list)  # List of task configs
    # P26: Manager review settings
    manager_review_enabled: bool = True
    manager_review_interval_minutes: int = 15  # Every 15 minutes
    manager_auto_approve_enabled: bool = True  # Auto-approve low-risk proposals
    manager_review_timeout_hours: int = 24  # Auto-complete reviews after timeout
    # WS-016: Cascade scheduling settings
    cascade_enabled: bool = True  # Enable cascade model
    queue_low_threshold: int = 3  # Trigger initiative when queue < N
    # E7-ext: Ideation-first ordering — fill defers until ideation has had a turn
    fill_defer_to_ideation: bool = True
    employee_quiet_after_exec_minutes: int = (
        15  # WI-004: Wait after executive activity (was 30)
    )
    exec_wait_for_initiative_minutes: int = (
        8  # WI-004: Exec waits for initiative to fill queue (was 15)
    )
    # P27: Roadmap scheduling settings
    roadmap_scheduling_enabled: bool = True
    roadmap_scan_interval_minutes: int = 15  # Scan ROADMAP.md every N minutes
    roadmap_auto_schedule_waves: bool = True  # Auto-schedule tasks from waves
    roadmap_respect_dependencies: bool = True  # Respect task dependencies
    roadmap_max_tasks_per_scan: int = 10  # Max tasks to schedule per scan
    # Signal-driven backlog generation (2026-08-13). The LAST-RESORT source:
    # goal autofill and roadmap intake are both finite and this repo exhausted
    # both at once (goals met/excluded, intake 17 of 17 handled), leaving a
    # healthy daemon idling on an empty queue. Runs only after those two have
    # had their turn and left the queue below threshold, so declared work
    # always outranks derived work. Long cooldown because the underlying
    # signals (coverage.json) only refresh nightly -- a short one would churn
    # the same measurement into duplicate briefs.
    backlog_generation_enabled: bool = True
    backlog_max_tasks_per_scan: int = 3
    backlog_cooldown_minutes: int = 60
    # P20: GSD/BMAD planning integration settings
    planning_enabled: bool = False  # DISABLED: orchestrator overrides complexity, causing all tasks to be planned. Direct execution is more reliable.
    planning_complexity_threshold: str = (
        "epic"  # Only epic tasks get planned (if re-enabled)
    )
    planning_auto_execute_waves: bool = True  # Auto-execute planned waves
    planning_max_revision_attempts: int = 1  # Max re-plan attempts on REVISE verdict
    planning_pass_threshold: int = 18  # Min score to pass plan check (0-25 scale)
    # P25: Full Autonomous Operation settings
    p25_enabled: bool = True  # Master switch for P25 features
    p25_adaptive_scheduling_enabled: bool = True  # Adaptive poll intervals
    p25_session_continuity_enabled: bool = True  # Session snapshots
    p25_approval_learning_enabled: bool = True  # Approval pattern learning
    p25_goal_scheduling_enabled: bool = True  # Goal-driven queue reordering
    p25_budget_governor_enabled: bool = True  # Budget-aware throttling
    p25_learning_loop_enabled: bool = True  # Outcome-based learning
    p25_snapshot_interval_seconds: int = 300  # Snapshot every 5 min
    p25_goal_refresh_interval_minutes: int = 60  # Refresh goals/insights hourly
    p25_queue_reorder_interval_minutes: int = 15  # Reorder queue every 15 min
    # P28: Central Orchestrator settings
    use_central_orchestrator: bool = True  # Use central orchestrator for task routing
    # P30: Self-improvement cycle settings
    improvement_enabled: bool = True
    improvement_cycle_interval_hours: int = (
        48  # WS-059: Sprint cadence (was 168h weekly)
    )
    # WS-059: Vision refresh (7-day direction cadence)
    vision_refresh_enabled: bool = True
    vision_refresh_interval_hours: int = 168  # 7 days
    # P18: Adaptive intervals settings
    adaptive_intervals_enabled: bool = False
    adaptive_intervals_min_hours: float = 1.0
    adaptive_intervals_max_hours: float = 24.0
    adaptive_intervals_auto_apply_threshold: float = 0.8
    adaptive_intervals_min_sessions: int = 20
    # P32: Post-completion proposal settings
    post_completion_proposals_enabled: bool = True
    post_completion_proposal_probability: float = 0.5
    # P41: Feedback monitor settings
    feedback_monitor_enabled: bool = True
    feedback_monitor_interval_minutes: int = 10
    # P43: Cron scheduler settings
    cron_scheduler_enabled: bool = True
    cron_check_interval_seconds: int = 60
    # P47: Artifact generation settings
    artifact_generation_enabled: bool = True
    artifact_generation_interval_hours: int = 24  # Generate once per day
    # P-Security: Branch protection health check (ADR-0002)
    branch_protection_check_enabled: bool = True
    branch_protection_check_interval_minutes: int = 60  # Hourly
    branch_protection_auto_restore: bool = False  # Opt-in only
    # P-Security: Unmergeable PR escalation.
    # Default False (fail-closed): escalation can auto-CLOSE gate-blocked PRs
    # after N skips, destroying real work. A config that omits the key must not
    # silently activate a destructive lever (2026-06-11: PRs #1050/#1051 were
    # auto-closed because a stale config file lacked escalationEnabled).
    auto_merge_escalation_enabled: bool = False
    auto_merge_escalation_threshold: int = 3  # Escalate after N skips
    auto_merge_escalation_max_attempts: int = 10  # Hard cap — close PR after N skips
    # Auto-update: restart daemon when origin/main has new commits
    git_update_check_enabled: bool = True
    git_update_check_interval_minutes: int = 5  # How often to git fetch
    git_update_max_worker_wait_cycles: int = 10  # Max cycles waiting for drain
    # Worker force-collect timeout. The base applies to trivial/standard tasks;
    # complex/epic tasks multiply it so long-running engine tasks are not
    # killed mid-flight (2026-07-06: fixed 40-min cap force-collected two epic
    # tasks that later finished, producing #34+#35 duplicates and a husk).
    worker_timeout_seconds: int = 2400  # 40 min base
    worker_timeout_multipliers: dict = field(
        default_factory=lambda: {
            "trivial": 1.0,
            "standard": 1.0,
            "complex": 2.0,
            "epic": 3.0,
        }
    )
    # Raw config for accessing nested settings not mapped to fields
    raw_config: dict = field(default_factory=dict)

    def __post_init__(self):
        """Convert string paths to Path objects if needed."""
        if isinstance(self.pid_file, str):
            self.pid_file = Path(self.pid_file)
        if isinstance(self.heartbeat_file, str):
            self.heartbeat_file = Path(self.heartbeat_file)
        if isinstance(self.log_file, str):
            self.log_file = Path(self.log_file)

    def config_hash(self) -> str:
        """Generate a hash of the configuration for change detection."""
        config_str = f"{self.poll_interval_seconds}:{self.max_tasks_per_hour}:{self.idle_timeout_seconds}"
        return hashlib.md5(config_str.encode()).hexdigest()[:12]

    @staticmethod
    def default_config_path(base_dir: Path | None = None) -> Path:
        """Resolve the default config file location.

        Prefers the canonical repo-root ``gatesmith.json`` (the file
        documented in CLAUDE.md and actively maintained), falling back to the
        legacy ``.claude/gatesmith.json``. Anchored at the repo root derived
        from this file's location, NOT the process cwd — the daemon previously
        loaded a stale ``.claude/gatesmith.json`` (last touched 2026-04-14)
        and ran with fail-open defaults for every key added since, which
        auto-closed PRs #1050/#1051 on 2026-06-11.

        Args:
            base_dir: Repo root override for tests. Defaults to the root
                inferred from ``__file__``.

        Returns:
            Path to the config file to load (may not exist; ``from_file``
            handles a missing file by returning defaults).
        """
        root = base_dir or Path(__file__).resolve().parent.parent.parent.parent
        canonical = root / "gatesmith.json"
        legacy = root / ".claude" / "gatesmith.json"
        if canonical.exists():
            return canonical
        if legacy.exists():
            return legacy
        return canonical

    @classmethod
    def from_file(cls, config_path: Path) -> "DaemonConfig":
        """Load configuration from a JSON file.

        Args:
            config_path: Path to configuration file.

        Returns:
            DaemonConfig instance with loaded values.
        """
        config = cls()

        if not config_path.exists():
            return config

        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict):
                _fill_auto_merge_from_template(data, config_path)
                _warn_merge_policy_shape(data, config_path)
            # Store raw config for accessing nested settings
            config.raw_config = data

            # Navigate to daemon config section
            autonomy = data.get("autonomy", {})
            cont_op = autonomy.get("continuousOperation", {})
            daemon_config = cont_op.get("daemon", {})

            # Apply values if present
            if "pidFile" in daemon_config:
                config.pid_file = Path(daemon_config["pidFile"])
            if "logDir" in daemon_config:
                config.log_file = Path(daemon_config["logDir"]) / "daemon.log"
            if "logLevel" in daemon_config:
                config.log_level = daemon_config["logLevel"]
            if "pollIntervalSeconds" in cont_op:
                config.poll_interval_seconds = cont_op["pollIntervalSeconds"]

            # Load proactive config
            proactive = data.get("proactive", {})
            config.proactive_enabled = proactive.get("enabled", True)
            config.proactive_scan_interval_minutes = proactive.get(
                "scanIntervalMinutes", 60
            )

            # Load cross-project config (P14)
            cross_project = data.get("crossProject", {})
            config.cross_project_enabled = cross_project.get("enabled", True)
            auto_routing = cross_project.get("autoRouting", {})
            config.cross_project_auto_routing = auto_routing.get("enabled", True)
            config.cross_project_confidence_threshold = auto_routing.get(
                "confidenceThreshold", 0.7
            )
            config.cross_project_require_approval_for = auto_routing.get(
                "requireApprovalFor", ["high_risk", "complex"]
            )

            # Load executive loop config (P17)
            exec_loop = data.get("executiveLoop", {})
            config.executive_loop_enabled = exec_loop.get("enabled", True)
            config.executive_loop_interval_hours = exec_loop.get("intervalHours", 8)
            config.executive_loop_trigger_on_empty_queue = exec_loop.get(
                "triggerOnEmptyQueue", True
            )
            config.executive_loop_skip_if_queue_above = exec_loop.get(
                "skipIfQueueAbove", 5
            )

            # Load adaptive intervals config (P18)
            adaptive_config = exec_loop.get("adaptiveIntervals", {})
            config.adaptive_intervals_enabled = adaptive_config.get("enabled", False)
            config.adaptive_intervals_min_hours = adaptive_config.get(
                "minInterval", 1.0
            )
            config.adaptive_intervals_max_hours = adaptive_config.get(
                "maxInterval", 24.0
            )
            config.adaptive_intervals_auto_apply_threshold = adaptive_config.get(
                "autoApplyThreshold", 0.8
            )
            config.adaptive_intervals_min_sessions = adaptive_config.get(
                "minSessionsForLearning", 20
            )

            # Load cascade scheduling config (WS-016)
            cascade = data.get("cascade", {})
            config.cascade_enabled = cascade.get("enabled", True)
            config.queue_low_threshold = cascade.get("queueLowThreshold", 3)
            config.employee_quiet_after_exec_minutes = cascade.get(
                "employeeQuietAfterExecMinutes", 30
            )
            config.exec_wait_for_initiative_minutes = cascade.get(
                "execWaitForInitiativeMinutes", 15
            )

            # Load employee initiative config (idle-backoff gate)
            emp_init = data.get("employeeInitiative", {})
            if "enabled" in emp_init:
                config.employee_initiative_enabled = emp_init["enabled"]
            if "intervalMinutes" in emp_init:
                config.employee_initiative_interval_minutes = emp_init[
                    "intervalMinutes"
                ]
            if "maxPerCycle" in emp_init:
                config.employee_initiative_max_per_cycle = emp_init["maxPerCycle"]
            config.employee_initiative_idle_backoff_threshold = emp_init.get(
                "idleBackoffThreshold", 3
            )
            config.employee_initiative_idle_cadence_minutes = emp_init.get(
                "idleCadenceMinutes", 120
            )

            # Load employeeIdeation config
            emp_ideation = data.get("employeeIdeation", {})
            config.employee_ideation_auto_convert_approved = emp_ideation.get(
                "autoConvertApproved", True
            )

            # Load fill ordering config (E7-ext: ideation-first ordering)
            fill_cfg = data.get("fill", {})
            config.fill_defer_to_ideation = fill_cfg.get("deferToIdeation", True)

            # Load P32: Post-completion proposal config
            post_completion = data.get("postCompletionProposals", {})
            config.post_completion_proposals_enabled = post_completion.get(
                "enabled", True
            )
            config.post_completion_proposal_probability = post_completion.get(
                "probability", 0.5
            )

            # Load top-level daemon config (overrides autonomy settings)
            daemon_top = data.get("daemon", {})
            if "maxTasksPerHour" in daemon_top:
                config.max_tasks_per_hour = daemon_top["maxTasksPerHour"]
            if "pollIntervalSeconds" in daemon_top:
                config.poll_interval_seconds = daemon_top["pollIntervalSeconds"]
            if "discoveryLoopIntervalSeconds" in daemon_top:
                config.discovery_loop_interval_seconds = daemon_top[
                    "discoveryLoopIntervalSeconds"
                ]
            if "idleTimeoutSeconds" in daemon_top:
                # WS-124: Default to 0 (disabled) for 24/7 operation.
                # LaunchAgent manages lifecycle; daemon should not self-exit.
                config.idle_timeout_seconds = 0
            if "pidFile" in daemon_top:
                config.pid_file = Path(daemon_top["pidFile"])
            if "heartbeatFile" in daemon_top:
                config.heartbeat_file = Path(daemon_top["heartbeatFile"])
            if "logFile" in daemon_top:
                config.log_file = Path(daemon_top["logFile"])
            if "logLevel" in daemon_top:
                config.log_level = daemon_top["logLevel"]

            # P22 Fix 2: Load stale cleanup config
            if "staleCleanupEnabled" in daemon_top:
                config.stale_cleanup_enabled = daemon_top["staleCleanupEnabled"]
            if "staleCleanupIntervalHours" in daemon_top:
                config.stale_cleanup_interval_hours = daemon_top[
                    "staleCleanupIntervalHours"
                ]
            if "staleTaskMaxAgeHours" in daemon_top:
                config.stale_task_max_age_hours = daemon_top["staleTaskMaxAgeHours"]

            # Autonomy queue reconcile (closed-unmerged hygiene)
            if "autonomyReconcileEnabled" in daemon_top:
                config.autonomy_reconcile_enabled = daemon_top[
                    "autonomyReconcileEnabled"
                ]
            if "autonomyReconcileIntervalHours" in daemon_top:
                config.autonomy_reconcile_interval_hours = daemon_top[
                    "autonomyReconcileIntervalHours"
                ]
            if "autonomyReconcileWindowDays" in daemon_top:
                config.autonomy_reconcile_window_days = daemon_top[
                    "autonomyReconcileWindowDays"
                ]
            if "prOpenReconcileIntervalSeconds" in daemon_top:
                config.pr_open_reconcile_interval_seconds = daemon_top[
                    "prOpenReconcileIntervalSeconds"
                ]
            if "strandedHarvestIntervalSeconds" in daemon_top:
                config.stranded_harvest_interval_seconds = daemon_top[
                    "strandedHarvestIntervalSeconds"
                ]

            # Stale-escalation auto-archive
            if "escalationArchiveEnabled" in daemon_top:
                config.escalation_archive_enabled = daemon_top[
                    "escalationArchiveEnabled"
                ]
            if "escalationArchiveIntervalHours" in daemon_top:
                config.escalation_archive_interval_hours = daemon_top[
                    "escalationArchiveIntervalHours"
                ]
            if "escalationArchiveStaleHours" in daemon_top:
                config.escalation_archive_stale_hours = daemon_top[
                    "escalationArchiveStaleHours"
                ]

            # P22 Fix 4: Load restart/supervision config
            if "restartOnCrash" in daemon_top:
                config.restart_on_crash = daemon_top["restartOnCrash"]

            # P23: Load document approval scan config
            if "documentScanEnabled" in daemon_top:
                config.document_scan_enabled = daemon_top["documentScanEnabled"]
            if "documentScanIntervalMinutes" in daemon_top:
                config.document_scan_interval_minutes = daemon_top[
                    "documentScanIntervalMinutes"
                ]
            if "maxRestarts" in daemon_top:
                config.max_restarts = daemon_top["maxRestarts"]
            if "restartDelaySeconds" in daemon_top:
                config.restart_delay_seconds = daemon_top["restartDelaySeconds"]
            if "workerTimeoutSeconds" in daemon_top:
                config.worker_timeout_seconds = int(daemon_top["workerTimeoutSeconds"])
            if "workerTimeoutMultipliers" in daemon_top:
                config.worker_timeout_multipliers = dict(
                    daemon_top["workerTimeoutMultipliers"]
                )

            # P27: Load roadmap scheduling config
            roadmap_config = data.get("roadmapScheduling", {})
            config.roadmap_scheduling_enabled = roadmap_config.get("enabled", True)
            config.roadmap_scan_interval_minutes = roadmap_config.get(
                "scanIntervalMinutes", 15
            )
            config.roadmap_auto_schedule_waves = roadmap_config.get(
                "autoScheduleWaves", True
            )
            config.roadmap_respect_dependencies = roadmap_config.get(
                "respectDependencies", True
            )
            # Signal-driven backlog generation. Defaults ON: the config key is
            # optional so an existing install gets the last-resort work source
            # without a humanProtected gatesmith.json edit.
            backlog_config = data.get("backlogGeneration", {})
            config.backlog_generation_enabled = backlog_config.get("enabled", True)
            config.backlog_max_tasks_per_scan = backlog_config.get("maxTasksPerScan", 3)
            config.backlog_cooldown_minutes = backlog_config.get("cooldownMinutes", 60)

            config.roadmap_max_tasks_per_scan = roadmap_config.get(
                "maxTasksPerScan", 10
            )

            # P20: Load planning config
            planning_config = data.get("planning", {})
            config.planning_enabled = planning_config.get("enabled", False)
            config.planning_complexity_threshold = planning_config.get(
                "complexityThreshold", "epic"
            )
            config.planning_auto_execute_waves = planning_config.get(
                "autoExecuteWaves", True
            )
            config.planning_max_revision_attempts = planning_config.get(
                "maxRevisionAttempts", 1
            )
            config.planning_pass_threshold = planning_config.get("passThreshold", 18)

            # P25: Load full autonomy config
            p25_config = data.get("p25", data.get("fullAutonomy", {}))
            config.p25_enabled = p25_config.get("enabled", True)
            config.p25_adaptive_scheduling_enabled = p25_config.get(
                "adaptiveSchedulingEnabled", True
            )
            config.p25_session_continuity_enabled = p25_config.get(
                "sessionContinuityEnabled", True
            )
            config.p25_approval_learning_enabled = p25_config.get(
                "approvalLearningEnabled", True
            )
            config.p25_goal_scheduling_enabled = p25_config.get(
                "goalSchedulingEnabled", True
            )
            config.p25_budget_governor_enabled = p25_config.get(
                "budgetGovernorEnabled", True
            )
            config.p25_learning_loop_enabled = p25_config.get(
                "learningLoopEnabled", True
            )
            config.p25_snapshot_interval_seconds = p25_config.get(
                "snapshotIntervalSeconds", 300
            )
            config.p25_goal_refresh_interval_minutes = p25_config.get(
                "goalRefreshIntervalMinutes", 60
            )
            config.p25_queue_reorder_interval_minutes = p25_config.get(
                "queueReorderIntervalMinutes", 15
            )

            # P43: Load cron scheduler config
            cron_config = data.get("cronScheduler", {})
            config.cron_scheduler_enabled = cron_config.get("enabled", True)
            config.cron_check_interval_seconds = cron_config.get(
                "checkIntervalSeconds", 60
            )

            # P47: Load artifact generation config
            artifact_config = data.get("artifactGeneration", {})
            config.artifact_generation_enabled = artifact_config.get("enabled", True)
            config.artifact_generation_interval_hours = artifact_config.get(
                "intervalHours", 24
            )

            # ADR-0002: Load branch protection config
            bp_config = data.get("branchProtection", {})
            config.branch_protection_check_enabled = bp_config.get("enabled", True)
            config.branch_protection_check_interval_minutes = bp_config.get(
                "checkIntervalMinutes", 60
            )
            config.branch_protection_auto_restore = bp_config.get("autoRestore", False)

            # ADR-0002: Load auto-merge escalation config
            auto_merge = data.get("autonomy", {}).get("autoMerge", {})
            if not isinstance(auto_merge, dict):
                auto_merge = {}
            # The merge-permission keys. autoMerge.enabled was never loaded
            # (the dataclass default True always won), so enabled=false in a
            # config could not stop the daemon's merge loop either.
            # Absent -> pre-fix behaviour; malformed -> the safe value.
            config.auto_merge_enabled = _config_bool(
                auto_merge, "enabled", default=True, permissive=True
            )
            config.auto_merge_allow_merge_to_main = _config_bool(
                auto_merge, "allowMergeToMain", default=True, permissive=True
            )
            config.auto_merge_require_approval = _config_bool(
                auto_merge, "requireApproval", default=False, permissive=False
            )
            # Fail-closed: absent key must NOT enable the auto-close lever
            # (matches the dataclass default; see field comment).
            config.auto_merge_escalation_enabled = auto_merge.get(
                "escalationEnabled", False
            )
            config.auto_merge_escalation_threshold = auto_merge.get(
                "escalationThreshold", 3
            )
            # WS-066-002: Full-trust security gates config
            config.auto_merge_full_trust = auto_merge.get("fullTrust", True)
            # Fail-closed: absent key keeps gateless repos blocked.
            config.auto_merge_allow_gateless_repos = auto_merge.get(
                "allowGatelessRepos", False
            )
            config.auto_merge_allowed_base_branches = auto_merge.get(
                "allowedBaseBranches", ["main", "master"]
            )
            _dgate = data.get("autonomy", {}).get("deliverableGate", {})
            config.auto_merge_require_deliverable_verdict = bool(
                _dgate.get("enabled", True) if isinstance(_dgate, dict) else True
            )
            config.auto_merge_blocked_paths = auto_merge.get(
                "blockedPaths",
                ["gatesmith.json", ".claude/settings.json", "CLAUDE.md"],
            )
            # Absent key → shared default seed; malformed value → also the
            # default seed (never fail open to "no holds" on this surface).
            # An explicit [] is honored as an operator opt-out.
            _rp = auto_merge.get("reviewPaths")
            if _rp is None or not (
                isinstance(_rp, list) and all(isinstance(p, str) for p in _rp)
            ):
                config.auto_merge_review_paths = _default_review_paths()
            else:
                config.auto_merge_review_paths = _rp
            config.auto_merge_max_diff_lines = auto_merge.get("maxDiffLines", 500)
            config.auto_merge_max_diff_files = auto_merge.get("maxDiffFiles", 20)
            config.auto_merge_secrets_scan = auto_merge.get("secretsScan", True)
            config.auto_merge_audit_enabled = auto_merge.get("auditEnabled", True)
            config.auto_merge_audit_max_entries = auto_merge.get("auditMaxEntries", 200)
            # WS-066-003: Pre-existing CI failure bypass
            config.auto_merge_bypass_preexisting_failures = auto_merge.get(
                "bypassPreexistingFailures", True
            )
            # Increment 2a: same fail-closed shape as reviewPaths above —
            # absent/malformed -> code default; an explicit [] is honored as
            # an operator opt-out.
            _bxc = auto_merge.get("bypassExcludedChecks")
            if _bxc is None or not (
                isinstance(_bxc, list) and all(isinstance(c, str) for c in _bxc)
            ):
                config.auto_merge_bypass_excluded_checks = list(
                    DEFAULT_BYPASS_EXCLUDED_CHECKS
                )
            else:
                config.auto_merge_bypass_excluded_checks = _bxc
            config.auto_merge_admin_max_per_day = auto_merge.get("adminMaxPerDay", 3)
            config.auto_merge_require_rollback_healthy = auto_merge.get(
                "requireRollbackHealthy", True
            )
            # WS-066-004: Self-healing CI
            ci_healer = data.get("autonomy", {}).get("ciHealer", {})
            config.ci_healer_enabled = ci_healer.get("enabled", True)
            config.ci_healer_max_attempts_per_pr = ci_healer.get("maxAttemptsPerPr", 3)
            config.ci_healer_max_heals_per_day = ci_healer.get("maxHealsPerDay", 10)

            # Silence sentinel (plan item 4, 2026-07-26)
            sentinel = data.get("autonomy", {}).get("silenceSentinel", {})
            config.silence_sentinel_enabled = sentinel.get("enabled", True)
            config.silence_sentinel_interval_minutes = sentinel.get(
                "intervalMinutes", 30
            )

            # LEDGER-5c: scheduled acceptance step (top-level `acceptance`
            # block, the same block acceptance_ledger/probe read).
            _apply_acceptance_config(config, data.get("acceptance"))

            # E8: CI-fail terminal path
            ci_fail_term = data.get("autonomy", {}).get("ciFailTerminal", {})
            config.ci_fail_terminal_enabled = ci_fail_term.get("enabled", True)
            config.ci_fail_terminal_cycles = int(ci_fail_term.get("failCycles", 5))
            config.ci_fail_max_requeues = int(ci_fail_term.get("maxRequeues", 1))
            # E8: Cross-regeneration build ceiling
            cross_regen = data.get("autonomy", {}).get("crossRegenCeiling", {})
            config.cross_regen_ceiling_enabled = cross_regen.get("enabled", True)
            config.cross_regen_max_builds = int(cross_regen.get("maxBuilds", 5))

            # WS-067-001: External signal integration
            external_signals = data.get("autonomy", {}).get("externalSignals", {})
            config.external_signals_enabled = external_signals.get("enabled", True)
            config.external_signals_max_age_hours = external_signals.get(
                "signalMaxAgeHours", 24
            )
            config.external_signals_max_boost = external_signals.get(
                "maxBoostPerTask", 50
            )

            # Git auto-update config
            git_update = data.get("gitAutoUpdate", {})
            config.git_update_check_enabled = git_update.get("enabled", True)
            config.git_update_check_interval_minutes = git_update.get(
                "checkIntervalMinutes", 5
            )
            config.git_update_max_worker_wait_cycles = git_update.get(
                "maxWorkerWaitCycles", 10
            )

            # P53: Load scheduled reports config
            scheduled_reports_cfg = data.get("scheduledReports", {})
            config.scheduled_reports_enabled = scheduled_reports_cfg.get(
                "enabled", True
            )
            config.scheduled_reports = scheduled_reports_cfg.get("reports", [])

            # Load scheduled tasks config (sales/operations activities)
            scheduled_tasks_cfg = data.get("scheduledTasks", {})
            config.scheduled_tasks_enabled = scheduled_tasks_cfg.get("enabled", True)
            config.scheduled_tasks = scheduled_tasks_cfg.get("tasks", [])

            # Load recurring product health tasks
            recurring_tasks_cfg = data.get("recurringTasks", {})
            config.recurring_tasks_enabled = recurring_tasks_cfg.get("enabled", True)
            config.recurring_tasks = recurring_tasks_cfg.get("tasks", [])

            return config

        except (json.JSONDecodeError, OSError, TypeError):
            return config


# -----------------------------------------------------------------------------
# State Dataclass
# -----------------------------------------------------------------------------


@dataclass
class DaemonState:
    """Current state of the daemon process.

    Attributes:
        pid: Process ID of the daemon
        started_at: ISO timestamp when daemon started
        version: Daemon version string
        config_hash: Hash of current configuration
        last_heartbeat: ISO timestamp of last heartbeat
        tasks_completed: Number of tasks completed this session
        tasks_failed: Number of tasks failed this session
        current_task: ID of task currently being processed (None if idle)
        last_proactive_scan: ISO timestamp of last proactive scan
        proactive_proposals_created: Count of proposals created this session
        cross_project_tasks_routed: Count of tasks auto-routed this session (P14)
        cross_project_tasks_queued: Count of tasks queued for approval (P14)
        last_rebalance_check: ISO timestamp of last rebalance check (P14)
        rebalance_proposals_created: Count of rebalance proposals this session (P14)
        vision_proposals_submitted: Count of vision refresh proposals submitted this session (WS-059)
    """

    pid: int
    started_at: str
    version: str = "1.0"
    config_hash: str = ""
    last_heartbeat: str = ""
    tasks_completed: int = 0
    tasks_failed: int = 0
    current_task: str | None = None
    last_proactive_scan: str = ""
    proactive_proposals_created: int = 0
    # Cross-project orchestration metrics (P14)
    cross_project_tasks_routed: int = 0
    cross_project_tasks_queued: int = 0
    last_rebalance_check: str = ""
    rebalance_proposals_created: int = 0
    # Strategic planning metrics (P15)
    last_strategic_planning: str = ""
    strategic_initiatives_created: int = 0
    strategic_tasks_queued: int = 0
    # P29: Strategic planning cycle metrics
    last_weekly_planning: str = ""
    last_daily_planning: str = ""
    weekly_planning_runs: int = 0
    daily_planning_runs: int = 0
    # Executive loop metrics (P17)
    last_executive_loop: str = ""
    executive_sessions_run: int = 0
    work_submitted_by_executives: int = 0
    # P22 Fix 2: Stale task cleanup metrics
    last_stale_cleanup: str = ""
    stale_tasks_cleaned: int = 0
    # Autonomy queue reconcile (closed-unmerged hygiene)
    last_autonomy_reconcile: str = ""
    last_pr_open_reconcile: str = ""
    last_stranded_harvest: str = ""
    last_escalation_archive: str = ""
    # P23: Document approval scan metrics
    last_document_scan: str = ""
    documents_discovered: int = 0
    documents_approved: int = 0
    # P26: Employee initiative metrics
    last_employee_initiative: str = ""
    employee_proposals_submitted: int = 0
    employee_initiative_cycles: int = 0
    # P34: Employee ideation metrics
    last_employee_ideation: str = ""
    employee_ideation_cycles: int = 0
    ideas_generated: int = 0
    # Approved-idea auto-conversion metrics
    last_idea_auto_convert: str = ""
    ideas_auto_converted: int = 0
    # WS-059: Vision refresh state
    last_vision_refresh: str = ""
    vision_refresh_cycles_run: int = 0
    vision_proposals_submitted: int = 0
    # P35: Auto-merge metrics
    last_auto_merge_check: str = ""
    auto_merge_checks: int = 0
    prs_merged: int = 0
    prs_skipped: int = 0
    # WS-066-001: Automated rollback metrics
    last_rollback_check: str = ""
    rollback_checks_run: int = 0
    rollbacks_executed: int = 0
    rollbacks_blocked: int = 0
    # WS-066-003: Admin merge metrics (pre-existing CI bypass)
    admin_merges_today: int = 0
    admin_merges_last_date: str = ""  # YYYY-MM-DD for daily reset
    # P53: Scheduled reports metrics
    scheduled_reports_run: dict = field(default_factory=dict)  # report_name -> last_run
    scheduled_reports_count: int = 0
    # Scheduled tasks metrics
    scheduled_tasks_run: dict = field(
        default_factory=dict
    )  # task_id -> last_queued ISO
    scheduled_tasks_count: int = 0
    # Recurring product health task metrics
    recurring_tasks_run: dict = field(
        default_factory=dict
    )  # task_id -> last_queued ISO
    recurring_tasks_count: int = 0
    # P26: Manager review metrics
    last_manager_review: str = ""
    tasks_reviewed: int = 0
    proposals_approved: int = 0
    proposals_rejected: int = 0
    # P27: Roadmap scheduling metrics
    last_roadmap_scan: str = ""
    roadmap_tasks_scheduled: int = 0
    roadmap_tasks_completed: int = 0
    roadmap_current_wave: int = 1
    # P20: GSD/BMAD planning metrics
    tasks_planned: int = 0  # Tasks that went through planning pipeline
    tasks_direct: int = 0  # Tasks executed directly (trivial complexity)
    planning_failures: int = 0  # Plan creation/validation failures
    # P25: Full Autonomous Operation metrics
    last_session_snapshot: str = ""
    last_goal_refresh: str = ""
    last_queue_reorder: str = ""
    p25_schedule_mode: str = "NORMAL"  # Current adaptive schedule mode
    p25_throttle_level: str = "NORMAL"  # Current budget throttle level
    p25_outcomes_recorded: int = 0  # Total outcomes recorded
    p25_auto_approvals: int = 0  # Auto-approved decisions
    p25_queue_reorders: int = 0  # Queue reorder operations
    p25_needs_initial_goal_refresh: bool = False  # Deferred goal computation
    # P30: Self-improvement cycle metrics
    last_improvement_cycle: str = ""
    improvement_cycles_run: int = 0
    # P18: Adaptive intervals metrics
    last_adaptive_check: str = ""
    adaptive_intervals_recommendations: int = 0
    # P32: Post-completion proposal metrics
    post_completion_proposals_submitted: int = 0
    # P41: Feedback monitor metrics
    last_feedback_check: str = ""
    feedback_checks_run: int = 0
    feedback_actions_taken: int = 0
    # P43: Cron scheduler metrics
    last_cron_check: str = ""
    cron_checks_run: int = 0
    cron_tasks_executed: int = 0
    # P47: Artifact generation metrics
    last_artifact_generation: str = ""
    artifact_reports_generated: int = 0
    # P-Security: Branch protection check metrics
    last_branch_protection_check: str = ""
    branch_protection_checks_run: int = 0
    branch_protection_degradations: int = 0
    # P-Security: Auto-merge escalation metrics
    auto_merge_escalations: int = 0
    # WS-066-002: Full-trust gate metrics
    auto_merge_gate_passes: int = 0
    auto_merge_gate_blocks: int = 0
    # Phase0-T0.6: Per-PR conflict rebase attempt counter (str(pr_number) -> int)
    pr_conflict_attempts: dict = field(default_factory=dict)
    # Phase2-T2.2: Per-task conflict close counter (task_id -> int).
    # Keyed by task_id (not pr_number) so the bound persists across close+regenerate
    # cycles. After N total closures for one task, escalate instead of re-queuing.
    task_conflict_attempts: dict = field(default_factory=dict)
    # E8: CI-fail terminal path — per-PR cycle counter (str(pr_number) -> int)
    pr_ci_fail_cycles: dict = field(default_factory=dict)
    # E8: per-task requeue counter (task_id -> int); persists across close+regen cycles
    task_ci_fail_requeues: dict = field(default_factory=dict)
    # E8: Cross-regeneration ceiling — total builds per task_id and per normalized title
    task_total_builds: dict = field(default_factory=dict)
    title_total_builds: dict = field(default_factory=dict)
    # E8: lifetime metrics for dashboards
    pr_ci_fail_requeues: int = 0
    pr_ci_fail_escalations: int = 0
    cross_regen_ceiling_blocks: int = 0
    # WS-120: Queue auto-fill metrics
    last_queue_autofill: str = ""
    queue_autofill_runs: int = 0
    queue_autofill_tasks_generated: int = 0
    last_backlog_generation: str = ""
    backlog_generation_runs: int = 0
    backlog_tasks_generated: int = 0
    last_silence_check: str = ""
    silence_alerts_raised: int = 0
    # LEDGER-5c: when the scheduled acceptance step was last launched
    last_acceptance_run: str = ""
    acceptance_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for JSON serialization."""
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "version": self.version,
            "config_hash": self.config_hash,
            "last_heartbeat": self.last_heartbeat,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "current_task": self.current_task,
            "last_proactive_scan": self.last_proactive_scan,
            "proactive_proposals_created": self.proactive_proposals_created,
            # Cross-project metrics (P14)
            "cross_project_tasks_routed": self.cross_project_tasks_routed,
            "cross_project_tasks_queued": self.cross_project_tasks_queued,
            "last_rebalance_check": self.last_rebalance_check,
            "rebalance_proposals_created": self.rebalance_proposals_created,
            # Strategic planning metrics (P15)
            "last_strategic_planning": self.last_strategic_planning,
            "strategic_initiatives_created": self.strategic_initiatives_created,
            "strategic_tasks_queued": self.strategic_tasks_queued,
            # P29: Strategic planning cycle metrics
            "last_weekly_planning": self.last_weekly_planning,
            "last_daily_planning": self.last_daily_planning,
            "weekly_planning_runs": self.weekly_planning_runs,
            "daily_planning_runs": self.daily_planning_runs,
            # Executive loop metrics (P17)
            "last_executive_loop": self.last_executive_loop,
            "executive_sessions_run": self.executive_sessions_run,
            "work_submitted_by_executives": self.work_submitted_by_executives,
            # P22: Stale task cleanup metrics
            "last_stale_cleanup": self.last_stale_cleanup,
            "stale_tasks_cleaned": self.stale_tasks_cleaned,
            "last_autonomy_reconcile": self.last_autonomy_reconcile,
            "last_pr_open_reconcile": self.last_pr_open_reconcile,
            "last_stranded_harvest": self.last_stranded_harvest,
            "last_escalation_archive": self.last_escalation_archive,
            # P23: Document approval scan metrics
            "last_document_scan": self.last_document_scan,
            "documents_discovered": self.documents_discovered,
            "documents_approved": self.documents_approved,
            # P26: Employee initiative metrics
            "last_employee_initiative": self.last_employee_initiative,
            "employee_proposals_submitted": self.employee_proposals_submitted,
            "employee_initiative_cycles": self.employee_initiative_cycles,
            # P34: Employee ideation metrics
            "last_employee_ideation": self.last_employee_ideation,
            "employee_ideation_cycles": self.employee_ideation_cycles,
            "ideas_generated": self.ideas_generated,
            # Approved-idea auto-conversion metrics
            "last_idea_auto_convert": self.last_idea_auto_convert,
            "ideas_auto_converted": self.ideas_auto_converted,
            # P26: Manager review metrics
            "last_manager_review": self.last_manager_review,
            "tasks_reviewed": self.tasks_reviewed,
            "proposals_approved": self.proposals_approved,
            "proposals_rejected": self.proposals_rejected,
            # P27: Roadmap scheduling metrics
            "last_roadmap_scan": self.last_roadmap_scan,
            "roadmap_tasks_scheduled": self.roadmap_tasks_scheduled,
            "roadmap_tasks_completed": self.roadmap_tasks_completed,
            "roadmap_current_wave": self.roadmap_current_wave,
            # P20: GSD/BMAD planning metrics
            "tasks_planned": self.tasks_planned,
            "tasks_direct": self.tasks_direct,
            "planning_failures": self.planning_failures,
            # P25: Full Autonomous Operation metrics
            "last_session_snapshot": self.last_session_snapshot,
            "last_goal_refresh": self.last_goal_refresh,
            "last_queue_reorder": self.last_queue_reorder,
            "p25_schedule_mode": self.p25_schedule_mode,
            "p25_throttle_level": self.p25_throttle_level,
            "p25_outcomes_recorded": self.p25_outcomes_recorded,
            "p25_auto_approvals": self.p25_auto_approvals,
            "p25_queue_reorders": self.p25_queue_reorders,
            # P30: Self-improvement cycle metrics
            "last_improvement_cycle": self.last_improvement_cycle,
            "improvement_cycles_run": self.improvement_cycles_run,
            # P18: Adaptive intervals metrics
            "last_adaptive_check": self.last_adaptive_check,
            "adaptive_intervals_recommendations": self.adaptive_intervals_recommendations,
            # P32: Post-completion proposal metrics
            "post_completion_proposals_submitted": self.post_completion_proposals_submitted,
            # P41: Feedback monitor metrics
            "last_feedback_check": self.last_feedback_check,
            "feedback_checks_run": self.feedback_checks_run,
            "feedback_actions_taken": self.feedback_actions_taken,
            # P43: Cron scheduler metrics
            "last_cron_check": self.last_cron_check,
            "cron_checks_run": self.cron_checks_run,
            "cron_tasks_executed": self.cron_tasks_executed,
            # P47: Artifact generation metrics
            "last_artifact_generation": self.last_artifact_generation,
            "artifact_reports_generated": self.artifact_reports_generated,
            # P-Security: Branch protection check metrics
            "last_branch_protection_check": self.last_branch_protection_check,
            "branch_protection_checks_run": self.branch_protection_checks_run,
            "branch_protection_degradations": self.branch_protection_degradations,
            # P-Security: Auto-merge escalation metrics
            "auto_merge_escalations": self.auto_merge_escalations,
            # WS-066-002: Full-trust gate metrics
            "auto_merge_gate_passes": self.auto_merge_gate_passes,
            "auto_merge_gate_blocks": self.auto_merge_gate_blocks,
            # Phase0-T0.6: conflict rebase attempt counter
            "pr_conflict_attempts": self.pr_conflict_attempts,
            # Phase2-T2.2: per-task conflict close counter
            "task_conflict_attempts": self.task_conflict_attempts,
            # E8: CI-fail terminal + cross-regen ceiling metrics
            "pr_ci_fail_cycles": self.pr_ci_fail_cycles,
            "task_ci_fail_requeues": self.task_ci_fail_requeues,
            "task_total_builds": self.task_total_builds,
            "title_total_builds": self.title_total_builds,
            "pr_ci_fail_requeues": self.pr_ci_fail_requeues,
            "pr_ci_fail_escalations": self.pr_ci_fail_escalations,
            "cross_regen_ceiling_blocks": self.cross_regen_ceiling_blocks,
            # WS-120: Queue auto-fill metrics
            "last_queue_autofill": self.last_queue_autofill,
            "queue_autofill_runs": self.queue_autofill_runs,
            "queue_autofill_tasks_generated": self.queue_autofill_tasks_generated,
            "last_backlog_generation": self.last_backlog_generation,
            "backlog_generation_runs": self.backlog_generation_runs,
            "backlog_tasks_generated": self.backlog_tasks_generated,
            "last_silence_check": self.last_silence_check,
            "silence_alerts_raised": self.silence_alerts_raised,
            "last_acceptance_run": self.last_acceptance_run,
            "acceptance_runs": self.acceptance_runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DaemonState":
        """Create state from dictionary."""
        return cls(
            pid=data.get("pid", 0),
            started_at=data.get("started_at", ""),
            version=data.get("version", "1.0"),
            config_hash=data.get("config_hash", ""),
            last_heartbeat=data.get("last_heartbeat", ""),
            tasks_completed=data.get("tasks_completed", 0),
            tasks_failed=data.get("tasks_failed", 0),
            current_task=data.get("current_task"),
            last_proactive_scan=data.get("last_proactive_scan", ""),
            proactive_proposals_created=data.get("proactive_proposals_created", 0),
            # Cross-project metrics (P14)
            cross_project_tasks_routed=data.get("cross_project_tasks_routed", 0),
            cross_project_tasks_queued=data.get("cross_project_tasks_queued", 0),
            last_rebalance_check=data.get("last_rebalance_check", ""),
            rebalance_proposals_created=data.get("rebalance_proposals_created", 0),
            # Strategic planning metrics (P15)
            last_strategic_planning=data.get("last_strategic_planning", ""),
            strategic_initiatives_created=data.get("strategic_initiatives_created", 0),
            strategic_tasks_queued=data.get("strategic_tasks_queued", 0),
            # P29: Strategic planning cycle metrics
            last_weekly_planning=data.get("last_weekly_planning", ""),
            last_daily_planning=data.get("last_daily_planning", ""),
            weekly_planning_runs=data.get("weekly_planning_runs", 0),
            daily_planning_runs=data.get("daily_planning_runs", 0),
            # Executive loop metrics (P17)
            last_executive_loop=data.get("last_executive_loop", ""),
            executive_sessions_run=data.get("executive_sessions_run", 0),
            work_submitted_by_executives=data.get("work_submitted_by_executives", 0),
            # P22: Stale task cleanup metrics
            last_stale_cleanup=data.get("last_stale_cleanup", ""),
            stale_tasks_cleaned=data.get("stale_tasks_cleaned", 0),
            last_autonomy_reconcile=data.get("last_autonomy_reconcile", ""),
            last_pr_open_reconcile=data.get("last_pr_open_reconcile", ""),
            last_stranded_harvest=data.get("last_stranded_harvest", ""),
            last_escalation_archive=data.get("last_escalation_archive", ""),
            # P23: Document approval scan metrics
            last_document_scan=data.get("last_document_scan", ""),
            documents_discovered=data.get("documents_discovered", 0),
            documents_approved=data.get("documents_approved", 0),
            # P26: Employee initiative metrics
            last_employee_initiative=data.get("last_employee_initiative", ""),
            employee_proposals_submitted=data.get("employee_proposals_submitted", 0),
            employee_initiative_cycles=data.get("employee_initiative_cycles", 0),
            # P34: Employee ideation metrics
            last_employee_ideation=data.get("last_employee_ideation", ""),
            employee_ideation_cycles=data.get("employee_ideation_cycles", 0),
            ideas_generated=data.get("ideas_generated", 0),
            # Approved-idea auto-conversion metrics
            last_idea_auto_convert=data.get("last_idea_auto_convert", ""),
            ideas_auto_converted=data.get("ideas_auto_converted", 0),
            # P26: Manager review metrics
            last_manager_review=data.get("last_manager_review", ""),
            tasks_reviewed=data.get("tasks_reviewed", 0),
            proposals_approved=data.get("proposals_approved", 0),
            proposals_rejected=data.get("proposals_rejected", 0),
            # P27: Roadmap scheduling metrics
            last_roadmap_scan=data.get("last_roadmap_scan", ""),
            roadmap_tasks_scheduled=data.get("roadmap_tasks_scheduled", 0),
            roadmap_tasks_completed=data.get("roadmap_tasks_completed", 0),
            roadmap_current_wave=data.get("roadmap_current_wave", 1),
            # P20: GSD/BMAD planning metrics
            tasks_planned=data.get("tasks_planned", 0),
            tasks_direct=data.get("tasks_direct", 0),
            planning_failures=data.get("planning_failures", 0),
            # P25: Full Autonomous Operation metrics
            last_session_snapshot=data.get("last_session_snapshot", ""),
            last_goal_refresh=data.get("last_goal_refresh", ""),
            last_queue_reorder=data.get("last_queue_reorder", ""),
            p25_schedule_mode=data.get("p25_schedule_mode", "NORMAL"),
            p25_throttle_level=data.get("p25_throttle_level", "NORMAL"),
            p25_outcomes_recorded=data.get("p25_outcomes_recorded", 0),
            p25_auto_approvals=data.get("p25_auto_approvals", 0),
            p25_queue_reorders=data.get("p25_queue_reorders", 0),
            # P30: Self-improvement cycle metrics
            last_improvement_cycle=data.get("last_improvement_cycle", ""),
            improvement_cycles_run=data.get("improvement_cycles_run", 0),
            # P18: Adaptive intervals metrics
            last_adaptive_check=data.get("last_adaptive_check", ""),
            adaptive_intervals_recommendations=data.get(
                "adaptive_intervals_recommendations", 0
            ),
            # P32: Post-completion proposal metrics
            post_completion_proposals_submitted=data.get(
                "post_completion_proposals_submitted", 0
            ),
            # P41: Feedback monitor metrics
            last_feedback_check=data.get("last_feedback_check", ""),
            feedback_checks_run=data.get("feedback_checks_run", 0),
            feedback_actions_taken=data.get("feedback_actions_taken", 0),
            # P43: Cron scheduler metrics
            last_cron_check=data.get("last_cron_check", ""),
            cron_checks_run=data.get("cron_checks_run", 0),
            cron_tasks_executed=data.get("cron_tasks_executed", 0),
            # P47: Artifact generation metrics
            last_artifact_generation=data.get("last_artifact_generation", ""),
            artifact_reports_generated=data.get("artifact_reports_generated", 0),
            # P-Security: Branch protection check metrics
            last_branch_protection_check=data.get("last_branch_protection_check", ""),
            branch_protection_checks_run=data.get("branch_protection_checks_run", 0),
            branch_protection_degradations=data.get(
                "branch_protection_degradations", 0
            ),
            # P-Security: Auto-merge escalation metrics
            auto_merge_escalations=data.get("auto_merge_escalations", 0),
            # WS-066-002: Full-trust gate metrics
            auto_merge_gate_passes=data.get("auto_merge_gate_passes", 0),
            auto_merge_gate_blocks=data.get("auto_merge_gate_blocks", 0),
            # Phase0-T0.6: conflict rebase attempt counter (validate int values on load)
            pr_conflict_attempts={
                k: int(v)
                for k, v in data.get("pr_conflict_attempts", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            # Phase2-T2.2: per-task conflict close counter (validate int values on load)
            task_conflict_attempts={
                k: int(v)
                for k, v in data.get("task_conflict_attempts", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            # E8: CI-fail terminal path counters
            pr_ci_fail_cycles={
                k: int(v)
                for k, v in data.get("pr_ci_fail_cycles", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            task_ci_fail_requeues={
                k: int(v)
                for k, v in data.get("task_ci_fail_requeues", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            # E8: Cross-regeneration ceiling counters
            task_total_builds={
                k: int(v)
                for k, v in data.get("task_total_builds", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            title_total_builds={
                k: int(v)
                for k, v in data.get("title_total_builds", {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
            # E8: lifetime metrics
            pr_ci_fail_requeues=data.get("pr_ci_fail_requeues", 0),
            pr_ci_fail_escalations=data.get("pr_ci_fail_escalations", 0),
            cross_regen_ceiling_blocks=data.get("cross_regen_ceiling_blocks", 0),
            # WS-120: Queue auto-fill metrics
            last_silence_check=data.get("last_silence_check", ""),
            silence_alerts_raised=data.get("silence_alerts_raised", 0),
            last_acceptance_run=data.get("last_acceptance_run", ""),
            acceptance_runs=data.get("acceptance_runs", 0),
            last_queue_autofill=data.get("last_queue_autofill", ""),
            queue_autofill_runs=data.get("queue_autofill_runs", 0),
            queue_autofill_tasks_generated=data.get(
                "queue_autofill_tasks_generated", 0
            ),
            last_backlog_generation=data.get("last_backlog_generation", ""),
            backlog_generation_runs=data.get("backlog_generation_runs", 0),
            backlog_tasks_generated=data.get("backlog_tasks_generated", 0),
        )


# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------


def setup_logging(config: DaemonConfig) -> None:
    """Configure logging for the daemon.

    Args:
        config: Daemon configuration with log settings.
    """
    global logger

    # Ensure log directory exists
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    # Configure root logger
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    # WS-059: Human-friendly log formatter.
    # Translates internal P-codes and technical prefixes into readable labels
    # so potential customers watching logs understand what the daemon is doing.
    _FRIENDLY_LABELS = {
        "P25: ": "Goals: ",
        "P28 routed task": "Routed task",
        "P28: ": "Router: ",
        "P30: ": "Self-Improvement: ",
        "P32: ": "Post-Task: ",
        "P34: ": "Ideation: ",
        "P36: ": "Efficiency: ",
        "P38: ": "Subsystems: ",
        "P41: ": "Feedback: ",
        "P43: ": "Social: ",
        "P47: ": "Reports: ",
        "P50: ": "Task Identity: ",
        "P51: ": "Output Check: ",
        "P52: ": "Deliverable Check: ",
        "P53: ": "Attribution: ",
        "P75: ": "Git Capture: ",
        "P84: ": "Agent Teams: ",
        "P87: ": "Queue Update: ",
        "P88: ": "PR Validation: ",
        "P89: ": "Test Scope: ",
        "[1/3 Maintenance]": "[Maintain]",
        "[2/3 Scheduling]": "[Schedule]",
        "[3/3 Execute]": "[Execute]",
        "[DISCOVERY]": "[Discover]",
    }

    class _FriendlyFormatter(logging.Formatter):
        def format(self, record):
            msg = record.getMessage()
            for code, label in _FRIENDLY_LABELS.items():
                if code in msg:
                    msg = msg.replace(code, label)
                    break  # One replacement per message
            record.msg = msg
            record.args = None  # Prevent double-formatting
            return super().format(record)

    formatter = _FriendlyFormatter(
        fmt='{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
        '"component": "%(name)s", "message": "%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # File handler with rotation (10MB per file, 5 backups)
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    # Configure logger
    logger = logging.getLogger("forge_daemon")
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.propagate = False

    # Also log to stderr if not daemonized
    if not config.daemonize:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


def format_task_log(result: dict, action: str) -> str:
    """Format a detailed, human-readable log message for task execution.

    Args:
        result: Result dict from poll_and_execute_once
        action: The action type (executed, failed, escalated, etc.)

    Returns:
        Formatted log message with task details
    """
    task = result.get("task", {})
    task_id = result.get("task_id", "unknown")

    # Get task title, truncate if too long
    title = task.get("title", task.get("description", "No description"))
    if len(title) > 60:
        title = title[:57] + "..."

    # Get employee info
    emp_activation = result.get("employee_activation", {})
    employee_id = emp_activation.get("employee_id", "unknown")

    # Get execution details if available
    context = emp_activation.get("context_loaded", {})
    memory_updated = emp_activation.get("memory_updated", False)

    # Build the message based on action
    if action == "executed":
        parts = [
            f"✓ COMPLETED: {title}",
            f"  Employee: {employee_id}",
            f"  Task ID: {task_id}",
        ]
        if context:
            parts.append(
                f"  Context: {context.get('memory_chars', 0):,} memory chars loaded"
            )
        if memory_updated:
            parts.append("  Memory: updated")
        # WS-049: Log git capture result
        git_capture = emp_activation.get("git_capture", {})
        if git_capture.get("captured"):
            n_files = len(git_capture.get("files_changed", []))
            branch = git_capture.get("branch", "?")
            pr_url = git_capture.get("pr_url", "")
            parts.append(f"  Git: {n_files} files → {branch}")
            if pr_url:
                parts.append(f"  PR: {pr_url}")
        return " | ".join([parts[0]] + [p.strip() for p in parts[1:]])

    elif action == "failed":
        error = result.get("reason", "Unknown error")
        return f"✗ FAILED: {title} | Employee: {employee_id} | Error: {error} | Task ID: {task_id}"

    elif action == "escalated":
        reason = result.get("reason", "Unknown")
        return f"⚠ ESCALATED: {title} | Reason: {reason} | Task ID: {task_id}"

    elif action == "blocked":
        reason = result.get("reason", "deliverable gate")
        return (
            f"⏸ HELD (manual review): {title} | Reason: {reason} | Task ID: {task_id}"
        )

    else:
        return f"{action.upper()}: {title} | Task ID: {task_id}"


# -----------------------------------------------------------------------------
# PID File Management
# -----------------------------------------------------------------------------


def write_pid_file(pid: int, config: DaemonConfig) -> None:
    """Write PID file with daemon state.

    Uses atomic write (temp file + rename) to prevent partial reads.

    Args:
        pid: Process ID to write.
        config: Daemon configuration.
    """
    # Ensure parent directory exists
    config.pid_file.parent.mkdir(parents=True, exist_ok=True)

    state = DaemonState(
        pid=pid,
        started_at=datetime.now(timezone.utc).isoformat(),
        version="1.0",
        config_hash=config.config_hash(),
        last_heartbeat=datetime.now(timezone.utc).isoformat(),
    )

    # Atomic write: write to temp file, then rename
    temp_fd, temp_path = tempfile.mkstemp(
        dir=config.pid_file.parent, prefix=".daemon.pid.", suffix=".tmp"
    )

    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)

        # Set permissions before rename
        os.chmod(temp_path, 0o644)

        # Atomic rename
        os.rename(temp_path, config.pid_file)

    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def read_pid_file(config: DaemonConfig) -> DaemonState | None:
    """Read and parse PID file.

    Args:
        config: Daemon configuration.

    Returns:
        DaemonState if file exists and is valid, None otherwise.
    """
    if not config.pid_file.exists():
        return None

    try:
        with open(config.pid_file, encoding="utf-8") as f:
            data = json.load(f)
        return DaemonState.from_dict(data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def remove_pid_file(config: DaemonConfig) -> None:
    """Remove PID file.

    Args:
        config: Daemon configuration.
    """
    try:
        config.pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID exists.

    Uses os.kill(pid, 0) which checks process existence without sending a signal.
    Returns True if the process is alive, False if it doesn't exist.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission — treat as alive
        return True
    except OSError:
        return False


# A daemon can only have started after the machine booted; anything earlier is
# a PID file from a previous boot. The slack absorbs clock adjustments after boot.
_BOOT_TIME_TOLERANCE = timedelta(minutes=5)


def system_boot_time() -> datetime | None:
    """Return the current boot time (UTC), or None if it cannot be determined.

    Lets the PID checks recognise a PID file written before the current boot:
    the process it names cannot have survived the reboot, even when the PID
    number is alive again under a different process.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            # "{ sec = 1787894826, usec = 462321 } Fri Aug 28 07:27:06 2026"
            _, found, rest = out.partition("sec =")
            secs = rest.split(",", 1)[0].strip() if found else ""
            if secs.isdigit():
                return datetime.fromtimestamp(int(secs), tz=timezone.utc)
        elif sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("btime "):
                        return datetime.fromtimestamp(
                            int(line.split()[1]), tz=timezone.utc
                        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def is_daemon_state_alive(
    state: DaemonState, *, boot_time: datetime | None = None
) -> bool:
    """Whether the daemon recorded in ``state`` is still the running process.

    ``os.kill(pid, 0)`` alone is not enough. The daemon is one of the first
    processes launchd starts after a boot, so it gets a low PID — and the next
    boot hands that same PID to some other early process. On 2026-08-28 PID 362
    belonged to ``autofsd`` while daemon.pid still named it, and every start
    attempt exited "already running" for hours. A daemon whose ``started_at``
    predates the current boot cannot be alive, whatever the PID says now.

    Falls back to plain PID liveness when the boot time or ``started_at`` is
    unknown, so an unreadable clock can never make two daemons start.
    """
    if not is_process_alive(state.pid):
        return False
    boot = boot_time if boot_time is not None else system_boot_time()
    if boot is None:
        return True
    try:
        started = datetime.fromisoformat(state.started_at)
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started >= boot - _BOOT_TIME_TOLERANCE


def describe_stale_pid(state: DaemonState) -> str:
    """Explain, for logs, why a PID file is being treated as stale."""
    if is_process_alive(state.pid):
        return (
            f"PID {state.pid} is alive but was recorded before the current boot "
            f"(started {state.started_at}) — the PID was reused by another process"
        )
    return f"PID {state.pid} not running"


def is_daemon_running(config: DaemonConfig) -> bool:
    """Check if daemon is currently running.

    Verifies both that PID file exists and that the process is actually running.
    Handles stale PID files by returning False if process doesn't exist.

    Args:
        config: Daemon configuration.

    Returns:
        True if daemon is running, False otherwise.
    """
    state = read_pid_file(config)

    if state is None:
        return False

    # Verify process is actually running — and is not a reused PID from a
    # previous boot.
    return is_daemon_state_alive(state)


# -----------------------------------------------------------------------------
# Heartbeat Management
# -----------------------------------------------------------------------------


def update_heartbeat(
    config: DaemonConfig,
    state: DaemonState,
    registry: Any = None,
) -> None:
    """Update heartbeat file with current state.

    Args:
        config: Daemon configuration.
        state: Current daemon state.
        registry: Optional SubsystemRegistry for aggregated heartbeat fields.
            When provided, subsystem fields are collected from the registry
            instead of being listed manually.
    """
    # Ensure parent directory exists
    config.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    state.last_heartbeat = now.isoformat()

    # Calculate uptime
    try:
        started = datetime.fromisoformat(state.started_at.replace("Z", "+00:00"))
        uptime_seconds = (now - started).total_seconds()
    except (ValueError, TypeError):
        uptime_seconds = 0

    # Base fields (always present, not owned by any subsystem)
    heartbeat_data: dict[str, Any] = {
        "last_heartbeat": state.last_heartbeat,
        "status": "running",
        "uptime_seconds": int(uptime_seconds),
    }

    # Subsystem fields — from registry if available, otherwise inline
    if registry is not None:
        heartbeat_data.update(registry.aggregate_heartbeat())
    else:
        # Legacy fallback: manual field listing (kept for backward compat
        # until all callers pass a registry)
        heartbeat_data.update(
            {
                "current_task": state.current_task,
                "tasks_completed_this_session": state.tasks_completed,
                "tasks_failed_this_session": state.tasks_failed,
                "poll_count": state.tasks_completed + state.tasks_failed,
                "last_proactive_scan": state.last_proactive_scan,
                "proactive_proposals_created": state.proactive_proposals_created,
                "cross_project_tasks_routed": state.cross_project_tasks_routed,
                "cross_project_tasks_queued": state.cross_project_tasks_queued,
                "last_rebalance_check": state.last_rebalance_check,
                "rebalance_proposals_created": state.rebalance_proposals_created,
                "last_roadmap_scan": state.last_roadmap_scan,
                "roadmap_tasks_scheduled": state.roadmap_tasks_scheduled,
                "roadmap_tasks_completed": state.roadmap_tasks_completed,
                "roadmap_current_wave": state.roadmap_current_wave,
                "tasks_planned": state.tasks_planned,
                "tasks_direct": state.tasks_direct,
                "planning_failures": state.planning_failures,
                "last_session_snapshot": state.last_session_snapshot,
                "last_goal_refresh": state.last_goal_refresh,
                "last_queue_reorder": state.last_queue_reorder,
                "p25_schedule_mode": state.p25_schedule_mode,
                "p25_throttle_level": state.p25_throttle_level,
                "p25_outcomes_recorded": state.p25_outcomes_recorded,
                "p25_auto_approvals": state.p25_auto_approvals,
                "p25_queue_reorders": state.p25_queue_reorders,
                "last_executive_loop": state.last_executive_loop,
                "executive_sessions_run": state.executive_sessions_run,
                "work_submitted_by_executives": state.work_submitted_by_executives,
                "last_strategic_planning": state.last_strategic_planning,
                "strategic_initiatives_created": state.strategic_initiatives_created,
                "strategic_tasks_queued": state.strategic_tasks_queued,
                "last_weekly_planning": state.last_weekly_planning,
                "last_daily_planning": state.last_daily_planning,
                "weekly_planning_runs": state.weekly_planning_runs,
                "daily_planning_runs": state.daily_planning_runs,
                "last_improvement_cycle": state.last_improvement_cycle,
                "improvement_cycles_run": state.improvement_cycles_run,
                "last_employee_ideation": state.last_employee_ideation,
                "employee_ideation_cycles": state.employee_ideation_cycles,
                "ideas_generated": state.ideas_generated,
                "last_auto_merge_check": state.last_auto_merge_check,
                "auto_merge_checks": state.auto_merge_checks,
                "prs_merged": state.prs_merged,
                "last_feedback_check": state.last_feedback_check,
                "feedback_checks_run": state.feedback_checks_run,
                "feedback_actions_taken": state.feedback_actions_taken,
            }
        )

    # G11 Queue health snapshot — queue_depth, blocked_ratio, throughput_per_hour
    try:
        _ensure_imports()
        _company_dir = company_resolver.get_company_dir()
        queue_file = _company_dir / "state/work_queue.json"
        with open(queue_file, encoding="utf-8") as _qf:
            _q = json.load(_qf)
        _pending = len(_q.get("pending", []))
        _in_progress = len(_q.get("in_progress", []))
        _blocked = len(_q.get("blocked", []))
        _total = _pending + _in_progress + _blocked
        _blocked_ratio = round(_blocked / _total, 3) if _total > 0 else 0.0
        _hours = max(uptime_seconds / 3600, 1 / 3600)  # avoid div-by-zero
        _completed = heartbeat_data.get(
            "tasks_completed_this_session", state.tasks_completed
        )
        heartbeat_data.update(
            {
                "queue_depth": _pending,
                "queue_in_progress": _in_progress,
                "queue_blocked": _blocked,
                "queue_blocked_ratio": _blocked_ratio,
                "throughput_per_hour": round(_completed / _hours, 2),
            }
        )
    except Exception as exc:
        logger = logging.getLogger(__name__)
        logger.debug("G11 queue health snapshot skipped: %s", exc)

    # WS-057-004: Report actual active worker thread count for G12 assessment
    try:
        if hasattr(state, "_active_workers"):
            heartbeat_data["active_workers"] = len(state._active_workers)
        else:
            heartbeat_data["active_workers"] = 0
    except Exception:
        heartbeat_data["active_workers"] = 0

    # Add circuit breaker state if available
    try:
        _ensure_imports()
        breaker, _ = loop_monitor.load_monitor_state()
        heartbeat_data["circuit_breaker_state"] = breaker.state.value
    except Exception:
        heartbeat_data["circuit_breaker_state"] = "unknown"

    # Atomic write
    temp_fd, temp_path = tempfile.mkstemp(
        dir=config.heartbeat_file.parent, prefix=".daemon.heartbeat.", suffix=".tmp"
    )

    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(heartbeat_data, f, indent=2)

        os.chmod(temp_path, 0o644)
        os.rename(temp_path, config.heartbeat_file)

    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    # Checkpoint live uptime into daemon_metrics.json so G7 can measure
    # sustained uptime even across sessions killed with SIGKILL.
    if _g_metrics_tracker is not None and _g_session_id is not None:
        try:
            _g_metrics_tracker.record_heartbeat(
                _g_session_id,
                uptime_seconds=int(uptime_seconds),
                tasks_completed=state.tasks_completed,
                tasks_failed=state.tasks_failed,
            )
        except Exception:
            pass


def ensure_heartbeat_initialized(config: DaemonConfig) -> None:
    """Write initial heartbeat on daemon startup if file doesn't exist.

    Prevents G11 false failures during the startup window before the
    heartbeat thread or main loop writes the first heartbeat.
    """
    if config.heartbeat_file.exists():
        return

    config.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

    initial_hb = {
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "status": "starting",
        "uptime_seconds": 0,
        "tasks_completed_this_session": 0,
        "tasks_failed_this_session": 0,
    }

    # Atomic write
    temp_fd, temp_path = tempfile.mkstemp(
        dir=config.heartbeat_file.parent,
        prefix=".daemon.heartbeat.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(initial_hb, f, indent=2)
        os.chmod(temp_path, 0o644)
        os.rename(temp_path, config.heartbeat_file)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def is_heartbeat_stale(config: DaemonConfig, max_age_seconds: int = 120) -> bool:
    """Check if the heartbeat file is stale (daemon may be hung or dead).

    Args:
        config: Daemon configuration with heartbeat_file path.
        max_age_seconds: Maximum age in seconds before the heartbeat is
            considered stale. Default is 120s (2× the 60s write interval).

    Returns:
        True if the heartbeat is missing or older than max_age_seconds.
        False if the heartbeat is fresh (daemon appears healthy).
    """
    if not config.heartbeat_file.exists():
        return True

    try:
        with open(config.heartbeat_file, encoding="utf-8") as f:
            data = json.load(f)

        last_hb = data.get("last_heartbeat")
        if not last_hb:
            return True

        last_ts = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last_ts).total_seconds()
        return age > max_age_seconds

    except (json.JSONDecodeError, OSError, ValueError):
        return True


# -----------------------------------------------------------------------------
# Signal Handling
# -----------------------------------------------------------------------------


_sigterm_count: int = 0
_sigterm_first_time: float = 0.0
_SIGTERM_THRESHOLD: int = 5  # After 5 SIGTERMs in 60s, actually shut down
_SIGTERM_WINDOW: float = 60.0  # Time window for counting SIGTERMs

# Stop ordering (2026-09-25). `forge_daemon.py stop` writes this marker BEFORE
# it unloads the LaunchAgent. `launchctl unload` delivers SIGTERM and then
# blocks until the job exits or launchd SIGKILLs it at the plist's ExitTimeOut
# (300 s). Because WS-110 ignores SIGTERM, every `stop` sat out the full 300 s
# and ended in SIGKILL: no worker wait, no worktree cleanup, no clean pid file.
# A SIGTERM that arrives while a fresh marker exists is the operator's stop,
# not a spurious launchd signal, so it is honoured as a graceful shutdown.
_stop_marker_file: Path | None = None
STOP_MARKER_MAX_AGE_SECONDS: float = 900.0


def _stop_marker_path(config: "DaemonConfig") -> Path:
    """Where `stop` leaves its marker: beside the pid file."""
    return config.pid_file.parent / "daemon.stop_requested"


def _stop_marker_is_fresh(marker: Path | None) -> bool:
    if marker is None:
        return False
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    return age <= STOP_MARKER_MAX_AGE_SECONDS


def _sleep_unless_shutdown(seconds: float, step: float = 1.0) -> None:
    """Sleep up to ``seconds``, returning within ``step`` of a shutdown request.

    A bare ``time.sleep`` is resumed after a Python signal handler returns
    (PEP 475), so a SIGUSR2 that arrived during the idle backoff (up to ~30
    min) went unseen until the sleep ran out.
    """
    # Nominal accounting (not a monotonic deadline) so a test that stubs
    # time.sleep does not turn this into a busy-wait.
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not _shutdown_requested:
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _handle_sigterm(signum: int, frame: Any) -> None:
    """WS-110/WS-117: Handle SIGTERM with smart fallback.

    Ignores the first few spurious SIGTERMs from launchd, but if we receive
    multiple SIGTERMs in a short window, assume it's intentional and shut down
    gracefully to avoid SIGKILL. A SIGTERM while `stop`'s marker is fresh is
    the operator's stop (launchctl unload) and shuts down gracefully at once.
    """
    global _sigterm_count, _sigterm_first_time, _shutdown_requested
    import time

    if _stop_marker_is_fresh(_stop_marker_file):
        _shutdown_requested = True
        logger.info(
            "Received SIGTERM with a stop marker present - initiating graceful shutdown"
        )
        return

    now = time.time()

    # Reset counter if outside the time window
    if _sigterm_first_time == 0.0 or (now - _sigterm_first_time) > _SIGTERM_WINDOW:
        _sigterm_count = 0
        _sigterm_first_time = now

    _sigterm_count += 1

    if _sigterm_count >= _SIGTERM_THRESHOLD:
        # Too many SIGTERMs - this is likely intentional, shut down gracefully
        logger.warning(
            f"WS-117: Received {_sigterm_count} SIGTERMs in {_SIGTERM_WINDOW}s "
            "- initiating graceful shutdown to avoid SIGKILL"
        )
        _shutdown_requested = True
    else:
        logger.warning(
            f"WS-110: Ignoring SIGTERM #{_sigterm_count} (launchd workaround)"
        )


def _handle_sigusr2(signum: int, frame: Any) -> None:
    """Handle SIGUSR2 - initiate graceful shutdown (explicit stop command)."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Received SIGUSR2 - initiating graceful shutdown")


def _handle_sighup(signum: int, frame: Any) -> None:
    """Handle SIGHUP - reload configuration."""
    global _reload_requested
    _reload_requested = True
    logger.info("Received SIGHUP - will reload configuration")


def _handle_sigusr1(signum: int, frame: Any) -> None:
    """Handle SIGUSR1 - write status to log."""
    global _status_requested
    _status_requested = True
    logger.info("Received SIGUSR1 - will write status")


def setup_signal_handlers(stop_marker: Path | None = None) -> None:
    """Set up signal handlers for daemon management.

    WS-110: SIGTERM is IGNORED because launchd sends spurious SIGTERMs.
    Use SIGUSR2 for intentional shutdown.

    Args:
        stop_marker: The file `stop` writes before unloading the LaunchAgent;
            a SIGTERM while it is fresh is honoured (see _handle_sigterm).

    Signals:
        SIGTERM -> IGNORED (launchd workaround), unless the stop marker is fresh
        SIGINT  -> Graceful shutdown (Ctrl+C)
        SIGUSR2 -> Graceful shutdown (explicit stop)
        SIGHUP  -> Reload configuration
        SIGUSR1 -> Write status to log
    """
    global _stop_marker_file
    _stop_marker_file = stop_marker
    signal.signal(signal.SIGTERM, _handle_sigterm)  # WS-110: ignored
    signal.signal(signal.SIGINT, _handle_sigusr2)  # Ctrl+C works
    signal.signal(signal.SIGUSR2, _handle_sigusr2)  # Explicit stop
    signal.signal(signal.SIGHUP, _handle_sighup)
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    logger.debug("Signal handlers installed (WS-110: SIGTERM ignored)")


# -----------------------------------------------------------------------------
# Daemonization (Unix Double-Fork Pattern)
# -----------------------------------------------------------------------------


def daemonize() -> int:
    """Daemonize the process using the Unix double-fork pattern.

    This detaches the process from the terminal and creates a new session.

    Returns:
        Child PID (in parent process after first fork, 0 in daemon process).

    Note:
        This function only works on Unix-like systems.
    """
    # First fork: detach from parent
    try:
        pid = os.fork()
        if pid > 0:
            # Parent process - return child PID
            return pid
    except OSError as e:
        logger.error(f"First fork failed: {e}")
        sys.exit(1)

    # Child process continues
    # Create new session (become session leader)
    os.setsid()

    # Second fork: prevent reacquiring terminal
    try:
        pid = os.fork()
        if pid > 0:
            # First child exits
            sys.exit(0)
    except OSError as e:
        logger.error(f"Second fork failed: {e}")
        sys.exit(1)

    # Daemon process continues
    # Change working directory to root to avoid locking mount points
    # (We stay in project root instead for operation_loop access)

    # Reset file creation mask
    os.umask(0o022)

    # Redirect standard file descriptors to /dev/null
    sys.stdin = open("/dev/null", "r")
    sys.stdout = open("/dev/null", "w")
    sys.stderr = open("/dev/null", "w")

    return 0


# -----------------------------------------------------------------------------
# Proactive Scanning
# -----------------------------------------------------------------------------


def _should_run_proactive_scan(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run a proactive scan.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if a proactive scan should be run.
    """
    if not config.proactive_enabled:
        return False

    if not state.last_proactive_scan:
        return True  # Never scanned before

    try:
        last_scan = datetime.fromisoformat(
            state.last_proactive_scan.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_scan).total_seconds()
        return elapsed >= (config.proactive_scan_interval_minutes * 60)
    except (ValueError, TypeError):
        return True


def _run_proactive_scan(state: DaemonState) -> int:
    """Run a proactive initiative scan.

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Number of proposals created/processed.
    """
    _ensure_imports()
    if initiative_engine is None:  # not in the public export
        return 0

    try:
        # Scan for opportunities
        proposals = initiative_engine.scan_all_opportunities()

        if not proposals:
            logger.debug("Proactive scan: no opportunities detected")
            state.last_proactive_scan = datetime.now(timezone.utc).isoformat()
            return 0

        logger.info(f"Proactive scan: found {len(proposals)} opportunities")

        # Execute batch (respects auto-approve settings and rate limits)
        results = initiative_engine.execute_proposal_batch(proposals)

        approved = sum(1 for r in results if r.approved)
        pending = sum(1 for r in results if not r.approved)

        logger.info(f"Proactive scan complete: {approved} approved, {pending} pending")

        # Update state
        state.last_proactive_scan = datetime.now(timezone.utc).isoformat()
        state.proactive_proposals_created += approved

        return approved

    except Exception as e:
        logger.error(f"Proactive scan failed: {e}")
        # Still update scan time to avoid retry storm
        state.last_proactive_scan = datetime.now(timezone.utc).isoformat()
        return 0


# -----------------------------------------------------------------------------
# Strategic Planning (P15)
# -----------------------------------------------------------------------------


def _should_run_strategic_planning(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run strategic planning.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if strategic planning should run.
    """
    if not config.strategic_planning_enabled:
        return False

    if not state.last_strategic_planning:
        return True  # Never run before

    try:
        last_run = datetime.fromisoformat(
            state.last_strategic_planning.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.strategic_planning_interval_hours
    except (ValueError, TypeError):
        return True


def _run_strategic_planning(state: DaemonState) -> dict[str, int]:
    """Run the strategic planning cycle.

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with counts: initiatives_created, tasks_queued
    """
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()

        # Run full planning cycle (lightweight=True: skip pytest, use cached data)
        result = strategic_planner.run_planning_cycle(company_dir, lightweight=True)

        logger.info(
            f"Strategic planning: {result['initiatives_proposed']} initiatives proposed, "
            f"{result['initiatives_auto_approved']} auto-approved, "
            f"{result['tasks_queued']} tasks queued"
        )

        # Update state
        state.last_strategic_planning = datetime.now(timezone.utc).isoformat()
        state.strategic_initiatives_created += result["initiatives_proposed"]
        state.strategic_tasks_queued += result["tasks_queued"]

        return {
            "initiatives_created": result["initiatives_proposed"],
            "tasks_queued": result["tasks_queued"],
        }

    except Exception as e:
        logger.error(f"Strategic planning failed: {e}")
        # Still update time to avoid retry storm
        state.last_strategic_planning = datetime.now(timezone.utc).isoformat()
        return {"initiatives_created": 0, "tasks_queued": 0}


# -----------------------------------------------------------------------------
# P30: Self-Improvement Cycle
# -----------------------------------------------------------------------------


def _should_run_improvement_cycle(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run improvement cycle.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if improvement cycle should run.
    """
    if not config.improvement_enabled:
        return False

    if not state.last_improvement_cycle:
        return True  # Never run before

    try:
        last_run = datetime.fromisoformat(
            state.last_improvement_cycle.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.improvement_cycle_interval_hours
    except (ValueError, TypeError):
        return True


def _run_improvement_cycle(state: DaemonState) -> dict[str, Any]:
    """Run the self-improvement cycle.

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with cycle results (detections_count, proposals_generated, etc.).
    """
    try:
        # Lazy import to avoid circular dependency
        # Use direct import (not relative) since daemon runs as standalone script
        import self_improvement_loop as sil  # type: ignore[no-redef]

        run_improvement_cycle = sil.run_improvement_cycle
        should_run_improvement_cycle = sil.should_run_improvement_cycle

        # Double-check with the loop's own should_run check
        should_run, reason = should_run_improvement_cycle()
        if not should_run:
            logger.info(f"Improvement cycle skipped: {reason}")
            # Update timestamp to prevent retry every cycle
            state.last_improvement_cycle = datetime.now(timezone.utc).isoformat()
            return {"skipped": True, "reason": reason}

        logger.info("Running self-improvement cycle...")
        result = run_improvement_cycle(dry_run=False)

        logger.info(
            f"Improvement cycle complete: {result.detections_count} detections, "
            f"{result.proposals_generated} proposals generated, "
            f"{result.proposals_submitted} submitted"
        )

        # Update state
        state.last_improvement_cycle = datetime.now(timezone.utc).isoformat()
        state.improvement_cycles_run += 1

        return {
            "detections_count": result.detections_count,
            "proposals_generated": result.proposals_generated,
            "proposals_submitted": result.proposals_submitted,
            "proposals_auto_approved": result.proposals_auto_approved,
            "proposals_pending_human": result.proposals_pending_human,
            "errors": result.errors,
        }

    except ImportError as e:
        logger.warning(f"Self-improvement module not available: {e}")
        return {"error": "module_not_available", "message": str(e)}
    except Exception as e:
        logger.error(f"Improvement cycle failed: {e}")
        # Still update time to avoid retry storm
        state.last_improvement_cycle = datetime.now(timezone.utc).isoformat()
        return {"error": "cycle_failed", "message": str(e)}


# -----------------------------------------------------------------------------
# P41: Feedback Monitor
# -----------------------------------------------------------------------------


def _should_run_feedback_check(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if feedback monitor should run based on interval.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if feedback check should run.
    """
    if not config.feedback_monitor_enabled:
        return False
    if not state.last_feedback_check:
        return True
    try:
        last = datetime.fromisoformat(state.last_feedback_check.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= config.feedback_monitor_interval_minutes * 60
    except (ValueError, TypeError):
        return True


def _run_feedback_check(state: DaemonState, company_dir: Path | None = None) -> dict:
    """Run feedback monitor check.

    Args:
        state: Current daemon state (will be updated).
        company_dir: Path to company directory. If None, resolved automatically.

    Returns:
        Dict with check results (actions_taken, details, etc.).
    """
    try:
        from feedback_monitor import FeedbackMonitor
    except ImportError:
        try:
            import sys as _sys

            _sys.path.insert(0, str(Path(__file__).parent))
            from feedback_monitor import FeedbackMonitor
        except ImportError:
            return {"error": "feedback_monitor not available"}

    if company_dir is None:
        _ensure_imports()
        try:
            company_dir = company_resolver.get_company_dir()
        except Exception as e:
            logger.warning(f"Could not resolve company dir for feedback check: {e}")
            return {"error": f"company_dir resolution failed: {e}"}

    try:
        monitor = FeedbackMonitor(company_dir)
        result = monitor.check_and_respond()

        # Update state
        state.last_feedback_check = datetime.now(timezone.utc).isoformat()
        state.feedback_checks_run += 1
        state.feedback_actions_taken += result.get("actions_taken", 0)

        return result

    except Exception as e:
        logger.error(f"Feedback monitor check failed: {e}")
        # Still update time to avoid retry storm
        state.last_feedback_check = datetime.now(timezone.utc).isoformat()
        return {"error": "check_failed", "message": str(e)}


# -----------------------------------------------------------------------------
# P43: Cron Scheduler
# -----------------------------------------------------------------------------


def _should_run_cron_check(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if cron scheduler should run based on interval.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if cron check should run.
    """
    if not config.cron_scheduler_enabled:
        return False

    # Check if cron scheduler module is available
    if cron_scheduler_mod is None:
        return False

    if not state.last_cron_check:
        return True

    try:
        last = datetime.fromisoformat(state.last_cron_check.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= config.cron_check_interval_seconds
    except (ValueError, TypeError):
        return True


def _run_cron_check(state: DaemonState, company_dir: Path | None = None) -> dict:
    """Run cron scheduler check to execute due tasks.

    Args:
        state: Current daemon state (will be updated).
        company_dir: Path to company directory. If None, resolved automatically.

    Returns:
        Dict with check results (tasks_executed, task_details, etc.).
    """
    _ensure_imports()

    # Check if cron scheduler module is available
    if cron_scheduler_mod is None:
        return {"error": "cron_scheduler not available", "tasks_executed": 0}

    if company_dir is None:
        try:
            company_dir = company_resolver.get_company_dir()
        except Exception as e:
            logger.warning(f"Could not resolve company dir for cron check: {e}")
            return {"error": f"company_dir resolution failed: {e}", "tasks_executed": 0}

    try:
        scheduler = cron_scheduler_mod.CronScheduler(company_dir)

        # Register content generation executor (WS-056-002)
        try:
            try:
                from . import social_content_generator as scg_mod
            except ImportError:
                import social_content_generator as scg_mod  # type: ignore[no-redef]
            scheduler.register_executor(
                "generate_content", scg_mod.content_generation_executor
            )
        except Exception as _exc:
            logger.warning(f"Could not register generate_content executor: {_exc}")

        results = scheduler.run_once()

        # Count successful executions
        tasks_executed = sum(1 for r in results if r.success)
        tasks_failed = sum(1 for r in results if not r.success)

        # Update state
        state.last_cron_check = datetime.now(timezone.utc).isoformat()
        state.cron_checks_run += 1
        state.cron_tasks_executed += tasks_executed

        return {
            "tasks_executed": tasks_executed,
            "tasks_failed": tasks_failed,
            "total_checked": len(results),
            "task_details": [
                {
                    "task_id": r.task_id,
                    "success": r.success,
                    "message": r.message,
                }
                for r in results
            ],
        }

    except Exception as e:
        logger.error(f"Cron scheduler check failed: {e}")
        # Still update time to avoid retry storm
        state.last_cron_check = datetime.now(timezone.utc).isoformat()
        return {"error": "check_failed", "message": str(e), "tasks_executed": 0}


# -----------------------------------------------------------------------------
# P47: Artifact Generation (daily report + social content)
# -----------------------------------------------------------------------------


def _should_run_artifact_generation(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to generate daily artifacts (report + content drafts).

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if artifact generation should run.
    """
    if not config.artifact_generation_enabled:
        return False

    if not state.last_artifact_generation:
        return True  # Never run before

    try:
        last_run = datetime.fromisoformat(
            state.last_artifact_generation.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.artifact_generation_interval_hours
    except (ValueError, TypeError):
        return True


def _run_artifact_generation(
    state: DaemonState, company_dir: Path | None = None
) -> dict:
    """Generate daily report and social content drafts.

    Args:
        state: Current daemon state (will be updated).
        company_dir: Path to company directory. If None, resolved automatically.

    Returns:
        Dict with generation results (report_path, drafts_count, etc.).
    """
    _ensure_imports()

    if company_dir is None:
        try:
            company_dir = company_resolver.get_company_dir()
        except Exception as e:
            logger.warning(
                f"Could not resolve company dir for artifact generation: {e}"
            )
            return {"error": f"company_dir resolution failed: {e}"}

    result: dict[str, Any] = {"report_path": None, "drafts_count": 0}

    # Generate daily report
    try:
        from . import daily_report as dr_mod
    except ImportError:
        try:
            import daily_report as dr_mod  # type: ignore[no-redef]
        except ImportError:
            dr_mod = None  # type: ignore[assignment]

    if dr_mod is not None:
        try:
            report_path = dr_mod.generate_daily_report(company_dir)
            result["report_path"] = str(report_path)
            logger.info(f"P47: Daily report generated: {report_path}")
        except Exception as e:
            logger.error(f"P47: Daily report generation failed: {e}")
            result["report_error"] = str(e)

    # Generate social content drafts
    try:
        from . import social_content_generator as scg_mod
    except ImportError:
        try:
            import social_content_generator as scg_mod  # type: ignore[no-redef]
        except ImportError:
            scg_mod = None  # type: ignore[assignment]

    if scg_mod is not None:
        try:
            drafts = scg_mod.generate_content_drafts(company_dir)
            result["drafts_count"] = len(drafts)
            logger.info(f"P47: Generated {len(drafts)} social content drafts")
        except Exception as e:
            logger.error(f"P47: Social content generation failed: {e}")
            result["drafts_error"] = str(e)

    # Update state
    state.last_artifact_generation = datetime.now(timezone.utc).isoformat()
    state.artifact_reports_generated += 1

    return result


# -----------------------------------------------------------------------------
# P18: Adaptive Intervals
# -----------------------------------------------------------------------------


def _should_check_adaptive_intervals(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to check adaptive interval recommendations.

    Adaptive intervals are checked once per day (24 hours) when enabled.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if we should check for interval recommendations.
    """
    if not config.adaptive_intervals_enabled:
        return False

    # Check if first run
    if not state.last_adaptive_check:
        return True

    # Check if 24 hours have elapsed since last check
    try:
        last_check = datetime.fromisoformat(
            state.last_adaptive_check.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_check).total_seconds() / 3600
        return elapsed_hours >= 24.0  # Check once per day
    except (ValueError, TypeError):
        return True


def _check_adaptive_intervals(
    config: DaemonConfig, state: DaemonState
) -> dict[str, Any]:
    """Check and optionally apply adaptive interval recommendations.

    Uses interval_learner to analyze session history and recommend optimal
    executive loop intervals. Auto-applies recommendations if:
    - recommendation.auto_apply is True
    - recommendation.confidence > config.adaptive_intervals_auto_apply_threshold

    Args:
        config: Daemon configuration.
        state: Current daemon state (will be updated).

    Returns:
        Dict with recommendation results and actions taken.
    """
    result = {
        "checked": True,
        "recommendation": None,
        "applied": False,
        "reason": "",
    }

    try:
        # Lazy import interval_learner
        try:
            from . import interval_learner
        except ImportError:
            import interval_learner  # type: ignore[no-redef]

        # Get current interval from config
        current_interval = config.executive_loop_interval_hours

        # Get recommendation
        recommendation = interval_learner.get_interval_recommendation(
            current_interval=float(current_interval),
            limit=100,
        )

        # Always log the recommendation
        result["recommendation"] = recommendation.to_dict()
        state.adaptive_intervals_recommendations += 1

        logger.info(
            f"P18 Adaptive intervals: recommendation received - "
            f"current={recommendation.current_interval_hours}h, "
            f"suggested={recommendation.suggested_interval_hours}h, "
            f"confidence={recommendation.confidence:.2f}, "
            f"auto_apply={recommendation.auto_apply}"
        )

        # Log reasoning for transparency
        if recommendation.reasoning:
            logger.info(f"P18 Adaptive intervals: {recommendation.reasoning}")

        # Check if we should auto-apply
        should_apply = (
            recommendation.auto_apply
            and recommendation.confidence
            >= config.adaptive_intervals_auto_apply_threshold
            and recommendation.suggested_interval_hours != current_interval
        )

        if should_apply:
            # Apply the new interval by updating gatesmith.json
            config_path = Path("gatesmith.json")
            if config_path.exists():
                try:
                    with open(config_path, encoding="utf-8") as f:
                        config_data = json.load(f)

                    # Update executiveLoop.intervalHours
                    if "executiveLoop" not in config_data:
                        config_data["executiveLoop"] = {}
                    old_interval = config_data["executiveLoop"].get(
                        "intervalHours", current_interval
                    )
                    config_data["executiveLoop"]["intervalHours"] = int(
                        recommendation.suggested_interval_hours
                    )

                    # Also update the in-memory config
                    config.executive_loop_interval_hours = int(
                        recommendation.suggested_interval_hours
                    )

                    # Atomic write
                    temp_fd, temp_path = tempfile.mkstemp(
                        dir=config_path.parent,
                        prefix=".gatesmith-config.",
                        suffix=".tmp",
                    )
                    try:
                        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                            json.dump(config_data, f, indent=2)
                        os.replace(temp_path, config_path)
                    except Exception:
                        try:
                            os.unlink(temp_path)
                        except OSError:
                            pass
                        raise

                    result["applied"] = True
                    result["reason"] = (
                        f"Auto-applied: {old_interval}h -> "
                        f"{recommendation.suggested_interval_hours}h "
                        f"(confidence={recommendation.confidence:.2f}, "
                        f"savings={recommendation.expected_savings_percent:.1f}%)"
                    )
                    logger.info(
                        f"P18 Adaptive intervals: AUTO-APPLIED interval change "
                        f"{old_interval}h -> {recommendation.suggested_interval_hours}h"
                    )

                except Exception as e:
                    result["reason"] = f"Failed to apply: {e}"
                    logger.warning(
                        f"P18 Adaptive intervals: failed to apply change: {e}"
                    )
            else:
                result["reason"] = "Config file not found"
                logger.warning("P18 Adaptive intervals: gatesmith.json not found")
        else:
            # Log why not applied
            if not recommendation.auto_apply:
                result["reason"] = (
                    "auto_apply=False (insufficient confidence or savings)"
                )
            elif (
                recommendation.confidence
                < config.adaptive_intervals_auto_apply_threshold
            ):
                result["reason"] = (
                    f"confidence {recommendation.confidence:.2f} < "
                    f"threshold {config.adaptive_intervals_auto_apply_threshold}"
                )
            elif recommendation.suggested_interval_hours == current_interval:
                result["reason"] = "suggested interval equals current interval"
            logger.info(f"P18 Adaptive intervals: not applied - {result['reason']}")

    except Exception as e:
        result["checked"] = False
        result["reason"] = f"Error: {e}"
        logger.error(f"P18 Adaptive intervals check failed: {e}")

    # Update state timestamp regardless of outcome
    state.last_adaptive_check = datetime.now(timezone.utc).isoformat()

    return result


# -----------------------------------------------------------------------------
# P29: Weekly/Daily Strategic Planning Cycles
# -----------------------------------------------------------------------------


def _should_run_weekly_planning_cycle(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run weekly planning cycle.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if weekly planning should run.
    """
    if not config.strategic_planning_enabled:
        return False

    if not state.last_weekly_planning:
        return True  # Never run before

    try:
        last_run = datetime.fromisoformat(
            state.last_weekly_planning.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.weekly_planning_interval_hours
    except (ValueError, TypeError):
        return True


def _should_run_daily_planning_cycle(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run daily planning cycle.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if daily planning should run.
    """
    if not config.strategic_planning_enabled:
        return False

    if not state.last_daily_planning:
        return True  # Never run before

    try:
        last_run = datetime.fromisoformat(
            state.last_daily_planning.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.daily_planning_interval_hours
    except (ValueError, TypeError):
        return True


def _run_weekly_planning_cycle(state: DaemonState) -> dict[str, Any]:
    """Run the weekly strategic planning cycle.

    Weekly cycle performs comprehensive planning:
    - Goal assessment and alignment review
    - Resource capacity analysis
    - Long-term initiative planning
    - Cross-project coordination

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with cycle results including goals_reviewed, initiatives_planned, etc.
    """
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()

        # WS-105: Sprint lifecycle management
        # Close current sprint if expired, open new one
        sprint_summary = {}
        try:
            current_sprint = strategic_planner.load_current_sprint(company_dir)
            if current_sprint and current_sprint.status == "active":
                # Check if sprint has expired
                from datetime import datetime as _dt
                from datetime import timezone as _tz

                ends = _dt.fromisoformat(current_sprint.ends_at.replace("Z", "+00:00"))
                if _dt.now(_tz.utc) >= ends:
                    sprint_summary = strategic_planner.close_sprint(company_dir)
                    if sprint_summary:
                        logger.info(
                            f"Sprint {sprint_summary.get('sprint_number')} closed: "
                            f"velocity={sprint_summary.get('velocity')}, "
                            f"completion={sprint_summary.get('completion_rate', 0):.0%}"
                        )
                    # Open new sprint (goal will be set by CTO in next exec loop)
                    new_sprint = strategic_planner.open_new_sprint(company_dir)
                    logger.info(f"Sprint {new_sprint.number} opened: {new_sprint.goal}")
            elif not current_sprint:
                # No sprint exists — create first one
                new_sprint = strategic_planner.open_new_sprint(company_dir)
                logger.info(f"First sprint opened: {new_sprint.id}")
        except Exception as _sprint_err:
            logger.debug(f"Sprint lifecycle: {_sprint_err}")

        # Call strategic_planner.run_weekly_cycle (added by Task 29.4)
        # lightweight=True: skip pytest, use cached coverage data
        result = strategic_planner.run_weekly_cycle(company_dir, lightweight=True)

        # Merge sprint info into result
        if sprint_summary:
            result["sprint_closed"] = sprint_summary

        logger.info(
            f"Weekly planning cycle: {result.get('goals_assessed', 0)} goals reviewed, "
            f"{result.get('initiatives_created', 0)} initiatives planned, "
            f"{result.get('tasks_queued', 0)} tasks generated"
        )

        # Update state
        state.last_weekly_planning = datetime.now(timezone.utc).isoformat()
        state.weekly_planning_runs += 1

        return result

    except Exception as e:
        logger.error(f"Weekly planning cycle failed: {e}")
        # Still update time to avoid retry storm
        state.last_weekly_planning = datetime.now(timezone.utc).isoformat()
        return {
            "goals_reviewed": 0,
            "initiatives_planned": 0,
            "tasks_generated": 0,
            "error": str(e),
        }


def _run_daily_planning_cycle(
    config: DaemonConfig, state: DaemonState
) -> dict[str, Any]:
    """Run the daily strategic planning cycle.

    Daily cycle performs tactical planning:
    - Progress check on active initiatives
    - Task prioritization and queue management
    - Blocker identification and escalation
    - Employee workload balancing

    Args:
        config: Daemon configuration.
        state: Current daemon state (will be updated).

    Returns:
        Dict with cycle results including tasks_prioritized, blockers_found, etc.
    """
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()

        # Call strategic_planner.run_daily_cycle (added by Task 29.4)
        # Only defer when ideation is actually enabled — if disabled it will never run
        # to set last_employee_ideation and the guard would fire permanently.
        defer_effective = (
            config.fill_defer_to_ideation and config.employee_ideation_enabled
        )
        result = strategic_planner.run_daily_cycle(
            company_dir,
            last_ideation=state.last_employee_ideation,
            defer_to_ideation=defer_effective,
        )

        logger.info(
            f"Daily planning cycle: {result.get('daily_tasks_created', 0)} tasks created, "
            f"{result.get('initiatives_updated', 0)} initiatives updated, "
            f"{result.get('active_initiatives', 0)} active"
        )

        # Update state
        state.last_daily_planning = datetime.now(timezone.utc).isoformat()
        state.daily_planning_runs += 1

        return result

    except Exception as e:
        logger.error(f"Daily planning cycle failed: {e}")
        # Still update time to avoid retry storm
        state.last_daily_planning = datetime.now(timezone.utc).isoformat()
        return {
            "tasks_prioritized": 0,
            "blockers_found": 0,
            "escalations": 0,
            "error": str(e),
        }


# -----------------------------------------------------------------------------
# Employee Availability Check
# -----------------------------------------------------------------------------


def _check_employees_available() -> bool:
    """Check if there are any employees available to execute tasks.

    Returns:
        True if employees exist in org.json, False otherwise.
    """
    _ensure_imports()
    try:
        company_dir = company_resolver.get_company_dir()
        org_path = company_dir / "org.json"
        if not org_path.exists():
            return False
        with open(org_path, encoding="utf-8") as f:
            org = json.load(f)
        employees = org.get("employees", [])
        return len(employees) > 0
    except Exception:
        # If we can't check, assume employees exist to allow execution
        return True


# -----------------------------------------------------------------------------
# Executive Loop (P17)
# -----------------------------------------------------------------------------


def _should_run_executive_loop(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run the executive loop.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if executive loop should run.
    """
    if not config.executive_loop_enabled:
        return False

    # Check if executives exist in org.json — skip if employees wiped
    _ensure_imports()
    try:
        company_dir = company_resolver.get_company_dir()
        org_path = company_dir / "org.json"
        if org_path.exists():
            with open(org_path, encoding="utf-8") as f:
                org = json.load(f)
            employees = org.get("employees", [])
            if not employees:
                logger.warning(
                    "Skipping executive loop: org.json has no employees. "
                    "Restore employees with /company-hire or fix org.json."
                )
                return False
    except Exception as e:
        logger.debug(f"Could not check org.json for executives: {e}")

    # Check skipIfQueueAbove - skip if queue has too many pending tasks (WS-009-001)
    # This saves tokens by letting employees work through the queue first
    if config.executive_loop_skip_if_queue_above > 0:
        _ensure_imports()
        try:
            company_dir = company_resolver.get_company_dir()
            queue_path = company_dir / "state/work_queue.json"
            if queue_path.exists():
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
                # Support both queue formats: list-based (queue["tasks"])
                # and dict-based (queue["pending"], queue["in_progress"])
                if "tasks" in queue:
                    pending = sum(
                        1 for t in queue["tasks"] if t.get("status") == "pending"
                    )
                else:
                    pending = len(queue.get("pending", []))
                if pending > config.executive_loop_skip_if_queue_above:
                    logger.info(
                        f"Skipping executive loop: {pending} pending tasks > "
                        f"threshold {config.executive_loop_skip_if_queue_above}"
                    )
                    return False
        except Exception as e:
            logger.debug(f"Could not check queue for skipIfQueueAbove: {e}")

    # Check if first run
    if not state.last_executive_loop:
        return True

    # Check interval
    try:
        last_run = datetime.fromisoformat(
            state.last_executive_loop.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        if elapsed_hours >= config.executive_loop_interval_hours:
            return True
    except (ValueError, TypeError):
        return True

    # Check empty queue trigger
    if config.executive_loop_trigger_on_empty_queue:
        _ensure_imports()
        try:
            company_dir = company_resolver.get_company_dir()
            queue_path = company_dir / "state/work_queue.json"
            if queue_path.exists():
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
                # Support both queue formats
                if "tasks" in queue:
                    pending = sum(
                        1 for t in queue["tasks"] if t.get("status") == "pending"
                    )
                    in_progress = sum(
                        1 for t in queue["tasks"] if t.get("status") == "in_progress"
                    )
                else:
                    pending = len(queue.get("pending", []))
                    in_progress = len(queue.get("in_progress", []))
                if pending == 0 and in_progress == 0:
                    # WS-016: Cascade compliance - wait for employee initiative first
                    if config.cascade_enabled:
                        now = datetime.now(timezone.utc)
                        # Check if employee initiative has had a chance to fill queue
                        if state.last_employee_initiative:
                            try:
                                init_time = datetime.fromisoformat(
                                    state.last_employee_initiative.replace(
                                        "Z", "+00:00"
                                    )
                                )
                                minutes_since = (now - init_time).total_seconds() / 60
                                if (
                                    minutes_since
                                    <= config.exec_wait_for_initiative_minutes
                                ):
                                    # Employee initiative ran recently, queue still empty
                                    # Executives should fill it
                                    logger.debug(
                                        f"Empty queue after employee initiative "
                                        f"({minutes_since:.1f}m ago) - exec can fill"
                                    )
                                    return True
                            except (ValueError, TypeError):
                                pass
                        # Employee initiative hasn't run recently, skip exec
                        # Let employee initiative get first crack at filling queue
                        logger.debug(
                            "Empty queue but employee initiative hasn't run recently - "
                            "deferring to employee initiative first"
                        )
                        return False
                    # Cascade disabled - original behavior
                    return True
        except Exception:
            pass

    return False


def _run_executive_loop(state: DaemonState) -> dict[str, int]:
    """Run the executive loop cycle.

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with counts: executives_invoked, decisions_made, work_submitted
    """
    _ensure_imports()

    try:
        # Determine trigger type
        trigger = "scheduled"
        if not state.last_executive_loop:
            trigger = "first_run"

        # Run executive loop
        result = executive_loop.run_executive_loop(trigger=trigger)

        if result["executives_invoked"] > 0 and result["decisions_made"] == 0:
            logger.warning(
                f"Executive loop: {result['executives_invoked']} executives invoked "
                f"but 0 decisions — executives may lack org context or inputs"
            )
        else:
            logger.info(
                f"Executive loop: {result['executives_invoked']} executives invoked, "
                f"{result['decisions_made']} decisions, "
                f"{result['work_submitted']} work items submitted"
            )

        # Update state
        state.last_executive_loop = datetime.now(timezone.utc).isoformat()
        state.executive_sessions_run += result["executives_invoked"]
        state.work_submitted_by_executives += result["work_submitted"]

        return {
            "executives_invoked": result["executives_invoked"],
            "decisions_made": result["decisions_made"],
            "work_submitted": result["work_submitted"],
        }

    except Exception as e:
        logger.error(f"Executive loop failed: {e}")
        # Still update time to avoid retry storm
        state.last_executive_loop = datetime.now(timezone.utc).isoformat()
        return {"executives_invoked": 0, "decisions_made": 0, "work_submitted": 0}


# -----------------------------------------------------------------------------
# Stale Task Cleanup (P22 Fix 2)
# -----------------------------------------------------------------------------


def _should_run_stale_cleanup(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run stale task cleanup.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if stale cleanup should run.
    """
    if not config.stale_cleanup_enabled:
        return False

    if not state.last_stale_cleanup:
        return True  # Never cleaned before

    try:
        last_cleanup = datetime.fromisoformat(
            state.last_stale_cleanup.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_cleanup).total_seconds()
        return elapsed >= (config.stale_cleanup_interval_hours * 3600)
    except (ValueError, TypeError):
        return True


def _run_stale_cleanup(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Run stale task cleanup.

    Args:
        config: Daemon configuration.
        state: Current daemon state (will be updated).

    Returns:
        Dict with counts: cleaned, archived
    """
    _ensure_imports()

    try:
        result = work_allocator.cleanup_stale_tasks(
            max_age_hours=config.stale_task_max_age_hours,
            archive=True,
            dry_run=False,
            # Zombie watchdog: kill in_progress tasks stuck >2h.
            # A worker that takes >2h has almost certainly died silently —
            # spawning Claude subprocesses rarely exceed 30min even for epic tasks.
            stuck_in_progress_hours=config.stuck_in_progress_max_hours,
        )

        cleaned_count = result.get("cleaned_count", 0)
        released_count = result.get("released_count", 0)
        if cleaned_count > 0:
            logger.info(
                f"Stale task cleanup: archived {cleaned_count} tasks "
                f"(older than {config.stale_task_max_age_hours}h)"
            )
        if released_count > 0:
            logger.warning(
                f"Zombie watchdog: released {released_count} tasks stuck "
                f"in_progress >{config.stuck_in_progress_max_hours}h"
            )

        # Update state
        state.last_stale_cleanup = datetime.now(timezone.utc).isoformat()
        state.stale_tasks_cleaned += cleaned_count

        return {"cleaned": cleaned_count, "archived": cleaned_count}

    except Exception as e:
        logger.error(f"Stale cleanup failed: {e}")
        # Still update time to avoid retry storm
        state.last_stale_cleanup = datetime.now(timezone.utc).isoformat()
        return {"cleaned": 0, "archived": 0}


def _should_run_autonomy_reconcile(config: DaemonConfig, state: DaemonState) -> bool:
    """Time-gate the autonomy queue reconcile (closed-unmerged hygiene).

    Mirrors :func:`_should_run_stale_cleanup`: runs on first boot, then once per
    ``autonomy_reconcile_interval_hours``.
    """
    if not config.autonomy_reconcile_enabled:
        return False

    if not state.last_autonomy_reconcile:
        return True  # Never reconciled before

    try:
        last_run = datetime.fromisoformat(
            state.last_autonomy_reconcile.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed >= (config.autonomy_reconcile_interval_hours * 3600)
    except (ValueError, TypeError):
        return True


def _run_autonomy_reconcile(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Flip stale completions whose PR ended CLOSED-unmerged.

    Keeps the local autonomy proxy / Tier-1 candidate set honest by retiring
    completed-with-PR tasks whose PR was actually closed without merging (e.g.
    human-closed canaries, off-target changes declined at review). Bounded to a
    recent window to cap the gh calls per run — newly-closed PRs are always
    recent. The reconcile itself is lock-safe and runs its gh checks outside the
    queue lock (see autonomy_metrics.reconcile_queue_closed_unmerged).
    """
    _ensure_imports()
    # Stamp the run time up front so a hard failure backs off the full interval
    # rather than retrying the (network-heavy) reconcile every cycle.
    state.last_autonomy_reconcile = datetime.now(timezone.utc).isoformat()
    try:
        result = autonomy_metrics.reconcile_queue_closed_unmerged(
            Path(".company"),
            window_days=config.autonomy_reconcile_window_days,
        )
        return {"reconciled": int(result.get("reconciled", 0))}
    except Exception as e:
        logger.warning(f"[Reconcile] Autonomy queue reconcile failed (non-fatal): {e}")
        return {"reconciled": 0}


def _should_run_pr_open_reconcile(config: DaemonConfig, state: DaemonState) -> bool:
    """Time-gate the fast pr_open-lane reconcile (P1-R2).

    Runs on first boot, then once per ``pr_open_reconcile_interval_seconds``.
    An interval <= 0 disables the fast path entirely (the 6h closed-unmerged
    reconcile still covers the lane eventually).
    """
    if config.pr_open_reconcile_interval_seconds <= 0:
        return False

    if not state.last_pr_open_reconcile:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_pr_open_reconcile.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed >= config.pr_open_reconcile_interval_seconds
    except (ValueError, TypeError):
        return True


def _run_pr_open_reconcile(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Advance pr_open tasks whose PR merged (or reset ones whose PR closed).

    P1-R2: the pr_open lane is tiny and time-sensitive — a merged PR should
    flip its task to completed within one fast cycle, not the 6-hour hygiene
    cadence (observed 2026-07-07: canary task sat in pr_open 45+ min until a
    manual reconcile). Cost-bounded: zero gh calls when the lane is empty;
    otherwise one gh call per pr_open task.
    """
    _ensure_imports()
    state.last_pr_open_reconcile = datetime.now(timezone.utc).isoformat()
    try:
        result = autonomy_metrics.reconcile_queue_closed_unmerged(
            Path(".company"),
            pr_open_only=True,
        )
        for change in result.get("changes", []) or []:
            logger.info(
                f"[Reconcile] pr_open: {change.get('task_id')} -> "
                f"{change.get('transition')} ({change.get('pr_url')})"
            )
        _run_challenger_tick()
        _run_rebase_tick()
        return {"reconciled": int(result.get("reconciled", 0))}
    except Exception as e:
        logger.warning(f"[Reconcile] pr_open fast reconcile failed (non-fatal): {e}")
        return {"reconciled": 0}


def _challenger_merge_block(pr_number) -> str | None:
    """CHALLENGER-STAGE: why the merge loop must hold this PR, or None."""
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import challenger_stage

        return challenger_stage.merge_block_reason(
            pr_number, company_dir=Path(".company")
        )
    except Exception:
        return None


def _run_challenger_tick() -> None:
    """CHALLENGER-STAGE: queue a challenger for each new product-goal PR and
    keep the stage's tasks in step with their parent (cancel, hold, merge)."""
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import challenger_stage

        tick = challenger_stage.tick(Path(".company"))
        for pr_url in tick.get("engaged", []):
            logger.info(f"[Challenger] queued a challenger review for {pr_url}")
        if tick.get("cancelled"):
            logger.info(f"[Challenger] cancelled stage task(s) {tick['cancelled']}")
        for err in tick.get("errors", []):
            logger.warning(f"[Challenger] {err}")
    except Exception as e:
        logger.warning(f"[Challenger] tick failed (non-fatal): {e}")


def _conflict_gate(pending: list, active_ids: set) -> list:
    """CONFLICT-AWARE-SCHEDULING (#727): the pending tasks the dispatcher may
    pick this cycle (the same task objects, deferred ones left out). Returns
    ``pending`` unchanged when the feature is off or anything fails."""
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import conflict_forecast

        return conflict_forecast.daemon_gate(
            pending, active_ids, Path(".company"), logger=logger
        )
    except Exception as e:
        logger.warning(f"[ConflictAware] gate skipped (non-fatal): {e}")
        return pending


def _run_rebase_tick() -> None:
    """AUTO-REBASE-CONFLICTING-PRS (#714): queue a rebase for each of the
    daemon's PRs that went CONFLICTING, and run the merge queue (which only
    labels ``ready-to-merge`` when auto-merge is not fully allowed)."""
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import pr_rebase

        pr_rebase.daemon_tick(Path(".company"), logger=logger)
    except Exception as e:
        logger.warning(f"[Rebase] tick failed (non-fatal): {e}")


def _should_run_escalation_archive(config: DaemonConfig, state: DaemonState) -> bool:
    """Time-gate the stale-escalation auto-archive.

    Mirrors :func:`_should_run_stale_cleanup`: runs on first boot, then once per
    ``escalation_archive_interval_hours``.
    """
    if not config.escalation_archive_enabled:
        return False

    if not state.last_escalation_archive:
        return True  # Never archived before

    try:
        last_run = datetime.fromisoformat(
            state.last_escalation_archive.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed >= (config.escalation_archive_interval_hours * 3600)
    except (ValueError, TypeError):
        return True


def _run_escalation_archive(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Move stale escalation records to the archive.

    Escalation records older than ``escalation_archive_stale_hours`` (resolved or
    long-dead ``repeated_failure`` dead-ends) otherwise keep inflating the
    health-score active-escalations factor forever. ``archive_stale_escalations``
    MOVES them to ``.company/archive/escalations/`` (recoverable, never deleted).
    """
    _ensure_imports()
    # Stamp up front so a hard failure backs off the full interval.
    state.last_escalation_archive = datetime.now(timezone.utc).isoformat()
    try:
        result = escalation.archive_stale_escalations(
            stale_hours=config.escalation_archive_stale_hours,
            dry_run=False,
        )
        return {"archived": int(result.get("archived_count", 0))}
    except Exception as e:
        logger.warning(f"[Escalation] Stale-archive failed (non-fatal): {e}")
        return {"archived": 0}


# ProjectK K4: when approved-but-unconverted ideas are waiting, convert them
# within this floor instead of a full ideation interval (which could be ~30 min
# under idle backoff). The floor bounds how often a persistently-unconvertible
# (duplicate) idea can re-trigger the converter.
_IDEA_AUTOCONVERT_PROMPT_FLOOR_SECONDS = 60


def _has_pending_approved_ideas() -> bool:
    """True if employee_ideas.json has any idea in status 'approved'.

    Reads through the converter's own loader so it sees the exact same file
    convert_approved_ideas() will act on. Non-fatal: any error -> False, so a
    read glitch never spins or blocks the loop.
    """
    _ensure_imports()
    try:
        data = idea_to_task_converter_mod.load_ideas()
        return any(i.get("status") == "approved" for i in data.get("ideas", []))
    except Exception:
        return False


def _should_run_idea_auto_convert(config: DaemonConfig, state: DaemonState) -> bool:
    """Decide whether to run the approved-idea auto-conversion this cycle.

    Two paths:
    - PROMPT (ProjectK K4): if approved-but-unconverted ideas exist, convert
      them within ``_IDEA_AUTOCONVERT_PROMPT_FLOOR_SECONDS`` rather than waiting
      a full ideation interval — a fresh company shouldn't sit with approved
      ideas for up to ~30 min under the idle backoff.
    - PIGGYBACK: otherwise, the historical once-per-``employee_ideation_interval_minutes``
      sweep (kept so behavior is unchanged when nothing is waiting).

    Disabled when ``employeeIdeation.autoConvertApproved`` is false (or absent — defaults True).
    """
    if not config.employee_ideation_auto_convert_approved:
        return False

    # PROMPT path — approved ideas are waiting.
    if _has_pending_approved_ideas():
        if not state.last_idea_auto_convert:
            return True
        try:
            last_run = datetime.fromisoformat(
                state.last_idea_auto_convert.replace("Z", "+00:00")
            )
            elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
            if elapsed >= _IDEA_AUTOCONVERT_PROMPT_FLOOR_SECONDS:
                return True
        except (ValueError, TypeError):
            return True

    # PIGGYBACK path — periodic sweep on the ideation interval.
    if not state.last_idea_auto_convert:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_idea_auto_convert.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed >= (config.employee_ideation_interval_minutes * 60)
    except (ValueError, TypeError):
        return True


def _idea_poll_shrink_needed(config: DaemonConfig) -> bool:
    """WS-119-style bypass: report whether approved ideas are waiting.

    Mirrors the pending-work-queue check (poll_interval shrunk to avoid
    idle-backoff delay) so approved ideas aren't stuck behind up to 30
    minutes of adaptive-scheduler idle backoff before the next maintenance
    cycle even evaluates them.
    """
    if not config.employee_ideation_auto_convert_approved:
        return False
    _ensure_imports()
    try:
        return bool(idea_to_task_converter_mod.has_approved_ideas())
    except Exception as e:
        logger.debug(f"[Maintenance] Idea pending-check failed (non-fatal): {e}")
        return False


def _run_idea_auto_convert(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Convert all ideas in status 'approved' to work queue tasks.

    Calls ``idea_to_task_converter.convert_approved_ideas()`` which routes
    through ``work_allocator.add_task()`` and therefore the admission gate.
    Ideas that the gate accepts are marked 'queued' in employee_ideas.json;
    duplicates stay 'approved' and will be retried next cycle; exceptions are
    caught so this path is always non-fatal.

    Logs one INFO line per converted idea as required by the acceptance criteria.
    """
    _ensure_imports()
    if idea_to_task_converter_mod is None:  # not in the public export
        return {"converted": 0}
    state.last_idea_auto_convert = datetime.now(timezone.utc).isoformat()
    try:
        results = idea_to_task_converter_mod.convert_approved_ideas(dry_run=False)
        converted = 0
        for r in results:
            if r.get("action") == "created":
                converted += 1
                logger.info(
                    f"[Maintenance] Idea auto-convert: idea {r['idea_id']} -> task {r['task_id']}"
                )
            elif r.get("action") == "duplicate":
                logger.debug(
                    f"[Maintenance] Idea auto-convert: idea {r['idea_id']} already has duplicate task, skipping"
                )
        state.ideas_auto_converted += converted
        return {"converted": converted}
    except Exception as e:
        logger.warning(f"[Maintenance] Idea auto-convert failed (non-fatal): {e}")
        return {"converted": 0}


# -----------------------------------------------------------------------------
# Startup Queue Canary (P50.2)
# -----------------------------------------------------------------------------


def _run_startup_canary() -> None:
    """Validate work_queue.json health before the main daemon loop.

    Checks:
    1. Queue file parses as valid JSON
    2. Required keys exist (pending, in_progress, completed, metadata)
    3. Creates backup before any repairs

    On failure: repairs in-place, never exits.
    """
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
    except Exception as e:
        logger.warning(f"Queue canary: cannot resolve company dir: {e}")
        return

    queue_path = company_dir / "state/work_queue.json"
    backup_path = company_dir / ".work_queue.json.bak"
    required_keys = {
        "pending": [],
        "in_progress": [],
        "completed": [],
        "metadata": {},
    }

    # Step 1: Load and parse queue
    queue: dict[str, Any] | None = None

    if queue_path.exists():
        try:
            raw = queue_path.read_text(encoding="utf-8")
            queue = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            logger.critical(f"Queue canary: work_queue.json parse failure: {e}")
            # Attempt backup restore
            if backup_path.exists():
                try:
                    backup_raw = backup_path.read_text(encoding="utf-8")
                    queue = json.loads(backup_raw)
                    logger.info(
                        "Queue canary: restored queue from .work_queue.json.bak"
                    )
                except (json.JSONDecodeError, ValueError) as e2:
                    logger.warning(f"Queue canary: backup also invalid: {e2}")
                    queue = None
            if queue is None:
                logger.warning(
                    "Queue canary: initializing empty queue with required keys"
                )
                queue = {
                    k: (list(v) if isinstance(v, list) else dict(v))
                    for k, v in required_keys.items()
                }
    else:
        logger.info("Queue canary: work_queue.json does not exist, initializing")
        queue = {
            k: (list(v) if isinstance(v, list) else dict(v))
            for k, v in required_keys.items()
        }

    # Step 2: Verify required keys, add missing ones
    repaired = False
    for key, default in required_keys.items():
        if key not in queue:
            logger.warning(
                f"Queue canary: missing required key '{key}', adding default"
            )
            queue[key] = list(default) if isinstance(default, list) else dict(default)
            repaired = True

    # Step 3: If repairs needed, backup then write
    if repaired or not queue_path.exists():
        # Create backup of current file before writing repairs
        if queue_path.exists():
            try:
                import shutil

                shutil.copy2(queue_path, backup_path)
            except OSError as e:
                logger.warning(f"Queue canary: failed to create backup: {e}")

        # Write repaired queue atomically (with QueueLock to prevent races)
        try:
            lock_path = company_dir / "runtime/queue.lock"
            with work_allocator.QueueLock(lock_path):
                fd, tmp_path = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    os.replace(tmp_path, str(queue_path))
                    logger.info("Queue canary: wrote repaired queue")
                except Exception:
                    os.unlink(tmp_path)
                    raise
        except Exception as e:
            logger.error(f"Queue canary: failed to write repaired queue: {e}")
            return

    pending = len(queue.get("pending", []))
    in_progress = len(queue.get("in_progress", []))
    completed = len(queue.get("completed", []))
    logger.info(
        f"Queue canary passed: {pending} pending, "
        f"{in_progress} in_progress, {completed} completed"
    )


# -----------------------------------------------------------------------------
# Git Auto-Update: restart daemon when origin/main has new commits
# -----------------------------------------------------------------------------

# Timestamp of last git update check
_last_git_update_check: float = 0.0


def _should_check_git_updates(config: DaemonConfig) -> bool:
    """Check if it's time to poll origin/main for new commits."""
    global _last_git_update_check
    if not config.git_update_check_enabled:
        return False
    now = time.time()
    interval = config.git_update_check_interval_minutes * 60
    return (now - _last_git_update_check) >= interval


def _check_for_git_updates(failures_file: Path | None = None) -> bool:
    """Fetch origin and check if origin/main is ahead of local main.

    Returns True if new commits are available. ``failures_file`` (the daemon
    passes _GIT_UPDATE_FAILURES_FILE) receives fetch failures and is cleared
    when local main turns out to be current; None records nothing.
    """
    global _last_git_update_check
    _last_git_update_check = time.time()
    try:
        # Fetch latest from origin (no merge)
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if fetch_result.returncode != 0:
            logger.warning(f"[Git Update] fetch failed: {fetch_result.stderr.strip()}")
            _record_git_update_failure(failures_file, "fetch", fetch_result.stderr)
            return False

        # Compare local HEAD with origin/main
        rev_result = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rev_result.returncode == 0:
            count = int(rev_result.stdout.strip())
            if count > 0:
                logger.info(f"[Git Update] {count} new commit(s) on origin/main")
                return True
            # Local main is at origin/main: the daemon is demonstrably current,
            # so an earlier streak (a transient fetch error, say) is over. A
            # streak that matters — origin ahead, pull failing — keeps taking
            # the True branch above and is judged by the pull every cycle.
            _clear_git_update_failures(failures_file)

        return False

    except subprocess.TimeoutExpired:
        logger.warning("[Git Update] fetch timed out")
        _record_git_update_failure(failures_file, "fetch", "timed out")
        return False
    except (OSError, ValueError) as e:
        logger.debug(f"[Git Update] check error: {e}")
        return False


# Paths whose local state is dirty BY DESIGN, and so can never clear.
#
# gatesmith.json and .claude/settings.json carry permanent local overrides
# (measured 2026-08-26: +28/-28 and +17/-4 against origin/main). The pre-pull
# dirty check treated them as "someone left uncommitted work here" and refused
# to pull — every cycle, forever. The daemon had therefore NEVER pulled: it ran
# whatever code it started with while its own merged fixes sat unused, and the
# per-cycle warning read like a transient condition rather than a permanent one.
#
# Excluding them is safe. They only matter to a fast-forward if the incoming
# commits actually touch them, and git refuses that case on its own — which the
# failure branch below now reports explicitly instead of blaming a dirty tree.
_PERMANENTLY_DIRTY_FALLBACK = (
    "product-site/*",
    "CLAUDE.md",
    "gatesmith.json",
    ".claude/settings.json",
    "pyproject.toml",
    "uv.lock",
)


def _permanently_dirty_paths() -> tuple[str, ...]:
    """humanProtected paths from config, falling back to the known set."""
    try:
        with open("gatesmith.json", encoding="utf-8") as f:
            declared = (json.load(f).get("humanProtected") or {}).get("paths")
        if isinstance(declared, list) and declared:
            return tuple(str(p) for p in declared if isinstance(p, str))
    except Exception:
        pass
    return _PERMANENTLY_DIRTY_FALLBACK


def _is_permanently_dirty(rel_path: str) -> bool:
    """True when a dirty path is one the daemon is forbidden to modify anyway."""
    return any(
        fnmatch.fnmatch(rel_path, pattern) for pattern in _permanently_dirty_paths()
    )


def _git_lock_policy():
    """company_resolver, imported lightly — preflight runs before _ensure_imports()."""
    if company_resolver is not None:
        return company_resolver
    try:
        from . import company_resolver as cr  # type: ignore[no-redef]
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr


_GIT_UPDATE_FAILURES_FILE = Path(".company/state/git_update_failures.json")


def _record_git_update_failure(
    failures_file: Path | None, stage: str, error: str
) -> None:
    """Persist one more consecutive git-update failure for the silence sentinel.

    314 consecutive failed pulls in mdlint-sandbox (2026-08-20 → 08-28)
    produced 314 WARNING lines and nothing else. The count, the first-failure
    time and the last error go to a state file the sentinel pages on after an
    hour (silence_sentinel.check_git_update_blocked).

    ``failures_file`` is explicit on purpose: the daemon passes
    _GIT_UPDATE_FAILURES_FILE, and a caller without one (tests, ad-hoc
    calls) records nothing. A cwd-relative default here wrote a fake
    3-failure streak into the real .company/state during a test run
    (2026-08-28) — the CLAUDE.md test-isolation antipattern.
    """
    if failures_file is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        data: dict = {}
        if failures_file.exists():
            try:
                data = json.loads(failures_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        record = {
            "consecutive": int(data.get("consecutive", 0) or 0) + 1,
            "first_failed_at": data.get("first_failed_at") or now,
            "last_failed_at": now,
            "stage": stage,
            "last_error": (error or "").strip()[:500],
        }
        failures_file.parent.mkdir(parents=True, exist_ok=True)
        failures_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _clear_git_update_failures(failures_file: Path | None) -> None:
    """A successful pull — or a check that finds local main current — ends
    the streak."""
    if failures_file is None:
        return
    try:
        failures_file.unlink(missing_ok=True)
    except OSError:
        pass


def _clear_stale_index_lock(repo_root: Path | None = None) -> float | None:
    """Remove ``.git/index.lock`` when it is stale by the shared policy.

    Policy — company_resolver.stale_git_lock_age: a 0-byte lock older than
    five minutes, or any lock older than an hour. An empty lock is a git
    process that died before writing the index (the incident lock was 0 bytes
    and 7.4 days old); a non-empty one may be a live interactive commit.

    Returns the age (seconds) of the lock that was removed, or None when
    there was nothing to remove (absent, not stale yet, or unremovable).
    """
    lock = (repo_root if repo_root is not None else Path(".")) / ".git" / "index.lock"
    try:
        if not lock.exists():
            return None
        age = _git_lock_policy().clear_stale_git_lock(lock)
    except OSError as e:
        logger.warning(f"[Git] Could not remove stale .git/index.lock: {e}")
        return None
    except Exception as e:
        logger.warning(f"[Git] Could not evaluate .git/index.lock: {e}")
        return None
    if age is None:
        return None
    logger.warning(
        f"[Git] Removed stale .git/index.lock ({int(age)}s old) — it was "
        "blocking every pull"
    )
    return age


def _preferred_base_ref() -> str:
    """Base ref for new task worktrees — see company_resolver.preferred_base_ref.

    Never blocks daemon operation: any failure falls back to local ``main``.
    """
    try:
        _ensure_imports()
        return company_resolver.preferred_base_ref(Path.cwd())
    except Exception:
        return "main"


def _branch_has_unpushed_commits(branch: str, cwd: Path | None = None) -> bool:
    """True when ``branch`` holds commits that no remote-tracking branch has.

    A force delete (``git branch -D``) of such a branch destroys work that
    exists nowhere else. When git cannot answer, the answer is True: keep the
    branch (2026-10-31: the cycle cleanup force-deleted any ``feat/*`` branch
    the primary checkout stood on, pushed or not).
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", branch, "--not", "--remotes"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True
    if result.returncode != 0:
        return True
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return True


def _perform_git_pull(failures_file: Path | None = None) -> bool:
    """Pull latest changes from origin/main (fast-forward only).

    Returns True if pull succeeded.
    """
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status_result.returncode == 0 and status_result.stdout.strip():
            # Filter out untracked files (??) — they don't conflict with pull —
            # and paths that are dirty by design and can never clear.
            tracked_changes = [
                line
                for line in status_result.stdout.strip().splitlines()
                if not line.startswith("??")
                and not _is_permanently_dirty(
                    line.split(None, 1)[-1] if " " in line else line
                )
            ]
            if tracked_changes:
                shown = tracked_changes[:5]
                more = len(tracked_changes) - 5
                suffix = f" (+{more} more)" if more > 0 else ""
                file_list = ", ".join(
                    s.split(None, 1)[-1] if " " in s else s for s in shown
                )
                logger.warning(
                    f"[Git Update] Dirty working tree, skipping pull — "
                    f"{len(tracked_changes)} tracked change(s): {file_list}{suffix}"
                )
                return False

        # Fast-forward only pull
        pull_result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pull_result.returncode == 0:
            logger.info(f"[Git Update] Pull succeeded: {pull_result.stdout.strip()}")
            _clear_git_update_failures(failures_file)
            return True
        else:
            stderr = pull_result.stderr.strip()
            # The one failure this design knowingly accepts: main changed a file
            # that carries permanent local overrides, so a fast-forward would
            # overwrite them and git refuses. Name it, because "pull failed" on
            # its own reads as transient and this state persists until a human
            # reconciles the override.
            if "would be overwritten" in stderr or "local changes" in stderr:
                logger.warning(
                    "[Git Update] Pull refused — origin/main changed a file that "
                    "carries permanent local overrides (%s). The daemon will keep "
                    "running its current code until this is reconciled by hand: %s",
                    ", ".join(_permanently_dirty_paths()),
                    stderr,
                )
            else:
                if "index.lock" in stderr and _clear_stale_index_lock() is not None:
                    retry = subprocess.run(
                        ["git", "pull", "--ff-only", "origin", "main"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if retry.returncode == 0:
                        logger.info(
                            "[Git Update] Pull succeeded after clearing a stale "
                            f"index.lock: {retry.stdout.strip()}"
                        )
                        _clear_git_update_failures(failures_file)
                        return True
                    stderr = retry.stderr.strip()
                logger.warning(f"[Git Update] Pull failed: {stderr}")
            _record_git_update_failure(failures_file, "pull", stderr)
            return False

    except subprocess.TimeoutExpired:
        logger.warning("[Git Update] Pull timed out")
        _record_git_update_failure(failures_file, "pull", "timed out")
        return False
    except OSError as e:
        logger.warning(f"[Git Update] Pull error: {e}")
        _record_git_update_failure(failures_file, "pull", str(e))
        return False


def _request_daemon_restart(reason: str = "git update") -> None:
    """Request a graceful daemon restart by signalling shutdown.

    The daemon's supervised execution loop (restart_on_crash) will
    restart it automatically, picking up the new code.
    """
    global _shutdown_requested
    logger.info(f"[Restart] Requesting daemon restart: {reason}")
    _shutdown_requested = True


# WS-119 1.6: Files whose changes warrant a daemon restart. The daemon
# loads these into memory at startup, so changes only take effect after
# a restart. Any other .py module the daemon has imported also triggers
# (see _is_engine_trigger), so this list need not name every module. Files outside this list (docs, website, tests, configs not
# read at boot) don't require a restart.
#
# Entries containing "/" are matched by exact repo-relative path.
# Bare filenames (no "/") are matched by basename only, and only when
# the changed path is not under tests/ or docs/ — preventing false
# positives like tests/test_forge_daemon.py matching "forge_daemon.py".
_RESTART_TRIGGER_FILES = (
    "forge_daemon.py",
    "employee_activator.py",
    "failure_recovery.py",
    "operation_loop.py",
    "work_allocator.py",
    "team_executor.py",
    "approval_learner.py",
    "strategic_planner.py",
    "goal_autofill.py",
    "goal_tracker.py",
    "orchestrator.py",
    ".claude/gatesmith.json",
    "gatesmith.json",
)

# Top-level directories whose changes never warrant a restart.
_RESTART_EXCLUDED_DIRS = frozenset({"tests", "docs"})


def _loaded_module_files() -> set[Path]:
    """Resolved paths of every module file this process has imported."""
    loaded: set[Path] = set()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            loaded.add(Path(filename).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return loaded


def _is_engine_trigger(
    changed_path: str,
    loaded_files: set[Path] | None = None,
    repo_root: Path | None = None,
) -> bool:
    """Return True if changed_path is an engine file that warrants a restart.

    Rules:
    - Paths under tests/ or docs/ are never engine triggers.
    - Trigger entries containing "/" are matched by exact repo-relative path.
    - Bare trigger names (no "/") are matched against the changed path's
      basename only, preventing substring false positives.
    - A .py file this process has imported (loaded_files) is a trigger, so
      a module the daemon runs in memory (e.g. silence_sentinel.py) never
      keeps running stale code after a pull, whether or not it is listed.
    """
    p = Path(changed_path)
    if p.parts and p.parts[0] in _RESTART_EXCLUDED_DIRS:
        return False
    for trig in _RESTART_TRIGGER_FILES:
        if "/" in trig:
            if changed_path == trig:
                return True
        else:
            if p.name == trig:
                return True
    if loaded_files and p.suffix == ".py":
        try:
            resolved = ((repo_root or Path.cwd()) / p).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if resolved in loaded_files:
            return True
    return False


def _git_pull_changed_engine_files() -> tuple[bool, list[str]]:
    """WS-119 1.6: After a successful git pull, return whether any
    engine-loaded files changed and which ones.

    The check uses `git diff --name-only ORIG_HEAD HEAD` which gives
    the files that just moved from origin into local. Empty list means
    no engine files changed and no restart is needed.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "ORIG_HEAD", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, []
        changed = [f for f in result.stdout.strip().splitlines() if f]
        loaded = _loaded_module_files()
        engine_changed = [f for f in changed if _is_engine_trigger(f, loaded)]
        return bool(engine_changed), engine_changed
    except Exception as e:
        logger.debug(f"WS-119 1.6: engine-file diff failed: {e}")
        # Fail safe: if we can't tell, assume restart needed (matches
        # current behavior of always restarting after a successful pull)
        return True, []


# -----------------------------------------------------------------------------
# Orphan Task Cleanup (WS-017 Analysis Fix)
# -----------------------------------------------------------------------------


def _cleanup_orphan_tasks(current_agent_id: str) -> dict:
    """Clean up tasks stuck in_progress from old daemon instances.

    When a daemon restarts, tasks assigned to the old daemon identity remain
    in_progress indefinitely.  For each such orphan we perform TWO liveness
    checks before deciding what to do (P1-R3):

      1. Claim-holder daemon pid still alive?  → skip (other daemon owns it).
      2. Task's worker subprocess still alive? → adopt (re-stamp + track).
      3. Both dead?                            → release back to pending.

    Args:
        current_agent_id: The current daemon's full agent ID
            (e.g. 'daemon-12345-1720000000').

    Returns:
        Dict with: orphans_found, released, released_task_ids, adopted_tasks
    """
    _ensure_imports()
    _empty = {
        "orphans_found": 0,
        "released": 0,
        "released_task_ids": [],
        "adopted_tasks": [],
    }

    try:
        company_dir = company_resolver.get_company_dir()
        queue_path = company_dir / "state/work_queue.json"
        lock_path = company_dir / "runtime/queue.lock"

        if not queue_path.exists():
            return _empty.copy()

        # Use QueueLock to prevent concurrent write races (P50 fix)
        with work_allocator.QueueLock(lock_path):
            with open(queue_path, encoding="utf-8") as f:
                queue = json.load(f)

            # Load worker PID registry once for the sweep (P1-R3).
            _worker_pids: dict[str, int] = {}
            # Sessions launched in their own process group (E1). Their group
            # outlives the daemon that launched them, so they are never
            # adopted — see "Check 2" below.
            _worker_groups: set[str] = set()
            _worker_records: dict[str, dict] = {}
            _pid_file = company_dir / "state" / "worker_pids.json"
            try:
                if _pid_file.exists():
                    with open(_pid_file, encoding="utf-8") as _pf:
                        _records = json.load(_pf)
                    for _tid, _rec in (_records or {}).items():
                        _p = _rec.get("pid") if isinstance(_rec, dict) else int(_rec)
                        if _p:
                            _worker_pids[str(_tid)] = int(_p)
                            if isinstance(_rec, dict) and _is_killable_worker_group(
                                int(_p), _rec.get("pgid")
                            ):
                                _worker_groups.add(str(_tid))
                                _worker_records[str(_tid)] = _rec
            except Exception:
                logger.warning(
                    "worker_pids.json unreadable; worker-pid check skipped "
                    "for orphan release — treating absent entries as dead"
                )

            # Collect foreign-daemon tasks from in_progress and blocked.
            in_progress_orphans = []
            blocked_orphans = []

            for task in queue.get("in_progress", []):
                if _is_daemon_task(task) and not _is_current_daemon_task(
                    task, current_agent_id
                ):
                    in_progress_orphans.append(("in_progress", task))

            for task in queue.get("blocked", []):
                if _is_daemon_task(task) and not _is_current_daemon_task(
                    task, current_agent_id
                ):
                    if not task.get("dependencies"):
                        # WS-119 1.7: Respect explicit human blocks.
                        if task.get("blocked_reason"):
                            continue
                        # Escalated tasks are terminal until a human resolves them.
                        if task.get("status") == "escalated":
                            continue
                        blocked_orphans.append(("blocked", task))

            all_orphans = in_progress_orphans + blocked_orphans

            if not all_orphans:
                return _empty.copy()

            released_count = 0
            released_ids: list[str] = []
            adopted_tasks: list[dict] = []

            for source_queue, orphan in all_orphans:
                task_id = str(orphan.get("task_id") or "")
                old_assignee = (
                    orphan.get("assigned_to") or orphan.get("claimed_by") or "unknown"
                )

                # Extract old daemon PID from claimed_by/assigned_to.
                # Handles both 'daemon-{pid}' and new 'daemon-{pid}-{ts}'.
                old_daemon_pid: int | None = None
                _parts = old_assignee.split("-")
                if len(_parts) >= 2 and _parts[0] == "daemon":
                    try:
                        old_daemon_pid = int(_parts[1])
                    except ValueError:
                        pass

                # Check 1: Is the claim-holder daemon process still alive?
                daemon_alive = old_daemon_pid is not None and is_process_alive(
                    old_daemon_pid
                )
                logger.debug(
                    f"Orphan check {task_id}: claim-holder={old_assignee} "
                    f"pid={old_daemon_pid} alive={daemon_alive}"
                )
                if daemon_alive:
                    # The process owning this claim is still running — skip.
                    # Note: if a non-daemon process recycled the pid, this
                    # causes a false-skip; _cleanup_stranded_tasks will
                    # release it next cycle once the composite identity
                    # 'daemon-pid-ts' no longer matches.
                    logger.info(
                        f"Skipping orphan {task_id}: claim-holder pid="
                        f"{old_daemon_pid} is still alive"
                    )
                    continue

                # Check 2: Is the task's worker subprocess still alive?
                worker_pid = _worker_pids.get(task_id)
                worker_alive = worker_pid is not None and is_process_alive(worker_pid)
                logger.debug(
                    f"Orphan check {task_id}: worker_pid={worker_pid} "
                    f"alive={worker_alive}"
                )

                if worker_alive and task_id in _worker_groups:
                    _ours, _why = _registered_group_is_ours(
                        _worker_records.get(task_id) or {}, worker_pid
                    )
                    if not _ours:
                        # A recycled pid, not our session: nothing to adopt
                        # and nothing to kill.
                        worker_alive = False
                        logger.info(
                            f"Orphan {task_id}: worker pid={worker_pid} is a "
                            f"stale entry, not ours ({_why})"
                        )
                if worker_alive and task_id in _worker_groups:
                    # E1: a session in its own process group survives its
                    # daemon (launchd no longer kills it with the job). It
                    # cannot be adopted with supervision: it is not this
                    # daemon's child, so there is no wait status, no output
                    # pipe, no stall / hard-cap / worktree-lost supervisor, and
                    # the result capture ran in the dead daemon's thread.
                    # Adopting it would keep an unsupervised bypass-permission
                    # session running. Release the task instead; the caller
                    # kills the group (_kill_surviving_worker_pids on
                    # released_task_ids) before anything is re-dispatched.
                    logger.warning(
                        f"Orphan {task_id}: worker group pgid={worker_pid} is "
                        "still alive but unsupervised — releasing the task and "
                        "killing the group instead of adopting it"
                    )
                elif worker_alive:
                    # Worker still running — adopt it into this daemon instance.
                    # Re-stamp identity in-place so stranded cleanup can track it.
                    orphan["claimed_by"] = current_agent_id
                    orphan["assigned_to"] = current_agent_id
                    orphan["recovery_note"] = (
                        f"Adopted by {current_agent_id} (prev: {old_assignee}); "
                        f"worker pid={worker_pid} still alive"
                    )
                    adopted_tasks.append({"task_id": task_id, "worker_pid": worker_pid})
                    logger.info(
                        f"Adopted orphan task {task_id}: worker pid={worker_pid} "
                        f"still alive (prev daemon={old_assignee}, now "
                        f"{current_agent_id})"
                    )
                    continue

                # Both daemon and worker dead → release back to pending.
                logger.info(
                    f"Released orphan task: {task_id} (was assigned to "
                    f"{old_assignee}; daemon pid={old_daemon_pid} dead, "
                    f"worker pid={worker_pid} dead/absent)"
                )

                queue[source_queue] = [
                    t for t in queue[source_queue] if t.get("task_id") != task_id
                ]

                orphan["assigned_to"] = None
                orphan["assigned_at"] = None
                orphan["claimed_by"] = None
                orphan["claimed_at"] = None
                orphan["started_at"] = None
                orphan["recovery_note"] = (
                    f"Released from orphan {source_queue} (was assigned to {old_assignee})"
                )

                queue["pending"].insert(0, orphan)
                released_count += 1
                if task_id:
                    released_ids.append(task_id)

            # Save updated queue (atomic write to prevent corruption)
            fd, tmp = tempfile.mkstemp(
                dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        return {
            "orphans_found": len(all_orphans),
            "released": released_count,
            "released_task_ids": released_ids,
            "adopted_tasks": adopted_tasks,
        }

    except Exception as e:
        logger.error(f"Orphan cleanup failed: {e}")
        return _empty.copy()


WORKER_GROUP_KILL_GRACE_SECONDS = 3.0
# A registered session's group must contain a process that started no later
# than this after the entry's started_at (uv starts just BEFORE the entry is
# written; claude within seconds of it). A pid recycled into an unrelated
# group — the user's own `claude` in a terminal is its own group leader —
# started later than that.
WORKER_IDENTITY_SLACK_SECONDS = 60.0


def _is_killable_worker_group(pid: Any, pgid: Any) -> bool:
    """Whether a registry entry names a worker process group we may signal.

    ``employee_activator`` launches each session as its own group leader, so a
    new-style entry has ``pgid == pid``. Anything else — no pgid (entries
    written before E1), a pgid that is not the pid, or this daemon's own group
    — is not treated as a group: signalling it could reach processes that are
    not the worker, including the daemon itself.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    if pgid != pid:
        return False
    try:
        return pgid != os.getpgrp()
    except OSError:
        return False


def _parse_etime(etime: str) -> float | None:
    """Seconds from ps's etime, ``[[DD-]HH:]MM:SS``."""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = [int(x) for x in etime.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts[-3:]
        return float(((days * 24 + h) * 60 + m) * 60 + sec)
    except (ValueError, TypeError):
        return None


def _worker_group_members(pgid: int) -> list[tuple[int, float | None, str]]:
    """``(pid, start_epoch, command)`` for every live process in group ``pgid``.

    ``-ww``: without it Linux ps truncates the command column and a claude
    member can lose the word that identifies it.
    """
    try:
        out = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pgid=,pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return []
    now = time.time()
    members: list[tuple[int, float | None, str]] = []
    for line in (out or "").splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4 or not fields[0].isdigit() or int(fields[0]) != pgid:
            continue
        if not fields[1].isdigit():
            continue
        age = _parse_etime(fields[2])
        start = now - age if age is not None else None
        members.append((int(fields[1]), start, fields[3].strip()))
    return members


def _worker_group_commands(pgid: int) -> list[str]:
    """Command lines of every live process in group ``pgid`` (best-effort)."""
    return [command for _, _, command in _worker_group_members(pgid)]


def _registered_group_is_ours(rec: dict, pgid: int) -> tuple[bool, str]:
    """Whether the live group ``pgid`` is still the session ``rec`` recorded.

    ``pgid == pid`` and a "claude" command line are not an identity: after a
    reboot, or once the pid is recycled, the number can belong to anyone's
    process group — a terminal ``claude`` session is its own group leader.
    Returns (True, "") when the group is ours, else (False, why).
    """
    members = _worker_group_members(pgid)
    if not members:
        return False, "group has no live members"
    if not any("claude" in command for _, _, command in members):
        return False, "no claude process in the group"
    started_at = rec.get("started_at")
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return False, "entry has no started_at"
    boot = system_boot_time()
    if (
        boot is not None
        and started_at < boot.timestamp() - _BOOT_TIME_TOLERANCE.total_seconds()
    ):
        return False, "entry was written before the current boot"
    starts = [start for _, start, _ in members if start is not None]
    if not starts:
        return False, "start times of the group's processes unknown"
    if min(starts) > started_at + WORKER_IDENTITY_SLACK_SECONDS:
        return False, (
            f"every process in the group started after the entry "
            f"(+{min(starts) - started_at:.0f}s) — the pid was reused"
        )
    return True, ""


def _process_group_alive(pgid: int) -> bool:
    """Whether group ``pgid`` still has a live member.

    macOS answers EPERM for a group whose only members are zombies (a leader
    nobody has reaped yet); that group has nothing left to kill.
    """
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _kill_registered_worker_groups(pgids: list[int], grace: float | None = None) -> int:
    """SIGTERM every group at once, ONE shared grace, then SIGKILL survivors.

    Returns how many groups received the SIGTERM. The daemon's own group and a
    group that is already gone are skipped. One grace for all groups keeps a
    shutdown with several busy workers inside launchd's ExitTimeOut.
    """
    try:
        own = os.getpgrp()
    except OSError:
        return 0
    signalled: list[int] = []
    for pgid in dict.fromkeys(pgids):
        if not isinstance(pgid, int) or pgid <= 1 or pgid == own:
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            continue
        signalled.append(pgid)
    if not signalled:
        return 0
    if grace is None:
        grace = WORKER_GROUP_KILL_GRACE_SECONDS
    deadline = time.time() + grace
    while time.time() < deadline and any(_process_group_alive(g) for g in signalled):
        time.sleep(0.1)
    for pgid in signalled:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return len(signalled)


def _kill_registered_worker_group(pgid: int, grace: float | None = None) -> bool:
    """SIGTERM one worker's process group, then SIGKILL whatever is left."""
    return _kill_registered_worker_groups([pgid], grace) == 1


SHUTDOWN_WORKER_JOIN_SECONDS = 60


def _set_worker_spawn_gate(closed: bool) -> None:
    """Close (shutdown) or reopen (start) employee_activator's spawn gate.

    Worker threads may hold the module under a package-qualified name; every
    loaded copy is set, and the plain module is imported if none is loaded so
    a thread that imports it later sees the gate closed.
    """
    mods = [
        m
        for name, m in list(sys.modules.items())
        if name.rsplit(".", 1)[-1] == "employee_activator"
        and hasattr(m, "close_worker_spawns")
    ]
    if not mods:
        try:
            import employee_activator as _ea  # type: ignore[import-not-found]

            mods = [_ea]
        except Exception as e:
            logger.warning(f"Spawn gate: employee_activator not importable: {e}")
            return
    for m in mods:
        try:
            (m.close_worker_spawns if closed else m.reopen_worker_spawns)()
        except Exception as e:
            logger.warning(f"Spawn gate: could not set on {m.__name__}: {e}")


def _stop_workers_for_shutdown(
    workers: list,
    join_seconds: float | None = None,
    grace: float | None = None,
) -> list[str]:
    """On daemon shutdown: wait for workers, then kill whatever still runs.

    ONE deadline for every join (not 60 s per worker), then every remaining
    worker group gets SIGTERM together and one shared grace before SIGKILL.
    Per-worker waits added up past launchd's 300 s ExitTimeOut with five busy
    workers; the daemon was then SIGKILLed, and since worker sessions have
    their own groups, launchd's kill no longer takes them — the survivors ran
    on unsupervised. Returns the task ids that were still running.
    """
    if join_seconds is None:
        join_seconds = SHUTDOWN_WORKER_JOIN_SECONDS
    # Close the spawn gate FIRST: a worker still before its launch at the
    # deadline would otherwise start its session just after the kill pass.
    _set_worker_spawn_gate(closed=True)
    deadline = time.time() + join_seconds
    for _wid, worker in workers:
        thread = worker.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.time()))
    left = [
        wid
        for wid, worker in workers
        if worker.get("thread") is None or worker["thread"].is_alive()
    ]
    if left:
        killed = _kill_surviving_worker_pids(left, grace=grace)
        logger.warning(
            f"Shutdown: {len(left)} worker(s) still running after "
            f"{join_seconds:.0f}s ({', '.join(left)}) — killed {killed} "
            "session group(s)"
        )
    return left


def _kill_surviving_worker_pids(task_ids: list, grace: float | None = None) -> int:
    """Kill claude worker processes recorded for released orphan tasks.

    A daemon restart kills the daemon's worker THREADS but not the claude CLI
    subprocesses they spawned. Releasing an orphan task while its previous-
    generation worker still runs creates duplicate concurrent execution — two
    workers racing on the same task (2026-07-06: duplicates destroyed each
    other's uncommitted work). employee_activator records worker PIDs in
    .company/state/worker_pids.json; kill any that are still alive and still
    look like claude workers. Entries whose PID is dead or reused by a
    non-claude process are skipped.

    E1 (ENGINE-FINDINGS 2026-09-20): the recorded pid is ``uv``, and the
    claude session is its child. An entry that records the session's process
    group (``pgid``, written since E1) has the whole group killed — even if
    ``uv`` itself is already gone — but only when the group is still the one
    the entry recorded (``_registered_group_is_ours``): after a reboot or pid
    reuse the same number can be someone else's ``claude``. All such groups
    are killed together with one shared grace. Older entries without a pgid
    keep the single-pid kill.
    """
    if not task_ids:
        return 0
    killed = 0
    try:
        _ensure_imports()
        pid_file = company_resolver.get_company_dir() / "state" / "worker_pids.json"
        if not pid_file.exists():
            return 0
        with open(pid_file, encoding="utf-8") as f:
            records = json.load(f)
        groups: list[int] = []
        for task_id in task_ids:
            rec = records.get(str(task_id)) or {}
            pid = rec.get("pid")
            if not isinstance(pid, int) or pid <= 1:
                continue
            pgid = rec.get("pgid")
            if _is_killable_worker_group(pid, pgid):
                ours, why = _registered_group_is_ours(rec, pgid)
                if not ours:
                    logger.info(
                        f"Worker kill: task {task_id} pgid={pgid} is a stale "
                        f"entry, not ours ({why}) — left alone"
                    )
                    continue
                groups.append(pgid)
                logger.info(
                    f"Worker kill: killing session group pgid={pgid} for task {task_id}"
                )
                continue
            try:
                ps_out = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
            except Exception:
                continue
            if "claude" not in ps_out:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
                logger.info(
                    f"Orphan cleanup: killed surviving worker pid={pid} "
                    f"for task {task_id}"
                )
            except OSError:
                pass
        killed += _kill_registered_worker_groups(groups, grace)
    except Exception as e:
        logger.debug(f"Surviving-worker kill failed (non-fatal): {e}")
    return killed


def _get_worker_timeout_seconds(config: DaemonConfig, complexity: str) -> float:
    """Return the force-collect wall-clock timeout for a worker.

    The base timeout (config.worker_timeout_seconds) is multiplied by the
    per-complexity factor so epic/complex tasks are not killed mid-flight.
    """
    multiplier = config.worker_timeout_multipliers.get(complexity, 1.0)
    return config.worker_timeout_seconds * multiplier


def _force_collect_on_timeout(
    task_id: str,
    worker: dict,
    worker_age: float,
    config: DaemonConfig,
) -> bool:
    """Kill a timed-out worker's subprocess and mark the slot failed.

    Called each collection cycle. If the worker has not yet exceeded its
    complexity-scaled timeout (or its thread has already stopped), returns
    False without touching anything.  When the timeout IS exceeded and the
    thread is still alive:
      1. Kills the recorded claude subprocess via _kill_surviving_worker_pids
         so the orphaned process cannot continue and deliver a duplicate.
      2. Sets worker["result"] to {"action": "failed", ...}.
      3. Returns True so the caller proceeds to failure recovery / requeue.

    2026-07-06 incident: a fixed 40-min cap force-collected two epic tasks
    while the underlying subprocesses kept running; both later finished,
    producing PR #34+#35 duplicates and a '(recovered)' husk that exhausted
    3/3 retries and blocked while its original completed with merged PR #37.
    """
    complexity = worker.get("task_complexity", "standard")
    effective_timeout = _get_worker_timeout_seconds(config, complexity)
    # CHALLENGER-STAGE: a task may declare a longer wall-clock budget.
    effective_timeout = max(
        effective_timeout, float(worker.get("task_timeout_seconds") or 0)
    )
    thread = worker.get("thread")
    if worker_age <= effective_timeout or not thread or not thread.is_alive():
        return False
    logger.warning(
        f"[Execute] Worker {task_id} timed out after {worker_age:.0f}s "
        f"(limit={effective_timeout:.0f}s, complexity={complexity}) — force-collecting"
    )
    # Kill the surviving subprocess BEFORE marking as failed so recovery
    # cannot requeue the task while the original is still running.
    _kill_surviving_worker_pids([task_id])
    worker["result"] = {
        "action": "failed",
        "reason": f"Worker timed out (>{effective_timeout:.0f}s)",
    }
    return True


def _worktree_has_unpushed_work(entry: Path) -> bool:
    """True when a worktree's branch holds commits that exist nowhere else.

    P1-R4 guard: force-removing such a worktree destroys finished work (the
    2026-07-07 R3 build sat committed-but-unpushed after a capture failure and
    would have been wiped at the 2h threshold). A worktree is "safe to remove"
    only when its HEAD commits are on origin/main or pushed to the branch's
    remote. Fail SAFE: any git error → True (do not delete what we cannot
    assess; the stranded-harvest sweep will log it).
    """
    try:
        is_repo = subprocess.run(
            ["git", "-C", str(entry), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if is_repo.returncode != 0:
            return False  # not a git worktree — no committed work to lose
        ahead = subprocess.run(
            ["git", "-C", str(entry), "rev-list", "--count", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if ahead.returncode != 0:
            return True
        if int(ahead.stdout.strip() or "0") == 0:
            return False  # nothing beyond origin/main — safe
        branch = subprocess.run(
            ["git", "-C", str(entry), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        branch_name = (branch.stdout or "").strip()
        if branch.returncode != 0 or not branch_name or branch_name == "HEAD":
            return True  # detached with ahead-commits — protect
        pushed = subprocess.run(
            [
                "git",
                "-C",
                str(entry),
                "rev-list",
                "--count",
                f"origin/{branch_name}..HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if pushed.returncode != 0:
            return True  # no remote branch — the ahead-commits are local-only
        return int(pushed.stdout.strip() or "0") > 0
    except Exception:
        return True


def _worktree_base() -> Path:
    """Per-project worktree base (/tmp/forge-worktrees/<project-id>).

    Namespaced so GC/harvest below never touches another project's
    worktrees — the base is machine-global and multiple Gatesmith daemons
    can run on one machine. Falls back to the legacy shared base only
    if the resolver import fails (must never block daemon operation).
    """
    try:
        from company_resolver import get_worktree_base

        return get_worktree_base()
    except Exception:
        return Path("/tmp/forge-worktrees")


def _state_dir() -> Path:
    """``.company/state`` via company_resolver — hermetic-aware under a test run.

    A bare ``Path(".company/state")`` is the primary checkout whenever pytest
    runs there: stranded_harvest.jsonl gained 16 fixture rows during one suite
    run on 2026-08-30 through exactly that path. The resolver redirects the
    framework's own .company to FORGE_HERMETIC_COMPANY_DIR; in production the
    two are the same directory.
    """
    try:
        return Path(_git_lock_policy().get_company_dir()) / "state"
    except Exception:
        return Path(".company") / "state"


def _append_harvest_audit(record: dict) -> None:
    """Append one JSONL line to the stranded-harvest audit (best-effort)."""
    try:
        path = _state_dir() / "stranded_harvest.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _load_worker_pids() -> dict[str, int]:
    """Read task_id -> pid from the worker-liveness registry (best-effort).

    Written by employee_activator's ``_record_worker_pid``; shared by every
    daemon-side sweep that must not act on a worktree/task a live worker
    still owns (stranded harvest, worktree GC, stranded-task release).
    """
    worker_pids: dict[str, int] = {}
    try:
        pid_file = Path(".company") / "state" / "worker_pids.json"
        if pid_file.exists():
            with open(pid_file, encoding="utf-8") as f:
                for tid, rec in (json.load(f) or {}).items():
                    pid = rec.get("pid") if isinstance(rec, dict) else int(rec)
                    if pid:
                        worker_pids[tid] = int(pid)
    except Exception:
        pass
    return worker_pids


def _process_cwd(pid: int) -> Path | None:
    """Best-effort current working directory of a live process via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return Path(line[1:])
    except Exception:
        return None
    return None


def _worktree_has_live_worker(entry: Path, worker_pids: dict[str, int]) -> bool:
    """True if a live worker is still working inside this worktree directory.

    2026-08-30 incident: the worktree GC removed a worker's active worktree
    37 minutes into its session because its liveness rule only looked at
    directory mtime, never at whether a process was actually working inside
    it — the worker survived another 30 minutes in an empty directory with
    no way to commit. The stranded-harvest sweep already refuses to touch a
    worktree whose task has a live registered pid (see
    ``_run_stranded_harvest``); this is that same check, plus a cwd fallback
    for the case where the task_id parsed from the directory name is not the
    pid record's key, made shared so every caller that can delete a worktree
    uses it.
    """
    task_id = entry.name.rsplit("-", 1)[0]
    matched_pid = worker_pids.get(task_id)
    if matched_pid and is_process_alive(matched_pid):
        return True
    try:
        entry_resolved = entry.resolve()
    except Exception:
        entry_resolved = entry
    for pid in worker_pids.values():
        if pid == matched_pid or not is_process_alive(pid):
            continue
        cwd = _process_cwd(pid)
        if cwd is None:
            continue
        try:
            cwd.relative_to(entry_resolved)
            return True
        except ValueError:
            continue
    return False


def _should_run_stranded_harvest(config: DaemonConfig, state: DaemonState) -> bool:
    """Time-gate the stranded-worktree harvest (P1-R4). <=0 disables."""
    if config.stranded_harvest_interval_seconds <= 0:
        return False
    if not state.last_stranded_harvest:
        return True
    try:
        last_run = datetime.fromisoformat(
            state.last_stranded_harvest.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed >= config.stranded_harvest_interval_seconds
    except (ValueError, TypeError):
        return True


def _run_stranded_harvest(
    config: DaemonConfig,
    state: DaemonState,
    *,
    base: Path | None = None,
    runner=None,
    min_age_seconds: int = 300,
) -> dict:
    """Harvest committed-but-unshipped work from dead workers' worktrees.

    P1-R4: a worker can complete (exit 0, work committed on its worktree
    branch) and still produce no PR — capture failure, or the task was
    orphan-released mid-run so collection never fired (observed 2026-07-07:
    the R3 build needed a manual harvest as PR #59). This sweep makes that
    outcome impossible to miss and usually self-healing:

    * worktree branch ahead with unpushed commits + worker dead + older than
      ``min_age_seconds`` → push the branch, open a PR, move the task to
      ``pr_open`` with the PR url (the reconcile machinery owns it from
      there), and write an audit line to ``stranded_harvest.jsonl``.
    * any failure along the way → WARNING log + audit line with the error —
      never silent.

    ``runner`` is injectable for tests (used for git-push and gh calls).
    """
    run = runner or subprocess.run
    # Lazy module resolution (house pattern) without clobbering test patches.
    # The global is named `work_allocator` (not `wa`) — use that key.
    if globals().get("work_allocator") is None:
        try:
            _ensure_imports()
        except Exception:
            pass
    _wa = globals().get("work_allocator")
    wt_base = base if base is not None else _worktree_base()
    state.last_stranded_harvest = datetime.now(timezone.utc).isoformat()
    result = {"scanned": 0, "harvested": 0, "failed": 0, "skipped": 0}
    if not wt_base.exists():
        return result

    # Worker liveness registry (written by employee_activator)
    worker_pids = _load_worker_pids()

    now = time.time()
    for entry in list(wt_base.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        result["scanned"] += 1
        # Worktree dir name is "<task_id>-<hex suffix>"
        task_id = entry.name.rsplit("-", 1)[0]
        try:
            if now - entry.stat().st_mtime < min_age_seconds:
                result["skipped"] += 1
                continue  # worker may be between exit and capture
        except OSError:
            result["skipped"] += 1
            continue
        if _worktree_has_live_worker(entry, worker_pids):
            result["skipped"] += 1
            continue  # live worker — not ours to touch
        if not _worktree_has_unpushed_work(entry):
            result["skipped"] += 1
            continue

        try:
            branch = subprocess.run(
                ["git", "-C", str(entry), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            if not branch or branch == "HEAD":
                raise RuntimeError("detached HEAD — cannot harvest a branch")

            # Skip if a PR already exists for this branch (any state)
            pr_check = run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    branch,
                    "--state",
                    "all",
                    "--json",
                    "number,url,state,headRefName",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            existing_prs = (
                json.loads(pr_check.stdout or "[]") if pr_check.returncode == 0 else []
            )
            if existing_prs:
                # Someone else opened it (a team harvest, a retried capture).
                # Skipping alone left the task with no pr_url, so it sat in
                # blocked as "no deliverable" and autofill minted a twin.
                try:
                    from . import pr_reality_check as _prc
                except ImportError:
                    import pr_reality_check as _prc  # type: ignore[no-redef]
                recorded = _prc.record_existing_pr(
                    _wa,
                    task_id,
                    existing_prs,
                    branch,
                    note="stranded-harvest: PR already open for this branch",
                )
                if recorded:
                    _append_harvest_audit(
                        {
                            "task_id": task_id,
                            "branch": branch,
                            "action": "pr_recorded",
                        }
                    )
                result["skipped"] += 1
                continue

            # PR-SCRATCH-GUARD (#674): refuse before anything is pushed when
            # the committed work adds scratch or unbriefed root files; the
            # except below records the refusal in the harvest audit.
            try:
                from . import pr_scratch_guard as _sg
            except ImportError:
                import pr_scratch_guard as _sg  # type: ignore[no-redef]
            _sg.raise_if_refused(entry, task_id=task_id)

            push = run(
                ["git", "-C", str(entry), "push", "-u", "origin", branch],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if push.returncode != 0:
                raise RuntimeError(f"push failed: {(push.stderr or '')[:200]}")

            title = task_id
            try:
                if _wa is not None:
                    q = _wa.load_queue()
                    for lane in (
                        "pending",
                        "in_progress",
                        "blocked",
                        "failed",
                        "pr_open",
                    ):
                        for t in q.get(lane, []) or []:
                            if t.get("task_id") == task_id and t.get("title"):
                                title = t["title"]
                                break
            except Exception:
                pass

            # Opens held (verdict-pending): a harvested PR was never judged
            # (#498 merged via ci.yml 80 s after creation, no verdict). The
            # `[task_id]` suffix lets the merge loop find the task and judge it.
            try:
                hold_args = _verdict_hold().create_args(run=run)
            except Exception as _vh_err:
                logger.warning(
                    f"[Harvest] verdict-pending label setup failed: {_vh_err}"
                )
                hold_args = []
            pr = run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--head",
                    branch,
                    "--title",
                    f"fix(daemon): harvested stranded work — {str(title)[:70]} "
                    f"[{task_id}]",
                    "--body",
                    (
                        "Automated stranded-worktree harvest (P1-R4): the worker "
                        f"for `{task_id}` completed with committed work on "
                        f"`{branch}` but no PR was captured. The daemon pushed "
                        "the branch and opened this PR; the task now sits in "
                        "pr_open and follows the normal merge/reconcile path. "
                        "It stays held (verdict-pending) until the deliverable "
                        "judge passes its head."
                    ),
                    *hold_args,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if pr.returncode != 0:
                raise RuntimeError(f"pr create failed: {(pr.stderr or '')[:200]}")
            pr_url = (pr.stdout or "").strip().splitlines()[-1] if pr.stdout else ""

            moved = False
            try:
                if _wa is not None:
                    upd = _wa.update_task(
                        task_id,
                        status="pr_open",
                        pr_url=pr_url or None,
                        branch_name=branch,
                        notes="stranded-harvest: PR created from orphaned worktree",
                    )
                    moved = bool(upd.get("success"))
            except Exception:
                moved = False

            logger.info(
                f"[Harvest] Stranded worktree {entry.name}: pushed {branch}, "
                f"opened {pr_url or 'PR'} (task moved to pr_open: {moved})"
            )
            _append_harvest_audit(
                {
                    "task_id": task_id,
                    "branch": branch,
                    "action": "harvested",
                    "pr_url": pr_url,
                    "task_moved": moved,
                }
            )
            result["harvested"] += 1
        except Exception as exc:
            logger.warning(
                f"[Harvest] Stranded worktree {entry.name} could NOT be "
                f"harvested: {exc} — manual: git -C {entry} push -u origin "
                f"<branch> && gh pr create --head <branch>"
            )
            _append_harvest_audit(
                {"task_id": task_id, "action": "failed", "error": str(exc)[:300]}
            )
            result["failed"] += 1

    return result


def _cleanup_stale_worktrees_at_startup(
    base: Path, max_age_seconds: int = 7200
) -> tuple[int, int]:
    """Remove stale worktrees left by previous daemon runs; spare recent ones.

    2026-07-06: this used to remove EVERYTHING under /tmp/forge-worktrees on
    startup, assuming nothing could be using them ("runs before workers
    spawn"). That assumption is false across daemon generations: an
    engine-update restart leaves the previous daemon's claude workers running,
    and wiping their worktrees destroys uncommitted work mid-task. Only remove
    entries older than max_age_seconds (matching the periodic GC's threshold);
    the periodic GC reaps younger ones later once they are truly inactive.

    Returns (removed_count, skipped_recent_count).
    """
    removed = 0
    skipped_recent = 0
    if not base.exists():
        return (0, 0)
    base_resolved = base.resolve()
    now = time.time()
    worker_pids = _load_worker_pids()
    for entry in list(base.iterdir()):
        # Security: Skip symlinks to prevent path traversal attacks
        if entry.is_symlink():
            logger.warning(f"WS-057: Skipping symlink {entry} (security)")
            continue
        try:
            resolved = entry.resolve()
            if not str(resolved).startswith(str(base_resolved) + "/"):
                logger.warning(f"WS-057: Path escapes base dir, skipping: {entry}")
                continue
        except Exception:
            continue
        if not entry.is_dir():
            continue
        # 2026-08-30: directory mtime alone is not a liveness signal — a
        # worker doing all its work in nested subdirectories never touches
        # the worktree root's mtime, so an active worker's worktree can
        # already look "stale" by this clock. Check for a live worker
        # unconditionally, before the age gate, and never remove its
        # worktree regardless of how old the root directory appears.
        if _worktree_has_live_worker(entry, worker_pids):
            logger.warning(
                f"WS-057: worktree {entry.name} looks stale by mtime but has "
                "a live worker process inside it — sparing it"
            )
            skipped_recent += 1
            continue
        try:
            if now - entry.stat().st_mtime <= max_age_seconds:
                skipped_recent += 1
                continue
        except OSError:
            continue
        # P1-R4: never delete a worktree whose commits exist nowhere else
        if _worktree_has_unpushed_work(entry):
            logger.warning(
                f"WS-057: worktree {entry.name} is stale but holds UNPUSHED "
                f"committed work — sparing it (stranded-harvest will handle)"
            )
            skipped_recent += 1
            continue
        try:
            # No lock needed here - runs during init before this daemon's
            # own workers spawn
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(entry)],
                capture_output=True,
                timeout=10,
            )
            removed += 1
        except Exception:
            pass
    return (removed, skipped_recent)


def _spawn_cooldown_remaining(
    recent_spawns: dict, task_id: str, now: float, cooldown_seconds: float = 120.0
) -> float:
    """Seconds left before task_id may spawn another worker (0 = allowed).

    2026-07-06: three workers were spawned for one task within 40 seconds when
    per-spawn bookkeeping failed; concurrent duplicates destroy each other's
    work. This cooldown makes rapid respawn structurally impossible whatever
    the bookkeeping bug of the day is.
    """
    last = recent_spawns.get(task_id)
    if last is None:
        return 0.0
    remaining = cooldown_seconds - (now - last)
    return remaining if remaining > 0 else 0.0


def _pick_next_pending_task(
    pending: list,
    active_ids: set,
    recent_spawns: dict,
    now: float,
    default_max_attempts: int = 3,
) -> tuple[dict | None, list]:
    """Pick the next dispatchable pending task for this cycle.

    Returns ``(next_task, exhausted_tasks)``. A candidate is passed over when it
    already has an active worker; it is returned in ``exhausted_tasks`` (for the
    caller to move to blocked) when its retries are spent; and it is skipped when
    its respawn cooldown is still running, so selection moves on to the next
    eligible task instead of stopping on one it cannot dispatch.

    2026-09-04 incident (task-...-9fc25d): the respawn cooldown was consulted
    only *after* the task had been selected, reserved, routed and logged, and
    the refusal then ended the cycle with ``continue``. One cooling-down task
    therefore claimed 61 consecutive dispatch cycles for itself — every one of
    them refused — while worker slots sat free and other pending tasks waited.
    Filtering here keeps a task the cooldown would refuse out of this cycle's
    candidate list. The guard at spawn time stays as defence in depth.
    """
    exhausted: list = []
    for candidate in pending:
        cid = candidate.get("task_id")
        # WS-101: Check if task is exhausted (retry_count >= max_attempts)
        if candidate.get("retry_count", 0) >= candidate.get(
            "max_attempts", default_max_attempts
        ):
            exhausted.append(candidate)
            continue
        if not cid or cid in active_ids:
            continue
        if _spawn_cooldown_remaining(recent_spawns, cid, now) > 0:
            continue
        return candidate, exhausted
    return None, exhausted


def _dispatch_blocked_by_live_worker(task_id: str) -> int | None:
    """Return the live worker pid for ``task_id``, or None if dispatch may proceed.

    2026-08-30 incident: a second worker was dispatched for a task while the
    first was demonstrably still alive, because the in-memory
    ``_active_workers`` slot had already been released — the task's queue
    entry showed ``in_progress`` with a ``failed`` result before the worker
    subprocess had actually exited (a torn-down worktree broke the worker's
    git commands mid-session without killing the worker process itself).
    Whatever wrote that queue state, dispatch must never trust it alone.
    This checks the on-disk pid registry by PROCESS liveness (``os.kill``),
    not a queue field something else may have written.
    """
    pid = _load_worker_pids().get(task_id)
    if pid and is_process_alive(pid):
        return pid
    return None


# -----------------------------------------------------------------------------
# Stranded Task Cleanup (per-cycle): worker died but daemon still alive
# -----------------------------------------------------------------------------


def _cleanup_stranded_tasks(
    state, current_agent_id: str, grace_seconds: int = 1800
) -> dict[str, int]:
    """Release tasks owned by current daemon that have no live worker.

    Complements `_cleanup_orphan_tasks` (which only handles foreign-daemon
    orphans on startup). This runs per-cycle so the queue self-heals when a
    worker subprocess dies (manual kill, crash, OOM) while the daemon is
    still running. Without this, the task is forever stranded — assigned to
    a live daemon but with no worker actually executing it.

    Detection criteria:
      - Task is in `in_progress` or `blocked` lane
      - Task is assigned to the current daemon (matches PID)
      - Task's `task_id` is NOT in `state._active_workers` (no live thread)
      - Task has no `blocked_reason` (respect human/operator-set blocks)
      - Task has no `operator_hold` and is not cancelled (gatesmith-queue hold/cancel)
      - Task has no `dependencies` (intentional dep-blocks)
      - Task was started > `grace_seconds` ago (avoid races during the
        reservation → planning → execution startup window)

    Args:
        state: Current daemon state with `_active_workers` dict.
        current_agent_id: This daemon's full agent ID (e.g. 'daemon-12345-1720000000').
        grace_seconds: Minimum task age before considered stranded.
            Default 1800 (30 min) matches WS-101 grace for complex tasks.

    Returns:
        Dict with counts: stranded_found, released
    """
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        queue_path = company_dir / "state/work_queue.json"
        lock_path = company_dir / "runtime/queue.lock"

        if not queue_path.exists():
            return {"stranded_found": 0, "released": 0}

        active_ids = set((getattr(state, "_active_workers", {}) or {}).keys())
        now = datetime.now(timezone.utc)

        # Lock-free pre-check: read the queue without acquiring the file lock
        # and bail out if there's nothing to do. Most cycles have zero
        # current-daemon tasks in in_progress/blocked, so acquiring the file
        # lock every cycle is pure contention overhead — and on macOS that
        # contention surfaced as recurring [Errno 11] EDEADLK errors that
        # eventually tripped the circuit breaker (PR #979 follow-up).
        # Reads are safe without the lock: writers are still serialized, we
        # may see a slightly stale snapshot, but that's fine for a sweep
        # that runs every cycle anyway.
        try:
            with open(queue_path, encoding="utf-8") as f:
                preview_queue = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"stranded_found": 0, "released": 0}

        # Load worker PID registry once for the batch — avoids repeated I/O
        # and is safe because we hold no lock at this point.
        _worker_pids: dict[str, int] = {}
        try:
            _pid_file = (
                company_resolver.get_company_dir() / "state" / "worker_pids.json"
            )
            if _pid_file.exists():
                with open(_pid_file, encoding="utf-8") as _pf:
                    _records = json.load(_pf)
                for _tid, _rec in (_records or {}).items():
                    _pid = _rec.get("pid") if isinstance(_rec, dict) else int(_rec)
                    if _pid:
                        _worker_pids[_tid] = int(_pid)
        except Exception:
            pass  # non-fatal: PID check is advisory; err on the side of safety

        def _is_candidate(task: dict, source: str) -> bool:
            if not _is_current_daemon_task(task, current_agent_id):
                return False
            tid = task.get("task_id")
            if not tid or tid in active_ids:
                return False
            # Never release a task whose worker subprocess is still alive.
            # Threads in _active_workers can terminate before the subprocess
            # finishes (e.g. during the 2400 s timeout force-collect path),
            # leaving an orphan that would race with the requeue.
            _wpid = _worker_pids.get(tid)
            if _wpid and is_process_alive(_wpid):
                return False
            if task.get("blocked_reason") or task.get("dependencies"):
                return False
            # QUEUE-CANCEL-STATUS: an operator hold or a cancel is a human's
            # stop — this sweep only recovers tasks whose worker DIED.
            if task.get("operator_hold") is True or task.get("status") == "cancelled":
                return False
            # Escalated tasks are terminal until a human resolves them —
            # releasing them re-creates the spawn→fail→escalate loop. Guards
            # historical entries that predate blocked_reason being set on
            # escalation (operation_loop release path).
            if task.get("status") == "escalated":
                return False
            started_at = task.get("started_at") or task.get("assigned_at")
            if started_at:
                try:
                    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    if (now - dt).total_seconds() < grace_seconds:
                        return False
                except (ValueError, TypeError):
                    pass
            return True

        has_candidate = any(
            _is_candidate(task, source)
            for source in ("in_progress", "blocked")
            for task in preview_queue.get(source, [])
        )
        if not has_candidate:
            return {"stranded_found": 0, "released": 0}

        # Re-read inside the lock and re-check candidates — task state may
        # have changed between the lock-free preview and acquiring the lock.
        with work_allocator.QueueLock(lock_path):
            with open(queue_path, encoding="utf-8") as f:
                queue = json.load(f)

            stranded: list[tuple[str, dict]] = []
            for source in ("in_progress", "blocked"):
                for task in queue.get(source, []):
                    if _is_candidate(task, source):
                        stranded.append((source, task))

            if not stranded:
                return {"stranded_found": 0, "released": 0}

            for source, task in stranded:
                tid = task.get("task_id")
                queue[source] = [t for t in queue[source] if t.get("task_id") != tid]
                task["assigned_to"] = None
                task["assigned_at"] = None
                task["claimed_by"] = None
                task["claimed_at"] = None
                task["started_at"] = None
                # Reset created_at so stale-task cleanup doesn't immediately archive
                # a just-recovered task whose original created_at > 24h.
                task["created_at"] = datetime.now(timezone.utc).isoformat()
                task["recovery_note"] = (
                    f"Released as stranded from {source} "
                    f"(no live worker for {current_agent_id})"
                )
                inserted = work_allocator.guarded_requeue_to_pending(queue, task)
                if inserted:
                    logger.info(
                        f"[Stranded] Released {tid} from {source}: "
                        f"no worker subprocess; daemon alive"
                    )
                else:
                    logger.info(
                        f"[Stranded] Skipped duplicate requeue for {tid} "
                        f"(already in pending)"
                    )

            fd, tmp = tempfile.mkstemp(
                dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        return {"stranded_found": len(stranded), "released": len(stranded)}

    except Exception as e:
        logger.error(f"Stranded cleanup failed: {e}")
        return {"stranded_found": 0, "released": 0}


# -----------------------------------------------------------------------------
# Document Approval Scan (P23)
# -----------------------------------------------------------------------------


def _should_run_document_scan(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run document approval scan.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if document scan should run.
    """
    if not config.document_scan_enabled:
        return False

    if not state.last_document_scan:
        return True  # Never scanned before

    try:
        last_scan = datetime.fromisoformat(
            state.last_document_scan.replace("Z", "+00:00")
        )
        elapsed = (datetime.now(timezone.utc) - last_scan).total_seconds()
        return elapsed >= (config.document_scan_interval_minutes * 60)
    except (ValueError, TypeError):
        return True


def _run_document_scan(state: DaemonState) -> dict[str, int]:
    """Run document approval scan.

    Scans for documents with approval frontmatter and routes them
    for approval based on their tier.

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with counts: discovered, routed, pending
    """
    _ensure_imports()

    result = {
        "discovered": 0,
        "routed": 0,
        "pending": 0,
    }
    if document_approvals is None:  # not in the public export
        return result

    try:
        # Scan for documents needing approval
        discovered = document_approvals.scan_documents_for_approval()
        result["discovered"] = len(discovered)

        if not discovered:
            logger.debug("Document scan: no documents found requiring approval")
            state.last_document_scan = datetime.now(timezone.utc).isoformat()
            return result

        logger.info(
            f"Document scan: found {len(discovered)} document(s) requiring approval"
        )

        # Load state and route each document
        approval_state = document_approvals.load_state()

        for doc in discovered:
            routing = document_approvals.route_for_approval(doc, approval_state)
            logger.info(
                f"Document routed: {doc.title} ({doc.approval_tier}) - "
                f"{routing['routing_logic']}"
            )
            result["routed"] += 1

        # Save state
        approval_state.last_scan = datetime.now(timezone.utc).isoformat()
        document_approvals.save_state(approval_state)

        result["pending"] = len(approval_state.pending_review)

        # Update daemon state
        state.last_document_scan = datetime.now(timezone.utc).isoformat()
        state.documents_discovered += result["discovered"]

        return result

    except Exception as e:
        logger.error(f"Document scan failed: {e}")
        # Still update time to avoid retry storm
        state.last_document_scan = datetime.now(timezone.utc).isoformat()
        return result


# -----------------------------------------------------------------------------
# Roadmap Scheduling (P27)
# -----------------------------------------------------------------------------


def _should_run_roadmap_scheduling(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run roadmap scheduling scan.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if roadmap scheduling should run.
    """
    if not config.roadmap_scheduling_enabled:
        return False

    if not state.last_roadmap_scan:
        return True  # Never scanned before

    try:
        last_scan = datetime.fromisoformat(
            state.last_roadmap_scan.replace("Z", "+00:00")
        )
        elapsed_minutes = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60
        return elapsed_minutes >= config.roadmap_scan_interval_minutes
    except (ValueError, TypeError):
        return True


def _run_roadmap_scheduling(config: DaemonConfig, state: DaemonState) -> dict[str, int]:
    """Run roadmap scheduling scan.

    Parses ROADMAP.md files and schedules eligible tasks to the work queue.

    Args:
        config: Daemon configuration.
        state: Current daemon state (will be updated).

    Returns:
        Dict with counts: tasks_found, tasks_scheduled, tasks_skipped
    """
    _ensure_imports()

    result = {
        "tasks_found": 0,
        "tasks_scheduled": 0,
        "tasks_skipped": 0,
        "tasks_failed": 0,
    }

    try:
        # Run the scheduling scan
        scan_result = roadmap_scheduler.run_scheduling_scan()

        result["tasks_found"] = scan_result.get("tasks_found", 0)
        result["tasks_scheduled"] = scan_result.get("tasks_scheduled", 0)
        result["tasks_skipped"] = scan_result.get("tasks_skipped", 0)
        result["tasks_failed"] = scan_result.get("tasks_failed", 0)

        # Update state
        state.last_roadmap_scan = datetime.now(timezone.utc).isoformat()
        state.roadmap_tasks_scheduled += result["tasks_scheduled"]
        state.roadmap_current_wave = scan_result.get("current_wave", 1)

        if result["tasks_scheduled"] > 0:
            logger.info(
                f"Roadmap scheduling: scheduled {result['tasks_scheduled']} tasks "
                f"from {result['tasks_found']} found (wave {state.roadmap_current_wave})"
            )
        else:
            logger.debug(
                f"Roadmap scheduling: {result['tasks_found']} tasks found, "
                f"none scheduled (wave {state.roadmap_current_wave})"
            )

        return result

    except Exception as e:
        logger.error(f"Roadmap scheduling failed: {e}")
        # Still update time to avoid retry storm
        state.last_roadmap_scan = datetime.now(timezone.utc).isoformat()
        return result


# -----------------------------------------------------------------------------
# GSD/BMAD Planning Integration (P20)
# -----------------------------------------------------------------------------

# Complexity levels that require planning (standard and above)
PLANNING_COMPLEXITY_LEVELS = ["standard", "complex", "epic"]


def _should_plan_task(task: dict, config: DaemonConfig) -> bool:
    """Determine if a task should go through planning pipeline.

    Args:
        task: Task dictionary from work queue.
        config: Daemon configuration.

    Returns:
        True if task complexity meets planning threshold.
    """
    if not config.planning_enabled:
        return False

    # Get task complexity (or detect it)
    complexity = task.get("complexity", task.get("estimated_complexity", ""))

    if not complexity:
        # Detect complexity from description
        _ensure_imports()
        try:
            description = task.get("description", task.get("title", ""))
            if task_planner is not None:
                plan = task_planner.plan_task(
                    {"task_id": "detect", "description": description}
                )
                complexity = plan.complexity
            else:
                complexity = "trivial"
        except Exception:
            complexity = "trivial"

    # Check against threshold
    threshold_index = (
        PLANNING_COMPLEXITY_LEVELS.index(config.planning_complexity_threshold)
        if config.planning_complexity_threshold in PLANNING_COMPLEXITY_LEVELS
        else 0
    )

    if complexity in PLANNING_COMPLEXITY_LEVELS:
        task_index = PLANNING_COMPLEXITY_LEVELS.index(complexity)
        return task_index >= threshold_index

    return False


def _plan_and_validate_task(
    task: dict,
    config: DaemonConfig,
    state: DaemonState,
) -> dict[str, Any]:
    """Plan a task and validate the plan.

    Implements the planner -> checker loop with revision attempts.

    Args:
        task: Task dictionary from work queue.
        config: Daemon configuration.
        state: Daemon state (updated with metrics).

    Returns:
        Dict with:
            - success: bool
            - plan: TaskPlan dict (if successful)
            - waves: List of wave dicts (if successful)
            - verdict: CheckerVerdict string
            - score: int (plan validation score)
            - reason: str (if failed)
    """
    _ensure_imports()

    task_id = task.get("task_id", "unknown")
    description = task.get("description", task.get("title", ""))

    result: dict[str, Any] = {
        "success": False,
        "plan": None,
        "waves": None,
        "verdict": None,
        "score": 0,
        "reason": "",
    }

    # Check if planning modules are available
    if task_planner is None or plan_checker is None:
        result["reason"] = "Planning modules not available"
        state.planning_failures += 1
        return result

    try:
        # Create initial plan
        logger.info(
            f"Planning task {task_id} (complexity: {task.get('complexity', 'detecting')})"
        )

        plan = task_planner.plan_task(
            {
                "task_id": task_id,
                "description": description,
                "complexity": task.get("complexity"),
                "files": task.get("files", []),
                "required_capabilities": task.get("required_capabilities", []),
            }
        )

        logger.info(
            f"Plan created for {task_id}: {len(plan.subtasks)} subtasks, "
            f"complexity={plan.complexity}, estimated={plan.total_estimated_minutes}min"
        )

        # Validate plan
        plan_dict = plan.to_dict()
        check_result = plan_checker.validate_plan(plan_dict)

        result["score"] = check_result.score
        result["verdict"] = check_result.verdict.value

        logger.info(
            f"Plan validated (score: {check_result.score}/25, verdict: {check_result.verdict.value})"
        )

        # Handle verdict
        if check_result.verdict == plan_checker.CheckerVerdict.PASS:
            # Plan passed - extract waves
            waves = task_planner.decompose_to_waves(plan)
            result["success"] = True
            result["plan"] = plan_dict
            result["waves"] = [w.to_dict() for w in waves]
            state.tasks_planned += 1
            return result

        elif check_result.verdict == plan_checker.CheckerVerdict.REVISE:
            # Attempt one revision
            if config.planning_max_revision_attempts > 0:
                logger.info(f"Plan needs revision, attempting re-plan for {task_id}")

                # Re-plan with issues as constraints
                revised_plan = task_planner.plan_task(
                    {
                        "task_id": task_id,
                        "description": description,
                        "complexity": plan.complexity,
                        "files": task.get("files", []),
                        "required_capabilities": task.get("required_capabilities", []),
                        "constraints": check_result.issues,  # Pass issues as constraints
                    }
                )

                # Re-validate
                revised_dict = revised_plan.to_dict()
                revised_check = plan_checker.validate_plan(revised_dict)

                result["score"] = revised_check.score
                result["verdict"] = revised_check.verdict.value

                logger.info(
                    f"Revised plan validated (score: {revised_check.score}/25, "
                    f"verdict: {revised_check.verdict.value})"
                )

                if revised_check.verdict == plan_checker.CheckerVerdict.PASS:
                    waves = task_planner.decompose_to_waves(revised_plan)
                    result["success"] = True
                    result["plan"] = revised_dict
                    result["waves"] = [w.to_dict() for w in waves]
                    state.tasks_planned += 1
                    return result

            # Revision failed or not attempted
            result["reason"] = (
                f"Plan revision failed: {', '.join(check_result.issues[:3])}"
            )
            state.planning_failures += 1

        else:  # ESCALATE
            result["reason"] = f"Plan escalated: {', '.join(check_result.issues[:3])}"
            state.planning_failures += 1

    except Exception as e:
        logger.error(f"Planning failed for {task_id}: {e}")
        result["reason"] = f"Planning exception: {str(e)}"
        state.planning_failures += 1

    return result


def _execute_planned_waves(
    task: dict,
    plan: dict,
    waves: list[dict],
    config: DaemonConfig,
    state: DaemonState,
    queue_path: Path,
    state_path: Path,
) -> dict[str, Any]:
    """Execute a planned task wave by wave.

    For each wave, executes subtasks and waits for completion
    before proceeding to the next wave.

    Args:
        task: Original task from work queue.
        plan: TaskPlan dictionary.
        waves: List of wave dictionaries.
        config: Daemon configuration.
        state: Daemon state.
        queue_path: Path to work queue.
        state_path: Path to session state.

    Returns:
        Dict with execution results.
    """
    _ensure_imports()

    task_id = task.get("task_id", "unknown")
    total_waves = len(waves)

    result: dict[str, Any] = {
        "success": True,
        "waves_completed": 0,
        "subtasks_completed": 0,
        "subtasks_failed": 0,
        "errors": [],
    }

    # Get subtasks lookup
    subtasks_by_id = {st["id"]: st for st in plan.get("subtasks", [])}

    for wave in waves:
        wave_num = wave.get("number", 0)
        subtask_ids = wave.get("subtask_ids", [])

        logger.info(f"Executing wave {wave_num}/{total_waves} for task {task_id}")

        for subtask_id in subtask_ids:
            subtask = subtasks_by_id.get(subtask_id, {})
            if not subtask:
                continue

            try:
                # Create a sub-task in the queue for this subtask
                subtask_description = subtask.get(
                    "description", f"Subtask {subtask_id}"
                )

                # Execute via operation_loop (existing mechanism)
                # For now, we log the execution intent
                # In a full implementation, this would create work items
                # and execute them through the existing employee activation
                logger.info(
                    f"[Wave {wave_num}] Subtask {subtask_id}: {subtask_description[:50]}..."
                )
                result["subtasks_completed"] += 1

            except Exception as e:
                logger.error(f"[Wave {wave_num}] Subtask {subtask_id} failed: {e}")
                result["subtasks_failed"] += 1
                result["errors"].append(f"Subtask {subtask_id}: {str(e)}")

        result["waves_completed"] += 1
        logger.info(f"[Wave {wave_num}/{total_waves}] Completed for task {task_id}")

    # Determine overall success
    if result["subtasks_failed"] > 0:
        result["success"] = False

    return result


# -----------------------------------------------------------------------------
# Employee Initiative (P26)
# -----------------------------------------------------------------------------


def _get_pending_queue_size() -> int:
    """Get the number of pending tasks in the queue."""
    _ensure_imports()
    try:
        queue = work_allocator.load_queue()
        return len(queue.get("pending", []))
    except Exception:
        return 0


def _should_run_employee_initiative(
    config: DaemonConfig, state: DaemonState, idle_cycles: int = 0
) -> bool:
    """Check if it's time to run employee initiative cycle.

    WS-016 Cascade Model:
    - Run when queue drops below threshold (event-driven)
    - Respect quiet period after executive activity
    - Fall back to interval-based if cascade disabled

    Idle-backoff gate: when the adaptive scheduler reports sustained idle cycles
    (queue has been empty repeatedly), initiative switches to a much slower
    cadence (configurable via ``employeeInitiative.idleCadenceMinutes``) or is
    suppressed entirely (``idleCadenceMinutes=0``).  This prevents burning
    Claude subscription sessions doing TODO-scans while there is no real work.

    Args:
        config: Daemon configuration.
        state: Current daemon state.
        idle_cycles: Consecutive idle cycles from the adaptive scheduler.

    Returns:
        True if employee initiative should run.
    """
    if not config.employee_initiative_enabled:
        return False

    now = datetime.now(timezone.utc)

    # Idle-backoff gate: sustained empty queue → use slow cadence or skip entirely.
    # Checked before the cascade/quiet-period logic so that even a low queue does
    # not trigger rapid initiative when nothing is producing real work signals.
    if idle_cycles >= config.employee_initiative_idle_backoff_threshold:
        idle_cadence = config.employee_initiative_idle_cadence_minutes
        if idle_cadence <= 0:
            return False
        if state.last_employee_initiative:
            try:
                last_run = datetime.fromisoformat(
                    state.last_employee_initiative.replace("Z", "+00:00")
                )
                if (now - last_run).total_seconds() < idle_cadence * 60:
                    return False
            except (ValueError, TypeError):
                pass
        return True

    # WS-016: Check quiet period after executive activity
    if config.cascade_enabled and state.last_executive_loop:
        try:
            exec_time = datetime.fromisoformat(
                state.last_executive_loop.replace("Z", "+00:00")
            )
            quiet_until = exec_time + timedelta(
                minutes=config.employee_quiet_after_exec_minutes
            )
            if now < quiet_until:
                # Still in quiet period after executive activity
                return False
        except (ValueError, TypeError):
            pass

    # WS-016: Check queue threshold (event-driven trigger)
    if config.cascade_enabled:
        pending_count = _get_pending_queue_size()
        if pending_count >= config.queue_low_threshold:
            # Queue has enough work, no need for initiative
            return False
        # Queue is low - should run initiative to fill it
        # But still respect minimum interval to prevent flooding
        if state.last_employee_initiative:
            try:
                last_run = datetime.fromisoformat(
                    state.last_employee_initiative.replace("Z", "+00:00")
                )
                # WI-004: Minimum 2.5 minute gap between initiative cycles (was 5 min)
                if (now - last_run).total_seconds() < 150:
                    return False
            except (ValueError, TypeError):
                pass
        return True

    # Fallback: Interval-based (if cascade disabled)
    if not state.last_employee_initiative:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_employee_initiative.replace("Z", "+00:00")
        )
        elapsed_minutes = (now - last_run).total_seconds() / 60
        return elapsed_minutes >= config.employee_initiative_interval_minutes
    except (ValueError, TypeError):
        return True


def _get_active_employees() -> list[str]:
    """Get list of active employee IDs from org.json."""
    _ensure_imports()

    try:
        company_dir = company_resolver.get_company_dir()
        org_path = company_dir / "org.json"

        if not org_path.exists():
            return []

        with open(org_path, "r") as f:
            org = json.load(f)
        # Normalize bare-string employees to dict records (ProjectK root-cause
        # fix). The persisted file is healed separately by the startup heal;
        # here we normalize in-memory. Import the real module locally so a test
        # that mocks the module-global company_resolver still normalizes.
        try:
            from . import company_resolver as _cr
        except ImportError:
            import company_resolver as _cr  # type: ignore[no-redef]
        org = _cr.normalize_org_employees(org, company_dir)

        employees = []
        for emp in org.get("employees", []):
            emp_id = emp.get("id")
            # Skip executives - they use executive_loop
            role = emp.get("role", "").lower()
            if emp_id and "ceo" not in role and "cto" not in role:
                employees.append(emp_id)

        return employees
    except (json.JSONDecodeError, OSError, RuntimeError):
        return []


def _run_employee_initiative(
    config: DaemonConfig,
    state: DaemonState,
) -> dict[str, int]:
    """Run employee initiative cycle.

    Selects a subset of employees and runs initiative generation for each.
    Generates and submits proposals based on TODOs and follow-ups.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        Dict with counts: employees_activated, proposals_submitted, proposals_rejected
    """
    _ensure_imports()

    result = {
        "employees_activated": 0,
        "proposals_submitted": 0,
        "proposals_rejected": 0,
    }
    if employee_initiative is None:  # not in the public export
        return result

    try:
        # Get active employees
        employees = _get_active_employees()

        if not employees:
            logger.debug("Employee initiative: no active employees found")
            state.last_employee_initiative = datetime.now(timezone.utc).isoformat()
            return result

        # Select subset of employees for this cycle
        import random

        selected = random.sample(
            employees, min(config.employee_initiative_max_per_cycle, len(employees))
        )

        logger.info(f"Employee initiative: activating {len(selected)} employees")

        for emp_id in selected:
            try:
                # Run initiative cycle for this employee
                cycle_result = employee_initiative.run_initiative_cycle(
                    employee_id=emp_id,
                    sources=["todos", "follow_ups"],
                    max_proposals=1,  # One proposal per employee per cycle
                    auto_submit=True,
                )

                result["employees_activated"] += 1

                if cycle_result.get("success"):
                    submitted = cycle_result.get("proposals_submitted", 0)
                    result["proposals_submitted"] += submitted
                    state.employee_proposals_submitted += submitted

                    if submitted > 0:
                        logger.info(
                            f"Employee initiative: {emp_id} submitted {submitted} proposal(s)"
                        )
                else:
                    error = cycle_result.get("error", "unknown")
                    logger.debug(f"Employee initiative: {emp_id} skipped - {error}")

            except Exception as e:
                logger.warning(f"Employee initiative failed for {emp_id}: {e}")
                result["proposals_rejected"] += 1

        # Update state
        state.last_employee_initiative = datetime.now(timezone.utc).isoformat()
        state.employee_initiative_cycles += 1

        return result

    except Exception as e:
        logger.error(f"Employee initiative cycle failed: {e}")
        state.last_employee_initiative = datetime.now(timezone.utc).isoformat()
        return result


# -----------------------------------------------------------------------------
# Compliance scan — venture scope -> review task routing (PR #966 follow-up)
# -----------------------------------------------------------------------------


def _run_compliance_scan(company_dir: Path) -> dict[str, Any]:
    """Per-cycle compliance scan for regulated ventures.

    Walks <project_root>/projects for venture-scope.json files. Any venture
    flagged `regulated: true` without an approved compliance-report.json
    gets a review task auto-queued to the legal-compliance-officer agent.

    Path: ensure_pending_reviews() expects the project root (parent of
    company_dir), since ventures live at <project_root>/projects/<id>/...
    not under company_dir itself. PR #967 originally passed company_dir
    directly, which silently looked for `.company/projects/` (which never
    exists) — masked because no real ventures were ever seeded for the
    daemon path. The CLI in venture_scope_monitor.py always did
    `get_company_dir().parent`; this aligns the daemon with that.

    Failure mode: any exception is logged and swallowed — the scan is
    non-critical background work and must not crash the cycle.
    """
    try:
        from . import venture_scope_monitor  # noqa: PLC0415
    except ImportError:
        try:
            import venture_scope_monitor  # type: ignore[no-redef]
        except ImportError as e:
            return {
                "ventures_scanned": 0,
                "pending_reviews": 0,
                "queued": [],
                "skipped_existing": [],
                "errors": [f"venture_scope_monitor import failed: {e}"],
                "dry_run": False,
            }

    try:
        company_root = company_dir.parent
        return venture_scope_monitor.ensure_pending_reviews(company_root)
    except Exception as e:
        logger.warning(f"Compliance scan failed (non-fatal): {e}")
        return {
            "ventures_scanned": 0,
            "pending_reviews": 0,
            "queued": [],
            "skipped_existing": [],
            "errors": [str(e)],
            "dry_run": False,
        }


def _run_customer_feedback_scan(company_dir: Path) -> dict[str, Any]:
    """Per-cycle customer-feedback scan.

    Walks <company_dir>/.company/customers/*/feedback.json. For each entry
    with status='unacknowledged', queues a customer-success-lead review
    task and atomically flips the entry's status to 'surfaced'.

    Idempotence comes from the on-disk status flip — re-runs see only
    new entries. PR #973 module; this is the daemon wiring.

    Failure mode: any exception is logged and swallowed — the scan is
    non-critical background work and must not crash the cycle.
    """
    try:
        from . import customer_feedback_monitor  # noqa: PLC0415
    except ImportError:
        try:
            import customer_feedback_monitor  # type: ignore[no-redef]
        except ImportError as e:
            return {
                "customers_scanned": 0,
                "surfaced_count": 0,
                "surfaced": [],
                "skipped_already_surfaced": 0,
                "errors": [f"customer_feedback_monitor import failed: {e}"],
                "dry_run": False,
            }

    # The monitor expects company_root (the project root, parent of .company),
    # not the company_dir itself. Compliance scan uses company_dir directly
    # because venture-scope.json lives under <company_dir>/projects/, but
    # feedback.json lives under <project_root>/.company/customers/, so the
    # right input is one level up.
    try:
        company_root = company_dir.parent
        return customer_feedback_monitor.surface_pending_feedback(company_root)
    except Exception as e:
        logger.warning(f"Customer-feedback scan failed (non-fatal): {e}")
        return {
            "customers_scanned": 0,
            "surfaced_count": 0,
            "surfaced": [],
            "skipped_already_surfaced": 0,
            "errors": [str(e)],
            "dry_run": False,
        }


# -----------------------------------------------------------------------------
# Employee Creative Ideation (P34)
# -----------------------------------------------------------------------------


def _should_run_queue_autofill(config: DaemonConfig, state: DaemonState) -> bool:
    """WS-120: Check if queue auto-fill should run.

    Triggers when the pending queue drops below config.queue_low_threshold
    and the minimum cooldown since the last autofill has elapsed.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if auto-fill should run now.
    """
    if not config.strategic_planning_enabled:
        return False

    # E7: Give ideation the first turn on startup — fill must not saturate workers
    # before the direction pipeline (ideation → approval → convert) has had a chance
    # to run. If ideation is enabled but has never run, defer fill until it does.
    if (
        config.fill_defer_to_ideation
        and config.employee_ideation_enabled
        and not state.last_employee_ideation
    ):
        return False

    pending_count = _get_pending_queue_size()
    if pending_count >= config.queue_low_threshold:
        return False

    if not state.last_queue_autofill:
        return True

    now = datetime.now(timezone.utc)
    try:
        last_run = datetime.fromisoformat(
            state.last_queue_autofill.replace("Z", "+00:00")
        )
        # Minimum 5-minute cooldown to prevent flooding
        if (now - last_run).total_seconds() < 300:
            return False
    except (ValueError, TypeError):
        pass

    return True


def _run_queue_autofill(
    config: DaemonConfig, state: DaemonState, company_dir: Path
) -> dict[str, Any]:
    """WS-120: Run queue auto-fill from active goals with lowest metric.

    Calls goal_autofill.autofill_queue_from_goals() and updates state
    with the results.

    Args:
        config: Daemon configuration.
        state: Current daemon state.
        company_dir: Path to the .company directory.

    Returns:
        Dict with tasks_created, pending_before, goals_targeted, skipped.
    """
    _ensure_imports()

    result: dict[str, Any] = {
        "tasks_created": 0,
        "pending_before": 0,
        "goals_targeted": [],
        "skipped": True,
    }
    try:
        # Only defer when ideation is actually enabled — if disabled it will never run
        # to set last_employee_ideation and the guard would fire permanently.
        defer_effective = (
            config.fill_defer_to_ideation and config.employee_ideation_enabled
        )
        result = goal_autofill.autofill_queue_from_goals(
            company_dir,
            last_ideation=state.last_employee_ideation,
            defer_to_ideation=defer_effective,
        )
    except Exception as exc:
        logger.warning(f"[WS-120] Queue auto-fill failed: {exc}")
        return result

    state.last_queue_autofill = datetime.now(timezone.utc).isoformat()
    state.queue_autofill_runs += 1
    created = result.get("tasks_created", 0)
    state.queue_autofill_tasks_generated += created

    return result


def _should_run_backlog_generation(config: DaemonConfig, state: DaemonState) -> bool:
    """Signal-driven backlog generation: the LAST-RESORT work source.

    Deliberately checked AFTER queue auto-fill in the same cycle, and it
    re-reads the pending count, so declared work always wins: if autofill or
    the roadmap scheduler just filled the queue this returns False and no
    derived work is minted. It only fires when everything else has had its
    turn and the queue is still short -- the state this repo sat in for a week
    (goals met or excluded, roadmap intake 17 of 17 handled, daemon idle).
    """
    if not config.backlog_generation_enabled:
        return False

    if _get_pending_queue_size() >= config.queue_low_threshold:
        return False

    if not state.last_backlog_generation:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_backlog_generation.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return True

    elapsed_minutes = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
    return elapsed_minutes >= config.backlog_cooldown_minutes


def _run_backlog_generation(config: DaemonConfig, state: DaemonState) -> dict[str, Any]:
    """Mint tasks from measured repo signals. Never raises."""
    _ensure_imports()

    result: dict[str, Any] = {"generated": 0, "minted": 0, "failed": 0}
    try:
        import backlog_generator

        signals = backlog_generator.generate(
            Path.cwd(), limit=config.backlog_max_tasks_per_scan
        )
        result = backlog_generator.mint(signals, dry_run=False)
    except Exception as exc:
        # A dry or broken signal source must never take the cycle down; it is
        # the last-resort lane, so failing quiet is correct.
        logger.debug(f"Backlog generation skipped: {exc}")

    state.last_backlog_generation = datetime.now(timezone.utc).isoformat()
    state.backlog_generation_runs += 1
    state.backlog_tasks_generated += result.get("minted", 0)

    return result


def _should_run_silence_sentinel(config: DaemonConfig, state: DaemonState) -> bool:
    """Silence sentinel (plan item 4): run on its configured interval."""
    if not config.silence_sentinel_enabled:
        return False
    if not state.last_silence_check:
        return True
    try:
        last_run = datetime.fromisoformat(
            state.last_silence_check.replace("Z", "+00:00")
        )
        interval = config.silence_sentinel_interval_minutes * 60
        return (datetime.now(timezone.utc) - last_run).total_seconds() >= interval
    except (ValueError, TypeError):
        return True


def _run_silence_sentinel(config: DaemonConfig, state: DaemonState) -> dict[str, Any]:
    """Run the silence sentinel cycle (scout / queue / restarts / brief skips).

    The sentinel raises escalation records + opt-in notifications itself;
    this wrapper only tracks daemon state and must never break the loop.
    """
    result: dict[str, Any] = {"findings": [], "alerts_raised": []}
    try:
        try:
            from . import silence_sentinel  # type: ignore[attr-defined]
        except ImportError:
            import silence_sentinel  # type: ignore[no-redef]

        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        sentinel_config = silence_sentinel.SentinelConfig(base_dir=base_dir)
        result = silence_sentinel.run_sentinel_cycle(sentinel_config)
    except Exception as exc:
        logger.warning(f"[Sentinel] Silence check failed: {exc}")
        return result

    state.last_silence_check = datetime.now(timezone.utc).isoformat()
    state.silence_alerts_raised += len(result.get("alerts_raised", []))
    return result


# -----------------------------------------------------------------------------
# LEDGER-5c: scheduled acceptance step (GAP-7)
#
# A clone of the silence-sentinel should-run/run pair with one difference the
# ADR-003 rule forces: the sub-steps (ledger run, divergence register, mutant
# canary, corpus confirm and — only when acceptance.oracle.enabled — the blind
# oracle, which reaches a model through agent_providers) run as SUBPROCESSES in
# ONE background thread. Each subprocess is in its own session and its whole
# process group is killed at its timeout. The loop only launches the thread and
# never waits on it, so a hanging sub-step can cost at most one thread, never a
# loop cycle.
# -----------------------------------------------------------------------------

_acceptance_thread: Any = None  # threading.Thread | None
_acceptance_current_proc: Any = None  # subprocess.Popen | None
_acceptance_last_result: dict[str, Any] = {}
_acceptance_atexit_registered: bool = False
# Seconds between SIGINT to a timed-out sub-step and SIGKILL to its group. The
# oracle (acceptance_probe.py) runs its model sessions in their OWN sessions,
# which a group kill does not reach; on SIGINT it raises KeyboardInterrupt and
# its LiveSessions.kill_all() ends every session first.
ACCEPTANCE_INTERRUPT_GRACE_SECONDS: float = 10.0

ACCEPTANCE_SCHEDULE_FILENAME = "scheduled_run.json"
ACCEPTANCE_LOCK_FILENAME = "scheduled_run.lock"


def _acceptance_project_dir(config: DaemonConfig) -> Path:
    if config.acceptance_project_dir is not None:
        return Path(config.acceptance_project_dir)
    return Path(__file__).resolve().parent.parent.parent.parent


def _acceptance_state_dir(config: DaemonConfig) -> Path:
    return _acceptance_project_dir(config) / ".company" / "state" / "acceptance"


def _read_persisted_acceptance_launch(config: DaemonConfig) -> str:
    """The last launch time written by ANY daemon process ("" if none).

    run_daemon_loop starts every process with a fresh DaemonState, so the
    in-memory last_acceptance_run is empty after each restart; the cadence has
    to survive restarts (1-5 starts/day plus restart storms in daemon.log).
    """
    try:
        data = json.loads(
            (_acceptance_state_dir(config) / ACCEPTANCE_SCHEDULE_FILENAME).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return ""
    value = data.get("launched_at") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _persist_acceptance_launch(config: DaemonConfig, launched_at: str) -> bool:
    """Atomically record a launch. Creates the state directory only under an
    existing project directory, never from a caller-supplied non-path."""
    project = _acceptance_project_dir(config)
    try:
        if not project.is_dir():
            return False
        state_dir = _acceptance_state_dir(config)
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / ACCEPTANCE_SCHEDULE_FILENAME
        tmp = state_dir / f".{ACCEPTANCE_SCHEDULE_FILENAME}.{os.getpid()}.tmp"
        tmp.write_text(
            json.dumps({"launched_at": launched_at, "daemon_pid": os.getpid()}),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return True
    except OSError as exc:
        logger.warning(f"[Acceptance] could not record the launch time: {exc}")
        return False


def _open_acceptance_lock(config: DaemonConfig, *, create: bool) -> int | None:
    path = _acceptance_state_dir(config) / ACCEPTANCE_LOCK_FILENAME
    if not create and not path.exists():
        return None
    try:
        if create and not path.parent.is_dir():
            if not _acceptance_project_dir(config).is_dir():
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None


def _acceptance_run_in_flight(config: DaemonConfig) -> bool:
    """True while some process holds the run lock.

    The running sub-step inherits the lock's file descriptor (pass_fds), so
    the lock stays held while it runs even after the daemon that launched it
    was SIGKILLed — a restarted daemon then skips instead of starting a second,
    concurrent run.
    """
    import fcntl

    fd = _open_acceptance_lock(config, create=False)
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _acceptance_ledger_budget(config: DaemonConfig, step_timeout: int) -> int:
    """LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES (#720): the ledger step's budget
    grows with the entries' learned timeouts, capped at 2x the configured step
    timeout so a hung product cannot hold the step for hours. A run that hits
    the budget still records the entries it finished (partial ledger)."""
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import acceptance_ledger

        budget = int(
            acceptance_ledger.scheduled_step_budget(
                _acceptance_project_dir(config), step_timeout
            )
        )
        return max(int(step_timeout), min(budget, 2 * int(step_timeout)))
    except Exception as e:
        logger.warning(f"[Acceptance] ledger budget fell back to {step_timeout}s: {e}")
        return int(step_timeout)


def _acceptance_step_plan(config: DaemonConfig) -> list[dict[str, Any]]:
    """The ordered sub-steps for one scheduled run.

    ``argv`` is None for a step that is skipped, with ``skipped`` naming why.
    The divergence register folds the ledger's last run, so it follows it.
    """
    hooks = Path(__file__).resolve().parent
    py = sys.executable or "python3"
    project = str(_acceptance_project_dir(config))
    step_timeout = config.acceptance_step_timeout_seconds
    ledger_budget = _acceptance_ledger_budget(config, step_timeout)

    plan: list[dict[str, Any]] = [
        {
            "name": "ledger",
            "argv": [
                py,
                str(hooks / "acceptance_ledger.py"),
                "run",
                "--step-timeout",
                str(ledger_budget),
            ],
            "timeout": ledger_budget,
        },
        {
            "name": "divergences",
            "argv": [py, str(hooks / "acceptance_divergences.py"), "divergences"],
            "timeout": step_timeout,
        },
    ]
    if config.acceptance_canary_artifact:
        plan.append(
            {
                "name": "canary",
                "argv": [
                    py,
                    str(hooks / "acceptance_canary.py"),
                    "canary",
                    "--artifact",
                    config.acceptance_canary_artifact,
                ],
                "timeout": step_timeout,
            }
        )
    else:
        plan.append(
            {
                "name": "canary",
                "argv": None,
                "skipped": "acceptance.canaryArtifact is not set",
            }
        )
    plan.append(
        {
            "name": "corpus_confirm",
            "argv": [py, str(hooks / "acceptance_corpus.py"), "confirm"],
            "timeout": step_timeout,
        }
    )
    if config.acceptance_oracle_enabled:
        plan.append(
            {
                "name": "oracle",
                "argv": [py, str(hooks / "acceptance_probe.py"), "oracle"],
                "timeout": config.acceptance_oracle_timeout_seconds,
            }
        )
    else:
        plan.append(
            {
                "name": "oracle",
                "argv": None,
                "skipped": "acceptance.oracle.enabled is not true",
            }
        )
    for step in plan:
        if step["argv"] is not None:
            step["argv"] = [*step["argv"], "--project-dir", project]
    return plan


def _kill_acceptance_step(grace: float | None = None) -> None:
    """End the running sub-step: SIGINT to its leader (the probe then kills
    its own model sessions), up to ``grace`` seconds, then SIGKILL to the whole
    process group. Used on timeout and at daemon exit."""
    proc = _acceptance_current_proc
    if proc is None or proc.poll() is not None:
        return
    wait_for = ACCEPTANCE_INTERRUPT_GRACE_SECONDS if grace is None else grace
    if wait_for > 0:
        try:
            os.kill(proc.pid, signal.SIGINT)
            proc.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            pass
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_acceptance_substep(
    name: str,
    argv: list[str],
    timeout: float,
    cwd: Path,
    pass_fds: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Run one sub-step; never raises. Returns name/status/returncode/tail."""
    global _acceptance_current_proc
    started = time.monotonic()
    outcome: dict[str, Any] = {"name": name, "returncode": None}
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except Exception as exc:  # missing interpreter, bad cwd, ...
        outcome.update(status="error", detail=f"could not start: {exc}")
        return outcome
    _acceptance_current_proc = proc
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
            outcome["returncode"] = proc.returncode
            outcome["status"] = "ok" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            _kill_acceptance_step()
            try:
                out, _ = proc.communicate(timeout=5)
            except Exception:
                out = b""
            outcome["returncode"] = proc.returncode
            outcome["status"] = "timeout"
        except Exception as exc:
            _kill_acceptance_step(grace=0)
            out = b""
            outcome["status"] = "error"
            outcome["detail"] = str(exc)
    finally:
        _acceptance_current_proc = None
    text = (out or b"").decode("utf-8", errors="replace").strip()
    outcome["tail"] = text[-500:]
    outcome["seconds"] = round(time.monotonic() - started, 1)
    return outcome


def _run_acceptance_steps(config: DaemonConfig) -> dict[str, Any]:
    """Run every planned sub-step in order, holding the run lock. Never raises.

    A failing, timed-out or unstartable sub-step is logged and the run moves
    on to the next one: each sub-step reads what earlier runs left on disk, and
    one broken producer must not silence the others.
    """
    import fcntl

    global _acceptance_last_result
    result: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }
    lock_fd = _open_acceptance_lock(config, create=True)
    try:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.info(
                    "[Acceptance] another scheduled run still holds the lock — skipped"
                )
                result["skipped"] = "in_flight"
                _acceptance_last_result = result
                return result
        pass_fds = (lock_fd,) if lock_fd is not None else ()
        project = _acceptance_project_dir(config)
        for step in _acceptance_step_plan(config):
            if step["argv"] is None:
                outcome = {
                    "name": step["name"],
                    "status": "skipped",
                    "detail": step["skipped"],
                }
            else:
                outcome = _run_acceptance_substep(
                    step["name"],
                    step["argv"],
                    step["timeout"],
                    project,
                    pass_fds=pass_fds,
                )
            result["steps"].append(outcome)
            status = outcome["status"]
            if status in ("ok", "skipped"):
                logger.info(
                    f"[Acceptance] {step['name']}: {status}"
                    + (f" ({outcome['detail']})" if status == "skipped" else "")
                )
            else:
                logger.warning(
                    f"[Acceptance] {step['name']}: {status} "
                    f"(rc={outcome.get('returncode')}) "
                    f"{outcome.get('detail') or outcome.get('tail', '')[-200:]}"
                )
    except Exception as exc:  # defensive: nothing here may escape the thread
        logger.warning(f"[Acceptance] scheduled run failed: {exc}")
        result["error"] = str(exc)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)  # releases the flock held by this process
            except OSError:
                pass
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    _acceptance_last_result = result
    return result


def _latest_iso(*values: str) -> datetime | None:
    latest = None
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _default_branch_moved(config: DaemonConfig) -> bool:
    """True when a merge to the default branch claims a head-triggered run.

    acceptance_ledger.claim_head_trigger decides: at most one claim per new
    default-branch sha, 10 min apart. Any failure reads as "not moved", so
    the interval cadence is the fallback.
    """
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import acceptance_ledger as ledger  # noqa: PLC0415

        return ledger.claim_head_trigger(
            _acceptance_project_dir(config), _acceptance_state_dir(config)
        )
    except Exception as exc:
        logger.warning(f"[Acceptance] could not check the default branch head: {exc}")
        return False


def _should_run_acceptance(config: DaemonConfig, state: DaemonState) -> bool:
    """LEDGER-5c: run the acceptance step on its cadence, only when enabled,
    never while a run (this daemon's or an earlier daemon's orphan) is in
    flight. The cadence reads the persisted launch time, so a daemon restart
    does not reset it. A default-branch merge since the last recorded run
    is due before the interval elapses — see ``_default_branch_moved``."""
    if not config.acceptance_enabled:
        return False
    if _acceptance_thread is not None and _acceptance_thread.is_alive():
        return False
    if _acceptance_run_in_flight(config):
        return False
    last_run = _latest_iso(
        state.last_acceptance_run, _read_persisted_acceptance_launch(config)
    )
    if last_run is None:
        return True
    now = datetime.now(timezone.utc)
    if last_run > now + timedelta(hours=1):
        # A launch time in the future (clock jump, hand-edited or corrupt file)
        # would otherwise suppress every run until that time, silently.
        logger.warning(
            f"[Acceptance] last launch time {last_run.isoformat()} is in the "
            "future; treating the scheduled run as due"
        )
        return True
    if _default_branch_moved(config):
        logger.info(
            "[Acceptance] default branch moved since the last recorded run; "
            "one head-triggered run is due"
        )
        return True
    interval = config.acceptance_interval_hours * 3600
    return (now - last_run).total_seconds() >= interval


def _run_acceptance(config: DaemonConfig, state: DaemonState) -> dict[str, Any]:
    """Launch the acceptance sub-steps in a background thread; never raises.

    The last-run timestamp is recorded at LAUNCH, in the daemon state and in
    .company/state/acceptance/scheduled_run.json, so the cadence is measured
    launch to launch, survives restarts, and a sub-step that hangs until its
    timeout cannot cause a re-launch storm.
    """
    global _acceptance_thread, _acceptance_atexit_registered
    try:
        import threading

        if not _acceptance_atexit_registered:
            atexit.register(_kill_acceptance_step, 2.0)
            _acceptance_atexit_registered = True
        thread = threading.Thread(
            target=_run_acceptance_steps,
            args=(config,),
            name="acceptance-step",
            daemon=True,
        )
        thread.start()
        _acceptance_thread = thread
    except Exception as exc:
        logger.warning(f"[Acceptance] could not launch the scheduled run: {exc}")
        return {"started": False, "error": str(exc)}
    launched_at = datetime.now(timezone.utc).isoformat()
    state.last_acceptance_run = launched_at
    state.acceptance_runs += 1
    _persist_acceptance_launch(config, launched_at)
    logger.info(
        "[Acceptance] scheduled run launched"
        + (" (oracle on)" if config.acceptance_oracle_enabled else "")
    )
    return {"started": True}


def _should_run_employee_ideation(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run employee creative ideation cycle.

    P34: Enables employees to generate creative ideas and suggestions
    based on their work experience, like in a real company.

    P36 Enhancement: Only run ideation if there have been recent task
    completions since the last ideation cycle. This prevents wasted cycles
    when employees have no new work to reflect on.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if ideation should run.
    """
    if not config.employee_ideation_enabled:
        return False

    if not state.last_employee_ideation:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_employee_ideation.replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        elapsed = (now - last_run).total_seconds()

        # Basic time check
        if elapsed < (config.employee_ideation_interval_minutes * 60):
            return False

        # P36: Check if there have been recent completions to reflect on
        # Skip ideation if no tasks completed since last cycle
        try:
            _ensure_imports()
            company_dir = company_resolver.get_company_dir()
            queue_path = company_dir / "state/work_queue.json"

            if queue_path.exists():
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)

                completed_tasks = queue.get("completed", [])
                recent_completions = 0

                for task in completed_tasks:
                    completed_at = task.get("completed_at", "")
                    if completed_at:
                        try:
                            task_time = datetime.fromisoformat(
                                completed_at.replace("Z", "+00:00")
                            )
                            if task_time > last_run:
                                recent_completions += 1
                        except (ValueError, TypeError):
                            pass

                # Only run ideation if at least 1 task completed since last cycle
                if recent_completions == 0:
                    logger.debug(
                        "P36: Skipping ideation - no completions since last cycle"
                    )
                    return False

                logger.debug(
                    f"P36: Running ideation - {recent_completions} completions since last cycle"
                )
        except Exception as e:
            logger.debug(f"P36: Could not check completions, proceeding anyway: {e}")

        return True

    except (ValueError, TypeError):
        return True


def _run_employee_ideation(
    config: DaemonConfig,
    state: DaemonState,
) -> dict:
    """Run employee creative ideation cycle.

    P34: Employees analyze their work patterns and generate creative ideas:
    - Process improvements from repeated tasks
    - Bottleneck observations from failures/retries
    - Capability expansion suggestions
    - Collaboration proposals
    - Strategic observations

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        Dict with ideation results.
    """
    _ensure_imports()

    result = {
        "employees_processed": 0,
        "ideas_generated": 0,
        "idea_types": {},
    }
    if employee_ideation is None:  # not in the public export
        return result

    try:
        # WS-058: Alternate between normal and bootstrap ideation.
        # Every 4th cycle, target under-represented employees (tasks_completed < 3)
        # so business-role employees (marketing, UX, sales) get a chance to ideate.
        use_bootstrap = state.employee_ideation_cycles % 4 == 3
        if use_bootstrap:
            logger.info(
                "P34: Bootstrap ideation cycle — targeting under-represented employees"
            )

        cycle_result = employee_ideation.run_ideation_cycle(
            max_employees=config.employee_ideation_max_employees,
            max_ideas_per_employee=config.employee_ideation_max_ideas_per_employee,
            dry_run=False,
            target_zero_activity=use_bootstrap,
        )

        result["employees_processed"] = len(cycle_result.get("employees_processed", []))
        result["ideas_generated"] = cycle_result.get("total_ideas", 0)

        # Count by type
        for idea in cycle_result.get("ideas_generated", []):
            idea_type = idea.get("type", "unknown")
            result["idea_types"][idea_type] = result["idea_types"].get(idea_type, 0) + 1

        # Update state
        state.last_employee_ideation = datetime.now(timezone.utc).isoformat()
        state.employee_ideation_cycles += 1
        state.ideas_generated += result["ideas_generated"]

        if result["ideas_generated"] > 0:
            logger.info(
                f"Employee ideation: {result['ideas_generated']} ideas from "
                f"{result['employees_processed']} employees"
            )
        else:
            logger.debug(
                f"Employee ideation: no new ideas from "
                f"{result['employees_processed']} employees"
            )

        return result

    except Exception as e:
        logger.error(f"Employee ideation cycle failed: {e}")
        state.last_employee_ideation = datetime.now(timezone.utc).isoformat()
        return result


# -----------------------------------------------------------------------------
# Auto-Merge Loop (P35)
# -----------------------------------------------------------------------------


def _should_run_auto_merge(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run auto-merge check.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if auto-merge check should run.
    """
    if not config.auto_merge_enabled:
        return False

    if not state.last_auto_merge_check:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_auto_merge_check.replace("Z", "+00:00")
        )
        elapsed_minutes = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
        return elapsed_minutes >= config.auto_merge_interval_minutes
    except (ValueError, TypeError):
        return True


def _get_secrets_patterns() -> list[tuple[str, str]]:
    """Return list of (name, regex_pattern) tuples for secrets scanning."""
    return [
        ("AWS Access Key", r"AKIA[0-9A-Z]{16}"),
        ("GitHub Token", r"gh[ps]_[A-Za-z0-9]{36,}"),
        ("Private Key", r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"),
        (
            "Hardcoded Password",
            r"""(?:password|passwd|pwd)\s*[:=]\s*['"][^'"]{8,}['"]""",
        ),
        (
            "Generic Secret",
            r"""(?:secret|token|api_key|apikey)\s*[:=]\s*['"][^'"]{8,}['"]""",
        ),
        ("Slack Token", r"xox[bpors]-[0-9a-zA-Z]{10,}"),
        ("Stripe Key", r"(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}"),
    ]


def _detect_preexisting_ci_failures(
    pr_number: int,
    failing_checks: list[str],
    excluded_checks: tuple[str, ...] = DEFAULT_BYPASS_EXCLUDED_CHECKS,
) -> tuple[bool, list[str]]:
    """
    WS-066-003: Detect if CI failures existed before this PR.

    Checks if the failing CI checks were also failing on main before the PR.

    Args:
        pr_number: PR number to check
        failing_checks: List of check names that are currently failing
        excluded_checks: Check names that must never be treated as
            pre-existing (increment 2a) — e.g. ``acceptance``, the ledger
            that judges the PR itself. If any failing check is in this set,
            the whole result is treated as not-pre-existing so admin merge
            can never bypass it.

    Returns:
        (all_preexisting, preexisting_checks) tuple where:
        - all_preexisting: True if ALL failures existed before this PR
        - preexisting_checks: List of check names that were pre-existing
    """
    import subprocess

    if not failing_checks:
        return True, []

    if any(check in excluded_checks for check in failing_checks):
        return False, []

    preexisting = []

    try:
        # Get the base commit (main before this PR's changes)
        base_result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "baseRefOid",
                "-q",
                ".baseRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if base_result.returncode != 0:
            return False, []

        base_sha = base_result.stdout.strip()
        if not base_sha:
            return False, []

        # Check what CI checks were failing on main at base commit
        checks_result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{base_sha}/check-runs",
                "--jq",
                '.check_runs[] | select(.conclusion == "failure") | .name',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if checks_result.returncode != 0:
            # Can't determine — assume not pre-existing
            return False, []

        main_failing = set(checks_result.stdout.strip().split("\n"))
        main_failing.discard("")  # Remove empty strings

        for check in failing_checks:
            if check in main_failing:
                preexisting.append(check)

        all_preexisting = len(preexisting) == len(failing_checks)
        return all_preexisting, preexisting

    except Exception as e:
        logger.debug(f"Pre-existing failure check failed: {e}")
        return False, []


def _check_admin_merge_rate_limit(
    state: DaemonState,
    max_per_day: int,
) -> bool:
    """
    WS-066-003: Check if we can perform another admin merge today.

    Args:
        state: Current daemon state
        max_per_day: Maximum admin merges allowed per day

    Returns:
        True if we can perform an admin merge, False if rate limited
    """
    today = datetime.now(timezone.utc).date().isoformat()

    # Reset counter if new day
    if state.admin_merges_last_date != today:
        state.admin_merges_today = 0
        state.admin_merges_last_date = today

    return state.admin_merges_today < max_per_day


def _check_rollback_health(company_dir: Path) -> bool:
    """
    WS-066-003: Check if the rollback executor is healthy.

    Requirements for admin merge:
    - Rollback system is enabled in config
    - Not rate-limited (haven't hit max rollbacks today)

    Args:
        company_dir: Path to .company directory

    Returns:
        True if rollback system is healthy and can handle failures
    """
    import json as json_mod

    # Check rollback state
    state_file = company_dir / "state" / "rollback_state.json"
    if not state_file.exists():
        # No rollbacks yet — healthy
        return True

    try:
        state = json_mod.loads(state_file.read_text())
        today = datetime.now(timezone.utc).date().isoformat()
        today_count = sum(
            1
            for r in state.get("rollbacks", [])
            if r.get("timestamp", "").startswith(today)
        )
        # Healthy if we haven't hit max rollbacks today (default 5)
        return today_count < 5
    except Exception:
        # Can't read state — assume unhealthy
        return False


def _blocked_or_review_path_match(pr_number: int, config: DaemonConfig) -> bool | None:
    """Check a PR's changed files against blockedPaths + reviewPaths only.

    Lighter-weight sibling of ``_check_security_gates`` for the legacy
    auto-merge branch (``auto_merge_full_trust=False``), which historically
    armed ``gh pr merge --auto`` with no path checks at all — a bypass of the
    full-trust path's blocked_paths gate. Running the full gate suite (CI
    checks, secrets scan, diff size) there would be a much larger behavior
    change for that operator override than this task calls for; this checks
    only the path guard (PR 257, 2026-07-17 — a gate/audit/steering file not
    on the exact blockedPaths list armed and merged before the operator's
    review hold could run).

    Returns True if any changed file matches, False if none do, and None on
    a ``gh`` fetch failure (fail closed — caller must treat None as a hit).
    """
    import fnmatch

    try:
        files_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--name-only"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if files_result.returncode != 0:
            return None
        files_list = [f for f in files_result.stdout.strip().split("\n") if f]
    except Exception:
        return None

    all_patterns = config.auto_merge_blocked_paths + config.auto_merge_review_paths
    for file_path in files_list:
        if any(fnmatch.fnmatch(file_path, pattern) for pattern in all_patterns):
            return True
    return False


def _hold_on_verdict_refusal(pr_number: int, verdict_gate: dict | None) -> None:
    """The merge gate refused on the verdict: stop every other lever too.

    ``gh pr merge --disable-auto`` first — a native auto-merge armed on an
    earlier (judged) head ignores labels and would merge this one. Then the
    label ci.yml reads: verdict-pending for a missing verdict (the loop will
    judge it), needs-manual-review for a negative one. Best-effort.
    """
    if not verdict_gate or verdict_gate.get("passed", True):
        return
    import subprocess as _sp

    try:
        if verdict_gate.get("status") == "missing":
            _verdict_hold().hold_for_new_head(pr_number)
            return
        _sp.run(
            ["gh", "pr", "merge", str(pr_number), "--disable-auto"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _sp.run(
            ["gh", "pr", "edit", str(pr_number), "--add-label", "needs-manual-review"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logger.warning(f"PR #{pr_number}: hold after verdict refusal failed: {exc}")


def _find_task_for_pr(pr_title: str) -> dict | None:
    """The queue task a daemon PR title names (``[task-…]``), in any lane."""
    task_id = _extract_task_id_from_pr_title(pr_title)
    if not task_id:
        return None
    if globals().get("work_allocator") is None:
        try:
            _ensure_imports()
        except Exception:
            return None
    wa = globals().get("work_allocator")
    if wa is None:
        return None
    queue = wa.load_queue()
    for lane in ("pr_open", "in_progress", "blocked", "failed", "pending", "completed"):
        for task in queue.get(lane, []) or []:
            if task.get("task_id") == task_id:
                return task
    return None


def _resolve_pending_verdict(
    pr_number: int,
    pr_title: str,
    branch: str,
    config: DaemonConfig,
    company_dir: Path,
    judge_budget: dict,
) -> str:
    """Settle a PR held only by verdict-pending. Returns released/held/rejected.

    * deliverable gate disabled: nothing ever judges — release.
    * a verdict is recorded for the CURRENT head: positive releases the label;
      negative adds needs-manual-review (the pending label stays).
    * no verdict for the head (a harvested PR, or a push moved the head): judge
      it now through the pre-merge gate if the task is in the queue and this
      cycle's judge budget allows — the gate releases the label on a pass for
      that head. No task: the PR stays held for the operator.
    """
    vh = _verdict_hold()
    if not config.auto_merge_require_deliverable_verdict:
        import subprocess as _sp

        _sp.run(
            ["gh", "pr", "edit", str(pr_number), "--remove-label", vh.PENDING_LABEL],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return "released"

    head = vh.current_head(pr_number)
    if not head:
        return "held"
    try:
        from merge_audit import lookup_verdict_record_for_sha
    except ImportError:  # pragma: no cover - import shim
        from .merge_audit import lookup_verdict_record_for_sha  # type: ignore

    rec = lookup_verdict_record_for_sha(company_dir, head)
    if rec is not None:
        if vh.is_positive(rec):
            released = vh.release_if_positive_for_head(pr_number, head, True)
            return "released" if released else "held"
        _hold_on_verdict_refusal(pr_number, {"passed": False, "status": "negative"})
        return "rejected"

    if judge_budget.get("left", 0) <= 0:
        return "held"
    task = _find_task_for_pr(pr_title)
    if task is None or pr_output_manager is None:
        logger.info(
            f"PR #{pr_number}: head {head[:12]} unjudged and no queue task to "
            f"judge it against — held (verdict-pending) for the operator"
        )
        return "held"
    judge_budget["left"] = judge_budget.get("left", 0) - 1
    gate = pr_output_manager.run_deliverable_gate(
        task.get("task_id", ""),
        task.get("title", ""),
        task.get("description", ""),
        str(pr_number),
        branch,
        daemon_executed=True,
    )
    return "released" if gate.get("pending_released") else "held"


def _deliverable_verdict_gate(
    head_sha: str, config: DaemonConfig, company_dir: Path
) -> dict:
    """Gate: the PR's head commit carries a positive deliverable verdict.

    Every PR this gate sees is daemon-authored (``_run_auto_merge`` handles
    only ``daemon/*`` branches from ``--author @me``), and every daemon PR is
    judged when it is opened — by the pre-merge gate on a successful task, or
    by the workflow executor, which then fails the task on a negative verdict.
    Until 2026-09-26 no merge lever read that verdict: a workflow-rejected PR
    carried no hold label and merged here on green CI.

    - verdict negative, blocked, or errored for ``head_sha`` -> refuse.
    - no verdict for ``head_sha`` -> refuse while the deliverable gate is
      enabled. The verdict is keyed by commit, so a push after judging (a
      retry, a rebase, a CI-heal commit) leaves the new head unjudged, and an
      earlier head's verdict never answers for it.
    - gate disabled (``autonomy.deliverableGate.enabled: false``): nothing
      writes verdicts, so a missing one passes; a recorded negative still
      refuses.
    """
    try:
        from merge_audit import lookup_verdict_record_for_sha
    except ImportError:  # pragma: no cover - import shim
        from .merge_audit import lookup_verdict_record_for_sha  # type: ignore

    rec = lookup_verdict_record_for_sha(company_dir, head_sha) if head_sha else None
    if rec is None:
        passed = not config.auto_merge_require_deliverable_verdict
        return {"passed": passed, "status": "missing", "sha": head_sha or None}
    positive = rec.get("addresses_task") is True and not rec.get("blocked")
    return {
        "passed": positive,
        "status": "positive" if positive else "negative",
        "sha": head_sha,
        "addresses_task": rec.get("addresses_task"),
        "confidence": rec.get("confidence"),
    }


def _check_security_gates(
    pr_number: int,
    config: DaemonConfig,
    company_dir: Path,
) -> tuple[bool, dict]:
    """Run security gate checks on a PR before auto-merge.

    Gates:
        1. CI checks — all must pass (PENDING → PENDING verdict)
        2. Secrets scan — diff must not contain secret patterns
        3. Blocked paths — diff must not touch protected files
        4. Review paths — diff must not touch gate/audit/steering subsystems
           (PR 257, 2026-07-17: broader glob hold than blocked_paths' exact list)
        5. Diff size — must be within configured thresholds
        6. Deliverable verdict — a positive judge verdict for the HEAD commit
           (see ``_deliverable_verdict_gate``)

    Returns:
        (passed, results) where results contains verdict,
        head_sha, and per-gate details.
    """
    import fnmatch
    import re
    import subprocess

    gates: dict = {}
    head_sha = ""
    verdict = "MERGE"

    # Step 1: Get HEAD SHA for TOCTOU prevention
    try:
        sha_result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefOid",
                "-q",
                ".headRefOid",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        head_sha = sha_result.stdout.strip()
    except Exception:
        head_sha = ""

    # Step 2: CI checks
    try:
        checks_result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                str(pr_number),
                "--json",
                "name,state,bucket",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checks_result.returncode == 0:
            import json as json_mod

            checks = json_mod.loads(checks_result.stdout or "[]")
            # gh >= 2.66 dropped the `conclusion` field from `pr checks`;
            # `bucket` is gh's own pass/fail normalization. Keep `state` as
            # a fallback for older payload shapes.
            passing_states = {"SUCCESS", "NEUTRAL", "SKIPPED"}
            passing_buckets = {"pass", "skipping"}
            pending_states = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED"}
            ci_passed = True
            has_pending = False
            failing_checks: list[str] = []  # WS-066-003: Track failing check names
            for check in checks:
                st = check.get("state", "")
                bucket = str(check.get("bucket", "") or "").lower()
                check_name = check.get("name", "unknown")
                if bucket == "pending" or st in pending_states:
                    has_pending = True
                    ci_passed = False
                elif bucket in passing_buckets or st in passing_states:
                    continue
                else:
                    ci_passed = False
                    failing_checks.append(check_name)  # WS-066-003
            # An empty check list means the repo has no CI gate at all —
            # refuse rather than treat silence as success.
            if not checks and not config.auto_merge_allow_gateless_repos:
                ci_passed = False
            if has_pending and not failing_checks:
                verdict = "PENDING"
            gates["ci_checks"] = {
                "passed": ci_passed,
                "failing_checks": failing_checks,  # WS-066-003
                "gateless": not checks,
            }
        else:
            gates["ci_checks"] = {"passed": False, "failing_checks": []}
    except Exception:
        gates["ci_checks"] = {"passed": False}

    # Step 3-4: Fetch diff and file list
    diff_text = ""
    files_list: list[str] = []
    diff_ok = True

    try:
        diff_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if diff_result.returncode == 0:
            diff_text = diff_result.stdout
        else:
            diff_ok = False
    except Exception:
        diff_ok = False

    try:
        files_result = subprocess.run(
            [
                "gh",
                "pr",
                "diff",
                str(pr_number),
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if files_result.returncode == 0:
            files_list = [f for f in files_result.stdout.strip().split("\n") if f]
        else:
            diff_ok = False
    except Exception:
        diff_ok = False

    if not diff_ok:
        gates["secrets_scan"] = {
            "passed": False,
            "patterns_matched": [],
        }
        gates["blocked_paths"] = {
            "passed": False,
            "matched": [],
        }
        gates["review_paths"] = {
            "passed": False,
            "matched": [],
        }
        gates["diff_size"] = {
            "passed": False,
            "lines": 0,
            "files_changed": 0,
        }
        gates["deliverable_verdict"] = _deliverable_verdict_gate(
            head_sha, config, company_dir
        )
        return False, {
            "verdict": "BLOCK",
            "head_sha": head_sha,
            "gates": gates,
            "block_reason": "diff_fetch_failed",
        }

    # Gate: Secrets scan
    if config.auto_merge_secrets_scan:
        patterns_matched = []
        for name, pattern in _get_secrets_patterns():
            if re.search(pattern, diff_text):
                patterns_matched.append(name)
        gates["secrets_scan"] = {
            "passed": len(patterns_matched) == 0,
            "patterns_matched": patterns_matched,
        }
    else:
        gates["secrets_scan"] = {
            "passed": True,
            "patterns_matched": [],
        }

    # Gate: Blocked paths
    matched_paths = []
    for file_path in files_list:
        for blocked in config.auto_merge_blocked_paths:
            if fnmatch.fnmatch(file_path, blocked):
                matched_paths.append(file_path)
                break
    gates["blocked_paths"] = {
        "passed": len(matched_paths) == 0,
        "matched": matched_paths,
    }

    # Gate: Review paths (broader glob hold — subsystem-level, not just exact
    # files; PR 257, 2026-07-17: a gate/audit/steering file not on the short
    # blocked_paths list armed and merged before the operator's review hold ran)
    review_matched = []
    for file_path in files_list:
        for pattern in config.auto_merge_review_paths:
            if fnmatch.fnmatch(file_path, pattern):
                review_matched.append(file_path)
                break
    gates["review_paths"] = {
        "passed": len(review_matched) == 0,
        "matched": review_matched,
    }

    # Gate: Diff size
    diff_lines = len(
        [ln for ln in diff_text.split("\n") if ln.startswith("+") or ln.startswith("-")]
    )
    files_changed = len(files_list)
    size_passed = (
        diff_lines <= config.auto_merge_max_diff_lines
        and files_changed <= config.auto_merge_max_diff_files
    )
    gates["diff_size"] = {
        "passed": size_passed,
        "lines": diff_lines,
        "files_changed": files_changed,
    }

    # Gate: Deliverable verdict for this exact head commit
    gates["deliverable_verdict"] = _deliverable_verdict_gate(
        head_sha, config, company_dir
    )

    # Determine overall result
    all_passed = all(g["passed"] for g in gates.values())
    if not all_passed and verdict != "PENDING":
        verdict = "BLOCK"
    if not all_passed:
        all_passed = False

    return all_passed, {
        "verdict": verdict,
        "head_sha": head_sha,
        "gates": gates,
    }


def _record_auto_merge_audit(
    gate_results: dict,
    pr_title: str,
    branch: str,
    merge_result: str,
    company_dir: Path,
    max_entries: int = 200,
) -> None:
    """Record an auto-merge decision in the audit log."""
    import json as json_mod
    import tempfile

    audit_path = company_dir / "state" / "auto_merge_audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {"version": 1, "entries": []}
    if audit_path.exists():
        try:
            data = json_mod.loads(audit_path.read_text())
            if not isinstance(data.get("entries"), list):
                data = {"version": 1, "entries": []}
        except (json_mod.JSONDecodeError, ValueError):
            data = {"version": 1, "entries": []}

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pr_number": gate_results.get("pr_number"),
        "pr_title": pr_title,
        "branch": branch,
        "verdict": gate_results.get("verdict"),
        "merge_result": merge_result,
        "gates": gate_results.get("gates", {}),
        "head_sha": gate_results.get("head_sha", ""),
    }
    data["entries"].append(entry)

    if len(data["entries"]) > max_entries:
        data["entries"] = data["entries"][-max_entries:]

    fd, tmp_path = tempfile.mkstemp(dir=str(audit_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json_mod.dump(data, f, indent=2)
        os.replace(tmp_path, str(audit_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _extract_task_id_from_pr_title(pr_title: str) -> str | None:
    """Extract task ID from a daemon PR title ending in '[task-XXXX]'."""
    import re as _re

    m = _re.search(r"\[(task-[^\]]+)\]", pr_title)
    return m.group(1) if m else None


def _normalize_title(title: str) -> str:
    """Strip task-ID suffix, lowercase, collapse non-alphanumeric for cross-regen title dedup.

    Returns empty string when the result is too short (< 15 chars) to avoid false collisions.
    """
    import re as _re

    t = _re.sub(r"\s*\[task-[^\]]+\]\s*$", "", title).strip()
    normalized = _re.sub(r"\W+", " ", t.lower()).strip()
    return normalized if len(normalized) >= 15 else ""


def _close_and_requeue_ci_failed_pr(
    pr_number: int,
    pr_title: str,
    task_id: str,
    failing_checks: list,
    company_dir: Path,
) -> bool:
    """Close a CI-failed PR and requeue its task with CI failure evidence (path-D1).

    Returns True on success, False if the PR close or queue update failed.
    """
    ci_evidence = "; ".join(str(c) for c in failing_checks[:5])
    close_comment = (
        f"Auto-closed (path-D): CI checks failing for {ci_evidence}. "
        "Task requeued with evidence for next attempt."
    )
    close_result = subprocess.run(
        [
            "gh",
            "pr",
            "close",
            str(pr_number),
            "--comment",
            close_comment,
            "--delete-branch",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if close_result.returncode != 0:
        logger.debug(
            f"[path-D1] PR #{pr_number} close failed: {close_result.stderr[:200]}"
        )
        return False

    # Requeue the task with CI evidence prepended to description
    _ensure_imports()
    queue_path = company_dir / "state/work_queue.json"
    lock_path = company_dir / "runtime/queue.lock"
    try:
        with work_allocator.QueueLock(lock_path):
            try:
                queue = json.loads(queue_path.read_text())
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                logger.warning(
                    f"[path-D1] Could not read queue after closing PR #{pr_number}"
                )
                return True  # PR closed OK; reconcile will return task to pending

            # Find task in pr_open lane
            task_obj = None
            pr_open = queue.get("pr_open", [])
            for t in pr_open:
                if t.get("task_id") == task_id:
                    task_obj = t
                    break

            if task_obj is None:
                logger.warning(
                    f"[path-D1] Task {task_id} not found in pr_open after closing "
                    f"PR #{pr_number}; reconcile will return it to pending without evidence"
                )
                return True

            # Prepend CI failure evidence to description
            evidence_prefix = (
                f"[CI-FAIL #{pr_number}]: Closing PR #{pr_number} after repeated CI failures. "
                f"Failing checks: {ci_evidence}. "
                "Fix these CI failures before the next attempt.\n\n"
            )
            existing_desc = task_obj.get("description", "")
            new_desc = (evidence_prefix + existing_desc)[:4000]

            task_obj["description"] = new_desc
            task_obj["status"] = "pending"
            task_obj.pop("pr_open_at", None)
            task_obj.pop("pr_url", None)

            pr_open.remove(task_obj)
            queue["pr_open"] = pr_open
            queue.setdefault("pending", []).insert(0, task_obj)

            fd, tmp = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    except Exception as e:
        logger.error(f"[path-D1] Queue update failed for task {task_id}: {e}")
        return False

    logger.warning(
        f"[path-D1] PR #{pr_number} closed; task {task_id} requeued with CI evidence"
    )
    return True


def _escalate_ci_failed_task(
    pr_number: int,
    pr_title: str,
    task_id: str,
    failing_checks: list,
    company_dir: Path,
) -> None:
    """Close a repeatedly CI-failed PR and move its task to blocked (path-D2)."""
    ci_evidence = "; ".join(str(c) for c in failing_checks[:5])
    close_comment = (
        "Auto-closed (path-D2): CI has been failing repeatedly across multiple builds. "
        "Task blocked pending human review."
    )
    try:
        subprocess.run(
            [
                "gh",
                "pr",
                "close",
                str(pr_number),
                "--comment",
                close_comment,
                "--delete-branch",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        logger.debug(f"[path-D2] PR #{pr_number} close failed: {e}")

    _ensure_imports()
    queue_path = company_dir / "state/work_queue.json"
    lock_path = company_dir / "runtime/queue.lock"
    try:
        with work_allocator.QueueLock(lock_path):
            try:
                queue = json.loads(queue_path.read_text())
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                queue = {"pending": [], "in_progress": [], "blocked": [], "pr_open": []}

            task_obj = None
            for t in queue.get("pr_open", []):
                if t.get("task_id") == task_id:
                    task_obj = t
                    break

            if task_obj is not None:
                queue["pr_open"].remove(task_obj)
                task_obj["status"] = "blocked"
                task_obj["blocked_reason"] = (
                    f"CI-fail ceiling: repeated CI failures across multiple builds. "
                    f"Failing: {ci_evidence}. Human review required."
                )
                task_obj.pop("pr_open_at", None)
                task_obj.pop("pr_url", None)
                queue.setdefault("blocked", []).append(task_obj)

                fd, tmp = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(queue, f, indent=2)
                    os.replace(tmp, str(queue_path))
                except Exception:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                    raise
    except Exception as e:
        logger.error(f"[path-D2] Queue update failed for task {task_id}: {e}")
        return

    logger.warning(
        f"[path-D2] PR #{pr_number} closed; task {task_id} blocked (CI-fail ceiling exhausted)"
    )


def _block_task_ceiling(
    task_id: str,
    build_count: int,
    max_builds: int,
    company_dir: Path,
) -> None:
    """Move a task to blocked because it has hit the cross-regeneration build ceiling."""
    _ensure_imports()
    queue_path = company_dir / "state/work_queue.json"
    lock_path = company_dir / "runtime/queue.lock"
    try:
        with work_allocator.QueueLock(lock_path):
            try:
                queue = json.loads(queue_path.read_text())
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                queue = {"pending": [], "in_progress": [], "blocked": []}

            task_obj = None
            for t in queue.get("pending", []):
                if t.get("task_id") == task_id:
                    task_obj = t
                    break

            if task_obj is None:
                logger.debug(
                    f"[ceiling] Task {task_id} not found in pending; "
                    "may have been claimed already"
                )
                return

            queue["pending"].remove(task_obj)
            task_obj["status"] = "blocked"
            task_obj["blocked_reason"] = (
                f"Build ceiling: {build_count} builds >= {max_builds}. "
                "Human review required before another attempt."
            )
            task_obj["ceiling_blocked_at"] = datetime.now(timezone.utc).isoformat()
            queue.setdefault("blocked", []).append(task_obj)

            fd, tmp = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    except Exception as e:
        logger.error(f"[ceiling] Queue update failed for task {task_id}: {e}")
        return

    logger.warning(
        f"[ceiling] Task {task_id} blocked after {build_count} builds "
        f"(ceiling={max_builds})"
    )


def _check_cross_regen_ceiling(
    config: "DaemonConfig",
    state: "DaemonState",
    task_id: str,
    task_title: str,
    company_dir: "Path",
) -> bool:
    """Check whether a task has hit the cross-regeneration build ceiling.

    Returns True when the ceiling is hit and the task has been moved to blocked.
    The caller is responsible for incrementing state.cross_regen_ceiling_blocks
    and skipping the spawn cycle.  Returns False when the spawn may proceed.
    """
    if not config.cross_regen_ceiling_enabled or not task_id:
        return False

    task_builds = state.task_total_builds.get(task_id, 0)
    title_key = _normalize_title(task_title) if task_title else ""
    title_builds = state.title_total_builds.get(title_key, 0) if title_key else 0

    if (
        task_builds < config.cross_regen_max_builds
        and title_builds < config.cross_regen_max_builds
    ):
        return False

    logger.warning(
        f"[Execute] Task {task_id} ceiling hit: "
        f"{task_builds} task-id builds, {title_builds} title builds "
        f"(ceiling={config.cross_regen_max_builds}) — blocking"
    )
    try:
        _block_task_ceiling(
            task_id,
            max(task_builds, title_builds),
            config.cross_regen_max_builds,
            company_dir,
        )
    except Exception as ceil_err:
        logger.debug(f"[Execute] Ceiling block failed: {ceil_err}")
    return True


# E8: worker results that mean no build actually happened. A worker can be
# spawned and exit inside the same cycle without ever running the task:
# ``idle`` (poll_and_execute_once found nothing to execute) and
# ``claim_failed`` (the queue entry was already owned by another writer).
_NO_BUILD_ACTIONS = frozenset({"idle", "claim_failed"})


def _refund_cross_regen_build(
    state: "DaemonState", task_id: str, title_key: str
) -> None:
    """Hand back the ceiling build a spawn took when the worker did no work.

    2026-09-04 incident (task-...-9fc25d): the ceiling fired at "5 task-id
    builds, 5 title builds" after only THREE workers had actually run. The
    other two spawns each returned ``idle`` within five seconds — the worker
    could not claim a queue entry the WS-105 recovery re-queue had just
    rewritten — but the counter is incremented at spawn time, so both no-ops
    were billed as builds and the task was blocked two real builds early.
    The count has to be taken at spawn (that is the only place holding the
    title), so it is refunded here once the worker's result shows that
    nothing was built. Counts never go below zero.
    """
    if not task_id:
        return
    remaining = state.task_total_builds.get(task_id, 0) - 1
    if remaining > 0:
        state.task_total_builds[task_id] = remaining
    else:
        state.task_total_builds.pop(task_id, None)
    if title_key:
        title_remaining = state.title_total_builds.get(title_key, 0) - 1
        if title_remaining > 0:
            state.title_total_builds[title_key] = title_remaining
        else:
            state.title_total_builds.pop(title_key, None)


def _append_merge_audit_chain(
    company_dir: Path,
    pr_number: int,
    pr_title: str,
    gate_results: dict | None,
    merge_lever: str,
) -> None:
    """Append one line to merge_audit.jsonl. Best-effort, never raises."""
    try:
        import sys as _sys

        hooks_dir = Path(__file__).resolve().parent
        if str(hooks_dir) not in _sys.path:
            _sys.path.insert(0, str(hooks_dir))
        from merge_audit import (
            append_merge_chain,
            ci_state_from_gates,
            lookup_deliverable_verdict_for_merge,
        )

        task_id = _extract_task_id_from_pr_title(pr_title)
        deliverable_verdict = lookup_deliverable_verdict_for_merge(
            company_dir, task_id, (gate_results or {}).get("head_sha") or None
        )
        ci_state = ci_state_from_gates(gate_results)
        append_merge_chain(
            company_dir,
            task_id=task_id,
            pr=pr_number,
            deliverable_verdict=deliverable_verdict,
            ci_state=ci_state,
            merge_lever=merge_lever,
        )
    except Exception:
        pass


# merge_gate refusals that need a human (a lasting reason); every other refusal
# is transient (checks, merge state, gh errors, a moved head) or is already a
# hold another mechanism owns (hold labels, challenger, verdict).
_GATE_HOLD_REASONS = (
    "touches blockedPaths",
    "touches reviewPaths",
    "secrets scan",
    "diff too large",
    "author gate",
    "fork:",
)


def _merge_gate_refusal(
    pr_number: int | str, head_sha: str, arming: bool = False
) -> str | None:
    """Why ``merge_gate.may_merge`` refuses ``pr_number`` at ``head_sha``
    (None = it may merge; ``arming``: may stay armed for GitHub's native
    auto-merge). Fails closed: an import error, an exception, or a gate that
    judged a different head is a refusal."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    try:
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import merge_gate

        refusal, judged = merge_gate.may_merge(
            pr_number,
            company_dir=project_root / ".company",
            project_root=project_root,
            expected_head=head_sha,
            arming=arming,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return f"merge gate unavailable: {type(exc).__name__}: {exc}"
    if refusal:
        return refusal
    if judged != head_sha:
        return "merge gate judged a different head"
    return None


def _run_auto_merge(config: DaemonConfig, state: DaemonState) -> dict:
    """Run auto-merge check for daemon PRs.

    Checks for PRs created by the daemon that pass all checks and merges them.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        dict with merged count and details.
    """
    result = {
        "prs_checked": 0,
        "prs_merged": 0,
        "prs_skipped": 0,
        "errors": [],
    }

    # Validate merge method to prevent command injection
    valid_methods = {"squash", "merge", "rebase"}
    if config.auto_merge_method not in valid_methods:
        logger.error(f"Invalid merge method: {config.auto_merge_method!r}")
        return result

    try:
        import subprocess

        # Get list of open PRs by daemon (include isDraft for draft handling)
        pr_list_result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--limit",
                str(_PR_LIST_LIMIT),
                "--json",
                "number,title,headRefName,baseRefName,isDraft,autoMergeRequest,mergeStateStatus,labels",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if pr_list_result.returncode != 0:
            logger.debug(f"gh pr list failed: {pr_list_result.stderr}")
            state.last_auto_merge_check = datetime.now(timezone.utc).isoformat()
            return result

        import json as json_mod

        prs = json_mod.loads(pr_list_result.stdout or "[]")
        result["prs_checked"] = len(prs)
        # A judge call blocks up to its timeout; resolve at most one unjudged
        # head per cycle so the loop keeps moving.
        judge_budget = {"left": 1}

        for pr in prs:
            pr_number = pr.get("number")
            pr_title = pr.get("title", "")
            branch = pr.get("headRefName", "")

            if pr_number is None:
                logger.debug("Skipping PR with missing number field")
                result["prs_skipped"] += 1
                continue

            # Only process daemon branches
            if not branch.startswith("daemon/"):
                result["prs_skipped"] += 1
                continue

            # Base-branch validation: on a fresh repo GitHub may have made a
            # daemon/* branch the default, silently redirecting every PR away
            # from main. Never merge into a daemon branch, and only merge into
            # configured base branches.
            base_branch = pr.get("baseRefName", "")
            if (
                base_branch.startswith("daemon/")
                or base_branch not in config.auto_merge_allowed_base_branches
            ):
                logger.warning(
                    f"PR #{pr_number} skipped — base branch {base_branch!r} is "
                    f"not an allowed merge target "
                    f"({config.auto_merge_allowed_base_branches})"
                )
                if config.auto_merge_escalation_enabled:
                    try:
                        _base_company_dir = (
                            Path(__file__).resolve().parent.parent.parent.parent
                            / ".company"
                        )
                        sk = _track_pr_skip(pr_number, pr_title, _base_company_dir)
                        if sk >= config.auto_merge_escalation_threshold:
                            _escalate_stuck_pr(
                                pr_number, pr_title, sk, _base_company_dir
                            )
                            state.auto_merge_escalations += 1
                    except Exception as _esc_err:
                        logger.debug(
                            f"PR #{pr_number} base-branch escalation failed: {_esc_err}"
                        )
                result["prs_skipped"] += 1
                continue

            # Hold-label gate: skip PRs held for human sign-off — the
            # deliverable gate's needs-manual-review and the operator's
            # awaiting-operator-review. Merging past either label would
            # silently bypass a human hold.
            pr_label_names = {lbl.get("name", "") for lbl in pr.get("labels", [])}
            held_labels = pr_label_names & _hold_label_names()
            if held_labels == {_pending_label()}:
                _company_dir_v = (
                    Path(__file__).resolve().parent.parent.parent.parent / ".company"
                )
                try:
                    _pending = _resolve_pending_verdict(
                        pr_number,
                        pr_title,
                        branch,
                        config,
                        _company_dir_v,
                        judge_budget,
                    )
                except Exception as _rv_err:
                    logger.warning(
                        f"PR #{pr_number}: pending-verdict resolution failed: {_rv_err}"
                    )
                    _pending = "held"
                if _pending == "released":
                    held_labels = set()
            # CHALLENGER-STAGE: a product-goal PR the challenger has not passed
            # is held even before any label made it there.
            if not held_labels:
                _ch_block = _challenger_merge_block(pr_number)
                if _ch_block:
                    held_labels = {_ch_block}
            if held_labels:
                # A hold label cannot stop GitHub's native auto-merge once it
                # is armed (PR #248 disarm gap; ci.yml disarms too, but only
                # on CI-triggering events) — disarm here so a hand-applied
                # hold takes effect within one daemon cycle.
                if pr.get("autoMergeRequest"):
                    try:
                        subprocess.run(
                            ["gh", "pr", "merge", str(pr_number), "--disable-auto"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        logger.info(
                            f"PR #{pr_number}: disarmed armed native auto-merge "
                            f"(hold label present)"
                        )
                    except (subprocess.TimeoutExpired, OSError) as _disarm_err:
                        logger.warning(
                            f"PR #{pr_number}: failed to disarm native "
                            f"auto-merge: {_disarm_err}"
                        )
                logger.info(
                    f"PR #{pr_number} skipped — carries hold label "
                    f"{', '.join(sorted(held_labels))}"
                )
                result["prs_skipped"] += 1
                continue

            # Operator merge policy (autoMerge.allowMergeToMain/requireApproval)
            # — a config-level hold, handled like a hold label: no draft flip,
            # no rebase, no merge of any kind, and an armed native auto-merge
            # is disarmed (it would otherwise merge without the daemon).
            config_block, approved_head = _merge_policy_check(config, pr_number)
            if config_block:
                if pr.get("autoMergeRequest"):
                    try:
                        subprocess.run(
                            ["gh", "pr", "merge", str(pr_number), "--disable-auto"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    except (subprocess.TimeoutExpired, OSError) as _disarm_err:
                        logger.warning(
                            f"PR #{pr_number}: failed to disarm native "
                            f"auto-merge: {_disarm_err}"
                        )
                _log_merge_config_skip(pr_number, config_block)
                result["prs_skipped"] += 1
                continue

            # Convert draft PRs to ready (drafts can't be auto-merged)
            if pr.get("isDraft"):
                logger.info(f"Converting draft PR #{pr_number} to ready")
                ready_result = subprocess.run(
                    ["gh", "pr", "ready", str(pr_number)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if ready_result.returncode != 0:
                    logger.debug(f"PR #{pr_number} ready failed: {ready_result.stderr}")
                    result["prs_skipped"] += 1
                    continue

            # Phase0-T0.6: Bounded conflict resolution — every CONFLICTING PR
            # must reach a terminal state within MAX_CONFLICT_REBASE_ATTEMPTS passes.
            # Three paths: (A) rebase succeeds → fall through to merge gates,
            # (B) rebase fails → close PR (task returns to pending via pr_open reconcile),
            # (C) attempts exhausted or subprocess error → escalate.
            _MAX_CONFLICT_REBASE_ATTEMPTS = 3
            pr_status = pr.get("mergeStateStatus", "")
            if pr_status == "DIRTY":
                pr_key = str(pr_number)
                attempts = state.pr_conflict_attempts.get(pr_key, 0)

                if attempts >= _MAX_CONFLICT_REBASE_ATTEMPTS:
                    # PATH C: still DIRTY after N attempts — escalate
                    logger.warning(
                        f"[Schedule] PR #{pr_number} still DIRTY after "
                        f"{attempts} rebase attempt(s) — escalating (path C)"
                    )
                    state.pr_conflict_attempts.pop(pr_key, None)
                    _company_dir = (
                        Path(__file__).resolve().parent.parent.parent.parent
                        / ".company"
                    )
                    if config.auto_merge_escalation_enabled:
                        try:
                            sk = _track_pr_skip(pr_number, pr_title, _company_dir)
                            if sk >= config.auto_merge_escalation_threshold:
                                _escalate_stuck_pr(
                                    pr_number, pr_title, sk, _company_dir
                                )
                                state.auto_merge_escalations += 1
                        except Exception as _esc_err:
                            logger.debug(
                                f"PR #{pr_number} conflict escalation failed: {_esc_err}"
                            )
                    result["prs_skipped"] += 1
                    continue

                # Increment before subprocess so an exception counts as an attempt
                state.pr_conflict_attempts[pr_key] = attempts + 1
                logger.info(
                    f"[Schedule] PR #{pr_number} is DIRTY — updating branch "
                    f"(attempt {attempts + 1}/{_MAX_CONFLICT_REBASE_ATTEMPTS})"
                )

                try:
                    update_result = subprocess.run(
                        ["gh", "pr", "update-branch", str(pr_number), "--rebase"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                except Exception as _rebase_err:
                    # Subprocess failed (timeout, auth, etc.) — counter stays
                    # incremented so repeated failures eventually reach PATH C
                    # and escalate rather than retrying forever.
                    logger.warning(
                        f"[Schedule] PR #{pr_number} rebase subprocess error "
                        f"(attempt {attempts + 1}/{_MAX_CONFLICT_REBASE_ATTEMPTS}): {_rebase_err}"
                    )
                    result["prs_skipped"] += 1
                    continue

                if update_result.returncode == 0:
                    # PATH A: rebase succeeded — clear counter, fall through to merge gates.
                    # The rebase made a new head: disarm native auto-merge and
                    # re-hold it for a verdict of its own (#581).
                    try:
                        _verdict_hold().hold_for_new_head(pr_number)
                    except Exception as _vh_err:
                        logger.warning(
                            f"PR #{pr_number}: verdict hold after rebase failed: {_vh_err}"
                        )
                    logger.info(
                        f"[Schedule] PR #{pr_number} rebased onto main "
                        f"(attempt {attempts + 1}) — continuing to merge gates (path A)"
                    )
                    state.pr_conflict_attempts.pop(pr_key, None)
                    # Fall through to merge logic below
                else:
                    # PATH B: rebase failed — irreconcilable conflict.
                    # Phase2-T2.2: Increment task-level counter (persists across
                    # close+regenerate cycles). After N total closures, escalate
                    # instead of re-queuing the task for another doomed cycle (T0.6 fix).
                    _task_id_b = _extract_task_id_from_pr_title(pr_title)
                    _task_total_attempts = 0
                    if _task_id_b:
                        _task_total_attempts = (
                            state.task_conflict_attempts.get(_task_id_b, 0) + 1
                        )
                        state.task_conflict_attempts[_task_id_b] = _task_total_attempts

                    if (
                        _task_id_b
                        and _task_total_attempts >= _MAX_CONFLICT_REBASE_ATTEMPTS
                    ):
                        # Task has exhausted N total attempts across regenerations:
                        # escalate instead of closing (prevents infinite re-queue loop).
                        logger.warning(
                            f"[Schedule] PR #{pr_number} task {_task_id_b} rebase failed "
                            f"(attempt {attempts + 1}); task total={_task_total_attempts} "
                            f">= {_MAX_CONFLICT_REBASE_ATTEMPTS} — escalating (path B→T0.6)"
                        )
                        state.task_conflict_attempts.pop(_task_id_b, None)
                        state.pr_conflict_attempts.pop(pr_key, None)
                        _company_dir_b = (
                            Path(__file__).resolve().parent.parent.parent.parent
                            / ".company"
                        )
                        if config.auto_merge_escalation_enabled:
                            try:
                                sk = _track_pr_skip(pr_number, pr_title, _company_dir_b)
                                if sk >= config.auto_merge_escalation_threshold:
                                    _escalate_stuck_pr(
                                        pr_number, pr_title, sk, _company_dir_b
                                    )
                                    state.auto_merge_escalations += 1
                            except Exception as _esc_b_err:
                                logger.debug(
                                    f"PR #{pr_number} T0.6 escalation failed: {_esc_b_err}"
                                )
                        result["prs_skipped"] += 1
                        continue

                    logger.warning(
                        f"[Schedule] PR #{pr_number} rebase failed "
                        f"(attempt {attempts + 1}) — closing (path B)"
                    )
                    close_result = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "close",
                            str(pr_number),
                            "--comment",
                            "Auto-closed: branch has irreconcilable merge conflicts. "
                            "Task will return to pending via reconcile (pr_open state machine).",
                            "--delete-branch",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if close_result.returncode != 0:
                        logger.debug(
                            f"[Schedule] PR #{pr_number} close failed: {close_result.stderr[:200]}"
                        )
                    state.pr_conflict_attempts.pop(pr_key, None)
                    result["prs_skipped"] += 1
                    continue

            # WS-066-002: Full-trust — security gates then direct merge
            if config.auto_merge_full_trust:
                company_dir = (
                    Path(__file__).resolve().parent.parent.parent.parent / ".company"
                )
                gate_passed, gate_results = _check_security_gates(
                    pr_number=pr_number,
                    config=config,
                    company_dir=company_dir,
                )
                # WS-066-003: Check for pre-existing CI failure bypass
                use_admin_merge = False
                if not gate_passed and config.auto_merge_bypass_preexisting_failures:
                    # Check if CI is the ONLY failing gate
                    gates = gate_results.get("gates", {})
                    ci_gate = gates.get("ci_checks", {})
                    other_gates_passed = all(
                        g.get("passed", False)
                        for name, g in gates.items()
                        if name != "ci_checks"
                    )
                    ci_failed = not ci_gate.get("passed", True)
                    failing_checks = ci_gate.get("failing_checks", [])

                    if ci_failed and other_gates_passed and failing_checks:
                        # Check if failures are pre-existing
                        all_preexisting, preexisting = _detect_preexisting_ci_failures(
                            pr_number,
                            failing_checks,
                            excluded_checks=tuple(
                                config.auto_merge_bypass_excluded_checks
                            ),
                        )
                        if all_preexisting:
                            # Check rate limit
                            if _check_admin_merge_rate_limit(
                                state, config.auto_merge_admin_max_per_day
                            ):
                                # Check rollback health
                                if (
                                    not config.auto_merge_require_rollback_healthy
                                    or _check_rollback_health(company_dir)
                                ):
                                    use_admin_merge = True
                                    logger.info(
                                        f"[Schedule] PR #{pr_number}: CI failures are pre-existing "
                                        f"({preexisting}), using admin merge"
                                    )
                                else:
                                    logger.warning(
                                        f"[Schedule] PR #{pr_number}: pre-existing CI failures but "
                                        f"rollback system unhealthy, skipping admin merge"
                                    )
                            else:
                                logger.warning(
                                    f"[Schedule] PR #{pr_number}: pre-existing CI failures but "
                                    f"admin merge rate limit reached ({state.admin_merges_today}/"
                                    f"{config.auto_merge_admin_max_per_day})"
                                )

                if not gate_passed and not use_admin_merge:
                    _hold_on_verdict_refusal(
                        pr_number,
                        gate_results.get("gates", {}).get("deliverable_verdict"),
                    )
                    # WS-066-004: Try to heal CI failures before escalating
                    heal_attempted = False
                    if config.ci_healer_enabled:
                        try:
                            from ci_healer import (
                                attempt_heal,
                                check_heal_rate_limit,
                                log_heal_activity,
                            )

                            if check_heal_rate_limit(
                                company_dir, config.ci_healer_max_heals_per_day
                            ):
                                project_root = (
                                    Path(__file__).resolve().parent.parent.parent.parent
                                )
                                heal_result = attempt_heal(
                                    pr_number, branch, project_root, company_dir
                                )
                                heal_attempted = True

                                if heal_result.get("fixes_successful", 0) > 0:
                                    logger.info(
                                        f"[Schedule] PR #{pr_number}: CI healer applied "
                                        f"{heal_result['fixes_successful']} fixes, "
                                        f"waiting for CI rerun"
                                    )
                                    log_heal_activity(
                                        pr_number, heal_result, company_dir
                                    )
                                    # Skip this PR, it will be re-evaluated after CI reruns
                                    result["prs_skipped"] += 1
                                    continue
                                elif heal_result.get("failures_found", 0) > 0:
                                    logger.debug(
                                        f"[Schedule] PR #{pr_number}: CI healer found "
                                        f"{heal_result['failures_found']} failures but "
                                        f"could not fix: {heal_result.get('errors', [])}"
                                    )
                        except ImportError:
                            logger.debug("CI healer module not available")
                        except Exception as e:
                            logger.debug(f"CI healer error: {e}")

                    # E8: path-D — CI-fail terminal path (analogue of path-B for dirty PRs).
                    # Count scheduler cycles where this PR has definitive (non-pending) CI
                    # failures. After ci_fail_terminal_cycles cycles, close and requeue once
                    # (path-D1); on repeat failure, block and escalate (path-D2).
                    if config.ci_fail_terminal_enabled:
                        _ci_gate_d = gate_results.get("gates", {}).get("ci_checks", {})
                        _failing_checks_d = _ci_gate_d.get("failing_checks", [])
                        if _failing_checks_d:
                            _pr_key_d = str(pr_number)
                            state.pr_ci_fail_cycles[_pr_key_d] = (
                                state.pr_ci_fail_cycles.get(_pr_key_d, 0) + 1
                            )
                            if (
                                state.pr_ci_fail_cycles[_pr_key_d]
                                >= config.ci_fail_terminal_cycles
                            ):
                                _task_id_d = _extract_task_id_from_pr_title(pr_title)
                                state.pr_ci_fail_cycles.pop(_pr_key_d, None)
                                if _task_id_d:
                                    _ci_requeues_d = state.task_ci_fail_requeues.get(
                                        _task_id_d, 0
                                    )
                                    if _ci_requeues_d >= config.ci_fail_max_requeues:
                                        logger.warning(
                                            f"[Schedule] PR #{pr_number} CI-fail ceiling: "
                                            f"task {_task_id_d} CI-fail-requeued "
                                            f"{_ci_requeues_d}x — escalating (path-D2)"
                                        )
                                        try:
                                            _escalate_ci_failed_task(
                                                pr_number,
                                                pr_title,
                                                _task_id_d,
                                                _failing_checks_d,
                                                company_dir,
                                            )
                                            state.pr_ci_fail_escalations += 1
                                            state.auto_merge_escalations += 1
                                        except Exception as _d2_err:
                                            logger.debug(
                                                f"Path-D2 escalation failed: {_d2_err}"
                                            )
                                    else:
                                        logger.warning(
                                            f"[Schedule] PR #{pr_number} CI-fail terminal: "
                                            f"closing and requeueing task {_task_id_d} "
                                            f"(path-D1, requeue #{_ci_requeues_d + 1})"
                                        )
                                        try:
                                            _ok_d = _close_and_requeue_ci_failed_pr(
                                                pr_number,
                                                pr_title,
                                                _task_id_d,
                                                _failing_checks_d,
                                                company_dir,
                                            )
                                            if _ok_d:
                                                state.task_ci_fail_requeues[
                                                    _task_id_d
                                                ] = _ci_requeues_d + 1
                                                state.pr_ci_fail_requeues += 1
                                        except Exception as _d1_err:
                                            logger.debug(
                                                f"Path-D1 requeue failed: {_d1_err}"
                                            )
                                    result["prs_skipped"] += 1
                                    continue

                    state.auto_merge_gate_blocks += 1
                    result["prs_skipped"] += 1
                    if config.auto_merge_escalation_enabled:
                        try:
                            sk = _track_pr_skip(
                                pr_number,
                                pr_title,
                                company_dir,
                            )
                            if sk >= config.auto_merge_escalation_threshold:
                                _escalate_stuck_pr(
                                    pr_number,
                                    pr_title,
                                    sk,
                                    company_dir,
                                )
                                state.auto_merge_escalations += 1
                        except Exception:
                            pass
                    if config.auto_merge_audit_enabled:
                        try:
                            _record_auto_merge_audit(
                                gate_results,
                                pr_title,
                                branch,
                                "blocked" if not heal_attempted else "heal_attempted",
                                company_dir,
                            )
                        except Exception:
                            pass
                    continue

                state.auto_merge_gate_passes += 1
                # TOCTOU: verify SHA unchanged
                expected_sha = gate_results.get("head_sha", "")
                if not expected_sha:
                    # The merge is pinned to the gated head below
                    # (--match-head-commit); no head, no merge.
                    logger.warning(
                        f"[Schedule] PR #{pr_number}: gates returned no head SHA "
                        f"— not merging"
                    )
                    result["prs_skipped"] += 1
                    continue
                if expected_sha:
                    sha_chk = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "view",
                            str(pr_number),
                            "--json",
                            "headRefOid",
                            "-q",
                            ".headRefOid",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if sha_chk.stdout.strip() != expected_sha:
                        result["prs_skipped"] += 1
                        continue
                # requireApproval: merge only the exact head a human approved
                # (a push after the approval — e.g. the CI healer — re-arms
                # the need for a fresh approval).
                if config.auto_merge_require_approval and (
                    not approved_head or expected_sha != approved_head
                ):
                    _log_merge_config_skip(
                        pr_number,
                        "requireApproval=true and the head moved since the approval",
                    )
                    result["prs_skipped"] += 1
                    continue
                # Re-read the approval at the last moment: a CHANGES_REQUESTED
                # (or a dismissed approval) posted while the gates ran leaves
                # the head unchanged, so the SHA pin alone would not catch it.
                if (
                    config.auto_merge_require_approval
                    and _pr_approved_head(pr_number) != expected_sha
                ):
                    _log_merge_config_skip(
                        pr_number,
                        "requireApproval=true and the approval no longer stands "
                        "at merge time",
                    )
                    result["prs_skipped"] += 1
                    continue

                # single-merge-gate: the daemon's own lever takes the decision
                # every other lever takes (merge_gate.may_merge) — hold labels,
                # challenger, verdict, rename-aware blocked/review paths, secrets,
                # diff caps, green checks — judged on the head pinned below.
                _gate_refusal = _merge_gate_refusal(pr_number, expected_sha)
                if _gate_refusal:
                    logger.info(
                        f"[Schedule] PR #{pr_number}: merge gate refuses — "
                        f"{_gate_refusal}"
                    )
                    state.auto_merge_gate_blocks += 1
                    result["prs_skipped"] += 1
                    # A refusal for a lasting reason (paths, secrets, size,
                    # author, base) goes to a human: hold label + comment, which
                    # also disarms a live native arm (it would merge the moment
                    # CI went green). Transient refusals (checks, merge state, a
                    # gh blip, a moved head) just wait for a later cycle — never
                    # counted toward the stuck-PR auto-close.
                    if _gate_refusal.startswith(_GATE_HOLD_REASONS):
                        try:
                            from pr_output_manager import apply_manual_review_label

                            apply_manual_review_label(
                                pr_number,
                                f"Gatesmith merge gate refuses this PR: {_gate_refusal}",
                            )
                        except Exception as _hold_err:
                            logger.warning(
                                f"[Schedule] PR #{pr_number}: hold after gate "
                                f"refusal failed: {_hold_err}"
                            )
                    continue
                merge_cmd = [
                    "gh",
                    "pr",
                    "merge",
                    str(pr_number),
                    f"--{config.auto_merge_method}",
                    # GitHub refuses the merge if the head moved after the
                    # gates (and the approval) read it — closes the window
                    # between the checks above and this call, --admin included.
                    "--match-head-commit",
                    expected_sha,
                ]
                # WS-066-003: Use --admin for pre-existing CI failure bypass
                if use_admin_merge:
                    merge_cmd.append("--admin")
                if config.auto_merge_delete_branch:
                    merge_cmd.append("--delete-branch")
                mr = subprocess.run(
                    merge_cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if mr.returncode == 0:
                    result["prs_merged"] += 1
                    state.prs_merged += 1
                    # WS-066-003: Track admin merges
                    if use_admin_merge:
                        state.admin_merges_today += 1
                        logger.info(
                            f"[Schedule] PR #{pr_number} merged with --admin "
                            f"({state.admin_merges_today}/{config.auto_merge_admin_max_per_day} today)"
                        )
                    if config.auto_merge_escalation_enabled:
                        try:
                            _clear_pr_skip(pr_number, company_dir)
                        except Exception:
                            pass
                    # Phase0-T0.6: clear conflict attempt counters on successful merge
                    state.pr_conflict_attempts.pop(str(pr_number), None)
                    _task_id_merge = _extract_task_id_from_pr_title(pr_title)
                    if _task_id_merge:
                        state.task_conflict_attempts.pop(_task_id_merge, None)
                    # E8: clear CI-fail cycle counter on successful merge
                    state.pr_ci_fail_cycles.pop(str(pr_number), None)
                    if config.auto_merge_audit_enabled:
                        try:
                            merge_result = (
                                "admin_merge" if use_admin_merge else "success"
                            )
                            _record_auto_merge_audit(
                                gate_results,
                                pr_title,
                                branch,
                                merge_result,
                                company_dir,
                            )
                        except Exception:
                            pass
                    _append_merge_audit_chain(
                        company_dir,
                        pr_number,
                        pr_title,
                        gate_results,
                        "full_trust_admin" if use_admin_merge else "full_trust",
                    )
                    # WS-109-008: Customer notification on PR merge
                    try:
                        from customer_notifier import notify_customer_pr_merged

                        # Extract task_id from branch name (daemon/task-XXXXX-...)
                        task_id = None
                        if branch.startswith("daemon/"):
                            parts = branch.split("/", 1)[1].split("-")
                            if len(parts) >= 2:
                                task_id = "-".join(parts[:3])  # task-YYYYMMDD-HASH
                        notify_customer_pr_merged(
                            pr_number=pr_number,
                            pr_title=pr_title,
                            task_id=task_id,
                            send_slack=True,
                        )
                    except Exception as _notif_err:
                        logger.debug(f"Customer notification skipped: {_notif_err}")
                else:
                    result["errors"].append(f"PR #{pr_number}: {mr.stderr[:200]}")
                    result["prs_skipped"] += 1
                    state.prs_skipped += 1
                    if config.auto_merge_escalation_enabled:
                        try:
                            sk = _track_pr_skip(
                                pr_number,
                                pr_title,
                                company_dir,
                            )
                            if sk >= config.auto_merge_escalation_threshold:
                                _escalate_stuck_pr(
                                    pr_number,
                                    pr_title,
                                    sk,
                                    company_dir,
                                )
                                state.auto_merge_escalations += 1
                        except Exception:
                            pass
            # Legacy: GitHub auto-merge (only reached when auto_merge_full_trust is
            # False, an operator override). This branch used to arm --auto with NO
            # blocked_paths/review_paths check at all — a genuine bypass, since the
            # full-trust path's _check_security_gates never ran on this branch.
            # Fixing the full CI/secrets/diff-size gate suite here would be a much
            # bigger behavior change for an operator override that previously
            # skipped all of that; instead, run only the path check (blockedPaths +
            # reviewPaths) — the specific gap this task closes (PR 257 lesson).
            elif config.auto_merge_require_approval:
                # Native --auto is not bound to the approved head: a later
                # push would merge unreviewed. Refuse the legacy arm.
                _log_merge_config_skip(
                    pr_number,
                    "requireApproval=true — legacy native --auto is not head-bound",
                )
                result["prs_skipped"] += 1
                continue
            elif not pr.get("autoMergeRequest"):
                _legacy_path_hit = _blocked_or_review_path_match(pr_number, config)
                if _legacy_path_hit is None or _legacy_path_hit:
                    result["prs_skipped"] += 1
                    state.prs_skipped += 1
                    continue
                _legacy_head = _pr_head_sha(pr_number)
                if not _legacy_head:
                    result["prs_skipped"] += 1
                    state.prs_skipped += 1
                    continue
                # single-merge-gate: the legacy arm too; it arms only a head
                # the gate would merge now (a pending PR waits for a later cycle).
                _gate_refusal = _merge_gate_refusal(pr_number, _legacy_head)
                if _gate_refusal:
                    logger.info(
                        f"[Schedule] PR #{pr_number}: merge gate refuses the "
                        f"legacy arm — {_gate_refusal}"
                    )
                    result["prs_skipped"] += 1
                    state.prs_skipped += 1
                    continue
                merge_cmd = [
                    "gh",
                    "pr",
                    "merge",
                    str(pr_number),
                    f"--{config.auto_merge_method}",
                    "--auto",
                    "--match-head-commit",
                    _legacy_head,
                ]
                if config.auto_merge_delete_branch:
                    merge_cmd.append("--delete-branch")
                mr = subprocess.run(
                    merge_cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if mr.returncode == 0:
                    result["prs_merged"] += 1
                    state.prs_merged += 1
                    if config.auto_merge_escalation_enabled:
                        try:
                            cd = (
                                Path(__file__).resolve().parent.parent.parent.parent
                                / ".company"
                            )
                            _clear_pr_skip(pr_number, cd)
                        except Exception:
                            pass
                    # Phase0-T0.6: clear conflict attempt counters on successful merge
                    state.pr_conflict_attempts.pop(str(pr_number), None)
                    _task_id_merge2 = _extract_task_id_from_pr_title(pr_title)
                    if _task_id_merge2:
                        state.task_conflict_attempts.pop(_task_id_merge2, None)
                    # E8: clear CI-fail cycle counter on successful merge
                    state.pr_ci_fail_cycles.pop(str(pr_number), None)
                    _append_merge_audit_chain(
                        Path(__file__).resolve().parent.parent.parent.parent
                        / ".company",
                        pr_number,
                        pr_title,
                        None,
                        "github_auto",
                    )
                    # WS-109-008: Customer notification on PR merge
                    try:
                        from customer_notifier import notify_customer_pr_merged

                        task_id = None
                        if branch.startswith("daemon/"):
                            parts = branch.split("/", 1)[1].split("-")
                            if len(parts) >= 2:
                                task_id = "-".join(parts[:3])
                        notify_customer_pr_merged(
                            pr_number=pr_number,
                            pr_title=pr_title,
                            task_id=task_id,
                            send_slack=True,
                        )
                    except Exception as _notif_err:
                        logger.debug(f"Customer notification skipped: {_notif_err}")
                else:
                    result["errors"].append(f"PR #{pr_number}: {mr.stderr[:200]}")
                    result["prs_skipped"] += 1
                    state.prs_skipped += 1
                    if config.auto_merge_escalation_enabled:
                        try:
                            cd = (
                                Path(__file__).resolve().parent.parent.parent.parent
                                / ".company"
                            )
                            sk = _track_pr_skip(
                                pr_number,
                                pr_title,
                                cd,
                            )
                            if sk >= config.auto_merge_escalation_threshold:
                                _escalate_stuck_pr(
                                    pr_number,
                                    pr_title,
                                    sk,
                                    cd,
                                )
                                state.auto_merge_escalations += 1
                        except Exception:
                            pass
            else:
                result["prs_skipped"] += 1

        # P81: Auto-close stale PRs with merge conflicts (older than 24h)
        _close_stale_prs(prs, result)

        state.last_auto_merge_check = datetime.now(timezone.utc).isoformat()
        state.auto_merge_checks += 1

        return result

    except Exception as e:
        logger.error(f"Auto-merge check failed: {e}")
        state.last_auto_merge_check = datetime.now(timezone.utc).isoformat()
        return result


def _close_stale_prs(prs: list[dict], result: dict, max_age_hours: int = 24) -> None:
    """Auto-close daemon PRs that have merge conflicts and are older than max_age_hours.

    This prevents stale PRs from accumulating when the codebase has moved on.
    Only closes PRs on daemon/ branches to avoid touching human-created PRs.
    """
    import subprocess

    for pr in prs:
        pr_number = pr.get("number")
        branch = pr.get("headRefName", "")

        if not branch.startswith("daemon/"):
            continue

        try:
            # Check PR mergeability and age
            detail_result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "mergeable,createdAt,statusCheckRollup",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if detail_result.returncode != 0:
                continue

            import json as json_mod

            detail = json_mod.loads(detail_result.stdout)
            mergeable = detail.get("mergeable", "UNKNOWN")
            created_at = detail.get("createdAt", "")

            # Check age
            if created_at:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_hours = (
                    datetime.now(timezone.utc) - created
                ).total_seconds() / 3600
            else:
                age_hours = 0

            # Close if CONFLICTING and older than max_age_hours
            if mergeable == "CONFLICTING" and age_hours >= max_age_hours:
                pr_title = pr.get("title", "")
                logger.info(
                    f"Closing stale PR #{pr_number} "
                    f"({age_hours:.0f}h old, conflicts): {pr_title}"
                )
                close_result = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "close",
                        str(pr_number),
                        "--comment",
                        f"Auto-closed: merge conflicts detected after "
                        f"{age_hours:.0f}h. The codebase has moved on. "
                        f"If this work is still needed, resubmit as a new task.",
                        "--delete-branch",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if close_result.returncode == 0:
                    result.setdefault("prs_closed", 0)
                    result["prs_closed"] += 1
                    logger.info(f"Closed stale PR #{pr_number}")

            # Also close if all checks failed and older than max_age_hours
            checks = detail.get("statusCheckRollup", [])
            if checks and age_hours >= max_age_hours:
                failed_checks = [
                    c for c in checks if c.get("conclusion") in ("FAILURE", "ERROR")
                ]
                if len(failed_checks) == len(checks) and len(checks) > 0:
                    pr_title = pr.get("title", "")
                    logger.info(
                        f"Closing failed PR #{pr_number} "
                        f"({age_hours:.0f}h old, all checks failed): "
                        f"{pr_title}"
                    )
                    close_result = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "close",
                            str(pr_number),
                            "--comment",
                            f"Auto-closed: all CI checks failed after "
                            f"{age_hours:.0f}h. If this work is still "
                            f"needed, resubmit as a new task.",
                            "--delete-branch",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if close_result.returncode == 0:
                        result.setdefault("prs_closed", 0)
                        result["prs_closed"] += 1

        except Exception as e:
            logger.debug(f"Stale PR check error for #{pr_number}: {e}")


# -----------------------------------------------------------------------------
# WS-066-001: Automated Rollback for Failed Daemon PRs
# -----------------------------------------------------------------------------


def _should_run_rollback_check(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run rollback check for failed daemon PRs.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if rollback check should run.
    """
    # Check if auto-rollback is enabled in config
    rollback_config = config.raw_config.get("autonomy", {}).get("autoRollback", {})
    if not rollback_config.get("enabled", False):
        return False

    if not state.last_rollback_check:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_rollback_check.replace("Z", "+00:00")
        )
        check_interval = rollback_config.get("checkIntervalSeconds", 300)
        elapsed_seconds = (datetime.now(timezone.utc) - last_run).total_seconds()
        return elapsed_seconds >= check_interval
    except (ValueError, TypeError):
        return True


def _run_rollback_check(config: DaemonConfig, state: DaemonState) -> dict:
    """Run rollback check for daemon PRs that failed CI after merge.

    Detects recently merged daemon PRs with failing checks on main
    and creates rollback PRs to revert the changes.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        dict with rollback check results.
    """
    import sys
    from pathlib import Path

    result = {
        "failed_prs_found": 0,
        "rollbacks_executed": 0,
        "rollbacks_blocked": 0,
        "errors": [],
    }

    rollback_config = config.raw_config.get("autonomy", {}).get("autoRollback", {})
    company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"

    try:
        # Import the rollback executor module
        hooks_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(hooks_dir))
        from pr_rollback_executor import (
            detect_failed_merges,
            execute_rollback,
        )

        sys.path.pop(0)

        # Detect failed merges
        lookback_hours = rollback_config.get("lookbackHours", 1)
        failed_prs = detect_failed_merges(
            lookback_hours=lookback_hours,
            company_dir=company_dir,
        )

        result["failed_prs_found"] = len(failed_prs)

        for failed_pr in failed_prs:
            pr_number = failed_pr.get("pr_number")
            pr_title = failed_pr.get("pr_title", "")
            commit_sha = failed_pr.get("commit_sha", "")
            failed_checks = failed_pr.get("failed_checks", [])

            # Build failure reason
            check_names = [c.get("name", "Unknown") for c in failed_checks]
            reason = f"CI checks failed after merge: {', '.join(check_names)}"

            logger.info(f"[WS-066-001] Detected failed PR #{pr_number}: {pr_title}")

            # Execute rollback
            rollback_result = execute_rollback(
                pr_number=pr_number,
                pr_title=pr_title,
                commit_sha=commit_sha,
                reason=reason,
                failure_details={"failed_checks": failed_checks},
                company_dir=company_dir,
                # A fresh revert PR has no approval yet, so requireApproval
                # alone is enough to leave it for a human.
                arm_auto_merge=_native_auto_merge_permitted(config),
            )

            if rollback_result.get("success"):
                result["rollbacks_executed"] += 1
                state.rollbacks_executed += 1
                rollback_pr = rollback_result.get("rollback_pr_number")
                logger.info(
                    f"[WS-066-001] Created rollback PR #{rollback_pr} "
                    f"for PR #{pr_number}"
                )
            else:
                result["rollbacks_blocked"] += 1
                state.rollbacks_blocked += 1
                error = rollback_result.get("error", "Unknown error")
                result["errors"].append(f"PR #{pr_number}: {error}")
                logger.warning(
                    f"[WS-066-001] Rollback blocked for PR #{pr_number}: {error}"
                )

        state.last_rollback_check = datetime.now(timezone.utc).isoformat()
        state.rollback_checks_run += 1

    except ImportError as e:
        logger.error(f"[WS-066-001] Failed to import rollback executor: {e}")
        result["errors"].append(f"Import error: {e}")
        state.last_rollback_check = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        logger.error(f"[WS-066-001] Rollback check failed: {e}")
        result["errors"].append(str(e))
        state.last_rollback_check = datetime.now(timezone.utc).isoformat()

    return result


# -----------------------------------------------------------------------------
# P53: Scheduled Reports
# -----------------------------------------------------------------------------


def _parse_cron_schedule(cron_expr: str) -> tuple[int, int]:
    """Parse cron expression and return (minute, hour).

    Only supports simple "M H * * *" patterns for daily scheduling.
    Returns (minute, hour) or (-1, -1) if invalid.
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return (-1, -1)
        minute = int(parts[0])
        hour = int(parts[1])
        return (minute, hour)
    except (ValueError, IndexError):
        return (-1, -1)


def _should_run_scheduled_report(
    report: dict, state: "DaemonState", now: datetime
) -> bool:
    """Check if a scheduled report should run now.

    Args:
        report: Report config with 'name' and 'schedule' (cron).
        state: Daemon state tracking last runs.
        now: Current time.

    Returns:
        True if report should run.
    """
    name = report.get("name", "")
    schedule = report.get("schedule", "")

    if not name or not schedule:
        return False

    # Parse schedule
    minute, hour = _parse_cron_schedule(schedule)
    if minute < 0 or hour < 0:
        return False

    # Check if current time matches schedule
    if now.minute != minute or now.hour != hour:
        return False

    # Check if already run today
    last_run = state.scheduled_reports_run.get(name, "")
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            # Already ran today?
            if last_run_dt.date() == now.date():
                return False
        except ValueError:
            pass

    return True


def _run_scheduled_report(
    report: dict, config: "DaemonConfig", state: "DaemonState"
) -> dict:
    """Execute a scheduled report.

    Args:
        report: Report config with 'name', 'command', 'outputPath', 'notify'.
        config: Daemon config.
        state: Daemon state.

    Returns:
        Result dict with success status.
    """
    name = report.get("name", "unknown")
    command = report.get("command", "")
    notify = report.get("notify", False)

    result = {"name": name, "success": False, "output": ""}

    if not command:
        logger.warning(f"Scheduled report '{name}' has no command")
        return result

    try:
        logger.info(f"[P53] Running scheduled report: {name}")

        # Execute command — use shlex.split for safe argument parsing (no shell injection)
        proc = subprocess.run(
            shlex.split(command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=str(Path.cwd()),
        )

        result["output"] = proc.stdout[:1000] if proc.stdout else ""
        result["success"] = proc.returncode == 0

        if proc.returncode == 0:
            logger.info(f"[P53] Report '{name}' completed successfully")
            state.scheduled_reports_run[name] = datetime.now(timezone.utc).isoformat()
            state.scheduled_reports_count += 1

            # Send notification if configured
            if notify:
                try:
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'display notification "Report generated" with title "Gatesmith: {name}"',
                        ],
                        timeout=5,
                    )
                except Exception:
                    pass
        else:
            logger.warning(
                f"[P53] Report '{name}' failed: {proc.stderr[:200] if proc.stderr else 'no output'}"
            )

    except subprocess.TimeoutExpired:
        logger.error(f"[P53] Report '{name}' timed out")
    except Exception as e:
        logger.error(f"[P53] Report '{name}' error: {e}")

    return result


def _check_scheduled_reports(config: "DaemonConfig", state: "DaemonState") -> int:
    """Check and run any due scheduled reports.

    Args:
        config: Daemon config with scheduled_reports list.
        state: Daemon state.

    Returns:
        Number of reports run.
    """
    if not config.scheduled_reports_enabled:
        return 0

    reports = config.scheduled_reports
    if not reports:
        return 0

    now = datetime.now(timezone.utc)
    reports_run = 0

    for report in reports:
        if _should_run_scheduled_report(report, state, now):
            result = _run_scheduled_report(report, config, state)
            if result.get("success"):
                reports_run += 1

    return reports_run


# -----------------------------------------------------------------------------
# Scheduled Tasks — queue work items for employees on a cron schedule
# -----------------------------------------------------------------------------


def _match_cron_field(field: str, value: int, min_val: int, max_val: int) -> bool:
    """Return True if `value` satisfies the cron `field` expression.

    Supports: *, N, */N, N-M, N,M,...
    """
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            start = min_val if base == "*" else int(base)
            if value >= start and (value - start) % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        else:
            if int(part) == value:
                return True
    return False


def _cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Return True if the full 5-field cron expression matches *dt*.

    Format: minute hour day-of-month month day-of-week (0=Sunday).
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        m_min, m_hour, m_dom, m_month, m_dow = parts
        dow = dt.weekday() + 1  # Python: 0=Mon; cron: 0=Sun so Mon→1 … Sun→0
        dow = dow % 7  # Sun=0, Mon=1, …, Sat=6
        return (
            _match_cron_field(m_min, dt.minute, 0, 59)
            and _match_cron_field(m_hour, dt.hour, 0, 23)
            and _match_cron_field(m_dom, dt.day, 1, 31)
            and _match_cron_field(m_month, dt.month, 1, 12)
            and _match_cron_field(m_dow, dow, 0, 6)
        )
    except (ValueError, IndexError):
        return False


def _should_run_scheduled_task(task: dict, state: "DaemonState", now: datetime) -> bool:
    """Return True if the scheduled task is due and hasn't run in this period."""
    task_id = task.get("id", "")
    schedule = task.get("schedule", "")
    if not task_id or not schedule:
        return False

    if not _cron_matches(schedule, now):
        return False

    # Prevent re-queuing within the same clock-minute
    last_run = state.scheduled_tasks_run.get(task_id, "")
    if last_run:
        try:
            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            # Already triggered in this exact minute
            if (
                last_dt.year == now.year
                and last_dt.month == now.month
                and last_dt.day == now.day
                and last_dt.hour == now.hour
                and last_dt.minute == now.minute
            ):
                return False
        except ValueError:
            pass

    return True


def _queue_scheduled_task(
    task: dict, config: "DaemonConfig", state: "DaemonState"
) -> bool:
    """Queue a scheduled task as a work item via work_allocator.add_task().

    Returns True on success.
    """
    try:
        from work_allocator import add_task  # type: ignore[import]
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from work_allocator import add_task  # type: ignore[import]
        except ImportError:
            logger.warning(
                "[ScheduledTasks] Cannot import work_allocator — skipping task queue"
            )
            return False

    task_id = task.get("id", "unknown")
    title = task.get("name", task_id)
    description = task.get("description", "")
    priority = int(task.get("priority", 3))
    capabilities = task.get("capabilities", [])
    complexity = task.get("estimatedComplexity", "standard")
    assigned_to = task.get("assignTo", "")
    schedule = task.get("schedule", "")

    # WS-108: Build rich description with full scheduled task context
    description_parts = []

    # Embed the assignTo hint so routing picks it up
    if assigned_to:
        description_parts.append(f"[Scheduled task — assign to: {assigned_to}]")
        description_parts.append("")

    # Main description
    if description:
        description_parts.append(description)
        description_parts.append("")

    # Add schedule context
    description_parts.append("---")
    description_parts.append(f"**Scheduled Task ID:** {task_id}")
    if schedule:
        description_parts.append(f"**Schedule:** `{schedule}`")
    description_parts.append("**Source:** scheduled (daemon-triggered)")
    if capabilities:
        description_parts.append(
            f"**Required Capabilities:** {', '.join(capabilities)}"
        )
    if complexity != "standard":
        description_parts.append(f"**Complexity:** {complexity}")

    # Include any additional metadata fields from the task config
    extra_fields = ["category", "department", "rationale", "acceptance_criteria"]
    for extra_field in extra_fields:
        value = task.get(extra_field)
        if value:
            field_name = extra_field.replace("_", " ").title()
            if isinstance(value, list):
                description_parts.append(f"**{field_name}:**")
                for item in value:
                    description_parts.append(f"- {item}")
            else:
                description_parts.append(f"**{field_name}:** {value}")

    full_description = "\n".join(description_parts)

    try:
        result = add_task(
            title=title,
            priority=priority,
            required_capabilities=capabilities if capabilities else None,
            estimated_complexity=complexity,
            description=full_description,
            source="scheduled",
        )
        queued_id = result.get("task_id", "")
        logger.info(f"[ScheduledTasks] Queued '{title}' → task {queued_id}")
        state.scheduled_tasks_run[task_id] = datetime.now(timezone.utc).isoformat()
        state.scheduled_tasks_count += 1
        return True
    except Exception as exc:
        logger.error(f"[ScheduledTasks] Failed to queue '{title}': {exc}")
        return False


def _check_scheduled_tasks(config: "DaemonConfig", state: "DaemonState") -> int:
    """Check all scheduled tasks and queue any that are due.

    Returns:
        Number of tasks queued.
    """
    if not config.scheduled_tasks_enabled:
        return 0

    tasks = config.scheduled_tasks
    if not tasks:
        return 0

    now = datetime.now(timezone.utc)
    queued = 0
    for task in tasks:
        if _should_run_scheduled_task(task, state, now):
            if _queue_scheduled_task(task, config, state):
                queued += 1

    return queued


# -----------------------------------------------------------------------------
# PR Skip Tracking & Escalation (ADR-0002 Feature 4)
# -----------------------------------------------------------------------------


def _load_skip_tracker(company_dir: Path) -> dict:
    """Load PR skip tracker from persistent storage."""
    tracker_path = company_dir / "auto_merge_skip_tracker.json"
    if tracker_path.exists():
        try:
            return json.loads(tracker_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"prs": {}}


def _save_skip_tracker(tracker: dict, company_dir: Path) -> None:
    """Save PR skip tracker with atomic write."""
    import tempfile

    tracker_path = company_dir / "auto_merge_skip_tracker.json"
    fd, tmp = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
    try:
        os.write(fd, json.dumps(tracker, indent=2).encode())
        os.close(fd)
        os.replace(tmp, str(tracker_path))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _track_pr_skip(pr_number: int, pr_title: str, company_dir: Path) -> int:
    """Track a PR merge skip. Returns current skip count."""
    tracker = _load_skip_tracker(company_dir)
    key = str(pr_number)
    if key not in tracker["prs"]:
        tracker["prs"][key] = {
            "title": pr_title,
            "skip_count": 0,
            "first_skip": datetime.now(timezone.utc).isoformat(),
        }
    tracker["prs"][key]["skip_count"] += 1
    tracker["prs"][key]["last_skip"] = datetime.now(timezone.utc).isoformat()
    _save_skip_tracker(tracker, company_dir)
    return tracker["prs"][key]["skip_count"]


def _clear_pr_skip(pr_number: int, company_dir: Path) -> None:
    """Clear skip tracking for a merged PR."""
    tracker = _load_skip_tracker(company_dir)
    key = str(pr_number)
    if key in tracker["prs"]:
        del tracker["prs"][key]
        _save_skip_tracker(tracker, company_dir)


def _escalate_stuck_pr(
    pr_number: int,
    pr_title: str,
    skip_count: int,
    company_dir: Path,
) -> None:
    """Create a work queue task for a stuck PR."""
    _ensure_imports()
    queue_path = company_dir / "state/work_queue.json"
    lock_path = company_dir / "runtime/queue.lock"

    # Use QueueLock to prevent concurrent write races (P50 fix)
    try:
        with work_allocator.QueueLock(lock_path):
            try:
                queue = json.loads(queue_path.read_text())
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                queue = {
                    "pending": [],
                    "in_progress": [],
                    "blocked": [],
                    "completed": [],
                }

            task_id = f"escalation-pr-{pr_number}"
            # Don't duplicate — check ALL queues including blocked/failed/completed
            for status_key in (
                "pending",
                "in_progress",
                "blocked",
                "completed",
                "failed",
            ):
                for t in queue.get(status_key, []):
                    if t.get("task_id") == task_id:
                        return

            # Hard cap: close PR after too many failed attempts.
            # Reduced from 10 to 3 — escalation tasks create new PRs that
            # also fail CI, spawning cascading escalation chains.
            MAX_ESCALATION_ATTEMPTS = 3
            if skip_count >= MAX_ESCALATION_ATTEMPTS:
                logger.warning(
                    f"PR #{pr_number} hit escalation cap ({MAX_ESCALATION_ATTEMPTS} skips), auto-closing"
                )
                try:
                    subprocess.run(
                        [
                            "gh",
                            "pr",
                            "close",
                            str(pr_number),
                            "-c",
                            f"Auto-closing: exceeded {MAX_ESCALATION_ATTEMPTS} failed auto-merge attempts. Fix CI and reopen manually.",
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as close_err:
                    logger.debug(f"Failed to close capped PR: {close_err}")
                _clear_pr_skip(pr_number, company_dir)
                return

            # Prevent nested escalations - if the PR title already contains
            # "Stuck PR", this is an escalation trying to fix another escalation.
            # Close it instead of creating another escalation loop.
            if "Stuck PR" in pr_title:
                logger.warning(
                    f"Closing nested escalation PR #{pr_number} to prevent loop"
                )
                try:
                    subprocess.run(
                        [
                            "gh",
                            "pr",
                            "close",
                            str(pr_number),
                            "-c",
                            "Auto-closing: nested escalation detected (PR about stuck PR)",
                        ],
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as close_err:
                    logger.debug(f"Failed to close nested escalation: {close_err}")
                return

            queue.setdefault("pending", []).append(
                {
                    "task_id": task_id,
                    "title": (
                        f"Stuck PR #{pr_number}: {pr_title} (skipped {skip_count}x)"
                    ),
                    "description": (
                        f"PR #{pr_number} has failed auto-merge {skip_count} times. "
                        "Investigate why checks are failing and fix or close the PR."
                    ),
                    "priority": 85,
                    "source": "auto_merge_escalation",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

            fd, tmp = tempfile.mkstemp(dir=str(company_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    except Exception as e:
        logger.error(f"Failed to escalate stuck PR #{pr_number}: {e}")
        return

    logger.warning(f"Escalated stuck PR #{pr_number} after {skip_count} skips")


# -----------------------------------------------------------------------------
# Branch Protection Health Check (ADR-0002 Feature 1)
# -----------------------------------------------------------------------------


def _should_run_branch_protection_check(
    config: DaemonConfig, state: DaemonState
) -> bool:
    """Check if it's time to run branch protection health check."""
    if not config.branch_protection_check_enabled:
        return False
    if not state.last_branch_protection_check:
        return True
    try:
        last = datetime.fromisoformat(state.last_branch_protection_check)
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed >= config.branch_protection_check_interval_minutes * 60
    except (ValueError, TypeError):
        return True


def _run_branch_protection_check(config: DaemonConfig, state: DaemonState) -> dict:
    """Run branch protection health check via BranchProtectionChecker."""
    result = {"status": "error", "issues": []}
    try:
        from branch_protection_check import BranchProtectionChecker

        config_path = (
            Path(__file__).resolve().parent.parent.parent.parent / "gatesmith.json"
        )
        checker = BranchProtectionChecker(config_path=config_path)
        check_result = checker.check()
        result = check_result.to_dict()

        state.last_branch_protection_check = datetime.now(timezone.utc).isoformat()
        state.branch_protection_checks_run += 1

        if check_result.status == "degraded":
            state.branch_protection_degradations += 1
            logger.warning(f"Branch protection degraded: {check_result.issues}")
            if config.branch_protection_auto_restore:
                restore = checker.restore()
                if restore.get("success"):
                    logger.info("Branch protection auto-restored")
                    result["auto_restored"] = True
        elif check_result.status == "ok":
            logger.info("Branch protection health check: OK")
        else:
            logger.info(f"Branch protection check: {check_result.status}")

    except ImportError:
        logger.debug("branch_protection_check module not available")
        result["issues"].append("Module not installed")
    except Exception as e:
        logger.error(f"Branch protection check failed: {e}")
        result["issues"].append(str(e))

    return result


# -----------------------------------------------------------------------------
# Manager Review Loop (P26 Tasks 26.8, 26.9)
# -----------------------------------------------------------------------------


def _should_run_manager_review(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run manager review cycle.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if manager review should run.
    """
    if not config.manager_review_enabled:
        return False

    if not state.last_manager_review:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_manager_review.replace("Z", "+00:00")
        )
        elapsed_minutes = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
        return elapsed_minutes >= config.manager_review_interval_minutes
    except (ValueError, TypeError):
        return True


def _run_manager_review(
    config: DaemonConfig,
    state: DaemonState,
) -> dict[str, int]:
    """Run manager review cycle.

    Performs:
    1. Auto-approve low-risk proposals (if enabled)
    2. Process pending reviews
    3. Auto-complete stale reviews (if timeout enabled)

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        Dict with counts: proposals_approved, proposals_rejected, tasks_reviewed
    """
    _ensure_imports()

    result = {
        "proposals_approved": 0,
        "proposals_rejected": 0,
        "tasks_reviewed": 0,
        "reviews_auto_completed": 0,
    }
    if manager_review is None:  # not in the public export
        return result

    try:
        # Get a manager for reviews
        managers = manager_review.get_managers()

        if not managers:
            logger.debug("Manager review: no managers configured")
            state.last_manager_review = datetime.now(timezone.utc).isoformat()
            return result

        # Use first available manager (could be randomized or load-balanced)
        reviewer_id = managers[0]

        # 1. Auto-approve low-risk proposals
        if config.manager_auto_approve_enabled:
            auto_result = manager_review.auto_approve_low_risk_proposals(
                reviewer_id=reviewer_id,
                max_approvals=3,
            )
            if auto_result.get("success"):
                approved = auto_result.get("approved", 0)
                result["proposals_approved"] += approved
                state.proposals_approved += approved

                if approved > 0:
                    logger.info(
                        f"Manager review: auto-approved {approved} low-risk proposals"
                    )

        # 2. Check for stale reviews (auto-complete after timeout)
        if config.manager_review_timeout_hours > 0:
            queue = work_allocator.load_queue()
            now = datetime.now(timezone.utc)
            timeout_seconds = config.manager_review_timeout_hours * 3600

            for task in queue.get("review", []):
                review_requested_at_str = task.get("review_requested_at")
                if review_requested_at_str:
                    try:
                        review_requested_at = datetime.fromisoformat(
                            review_requested_at_str.replace("Z", "+00:00")
                        )
                        age_seconds = (now - review_requested_at).total_seconds()

                        if age_seconds > timeout_seconds:
                            # Auto-complete with default quality score
                            complete_result = work_allocator.complete_review(
                                task_id=task.get("task_id"),
                                reviewer_id="auto-review",
                                feedback="Auto-completed: review timeout exceeded",
                                quality_score=0.7,  # Default "good" score
                            )
                            if complete_result.get("success"):
                                result["reviews_auto_completed"] += 1
                                state.tasks_reviewed += 1
                                logger.info(
                                    f"Manager review: auto-completed stale review "
                                    f"for {task.get('title')}"
                                )
                    except (ValueError, TypeError):
                        pass

        # Update state
        state.last_manager_review = datetime.now(timezone.utc).isoformat()

        return result

    except Exception as e:
        logger.error(f"Manager review cycle failed: {e}")
        state.last_manager_review = datetime.now(timezone.utc).isoformat()
        return result


# -----------------------------------------------------------------------------
# Vision Auto-Refresh (WS-059)
# -----------------------------------------------------------------------------


def _should_run_vision_refresh(config: DaemonConfig, state: DaemonState) -> bool:
    """Check if it's time to run vision auto-refresh.

    Vision refresh runs every 7 days (168 hours) to assess goal progress
    and propose vision updates for CEO review.

    Args:
        config: Daemon configuration.
        state: Current daemon state.

    Returns:
        True if vision refresh should run.
    """
    if not config.vision_refresh_enabled:
        return False

    if not state.last_vision_refresh:
        return True

    try:
        last_run = datetime.fromisoformat(
            state.last_vision_refresh.replace("Z", "+00:00")
        )
        elapsed_hours = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
        return elapsed_hours >= config.vision_refresh_interval_hours
    except (ValueError, TypeError):
        return True


def _create_default_vision(vision_path: Path, company_dir: Path) -> None:
    """Create a default vision.md file when missing.

    Uses org.json company info and generates default quarterly goals.
    This ensures the daemon can always track strategic progress.
    """
    try:
        # Load company info
        org_path = company_dir / "org.json"
        company_name = "Company"
        company_desc = "Software development company"
        if org_path.exists():
            try:
                with open(org_path, encoding="utf-8") as f:
                    org = json.load(f)
                company_name = org.get("company", {}).get("name", company_name)
                company_desc = org.get("company", {}).get("description", company_desc)
            except Exception:
                pass

        # Generate current quarter
        now = datetime.now(timezone.utc)
        quarter = (now.month - 1) // 3 + 1
        period = f"Q{quarter} {now.year}"

        # Create default vision content
        content = f"""# {company_name} Vision

{company_desc}

### Period: {period} [status: active]

## Goals

| ID | Category | Goal | Success Criteria | Owner |
|----|----------|------|-----------------|-------|
| G1 | Quality | Increase test coverage | Coverage improves by 10%+ | forge-cto |
| G2 | Stability | Reduce critical bugs | Zero P0 issues for 14 days | forge-cto |
| G3 | Operations | Improve task completion rate | 90%+ tasks complete successfully | forge-cto |
| G4 | Documentation | Complete core documentation | All major features documented | technical-writer |

### Key Initiatives

1. **Quality Improvement** - Focus on test coverage and code quality
2. **Operational Excellence** - Improve daemon reliability and task success rate
3. **Documentation** - Ensure all features are well documented

---
*Auto-generated by daemon on {now.strftime("%Y-%m-%d")}. Update with company-specific goals.*
"""

        vision_path.write_text(content)
        logger.info(f"Vision refresh: created default vision.md for {period}")

    except Exception as e:
        logger.error(f"Vision refresh: failed to create default vision.md: {e}")


def _run_vision_refresh(state: DaemonState) -> dict[str, Any]:
    """Run vision auto-refresh cycle.

    Performs:
    1. Read .company/vision.md and extract active goals
    2. Load work queue completion data
    3. Cross-reference completions with goals
    4. Write proposed updates to .company/vision_proposal.md
    5. Create a task for forge-ceo to review and approve

    Args:
        state: Current daemon state (will be updated).

    Returns:
        Dict with: goals_assessed, achieved_count, proposal_created, task_queued, errors
    """
    _ensure_imports()

    result: dict[str, Any] = {
        "goals_assessed": 0,
        "achieved_count": 0,
        "proposal_created": False,
        "task_queued": False,
        "errors": [],
    }

    try:
        company_dir = company_resolver.get_company_dir()
        vision_path = company_dir / "vision.md"
        queue_path = company_dir / "state" / "work_queue.json"
        proposal_path = company_dir / "vision_proposal.md"

        # Step 1: Read vision.md (auto-create if missing)
        if not vision_path.exists():
            logger.info("Vision refresh: vision.md not found, creating default")
            _create_default_vision(vision_path, company_dir)
            if not vision_path.exists():
                logger.warning("Vision refresh: failed to create vision.md, skipping")
                state.last_vision_refresh = datetime.now(timezone.utc).isoformat()
                return result

        # Import goal_tracker locally (not in _ensure_imports)
        try:
            from . import goal_tracker as gt
        except ImportError:
            import goal_tracker as gt  # type: ignore[no-redef]

        active_period = gt.get_active_period(vision_path)
        if not active_period:
            logger.info("Vision refresh: no active period found, skipping")
            state.last_vision_refresh = datetime.now(timezone.utc).isoformat()
            return result

        goals = gt.parse_goals_from_vision(vision_path, period=active_period)
        result["goals_assessed"] = len(goals)

        # Step 2: Load queue completion data
        completed_tasks: list[dict] = []
        if queue_path.exists():
            try:
                with open(queue_path, encoding="utf-8") as f:
                    queue = json.load(f)
                all_completed = queue.get("completed", [])

                # Filter to tasks completed since last refresh
                if state.last_vision_refresh:
                    try:
                        last_run = datetime.fromisoformat(
                            state.last_vision_refresh.replace("Z", "+00:00")
                        )
                        for task in all_completed:
                            cat = task.get("completed_at", "")
                            if cat:
                                try:
                                    ct = datetime.fromisoformat(
                                        cat.replace("Z", "+00:00")
                                    )
                                    if ct >= last_run:
                                        completed_tasks.append(task)
                                except (ValueError, TypeError):
                                    pass
                    except (ValueError, TypeError):
                        completed_tasks = list(all_completed)
                else:
                    completed_tasks = list(all_completed)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Vision refresh: failed to load queue: {e}")

        # Step 3: Assess goal achievement
        achieved_goals: list[dict] = []
        for goal in goals:
            goal_id = goal.id.lower() if hasattr(goal, "id") else str(goal).lower()
            goal_name = goal.name if hasattr(goal, "name") else str(goal)

            # Count completed tasks mentioning this goal
            related = sum(
                1
                for t in completed_tasks
                if goal_id in t.get("title", "").lower()
                or goal_id in t.get("description", "").lower()
            )
            # Also check total completed count as a general progress signal
            status = "likely_complete" if related >= 5 else "in_progress"
            achieved_goals.append(
                {
                    "goal_id": goal_id,
                    "goal_name": goal_name,
                    "related_tasks": related,
                    "status": status,
                }
            )

        achieved_count = sum(
            1 for g in achieved_goals if g["status"] == "likely_complete"
        )
        result["achieved_count"] = achieved_count

        # Step 4: Generate proposal if there's meaningful data
        if achieved_count > 0 or len(completed_tasks) >= 10:
            proposal = _generate_vision_proposal(
                active_period=active_period,
                completed_tasks=completed_tasks,
                achieved_goals=achieved_goals,
            )

            # Write proposal using atomic write (tempfile + os.replace)
            import os
            import tempfile

            proposal_dir = proposal_path.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(proposal_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(proposal)
                os.replace(tmp_path, str(proposal_path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            result["proposal_created"] = True
            logger.info(f"Vision refresh: proposal written to {proposal_path}")

            # Step 5: Create CEO review task
            task_result = work_allocator.add_task(
                title="Vision Refresh: Review and Approve Proposed Changes",
                priority=2,
                estimated_complexity="standard",
                description=(
                    f"[Assign to: forge-ceo]\n\n"
                    f"Review the vision refresh proposal at "
                    f".company/vision_proposal.md\n\n"
                    f"**Summary:**\n"
                    f"- Active period: {active_period}\n"
                    f"- Tasks completed since last refresh: "
                    f"{len(completed_tasks)}\n"
                    f"- Goals likely achieved: {achieved_count}\n"
                    f"- Goals in progress: "
                    f"{len(achieved_goals) - achieved_count}\n\n"
                    f"**Action required:** Review the proposal and either:\n"
                    f"1. Approve — update vision.md with proposed changes\n"
                    f"2. Reject — discard proposal and note reasons\n\n"
                    f"Human must approve vision changes."
                ),
                source="daemon-vision-refresh",
            )

            if task_result.get("task_id"):
                result["task_queued"] = True
                state.vision_proposals_submitted += 1
                logger.info(
                    f"Vision refresh: CEO review task created: {task_result['task_id']}"
                )
        else:
            logger.info(
                f"Vision refresh: no proposal needed "
                f"(achieved={achieved_count}, completed={len(completed_tasks)})"
            )

        # Update state
        state.last_vision_refresh = datetime.now(timezone.utc).isoformat()
        state.vision_refresh_cycles_run += 1

        return result

    except Exception as e:
        logger.error(f"Vision refresh failed: {e}")
        result["errors"].append(str(e))
        # Still update timestamp to avoid retry storm
        state.last_vision_refresh = datetime.now(timezone.utc).isoformat()
        return result


def _generate_vision_proposal(
    active_period: str,
    completed_tasks: list[dict],
    achieved_goals: list[dict],
) -> str:
    """Generate a vision proposal markdown document.

    Args:
        active_period: Current active period name (e.g. "Q2 2026").
        completed_tasks: Recently completed tasks from the queue.
        achieved_goals: Goal assessment results.

    Returns:
        Proposal markdown content.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    likely_complete = [g for g in achieved_goals if g["status"] == "likely_complete"]
    in_progress = [g for g in achieved_goals if g["status"] != "likely_complete"]

    lines = [
        "# Vision Refresh Proposal",
        "",
        f"**Generated:** {now_iso}",
        f"**Active Period:** {active_period}",
        "",
        "## Executive Summary",
        "",
        "This proposal reflects accomplishments since the last vision refresh.",
        f"Based on {len(completed_tasks)} completed tasks in the review window.",
        "",
    ]

    if likely_complete:
        lines.append("## Goals Likely Achieved")
        lines.append("")
        for g in likely_complete:
            lines.append(
                f"- **{g['goal_id'].upper()}: {g['goal_name']}** "
                f"— {g['related_tasks']} related tasks completed"
            )
        lines.append("")

    if in_progress:
        lines.append("## Goals In Progress")
        lines.append("")
        for g in in_progress:
            lines.append(
                f"- **{g['goal_id'].upper()}: {g['goal_name']}** "
                f"— {g['related_tasks']} related tasks"
            )
        lines.append("")

    lines.append("## Recent Completions (sample)")
    lines.append("")
    for task in completed_tasks[:10]:
        lines.append(f"- {task.get('title', 'Unknown task')}")
    lines.append("")

    lines.append("## Proposed Actions")
    lines.append("")
    lines.append(f"1. Mark {len(likely_complete)} goal(s) as complete in vision.md")
    lines.append(
        "2. Consider advancing to next planning period if all active goals complete"
    )
    lines.append("3. Identify and add goals for next period")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "**Requires Human Approval:** This proposal must be reviewed and "
        "approved by forge-ceo before vision.md is updated."
    )
    lines.append("")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Cross-Project Routing (P14)
# -----------------------------------------------------------------------------


def _process_cross_project_routing(
    config: DaemonConfig,
    state: DaemonState,
    agent_id: str,
) -> dict[str, Any]:
    """
    Process cross-project routing for pending tasks.

    Checks tasks for routing suggestions and either:
    1. Auto-routes if confidence > threshold AND employee has access
    2. Queues for approval if task is complex/high_risk

    Args:
        config: Daemon configuration
        state: Current daemon state (will be updated)
        agent_id: The daemon's agent ID

    Returns:
        Dict with routing results
    """
    _ensure_imports()

    if (
        not config.cross_project_enabled
        or not config.cross_project_auto_routing
        or project_orchestrator is None  # not in the public export
    ):
        return {"processed": 0, "routed": 0, "queued": 0, "skipped": 0}

    results = {
        "processed": 0,
        "routed": 0,
        "queued": 0,
        "skipped": 0,
        "details": [],
    }

    try:
        # Get pending tasks from all projects
        task_list = work_allocator.list_tasks(status="pending", all_projects=True)
        pending_tasks = task_list.get("pending", [])

        for task in pending_tasks:
            task_id = task.get("task_id")
            results["processed"] += 1

            # Get routing suggestions for this task
            suggestions = project_orchestrator.get_routing_suggestions(task)

            if not suggestions:
                results["skipped"] += 1
                continue

            # Get top suggestion
            top_suggestion = suggestions[0]
            confidence = top_suggestion.get("confidence", 0.0)
            target_project = top_suggestion.get("project_id")

            # Skip if confidence below threshold
            if confidence < config.cross_project_confidence_threshold:
                results["skipped"] += 1
                logger.debug(
                    f"Task {task_id}: routing confidence {confidence:.2f} "
                    f"below threshold {config.cross_project_confidence_threshold}"
                )
                continue

            # Determine if this task requires approval
            complexity = task.get("estimated_complexity", "standard")
            requires_approval = complexity in config.cross_project_require_approval_for

            # Also check for high_risk flag
            if task.get("high_risk", False):
                requires_approval = True

            if requires_approval:
                # Queue for human approval instead of auto-routing
                # Add routing suggestion to task notes for human review
                note = (
                    f"Cross-project routing suggestion: route to {target_project} "
                    f"(confidence: {confidence:.2f}). Requires approval due to {complexity}."
                )
                work_allocator.update_task(task_id=task_id, notes=note)
                results["queued"] += 1
                state.cross_project_tasks_queued += 1
                logger.info(
                    f"Task {task_id}: queued for approval (route to {target_project})"
                )
                results["details"].append(
                    {
                        "task_id": task_id,
                        "action": "queued",
                        "target_project": target_project,
                        "confidence": confidence,
                        "reason": f"requires approval ({complexity})",
                    }
                )
            else:
                # Validate access before routing
                if not project_orchestrator.validate_cross_project_access(
                    agent_id, target_project
                ):
                    results["skipped"] += 1
                    logger.debug(
                        f"Task {task_id}: skipped, daemon lacks access to {target_project}"
                    )
                    continue

                # Auto-route the task
                route_result = work_allocator.route_task(
                    task_id=task_id,
                    target_project_id=target_project,
                    employee_id=agent_id,
                    reason=f"Auto-routed by daemon (confidence: {confidence:.2f})",
                )

                if route_result.get("success"):
                    results["routed"] += 1
                    state.cross_project_tasks_routed += 1
                    logger.info(f"Task {task_id}: auto-routed to {target_project}")
                    results["details"].append(
                        {
                            "task_id": task_id,
                            "action": "routed",
                            "target_project": target_project,
                            "confidence": confidence,
                        }
                    )
                else:
                    results["skipped"] += 1
                    logger.warning(
                        f"Task {task_id}: routing failed - {route_result.get('error')}"
                    )

    except Exception as e:
        logger.error(f"Cross-project routing failed: {e}")

    return results


def _check_employee_rebalancing(
    config: DaemonConfig,
    state: DaemonState,
) -> dict[str, Any]:
    """
    Check for employee load imbalance and generate rebalancing proposals.

    Does NOT auto-execute rebalancing — only generates proposals for human review.

    Args:
        config: Daemon configuration
        state: Current daemon state (will be updated)

    Returns:
        Dict with rebalancing check results
    """
    _ensure_imports()

    if not config.cross_project_enabled or employee_pool is None:
        return {"checked": False, "proposals": 0}

    results = {
        "checked": True,
        "proposals": 0,
        "overloaded": 0,
        "underutilized": 0,
        "suggestions": [],
    }

    try:
        # Get rebalancing suggestions from employee_pool
        balance_result = employee_pool.balance_employee_load()

        if not balance_result.get("success"):
            logger.warning("Employee rebalancing check failed")
            return results

        suggestions = balance_result.get("suggestions", [])
        summary = balance_result.get("summary", {})

        results["overloaded"] = summary.get("overloaded_count", 0)
        results["underutilized"] = summary.get("underutilized_count", 0)
        results["proposals"] = len(suggestions)
        results["suggestions"] = suggestions

        # Update state
        state.last_rebalance_check = datetime.now(timezone.utc).isoformat()
        state.rebalance_proposals_created += len(suggestions)

        if suggestions:
            logger.info(
                f"Rebalance check: {len(suggestions)} proposals "
                f"(overloaded={results['overloaded']}, underutilized={results['underutilized']})"
            )

            # Log proposals for visibility (but don't execute)
            for suggestion in suggestions:
                action = suggestion.get("action")
                if action == "transfer":
                    logger.info(
                        f"Rebalance proposal: transfer project {suggestion.get('project_id')} "
                        f"from {suggestion.get('from_employee_id')} to {suggestion.get('to_employee_id')}"
                    )
                elif action == "unassign":
                    logger.info(
                        f"Rebalance proposal: unassign {suggestion.get('employee_id')} "
                        f"from {suggestion.get('project_id')}"
                    )
        else:
            logger.debug("Rebalance check: no imbalance detected")

    except Exception as e:
        logger.error(f"Employee rebalancing check failed: {e}")

    return results


def _get_cross_project_metrics() -> dict[str, Any]:
    """
    Get current cross-project metrics for status reporting.

    Returns:
        Dict with cross-project statistics
    """
    _ensure_imports()

    metrics = {
        "cross_project_task_count": 0,
        "routing_activity": [],
        "employee_distribution": {},
    }

    try:
        # Get cross-project tasks
        cross_tasks = work_allocator.get_cross_project_tasks()
        metrics["cross_project_task_count"] = len(cross_tasks)

        # Extract routing activity from tasks
        for task in cross_tasks[-10:]:  # Last 10 for recent activity
            history = task.get("routing_history", [])
            if history:
                latest = history[-1]
                metrics["routing_activity"].append(
                    {
                        "task_id": task.get("task_id"),
                        "from_project": latest.get("from_project"),
                        "to_project": latest.get("to_project"),
                        "routed_at": latest.get("routed_at"),
                    }
                )

        # Get employee distribution (employee_pool is not in the public export)
        load_info = (
            employee_pool.get_employee_project_load()
            if employee_pool is not None
            else {}
        )
        if load_info.get("success"):
            employee_load = load_info.get("employee_load", {})
            for emp_id, info in employee_load.items():
                metrics["employee_distribution"][emp_id] = {
                    "project_count": info.get("project_count", 0),
                    "projects": info.get("projects", []),
                    "at_capacity": info.get("at_capacity", False),
                }

    except Exception as e:
        logger.debug(f"Failed to get cross-project metrics: {e}")

    return metrics


# -----------------------------------------------------------------------------
# Preflight Self-Test (Task 83.8)
# -----------------------------------------------------------------------------


def run_preflight_checks(config: DaemonConfig) -> list[dict]:
    """Run preflight self-tests before entering the main daemon loop.

    Validates that the environment is healthy enough for the daemon to
    produce real work. Returns a list of check results. If any critical
    check fails, the daemon should not start.

    Returns:
        List of dicts: {"name": str, "ok": bool, "critical": bool, "detail": str}
    """
    results: list[dict] = []

    def _check(name: str, critical: bool, fn):
        try:
            ok, detail = fn()
            results.append(
                {"name": name, "ok": ok, "critical": critical, "detail": detail}
            )
        except Exception as e:
            results.append(
                {"name": name, "ok": False, "critical": critical, "detail": str(e)}
            )

    # 1. Git repo is valid
    def check_git_repo():
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return True, f"git dir: {r.stdout.strip()}"
        return False, f"not a git repo: {r.stderr.strip()}"

    # 2. The configured execution provider's CLI(s) are available.
    # Presence-only via 'which' — direct CLI invocation is forbidden in
    # forge_daemon.py (test_forge_daemon_no_direct_cli_invocation), and auth
    # health lives in the per-task worker preflight (agent_providers).
    def check_provider_cli():
        try:
            from agent_providers import resolve_provider

            spec = resolve_provider(config.raw_config or {}, "standard")
        except ValueError as exc:
            return False, f"execution provider misconfigured: {exc}"
        except ImportError:
            # Legacy installs without the provider module: historical check.
            spec = None

        # Built via a variable so the no-direct-cli-invocation source scan
        # doesn't mistake these exe lists for a claude argv.
        claude_exe = "claude"
        if spec is None:
            required = [claude_exe]
        elif spec.provider_type == "claude" and spec.auth == "ollama":
            required = ["ollama", claude_exe]
        elif spec.provider_type == "codex":
            required = ["codex"]
        else:
            required = [claude_exe]

        found: list[str] = []
        for exe in required:
            r = subprocess.run(
                ["which", exe],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                return False, f"{exe} CLI not found in PATH"
            found.append(f"{exe} at {r.stdout.strip()}")
        return True, "; ".join(found)

    # 3. Git remote is reachable (lightweight — just ls-remote HEAD)
    def check_git_remote():
        r = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return True, "origin reachable"
        return False, f"cannot reach origin: {r.stderr.strip()[:100]}"

    # 4. Worktree base dir is writable
    def check_worktree_dir():
        wt_base = _worktree_base()
        wt_base.mkdir(parents=True, exist_ok=True)
        test_file = wt_base / ".preflight-test"
        try:
            test_file.write_text("ok")
            test_file.unlink()
            return True, f"{wt_base} writable"
        except OSError as e:
            return False, f"{wt_base} not writable: {e}"

    # 5. Required state directories exist
    def check_state_dirs():
        required = [
            Path(".company"),
            Path(".company/state"),
            Path(".company/logs"),
        ]
        missing = [str(d) for d in required if not d.exists()]
        if missing:
            return False, f"missing dirs: {', '.join(missing)}"
        return True, "all state dirs present"

    # 6. org.json is valid JSON
    def check_org_json():
        org_path = Path(".company/org.json")
        if not org_path.exists():
            return False, "org.json missing"
        try:
            import json as _json

            data = _json.loads(org_path.read_text())
            emp_count = len(data.get("employees", []))
            return True, f"org.json valid ({emp_count} employees)"
        except (ValueError, OSError) as e:
            return False, f"org.json invalid: {e}"

    # 7. No stale .git/index.lock — a stale one is removed, not just reported:
    # a WARN here used to be the last anyone heard of it while every pull
    # failed for days. Policy: company_resolver.stale_git_lock_age.
    def check_git_lock():
        lock = Path(".git/index.lock")
        if lock.exists():
            try:
                st = lock.stat()
            except OSError:
                return False, ".git/index.lock exists, cannot stat"
            age = time.time() - st.st_mtime
            try:
                stale_age = _git_lock_policy().stale_git_lock_age(lock)
            except Exception:
                stale_age = None
            if stale_age is not None:
                removed = _clear_stale_index_lock()
                if removed is not None:
                    return (
                        True,
                        f"removed stale .git/index.lock ({int(removed)}s old, "
                        f"{st.st_size} bytes)",
                    )
                return (
                    False,
                    f"stale .git/index.lock ({int(age)}s old) could not be removed",
                )
            if age > 300:
                return (
                    False,
                    f".git/index.lock is {int(age)}s old and {st.st_size} bytes — "
                    "left in place: a non-empty lock is only removed after 1h "
                    "(it may be a live interactive commit); remove it by hand if "
                    "no git process is running",
                )
            return (
                True,
                f".git/index.lock exists but recent ({int(age)}s) — may be active",
            )
        return True, "no lock file"

    # 8. .company/state is not inside a cloud-sync folder (iCloud/OneDrive/Dropbox).
    # Pure path-component check — no subprocess, no platform probes, never critical.
    def check_sync_state():
        state_path = Path(".company/state")
        parts = set(state_path.resolve().parts)
        _ICLOUD = {"com~apple~CloudDocs", "Mobile Documents"}
        if _ICLOUD & parts:
            marker = next(iter(_ICLOUD & parts))
            return (
                False,
                f".company/state is inside an iCloud-synced folder ('{marker}' in path) — state files may be deleted by sync",
            )
        if "OneDrive" in parts:
            return (
                False,
                ".company/state is inside an OneDrive-synced folder — state files may conflict",
            )
        if "Dropbox" in parts:
            return (
                False,
                ".company/state is inside a Dropbox-synced folder — state files may conflict",
            )
        return True, ".company/state not inside a cloud-sync folder"

    _check("git-repo", critical=True, fn=check_git_repo)
    _check("provider-cli", critical=True, fn=check_provider_cli)
    _check("git-remote", critical=False, fn=check_git_remote)
    _check("worktree-dir", critical=False, fn=check_worktree_dir)
    _check("state-dirs", critical=True, fn=check_state_dirs)
    _check("org-json", critical=True, fn=check_org_json)
    _check("git-lock", critical=False, fn=check_git_lock)
    _check("sync-state", critical=False, fn=check_sync_state)

    return results


def log_preflight_results(results: list[dict]) -> bool:
    """Log preflight results and return True if all critical checks passed."""
    all_critical_ok = True
    for r in results:
        status = "PASS" if r["ok"] else ("FAIL" if r["critical"] else "WARN")
        if r["critical"] and not r["ok"]:
            all_critical_ok = False
        logger.info(f"Preflight [{status}] {r['name']}: {r['detail']}")
    return all_critical_ok


# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------


def run_daemon_loop(config: DaemonConfig) -> int:
    """Main daemon execution loop.

    Polls for tasks and executes them, respecting circuit breaker state
    and updating heartbeat regularly.

    Args:
        config: Daemon configuration.

    Returns:
        Exit code (0 for normal shutdown, 1 for error).
    """
    global _shutdown_requested, _reload_requested, _status_requested

    _ensure_imports()

    # Task 83.8: Preflight self-test — catch broken environment before wasting cycles
    preflight_results = run_preflight_checks(config)
    if not log_preflight_results(preflight_results):
        failed = [r for r in preflight_results if r["critical"] and not r["ok"]]
        logger.error(
            f"Preflight failed: {', '.join(r['name'] for r in failed)}. "
            "Fix these issues before starting the daemon."
        )
        return 1

    # Initialize state
    state = DaemonState(
        pid=os.getpid(),
        started_at=datetime.now(timezone.utc).isoformat(),
        config_hash=config.config_hash(),
    )

    # P1-R3: Stable daemon identity including start timestamp.
    # The epoch suffix means PID reuse across restarts produces a distinct
    # identity, so the orphan sweep never mistakes old claims for our own.
    _daemon_start_epoch = int(
        datetime.fromisoformat(state.started_at.replace("Z", "+00:00")).timestamp()
    )
    _daemon_agent_id = f"daemon-{state.pid}-{_daemon_start_epoch}"
    logger.info(f"Daemon identity: {_daemon_agent_id}")
    # Workers may launch sessions again (a previous loop's shutdown in this
    # process closed the gate).
    _set_worker_spawn_gate(closed=False)

    # P38: Initialize subsystem registry
    _subsystem_registry = None
    try:
        try:
            from .subsystem_registry import (
                AutoMergeSubsystem,
                CircuitBreakerSubsystem,
                CrossProjectSubsystem,
                DocumentApprovalsSubsystem,
                EmployeeIdeationSubsystem,
                ExecutiveLoopSubsystem,
                P25AutonomySubsystem,
                ProactiveSubsystem,
                RoadmapSchedulingSubsystem,
                SchedulingEfficiencySubsystem,
                SelfImprovementSubsystem,
                StrategicPlanningSubsystem,
                SubsystemRegistry,
                TaskExecutionSubsystem,
            )
        except ImportError:
            from subsystem_registry import (
                AutoMergeSubsystem,
                CircuitBreakerSubsystem,
                CrossProjectSubsystem,
                DocumentApprovalsSubsystem,
                EmployeeIdeationSubsystem,
                ExecutiveLoopSubsystem,
                P25AutonomySubsystem,
                ProactiveSubsystem,
                RoadmapSchedulingSubsystem,
                SchedulingEfficiencySubsystem,
                SelfImprovementSubsystem,
                StrategicPlanningSubsystem,
                SubsystemRegistry,
                TaskExecutionSubsystem,
            )

        _subsystem_registry = SubsystemRegistry()
        for subsystem_cls in [
            TaskExecutionSubsystem,
            ProactiveSubsystem,
            CrossProjectSubsystem,
            StrategicPlanningSubsystem,
            RoadmapSchedulingSubsystem,
            P25AutonomySubsystem,
            ExecutiveLoopSubsystem,
            SelfImprovementSubsystem,
            EmployeeIdeationSubsystem,
            AutoMergeSubsystem,
            SchedulingEfficiencySubsystem,
            CircuitBreakerSubsystem,
            DocumentApprovalsSubsystem,
        ]:
            _subsystem_registry.register(subsystem_cls(state, config))
        logger.info(
            f"P38: Subsystem registry initialized with "
            f"{len(_subsystem_registry.get_all())} subsystems"
        )
    except Exception as e:
        logger.warning(f"P38: Subsystem registry init failed, using legacy mode: {e}")
        _subsystem_registry = None

    # Get paths for operation loop
    try:
        company_dir = company_resolver.get_company_dir()
        queue_path = company_dir / "state/work_queue.json"
        state_path = company_dir / "state/session_state.json"
    except Exception as e:
        logger.error(f"Failed to resolve company directory: {e}")
        return 1

    # P28.3: Initialize unified state management with migration
    unified_state_mgr = None
    try:
        if unified_state_mod:
            unified_state_mgr = unified_state_mod.UnifiedStateManager(
                company_dir=company_dir
            )
            # Run migration automatically on first startup (idempotent)
            unified_state_mgr.migrate()
            logger.info("P28.3: Unified state manager initialized")

            # Initialize daemon session in unified state
            unified_state = unified_state_mgr.load()
            if not unified_state.start_time:
                # New session - set start time
                unified_state_mgr.update(
                    start_time=state.started_at,
                    start_health=100,
                )
                logger.info("P28.3: Session state initialized through unified state")
    except Exception as e:
        logger.warning(f"P28.3: Unified state init failed, using direct access: {e}")
        unified_state_mgr = None  # Fallback to direct file access

    # Load circuit breaker configuration
    breaker_config = loop_monitor.load_config()

    # Track consecutive idle polls for idle timeout
    consecutive_idle_polls = 0
    last_activity_time = time.time()

    # Track loop iterations for periodic rebalance checks (P14)
    iteration_count = 0

    logger.info(
        f"Daemon loop started (pid={state.pid}, poll_interval={config.poll_interval_seconds}s)"
    )
    if config.cross_project_enabled:
        logger.info(
            f"Cross-project routing enabled (auto_routing={config.cross_project_auto_routing}, "
            f"threshold={config.cross_project_confidence_threshold})"
        )

    def _get_queue_summary() -> str:
        """Get a compact work queue summary for cycle headers."""
        try:
            with open(queue_path, encoding="utf-8") as f:
                wq = json.load(f)
            pending = len(wq.get("pending", []))
            in_prog = len(wq.get("in_progress", []))
            blocked = len(wq.get("blocked", []))
            completed = len(wq.get("completed", []))
            return f"queue: {pending} pending, {in_prog} active, {blocked} blocked, {completed} done"
        except Exception:
            return "queue: unknown"

    cycle_number = 0
    # 2026-07-06: last spawn time per task_id — feeds the respawn cooldown
    # that prevents duplicate concurrent workers for one task.
    _recent_spawn_at: dict[str, float] = {}

    # P50.2: Validate queue health before main loop
    _run_startup_canary()

    # G11 Fix: Ensure heartbeat file exists immediately on startup
    # Prevents G11 false failures during the window before first heartbeat write
    try:
        ensure_heartbeat_initialized(config)
        logger.info("Heartbeat initialized (startup)")
    except Exception as e:
        logger.warning(f"Heartbeat initialization failed (non-fatal): {e}")

    # WS-017 / P1-R3: Clean up orphan tasks from previous daemon instances.
    # Passes the composite identity so the sweep uses pid+timestamp, not just pid.
    orphan_result = _cleanup_orphan_tasks(_daemon_agent_id)
    if orphan_result.get("released", 0) > 0:
        logger.info(
            f"Orphan cleanup: released {orphan_result['released']} tasks "
            f"from previous daemon instances"
        )
        # 2026-07-06: the previous daemon's claude workers may still be
        # running (a restart kills threads, not child processes). Kill them
        # before this daemon re-executes the same tasks, or two generations
        # of workers race on the same task and destroy each other's work.
        _kill_surviving_worker_pids(orphan_result.get("released_task_ids", []))

    # P1-R3: Pre-populate _active_workers with adopted tasks so the daemon
    # doesn't spawn duplicate workers for still-running subprocesses.
    # Per-cycle adopted-entry handling keeps the slot occupied until the
    # subprocess exits, then drops it so _cleanup_stranded_tasks can release.
    _adopted = orphan_result.get("adopted_tasks", [])
    if _adopted:
        state._active_workers = {}
        for _entry in _adopted:
            _tid = _entry["task_id"]
            _wpid = _entry["worker_pid"]
            state._active_workers[_tid] = {
                "thread": None,
                "status": "adopted",
                "pid": _wpid,
                "started_at": time.time(),
                "result": {},
            }
        logger.info(
            f"Startup: adopted {len(_adopted)} tasks with live workers: "
            f"{[e['task_id'] for e in _adopted]}"
        )

    # WS-057: Clean up stale worktrees from previous daemon runs.
    # 2026-07-06: age-guarded — recent worktrees may belong to still-running
    # workers from the previous daemon generation.
    try:
        _stale_count, _spared_count = _cleanup_stale_worktrees_at_startup(
            _worktree_base()
        )
        if _stale_count > 0 or _spared_count > 0:
            logger.info(
                f"WS-057: Cleaned up {_stale_count} stale worktrees "
                f"(spared {_spared_count} recent, possibly live) "
                f"from {_worktree_base()}/"
            )
    except Exception as _e:
        logger.debug(f"WS-057: Stale worktree cleanup failed (non-fatal): {_e}")

    # Task 83.6: Clean up stale feat/task-* and daemon/* local branches on startup
    try:
        _branch_list = subprocess.run(
            ["git", "branch", "--list", "feat/task-*", "daemon/*"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if _branch_list.returncode == 0 and _branch_list.stdout.strip():
            # Get worktree branches to avoid deleting active ones
            _wt_branches_result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            _wt_branches = set()
            if _wt_branches_result.returncode == 0:
                for _line in _wt_branches_result.stdout.splitlines():
                    if _line.startswith("branch refs/heads/"):
                        _wt_branches.add(_line.split("refs/heads/", 1)[1])

            _cleaned = 0
            # Measure "merged" against the ref worktrees branch from — local
            # main can be days behind when its pull is blocked.
            _merged_ref = _preferred_base_ref()
            for _branch_line in _branch_list.stdout.strip().splitlines():
                _branch_name = _branch_line.strip().lstrip("* ")
                if not _branch_name or _branch_name in ("main", "master"):
                    continue
                if _branch_name in _wt_branches:
                    continue
                # Check if branch is merged into main
                _merged = subprocess.run(
                    ["git", "branch", "--merged", _merged_ref, "--list", _branch_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if _merged.returncode == 0 and _branch_name in _merged.stdout:
                    # Safe to delete — already merged
                    subprocess.run(
                        ["git", "branch", "-d", _branch_name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    _cleaned += 1
                else:
                    # Not merged — check age via last commit date
                    _log = subprocess.run(
                        ["git", "log", "-1", "--format=%ct", _branch_name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if _log.returncode == 0 and _log.stdout.strip():
                        _age_days = (time.time() - float(_log.stdout.strip())) / 86400
                        if _age_days > 7 and _branch_has_unpushed_commits(_branch_name):
                            logger.warning(
                                f"Kept stale branch '{_branch_name}' "
                                f"({int(_age_days)}d old, unmerged): it has "
                                "commits that are on no remote"
                            )
                        elif _age_days > 7:
                            # Older than 7 days, unmerged, every commit pushed
                            subprocess.run(
                                ["git", "branch", "-D", _branch_name],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            _cleaned += 1
                            logger.info(
                                f"Force-cleaned stale branch '{_branch_name}' "
                                f"({int(_age_days)}d old, unmerged)"
                            )
            if _cleaned > 0:
                logger.info(f"Branch cleanup: removed {_cleaned} stale local branches")
    except Exception as _e:
        logger.debug(f"Branch cleanup failed (non-fatal): {_e}")

    # P25: Initialize full autonomy components
    p25_scheduler = None
    p25_continuity = None
    p25_governor = None
    last_snapshot_time = time.time()
    last_goal_refresh_time = time.time()
    last_queue_reorder_time = time.time()

    if config.p25_enabled:
        try:
            # Initialize adaptive scheduler
            # The P25 modules are not in the public export; each one is
            # skipped when absent so the others still initialise.
            if (
                config.p25_adaptive_scheduling_enabled
                and adaptive_scheduler_mod is not None
            ):
                p25_scheduler = adaptive_scheduler_mod.AdaptiveScheduler()
                logger.info("P25: Adaptive scheduler initialized")

            # Initialize session continuity and restore from snapshot
            if (
                config.p25_session_continuity_enabled
                and session_continuity_mod is not None
            ):
                p25_continuity = session_continuity_mod.SessionContinuity()
                snapshot = p25_continuity.restore_snapshot()
                if snapshot:
                    logger.info(
                        f"P25: Restored session from snapshot "
                        f"(captured at {snapshot.captured_at})"
                    )
                    # Handle any in-progress work from previous session
                    recovery_result = p25_continuity.handle_partial_work(snapshot)
                    if recovery_result.get("recovered", 0) > 0:
                        logger.info(
                            f"P25: Recovered {recovery_result['recovered']} tasks "
                            f"from previous session"
                        )

            # Initialize budget governor
            if config.p25_budget_governor_enabled and budget_governor_mod is not None:
                p25_governor = budget_governor_mod.BudgetGovernor()
                throttle_level = p25_governor.compute_throttle_level()
                state.p25_throttle_level = throttle_level.value
                logger.info(
                    f"P25: Budget governor initialized (level={throttle_level.value})"
                )

            # Initial goal refresh - DEFERRED to first cycle to avoid blocking startup
            # Goal assessment runs pytest which can take 10+ minutes
            if config.p25_goal_scheduling_enabled:
                logger.info(
                    "P25: Goal scheduling enabled (will compute on first cycle)"
                )
                # Mark that we need to compute on first cycle
                state.p25_needs_initial_goal_refresh = True

            logger.info("P25: Full Autonomous Operation features enabled")
        except Exception as e:
            logger.warning(f"P25: Initialization failed (non-fatal): {e}")

    # P28: Initialize central orchestrator
    central_orchestrator = None
    if config.use_central_orchestrator:
        if orchestrator_mod is not None:
            try:
                central_orchestrator = orchestrator_mod.Orchestrator()
                logger.info("P28: Central orchestrator initialized")
            except Exception as e:
                logger.warning(
                    f"P28: Orchestrator initialization failed (non-fatal): {e}"
                )
                central_orchestrator = None
        else:
            logger.debug(
                "P28: Orchestrator module not available, using fallback routing"
            )

    # Start background heartbeat thread — writes daemon.heartbeat every 60s
    # so the file stays fresh even during long-running task executions.
    import threading as _threading

    # WS-057-002: Serialize worktree creation to prevent git lock contention
    # when multiple workers spawn simultaneously. Without this, concurrent
    # `git worktree add` calls fail ~10% of the time due to .git/index.lock.
    _worktree_creation_lock = _threading.Lock()
    _active_workers_lock = (
        _threading.Lock()
    )  # WS-057-003: Protect state._active_workers dict

    _HEARTBEAT_INTERVAL = (
        30  # seconds (G11: reduced from 60s for faster staleness detection)
    )

    def _heartbeat_worker():
        """Background thread: update heartbeat file every 60 seconds."""
        while not _shutdown_requested:
            try:
                update_heartbeat(config, state, registry=_subsystem_registry)
            except Exception as _hb_exc:
                logger.debug(f"Heartbeat write failed (non-fatal): {_hb_exc}")
            # Sleep in small increments so shutdown is responsive
            for _ in range(_HEARTBEAT_INTERVAL):
                if _shutdown_requested:
                    break
                time.sleep(1)

    _heartbeat_thread = _threading.Thread(
        target=_heartbeat_worker,
        name="daemon-heartbeat",
        daemon=True,
    )
    _heartbeat_thread.start()
    logger.info(f"Heartbeat thread started (interval={_HEARTBEAT_INTERVAL}s)")

    # WS-119: Heartbeat watchdog — if heartbeat goes stale (>5min) the daemon
    # has hung in some blocking call. Self-SIGKILL so launchd's KeepAlive
    # restarts a fresh process. This addresses the "0% CPU stuck" state where
    # the process is alive but not making progress.
    _WATCHDOG_STALE_SECONDS = 300  # 5 minutes
    _WATCHDOG_CHECK_INTERVAL = 60  # check every minute

    def _watchdog_worker():
        """Background thread: SIGKILL self if heartbeat goes stale >5min."""
        # Wait an initial grace period so startup writes the first heartbeat
        for _ in range(_WATCHDOG_STALE_SECONDS):
            if _shutdown_requested:
                return
            time.sleep(1)
        while not _shutdown_requested:
            try:
                if config.heartbeat_file.exists():
                    age = time.time() - config.heartbeat_file.stat().st_mtime
                    if age > _WATCHDOG_STALE_SECONDS:
                        logger.error(
                            f"WS-119 watchdog: heartbeat stale ({age:.0f}s > "
                            f"{_WATCHDOG_STALE_SECONDS}s), self-killing for restart"
                        )
                        # SIGKILL self so launchd KeepAlive restarts a fresh process
                        os.kill(os.getpid(), signal.SIGKILL)
            except Exception as _wd_exc:
                logger.debug(f"Watchdog check failed: {_wd_exc}")
            for _ in range(_WATCHDOG_CHECK_INTERVAL):
                if _shutdown_requested:
                    return
                time.sleep(1)

    _watchdog_thread = _threading.Thread(
        target=_watchdog_worker,
        name="daemon-watchdog",
        daemon=True,
    )
    _watchdog_thread.start()
    logger.info(
        f"WS-119 watchdog started (stale_threshold={_WATCHDOG_STALE_SECONDS}s, "
        f"check_interval={_WATCHDOG_CHECK_INTERVAL}s)"
    )

    # WS-057/WI-004: Discovery loop — runs discovery/planning operations on a 2.5-min cadence
    # Thread-safety invariant: discovery thread only writes discovery-specific state fields
    # (e.g. strategic_initiatives_created, improvement_cycles_run). Main loop only writes
    # execution-specific fields (e.g. tasks_completed, tasks_failed). No field is written
    # by both threads. Heartbeat thread reads all fields (GIL-safe for simple attrs).
    _DISCOVERY_INTERVAL = config.discovery_loop_interval_seconds

    def _discovery_loop_worker():
        """Background thread: run discovery & planning operations every 2.5 minutes.

        Currently a no-op -- see DISCOVERY_DISABLED_REASON. Everything below the
        ``return`` is retained, not live: it is what has to work again once the
        AGENT-mode freeze is fixed.
        """
        if not DISCOVERY_ENABLED:
            logger.warning(
                "[DISCOVERY] Thread DISABLED (%s). Dormant: %s",
                DISCOVERY_DISABLED_REASON,
                ", ".join(DISCOVERY_DORMANT_SUBSYSTEMS),
            )
            return
        logger.info(f"[DISCOVERY] Thread started (interval={_DISCOVERY_INTERVAL}s)")
        while not _shutdown_requested:
            _disc_cycle_start = time.time()
            _disc_ops_run = 0
            _disc_ops_failed = 0

            # --- Proactive scan ---
            try:
                if _should_run_proactive_scan(config, state):
                    logger.info("[DISCOVERY] Proactive initiative scan...")
                    proposals_created = _run_proactive_scan(state)
                    logger.info(
                        f"[DISCOVERY] Proactive scan: {proposals_created} proposals created"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Proactive scan failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Strategic planning ---
            try:
                if _should_run_strategic_planning(config, state):
                    logger.info("[DISCOVERY] Strategic planning cycle...")
                    planning_result = _run_strategic_planning(state)
                    logger.info(
                        f"[DISCOVERY] Strategic planning: "
                        f"{planning_result.get('initiatives_proposed', 0)} initiatives, "
                        f"{planning_result.get('tasks_queued', 0)} tasks queued"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Strategic planning failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Weekly planning cycle (P29) ---
            try:
                if _should_run_weekly_planning_cycle(config, state):
                    logger.info("[DISCOVERY] Weekly planning cycle...")
                    weekly_result = _run_weekly_planning_cycle(state)
                    logger.info(
                        f"[DISCOVERY] Weekly planning: "
                        f"{weekly_result.get('goals_reviewed', 0)} goals reviewed, "
                        f"{weekly_result.get('initiatives_planned', 0)} initiatives planned"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Weekly planning failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Daily planning cycle (P29) ---
            try:
                if _should_run_daily_planning_cycle(config, state):
                    logger.info("[DISCOVERY] Daily planning cycle...")
                    daily_result = _run_daily_planning_cycle(config, state)
                    logger.info(
                        f"[DISCOVERY] Daily planning: "
                        f"{daily_result.get('tasks_prioritized', 0)} tasks prioritized, "
                        f"{daily_result.get('blockers_found', 0)} blockers found"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Daily planning failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Executive loop ---
            try:
                if _should_run_executive_loop(config, state):
                    logger.info("[DISCOVERY] Executive loop...")
                    exec_result = _run_executive_loop(state)
                    logger.info(
                        f"[DISCOVERY] Executives: {exec_result.get('executives_invoked', 0)} invoked, "
                        f"{exec_result.get('decisions_made', 0)} decisions, "
                        f"{exec_result.get('work_submitted', 0)} work items"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Executive loop failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Self-improvement cycle (P30) ---
            try:
                if config.improvement_enabled and _should_run_improvement_cycle(
                    config, state
                ):
                    logger.info("[DISCOVERY] Self-improvement cycle...")
                    improve_result = _run_improvement_cycle(state)
                    if not improve_result.get("skipped", False):
                        logger.info(
                            f"[DISCOVERY] Improvement: "
                            f"{improve_result.get('detections_count', 0)} detections, "
                            f"{improve_result.get('proposals_submitted', 0)} proposals submitted"
                        )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Self-improvement failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Feedback monitor check (P41) ---
            try:
                if _should_run_feedback_check(config, state):
                    logger.info("[DISCOVERY] Feedback monitor check...")
                    fb_result = _run_feedback_check(state, company_dir=company_dir)
                    if fb_result.get("actions_taken", 0) > 0:
                        logger.info(
                            f"[DISCOVERY] Feedback monitor: "
                            f"{fb_result['actions_taken']} corrective actions taken"
                        )
                        if fb_result.get("details"):
                            for detail in fb_result["details"]:
                                logger.info(f"  - {detail}")
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Feedback check failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Escalation cleanup ---
            # Archive resolved escalations to prevent accumulation in the active
            # directory. Without this, resolved files pile up and can inflate the
            # active_escalations health metric during brief status-transition windows.
            try:
                from escalation import archive_stale_escalations

                archive_result = archive_stale_escalations(stale_hours=1)
                archived_count = archive_result.get("archived_count", 0)
                if archived_count > 0:
                    logger.info(
                        f"[DISCOVERY] Escalation cleanup: archived {archived_count} "
                        f"resolved escalations"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Escalation cleanup failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Artifact generation (P47) ---
            try:
                if _should_run_artifact_generation(config, state):
                    logger.info("[DISCOVERY] Artifact generation...")
                    artifact_result = _run_artifact_generation(state, company_dir)
                    report = artifact_result.get("report_path", "none")
                    drafts = artifact_result.get("drafts_count", 0)
                    logger.info(
                        f"[DISCOVERY] Artifacts: report={report}, "
                        f"{drafts} content drafts"
                    )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Artifact generation failed: {_disc_exc}")
                _disc_ops_failed += 1

            # --- Vision auto-refresh (WS-059) ---
            try:
                if _should_run_vision_refresh(config, state):
                    logger.info("[DISCOVERY] Vision auto-refresh...")
                    vision_result = _run_vision_refresh(state)
                    logger.info(
                        f"[DISCOVERY] Vision refresh: "
                        f"{vision_result.get('goals_assessed', 0)} goals assessed, "
                        f"{vision_result.get('achieved_count', 0)} achieved, "
                        f"proposal={'yes' if vision_result.get('proposal_created') else 'no'}"
                    )
                    if vision_result.get("errors"):
                        logger.warning(
                            f"Vision refresh errors: {vision_result['errors']}"
                        )
                    _disc_ops_run += 1
            except Exception as _disc_exc:
                logger.error(f"[DISCOVERY] Vision refresh failed: {_disc_exc}")
                _disc_ops_failed += 1

            _disc_elapsed = time.time() - _disc_cycle_start
            if _disc_ops_run > 0 or _disc_ops_failed > 0:
                logger.info(
                    f"[DISCOVERY] Cycle complete: {_disc_ops_run} ops run, "
                    f"{_disc_ops_failed} failed, {_disc_elapsed:.1f}s elapsed"
                )

            # Sleep in 1-second increments for responsive shutdown
            for _ in range(_DISCOVERY_INTERVAL):
                if _shutdown_requested:
                    break
                time.sleep(1)

        logger.info("[DISCOVERY] Thread shutting down")

    _discovery_thread = _threading.Thread(
        target=_discovery_loop_worker,
        name="daemon-discovery",
        daemon=True,
    )
    _discovery_thread.start()
    # Report what the thread will actually DO, not that it was spawned. This
    # line used to read "Discovery thread started (interval=1800s)"
    # unconditionally, eight seconds after the worker logged that it was
    # disabled and returned -- so every startup published a healthy-looking
    # signal for ten dormant subsystems, and the disable read as noise.
    if DISCOVERY_ENABLED:
        logger.info(
            f"[DISCOVERY] Discovery thread started (interval={_DISCOVERY_INTERVAL}s)"
        )
    else:
        logger.warning(
            "[DISCOVERY] Discovery thread NOT running (%s) — %d subsystems dormant",
            DISCOVERY_DISABLED_REASON,
            len(DISCOVERY_DORMANT_SUBSYSTEMS),
        )

    # Task 83.5: Track loop timing to detect sleep/wake gaps
    _last_loop_time = time.time()
    _SLEEP_WAKE_THRESHOLD = 300  # 5 minutes gap = likely machine slept

    while not _shutdown_requested:
        try:
            # Task 83.5: Detect sleep/wake — if loop gap exceeds threshold,
            # the machine likely slept. Log it and verify git remote is reachable.
            _now = time.time()
            _loop_gap = _now - _last_loop_time
            _last_loop_time = _now
            if _loop_gap > _SLEEP_WAKE_THRESHOLD:
                logger.warning(
                    f"Sleep/wake detected: {int(_loop_gap)}s gap between loop iterations. "
                    "Verifying environment health..."
                )
                try:
                    _wake_check = subprocess.run(
                        ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if _wake_check.returncode != 0:
                        logger.warning(
                            "Post-wake: git remote unreachable, waiting 30s for network"
                        )
                        _sleep_unless_shutdown(30)
                except (subprocess.TimeoutExpired, OSError):
                    logger.warning("Post-wake: git check timed out, continuing")

            # Handle reload request
            if _reload_requested:
                _reload_requested = False
                logger.info("Reloading configuration")
                breaker_config = loop_monitor.load_config()
                # Hot-reload daemon config from gatesmith.json.
                # Preserve pid_file and log_file — changing those mid-run is unsafe.
                try:
                    _reload_path = DaemonConfig.default_config_path()
                    new_config = DaemonConfig.from_file(_reload_path)
                    new_config.pid_file = config.pid_file
                    new_config.log_file = config.log_file
                    old_interval = config.poll_interval_seconds
                    config = new_config
                    if config.poll_interval_seconds != old_interval:
                        logger.info(
                            f"Poll interval updated: {old_interval}s -> {config.poll_interval_seconds}s"
                        )
                    logger.info(f"Daemon config reloaded from {_reload_path}")
                except Exception as e:
                    logger.warning(f"Failed to reload daemon config: {e}")

            # Handle status request
            if _status_requested:
                _status_requested = False
                logger.info(
                    f"Status: tasks_completed={state.tasks_completed}, "
                    f"tasks_failed={state.tasks_failed}, "
                    f"current_task={state.current_task}"
                )

            # Update heartbeat (P38: pass registry for aggregated fields)
            update_heartbeat(config, state, registry=_subsystem_registry)

            # Merge policy disarm sweep — only ever DISARMS, so it runs before
            # every early exit below (circuit breaker, rate limit, session
            # limit, budget pause) and even with autoMerge.enabled false: a PR
            # armed before merging was turned off must not merge on green.
            try:
                _run_policy_disarm_sweep(config)
            except Exception as _sweep_err:
                logger.warning(f"Policy disarm sweep failed: {_sweep_err}")

            # Check circuit breaker
            breaker, health_metrics = loop_monitor.load_monitor_state()

            if not loop_monitor.check_circuit_breaker(breaker, breaker_config):
                logger.warning(
                    f"Circuit breaker OPEN (failures={breaker.failure_count}), "
                    f"waiting for recovery"
                )
                loop_monitor.save_monitor_state(breaker, health_metrics)

                # Sleep before checking again
                _sleep_unless_shutdown(config.poll_interval_seconds)
                continue

            # Save any state changes from circuit breaker check
            loop_monitor.save_monitor_state(breaker, health_metrics)

            # Check rate limit (tasks per hour)
            if health_metrics.tasks_this_hour >= config.max_tasks_per_hour:
                logger.info(
                    f"Rate limit reached ({health_metrics.tasks_this_hour}/{config.max_tasks_per_hour} tasks/hour), "
                    f"waiting"
                )
                _sleep_unless_shutdown(config.poll_interval_seconds)
                continue

            # 2026-07-06 incident: check for Claude session/usage limit before ANY
            # activation.  When the CLI subscription window is exhausted, every new
            # worker session burns quota for nothing.  The guard pauses ALL activation
            # pathways (queue workers, employee initiative, teams, judges) until the
            # stated reset time + buffer.  Heartbeat and watchdog continue normally.
            try:
                try:
                    from . import session_limit_guard as _slg
                except ImportError:
                    import session_limit_guard as _slg  # type: ignore[no-redef]
                _sl_paused, _sl_reason = _slg.is_activation_paused(company_dir)
                if _sl_paused:
                    logger.warning(
                        f"[session_limit_guard] ACTIVATION PAUSED — {_sl_reason}"
                    )
                    update_heartbeat(config, state)
                    _sleep_unless_shutdown(min(config.poll_interval_seconds, 60))
                    continue
            except Exception as _sl_exc:
                logger.debug(f"[session_limit_guard] check failed: {_sl_exc}")

            # Increment iteration counter for periodic checks
            iteration_count += 1
            cycle_number += 1
            cycle_start = time.time()

            # P25: Budget governor check before any work
            if (
                config.p25_enabled
                and config.p25_budget_governor_enabled
                and p25_governor
            ):
                try:
                    throttle_level = p25_governor.compute_throttle_level()
                    state.p25_throttle_level = throttle_level.value
                    if throttle_level.value == "PAUSED":
                        logger.warning("P25: Budget PAUSED — skipping cycle")
                        _sleep_unless_shutdown(config.poll_interval_seconds)
                        continue
                except Exception as e:
                    logger.debug(f"P25: Budget check failed: {e}")

            # P25: Adaptive scheduling — adjust poll interval based on context
            poll_interval = config.poll_interval_seconds
            if (
                config.p25_enabled
                and config.p25_adaptive_scheduling_enabled
                and p25_scheduler
            ):
                try:
                    mode = p25_scheduler.compute_mode()
                    state.p25_schedule_mode = mode.value
                    poll_interval = p25_scheduler.get_poll_interval()
                except Exception as e:
                    logger.debug(f"P25: Adaptive scheduling failed: {e}")

            # WS-119: When pending queue has work, shrink poll interval so
            # Execute picks up the next task quickly instead of waiting for the
            # adaptive scheduler's default (30s) or idle-mode backoff (up to
            # 60s+). This decouples Execute throughput from Discover pacing.
            try:
                with open(queue_path, encoding="utf-8") as _qf:
                    _queue_snapshot = json.load(_qf)
                _pending_count = len(_queue_snapshot.get("pending", []))
                if _pending_count > 0:
                    poll_interval = min(poll_interval, 5)
                elif _has_pending_approved_ideas():
                    # ProjectK K4: approved ideas are waiting but no tasks exist
                    # yet — don't let the idle backoff (up to ~30 min) sit on
                    # them. Poll at the convert floor so they turn into tasks
                    # within ~1 min. A moderate cadence (not the 5s task
                    # fast-path) avoids tight-spinning if an approved idea is a
                    # persistent duplicate.
                    poll_interval = min(
                        poll_interval, _IDEA_AUTOCONVERT_PROMPT_FLOOR_SECONDS
                    )
            except Exception as _qe:
                logger.debug(f"WS-119: queue depth check failed: {_qe}")

            # Approved-idea auto-convert must not wait out idle backoff (up to
            # 30 min) — shrink the poll interval so the maintenance phase
            # re-evaluates within one normal poll instead.
            if _idea_poll_shrink_needed(config):
                poll_interval = min(poll_interval, config.poll_interval_seconds)

            # P25: Periodic goal refresh (every hour by default)
            # Also handles deferred initial goal refresh from startup
            now_ts = time.time()
            needs_refresh = (
                config.p25_enabled
                and config.p25_goal_scheduling_enabled
                and (
                    state.p25_needs_initial_goal_refresh
                    or now_ts - last_goal_refresh_time
                    >= config.p25_goal_refresh_interval_minutes * 60
                )
            )
            if needs_refresh:
                # Run goal computation in background thread to avoid blocking
                # pytest can take 10+ minutes for test coverage assessment
                def _background_goal_refresh():
                    try:
                        goal_scheduler_mod.compute_goal_priorities()
                        if (
                            config.p25_learning_loop_enabled
                            and learning_loop_mod is not None
                        ):
                            learning_loop_mod.compute_insights()
                        if (
                            config.p25_approval_learning_enabled
                            and approval_learner_mod is not None
                        ):
                            approval_learner_mod.refresh_policy()
                        state.last_goal_refresh = datetime.now(timezone.utc).isoformat()
                        logger.info(
                            "P25: Goal priorities and learning insights refreshed"
                        )
                    except Exception as e:
                        logger.debug(f"P25: Goal refresh failed: {e}")

                import threading

                refresh_thread = threading.Thread(
                    target=_background_goal_refresh,
                    name="p25-goal-refresh",
                    daemon=True,
                )
                refresh_thread.start()
                last_goal_refresh_time = now_ts
                state.p25_needs_initial_goal_refresh = False
                logger.info("P25: Goal refresh started in background")

            # P25: Periodic queue reorder by goals (every 15 min by default)
            if (
                config.p25_enabled
                and config.p25_goal_scheduling_enabled
                and now_ts - last_queue_reorder_time
                >= config.p25_queue_reorder_interval_minutes * 60
            ):
                try:
                    # WS-067-001: Get external signal boosts
                    signal_boosts = None
                    if config.external_signals_enabled:
                        try:
                            from signal_processor import get_signal_boosts

                            signal_boosts = get_signal_boosts(company_dir)
                            if signal_boosts:
                                logger.info(
                                    f"WS-067: {len(signal_boosts)} signal boosts active"
                                )
                        except Exception as sig_err:
                            logger.debug(f"WS-067: Signal processing error: {sig_err}")

                    reorder_result = goal_scheduler_mod.reorder_pending_queue(
                        signal_boosts=signal_boosts
                    )
                    last_queue_reorder_time = now_ts
                    state.last_queue_reorder = datetime.now(timezone.utc).isoformat()
                    state.p25_queue_reorders += 1
                    if reorder_result.tasks_reordered > 0:
                        logger.info(
                            f"P25: Queue reordered by goals "
                            f"({reorder_result.tasks_reordered} tasks repositioned)"
                        )
                except Exception as e:
                    logger.debug(f"P25: Queue reorder failed: {e}")

            # P18: Check adaptive interval recommendations (once per day)
            if _should_check_adaptive_intervals(config, state):
                logger.info("P18: Checking adaptive interval recommendations...")
                adaptive_result = _check_adaptive_intervals(config, state)
                if adaptive_result.get("applied"):
                    logger.info(
                        f"P18: Interval recommendation applied - {adaptive_result.get('reason')}"
                    )

            # Log cycle header with queue state
            queue_summary = _get_queue_summary()
            logger.info(f"--- Cycle {cycle_number} | {queue_summary} ---")

            # Safety: ensure daemon is on main branch (auto-PR may have left us on a feature branch)
            try:
                _branch_check = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                _current = (
                    _branch_check.stdout.strip()
                    if _branch_check.returncode == 0
                    else ""
                )
                if _current and _current != "main" and _current != "master":
                    logger.warning(f"Daemon on branch '{_current}', switching to main")
                    _checkout = subprocess.run(
                        ["git", "checkout", "main"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    # Clean up the leftover feature branch to prevent clutter
                    # Guard: skip deletion if any active worktree is using this branch
                    if _checkout.returncode == 0 and (
                        _current.startswith("feat/") or _current.startswith("daemon/")
                    ):
                        _wt_list = subprocess.run(
                            ["git", "worktree", "list", "--porcelain"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        _branch_in_use = (
                            (f"branch refs/heads/{_current}" in _wt_list.stdout)
                            if _wt_list.returncode == 0
                            else False
                        )
                        if _branch_in_use:
                            logger.info(
                                f"Skipping branch cleanup '{_current}' — active worktree using it"
                            )
                        else:
                            _del = subprocess.run(
                                ["git", "branch", "-d", _current],
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            if _del.returncode == 0:
                                logger.info(f"Cleaned up local branch '{_current}'")
                            elif _branch_has_unpushed_commits(_current):
                                logger.warning(
                                    f"Kept local branch '{_current}': it has "
                                    "commits that are on no remote"
                                )
                            else:
                                # -d fails on commits main lacks; -D is safe
                                # only because every commit is on a remote
                                subprocess.run(
                                    ["git", "branch", "-D", _current],
                                    capture_output=True,
                                    text=True,
                                    timeout=15,
                                )
                                logger.info(f"Force-cleaned local branch '{_current}'")
            except (subprocess.TimeoutExpired, OSError):
                pass

            # Phase 1: Discovery runs in background thread (WS-057)

            # Phase 1: Maintenance

            if _should_run_stale_cleanup(config, state):
                cleanup_result = _run_stale_cleanup(config, state)
                cleaned = cleanup_result.get("cleaned", 0)
                if cleaned > 0:
                    logger.info(f"[1/3 Maintenance] Archived {cleaned} stale tasks")

            if _should_run_document_scan(config, state):
                scan_result = _run_document_scan(state)
                discovered = scan_result.get("discovered", 0)
                if discovered > 0:
                    logger.info(
                        f"[1/3 Maintenance] Documents: {discovered} discovered, "
                        f"{scan_result.get('pending', 0)} pending approval"
                    )

            if _should_run_autonomy_reconcile(config, state):
                reconcile_result = _run_autonomy_reconcile(config, state)
                reconciled = reconcile_result.get("reconciled", 0)
                if reconciled > 0:
                    logger.info(
                        f"[1/3 Maintenance] Autonomy queue: reconciled "
                        f"{reconciled} stale completion(s) (PR closed-unmerged)"
                    )

            if _should_run_pr_open_reconcile(config, state):
                pr_open_result = _run_pr_open_reconcile(config, state)
                pr_open_reconciled = pr_open_result.get("reconciled", 0)
                if pr_open_reconciled > 0:
                    logger.info(
                        f"[1/3 Maintenance] pr_open lane: advanced "
                        f"{pr_open_reconciled} task(s) against PR state"
                    )

            if _should_run_stranded_harvest(config, state):
                harvest_result = _run_stranded_harvest(config, state)
                if harvest_result.get("harvested", 0) or harvest_result.get(
                    "failed", 0
                ):
                    logger.info(
                        f"[1/3 Maintenance] Stranded harvest: "
                        f"{harvest_result['harvested']} PR(s) created, "
                        f"{harvest_result['failed']} failed "
                        f"({harvest_result['scanned']} worktree(s) scanned)"
                    )

            if _should_run_escalation_archive(config, state):
                archive_result = _run_escalation_archive(config, state)
                archived = archive_result.get("archived", 0)
                if archived > 0:
                    logger.info(
                        f"[1/3 Maintenance] Escalations: archived {archived} "
                        f"stale record(s)"
                    )

            if _should_run_idea_auto_convert(config, state):
                convert_result = _run_idea_auto_convert(config, state)
                converted = convert_result.get("converted", 0)
                if converted > 0:
                    logger.info(
                        f"[1/3 Maintenance] Ideas: auto-converted {converted} "
                        f"approved idea(s) to queue task(s)"
                    )

            # Phase 2: Scheduling

            if _should_run_roadmap_scheduling(config, state):
                roadmap_result = _run_roadmap_scheduling(config, state)
                scheduled = roadmap_result.get("tasks_scheduled", 0)
                if scheduled > 0:
                    logger.info(
                        f"[2/3 Scheduling] Roadmap: {scheduled} tasks scheduled from wave {state.roadmap_current_wave}"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            # P43: Cron scheduler check
            if _should_run_cron_check(config, state):
                cron_result = _run_cron_check(state, company_dir)
                executed = cron_result.get("tasks_executed", 0)
                if executed > 0:
                    logger.info(
                        f"[2/3 Scheduling] Cron: {executed} scheduled tasks executed"
                    )
                    # Log individual task details at debug level
                    for task_detail in cron_result.get("task_details", []):
                        if task_detail.get("success"):
                            logger.debug(
                                f"  - Task {task_detail['task_id']}: {task_detail['message']}"
                            )
                        else:
                            logger.warning(
                                f"  - Task {task_detail['task_id']} failed: {task_detail['message']}"
                            )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()
                elif cron_result.get("error"):
                    logger.debug(
                        f"[2/3 Scheduling] Cron check: {cron_result.get('error')}"
                    )

            _initiative_idle_cycles = 0
            if p25_scheduler:
                try:
                    _initiative_idle_cycles = p25_scheduler.get_backoff_status().get(
                        "consecutive_idle_cycles", 0
                    )
                except Exception:
                    pass
            if _should_run_employee_initiative(
                config, state, idle_cycles=_initiative_idle_cycles
            ):
                initiative_result = _run_employee_initiative(config, state)
                proposals = initiative_result.get("proposals_submitted", 0)
                if proposals > 0:
                    logger.info(
                        f"[2/3 Scheduling] Employee initiative: "
                        f"{initiative_result.get('employees_activated', 0)} employees, "
                        f"{proposals} proposals"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            # Compliance scan: auto-queue reviews for regulated ventures
            # lacking an approved compliance-report.json (PR #966 follow-up).
            # Cheap scan (dir walk + file reads); tasks are deduped by
            # deterministic task_id so it's safe to run every cycle.
            compliance_result = _run_compliance_scan(company_dir)
            if compliance_result.get("queued"):
                logger.info(
                    f"[2/3 Scheduling] Compliance scan: queued review for "
                    f"{len(compliance_result['queued'])} venture(s): "
                    f"{compliance_result['queued']}"
                )
                consecutive_idle_polls = 0
                last_activity_time = time.time()

            # Customer-feedback scan: surface unacknowledged feedback entries
            # as customer-success-lead review tasks (PR #973 follow-up).
            # Idempotent via on-disk status flip; safe to run every cycle.
            feedback_result = _run_customer_feedback_scan(company_dir)
            if feedback_result.get("surfaced_count", 0) > 0:
                surfaced = feedback_result.get("surfaced", [])
                customers = sorted({s["customer_id"] for s in surfaced})
                logger.info(
                    f"[2/3 Scheduling] Customer-feedback scan: surfaced "
                    f"{feedback_result['surfaced_count']} entries from "
                    f"{len(customers)} customer(s): {customers}"
                )
                consecutive_idle_polls = 0
                last_activity_time = time.time()

            # WS-120: Queue auto-fill from active goals with lowest metric
            if _should_run_queue_autofill(config, state):
                autofill_result = _run_queue_autofill(config, state, company_dir)
                created = autofill_result.get("tasks_created", 0)
                if created > 0:
                    logger.info(
                        f"[2/3 Scheduling] Queue auto-fill: "
                        f"{created} tasks added for goals "
                        f"{autofill_result.get('goals_targeted', [])}"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            # Signal-driven backlog: last resort, AFTER autofill so declared
            # goal work always outranks work derived from repo measurements.
            # _should_run_backlog_generation re-reads the pending count, so a
            # successful autofill above suppresses this in the same cycle.
            if _should_run_backlog_generation(config, state):
                backlog_result = _run_backlog_generation(config, state)
                minted = backlog_result.get("minted", 0)
                if minted > 0:
                    logger.info(
                        f"[2/3 Scheduling] Signal backlog: {minted} task(s) "
                        f"minted from repo signals "
                        f"({backlog_result.get('generated', 0)} generated)"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            # P34: Employee creative ideation
            if _should_run_employee_ideation(config, state):
                ideation_result = _run_employee_ideation(config, state)
                ideas = ideation_result.get("ideas_generated", 0)

                # P36: Track ideation efficiency
                if p25_scheduler:
                    try:
                        p25_scheduler.record_ideation_cycle(
                            ideas_generated=ideas, skipped=False
                        )
                    except Exception as e:
                        logger.debug(f"P36: Failed to record ideation cycle: {e}")

                if ideas > 0:
                    logger.info(
                        f"[2/3 Scheduling] Employee ideation: "
                        f"{ideation_result.get('employees_processed', 0)} employees, "
                        f"{ideas} ideas"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            if _should_run_manager_review(config, state):
                review_result = _run_manager_review(config, state)
                approved = review_result.get("proposals_approved", 0)
                reviewed = review_result.get("tasks_reviewed", 0)
                auto_completed = review_result.get("reviews_auto_completed", 0)
                if approved > 0 or reviewed > 0 or auto_completed > 0:
                    logger.info(
                        f"[2/3 Scheduling] Manager review: {approved} approved, "
                        f"{reviewed} reviewed, {auto_completed} auto-completed"
                    )

            # P35: Auto-merge daemon PRs
            if _should_run_auto_merge(config, state):
                merge_result = _run_auto_merge(config, state)
                merged = merge_result.get("prs_merged", 0)
                checked = merge_result.get("prs_checked", 0)
                if merged > 0:
                    logger.info(
                        f"[2/3 Scheduling] Auto-merge: {merged}/{checked} PRs merged"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

            # Silence sentinel (plan item 4): make silent failure observable
            if _should_run_silence_sentinel(config, state):
                sentinel_result = _run_silence_sentinel(config, state)
                _findings = sentinel_result.get("findings", [])
                _paged = sentinel_result.get("alerts_raised", [])
                if _findings:
                    logger.warning(
                        f"[Sentinel] Silence conditions: "
                        f"{[f['condition'] for f in _findings]}"
                        + (f" — paged: {_paged}" if _paged else " (within dedup)")
                    )

            # LEDGER-5c: scheduled acceptance step (GAP-7), off by code
            # default. Launches a background thread and returns at once.
            if _should_run_acceptance(config, state):
                _run_acceptance(config, state)

            # WS-066-001: Automated rollback for failed daemon PRs
            if _should_run_rollback_check(config, state):
                rollback_result = _run_rollback_check(config, state)
                found = rollback_result.get("failed_prs_found", 0)
                executed = rollback_result.get("rollbacks_executed", 0)
                blocked = rollback_result.get("rollbacks_blocked", 0)
                if found > 0:
                    logger.info(
                        f"[2/3 Scheduling] Rollback check: "
                        f"{found} failed, {executed} rolled back, {blocked} blocked"
                    )
                    if executed > 0:
                        consecutive_idle_polls = 0
                        last_activity_time = time.time()

            # P53: Scheduled reports
            reports_run = _check_scheduled_reports(config, state)
            if reports_run > 0:
                logger.info(f"[2/3 Scheduling] Scheduled reports: {reports_run} run")

            # Scheduled tasks (sales/operations activities)
            tasks_queued = _check_scheduled_tasks(config, state)
            if tasks_queued > 0:
                logger.info(f"[2/3 Scheduling] Scheduled tasks queued: {tasks_queued}")

            # ADR-0002: Branch protection health check
            if _should_run_branch_protection_check(config, state):
                bp_result = _run_branch_protection_check(config, state)
                bp_status = bp_result.get("status", "unknown")
                if bp_status == "degraded":
                    logger.warning(
                        f"[2/3 Scheduling] Branch protection degraded: "
                        f"{bp_result.get('issues', [])}"
                    )
                elif bp_status == "ok":
                    logger.info("[2/3 Scheduling] Branch protection: OK")

            # Git auto-update: check if origin/main has new commits
            if _should_check_git_updates(config):
                if _check_for_git_updates(_GIT_UPDATE_FAILURES_FILE):
                    if _perform_git_pull(_GIT_UPDATE_FAILURES_FILE):
                        # WS-119 1.6: Only restart if engine-loaded files changed.
                        # Doc/website/test changes don't need a restart.
                        engine_changed, changed_files = _git_pull_changed_engine_files()
                        if engine_changed:
                            logger.info(
                                f"[2/3 Scheduling] Git update pulled, "
                                f"engine files changed: {changed_files[:3]}"
                            )
                            _request_daemon_restart(
                                reason=f"engine files changed: {','.join(changed_files[:3])}"
                            )
                            continue
                        else:
                            logger.info(
                                "[2/3 Scheduling] Git update pulled, no engine "
                                "files changed — daemon continues running"
                            )
                    else:
                        logger.warning(
                            "[2/3 Scheduling] Git update available but pull failed"
                        )

            # Cross-project routing check (P14)
            # Process routing suggestions for pending tasks
            if config.cross_project_enabled and config.cross_project_auto_routing:
                routing_result = _process_cross_project_routing(
                    config=config,
                    state=state,
                    agent_id=_daemon_agent_id,
                )
                if routing_result.get("routed", 0) > 0:
                    logger.info(
                        f"Cross-project routing: {routing_result['routed']} tasks routed, "
                        f"{routing_result['queued']} queued for approval"
                    )

            # Employee rebalancing check (P14)
            # Run every N iterations to avoid excessive checks
            if (
                config.cross_project_enabled
                and config.rebalance_check_interval > 0
                and iteration_count % config.rebalance_check_interval == 0
            ):
                rebalance_result = _check_employee_rebalancing(config, state)
                if rebalance_result.get("proposals", 0) > 0:
                    logger.info(
                        f"Employee rebalancing: {rebalance_result['proposals']} proposals generated "
                        f"(overloaded={rebalance_result['overloaded']}, "
                        f"underutilized={rebalance_result['underutilized']})"
                    )

            # Phase 3: Execute tasks from the queue (WS-057-002: parallel workers)
            # Skip execution if no employees available (prevent futile attempts)
            has_employees = _check_employees_available()
            if not has_employees:
                logger.info(
                    f"[3/3 Execute] No pending tasks — idle (cycle {cycle_number} took {time.time() - cycle_start:.0f}s)"
                )
                # Sleep before next poll
                _sleep_unless_shutdown(config.poll_interval_seconds)
                continue

            # WS-057-002: Collect completed workers before spawning new ones

            if not hasattr(state, "_active_workers"):
                state._active_workers = {}  # task_id -> {thread, worktree_path, result}

            # WS-057-003: Protect dict access with lock to prevent race conditions
            with _active_workers_lock:
                for _wid in list(state._active_workers.keys()):
                    _worker = state._active_workers[_wid]
                    _worker_age = time.time() - _worker.get("started_at", time.time())

                    # WS-106: Clean up stale reservations (reserving > 60s without executing)
                    # This handles cases where planning failed or cycle ended without spawning
                    if _worker.get("status") == "reserving":
                        if _worker_age > 60:
                            logger.debug(
                                f"[Execute] Cleaning up stale reservation: {_wid} "
                                f"(age: {_worker_age:.0f}s)"
                            )
                            del state._active_workers[_wid]
                        continue  # Skip regular worker processing for reservations

                    # P1-R3: Adopted workers from startup have no thread object.
                    # Monitor via worker pid; drop the slot when the subprocess exits
                    # so _cleanup_stranded_tasks can release the queue entry.
                    if _worker.get("status") == "adopted":
                        _wpid = _worker.get("pid")
                        if _wpid and is_process_alive(_wpid):
                            continue  # Still running, keep slot occupied
                        logger.info(
                            f"[Adopt] Adopted worker for {_wid} (pid={_wpid}) "
                            f"has exited; task will be released by stranded sweep"
                        )
                        del state._active_workers[_wid]
                        continue

                    # Check if worker is done OR timed out. Timeout scales with
                    # task complexity; see _force_collect_on_timeout for details.
                    _worker_timed_out = _force_collect_on_timeout(
                        _wid, _worker, _worker_age, config
                    )
                    _thread = _worker.get("thread")
                    # WS-106: Use _thread variable to avoid None dereference
                    if not _thread or not _thread.is_alive() or _worker_timed_out:
                        _wr = _worker.get("result", {})
                        _wt_path = _worker.get("worktree_path")
                        _action = _wr.get("action", "error")

                        # E8: the ceiling build was counted when this worker was
                        # spawned. If the worker never ran the task, give it back
                        # — otherwise a no-op spawn burns a build and the ceiling
                        # blocks the task before it has had K real attempts.
                        if _action in _NO_BUILD_ACTIONS:
                            _refund_cross_regen_build(
                                state, _wid, _worker.get("e8_title_key", "")
                            )

                        if _action == "executed":
                            state.tasks_completed += 1
                            # E8: clear cross-regen build counter on success
                            state.task_total_builds.pop(_wid, None)
                            loop_monitor.record_success(breaker, health_metrics)
                            loop_monitor.save_monitor_state(breaker, health_metrics)
                            if p25_scheduler:
                                try:
                                    p25_scheduler.record_activity()
                                except Exception:
                                    pass
                            logger.info(f"[3/3 Execute] Worker completed: {_wid} ✓")
                            # WS-106-003: Record successful resolution for pattern learning
                            try:
                                from escalation_router import record_resolution

                                _task_emp = _wr.get("assigned_to") or _wr.get(
                                    "executed_by"
                                )
                                if _task_emp:
                                    record_resolution(_wid, _task_emp, "resolved")
                            except ImportError:
                                pass
                            except Exception as _rec_err:
                                logger.debug(
                                    f"WS-106-003: Resolution record failed: {_rec_err}"
                                )
                        elif _action == "failed":
                            _wr_reason = _wr.get("reason", "unknown")

                            # WS-105: Attempt autonomous failure recovery before counting as failure
                            _recovery_success = False
                            if failure_recovery_mod:
                                try:
                                    _failed_task = _wr.get("task", {}) or {}
                                    if not _failed_task.get("task_id"):
                                        _failed_task["task_id"] = _wid
                                    _exit_code = _wr.get("exit_code")
                                    _recoverer = (
                                        failure_recovery_mod.RecoveryOrchestrator(
                                            company_dir
                                        )
                                    )
                                    _rec_result = _recoverer.attempt_recovery(
                                        task=_failed_task,
                                        error_msg=_wr_reason,
                                        exit_code=_exit_code,
                                        queue_path=queue_path,
                                    )
                                    if _rec_result.success:
                                        _recovery_success = True
                                        logger.info(
                                            f"[WS-105] Autonomous recovery for {_wid}: "
                                            f"strategy={_rec_result.strategy_used.value}, "
                                            f"attempt={_rec_result.recovery_attempt_num}"
                                        )
                                except Exception as _rec_exc:
                                    logger.debug(
                                        f"[WS-105] Recovery attempt failed: {_rec_exc}"
                                    )

                            # If recovery succeeded, skip failure recording
                            if _recovery_success:
                                # WS-118: MARK_COMPLETE means task already succeeded — complete it
                                if (
                                    _rec_result
                                    and _rec_result.strategy_used
                                    == failure_recovery_mod.RecoveryStrategy.MARK_COMPLETE
                                ):
                                    state.tasks_completed += 1
                                    loop_monitor.record_success(breaker, health_metrics)
                                    loop_monitor.save_monitor_state(
                                        breaker, health_metrics
                                    )
                                    try:
                                        operation_loop.release_task(
                                            queue_path=queue_path,
                                            task_id=_wid,
                                            result="completed",
                                        )
                                    except Exception as _rel_err:
                                        logger.debug(
                                            f"[WS-118] release_task failed: {_rel_err}"
                                        )
                                    logger.info(
                                        f"[3/3 Execute] Worker {_wid} recovery: "
                                        f"goal already achieved — marked complete"
                                    )
                                else:
                                    logger.info(
                                        f"[3/3 Execute] Worker {_wid} recovered - re-queued for retry"
                                    )
                            else:
                                # Normal failure path - recovery failed or not attempted
                                state.tasks_failed += 1
                                # P-G7: Skip circuit breaker for pr_test_failure and
                                # WS-119 1.8 phantom-guard failures — these indicate task
                                # quality issues, not daemon health degradation.
                                _wr_is_pr_test_failure = (
                                    "Tests failed" in _wr_reason
                                    and (
                                        "PR creation failed" in _wr_reason
                                        or "pr_test_failure" in _wr_reason
                                    )
                                )
                                _wr_is_phantom_guard = (
                                    "no deliverable" in _wr_reason.lower()
                                    or "WS-119 1.8" in _wr_reason
                                    or "no PR was created" in _wr_reason
                                )
                                if not (_wr_is_pr_test_failure or _wr_is_phantom_guard):
                                    loop_monitor.record_failure(
                                        breaker,
                                        health_metrics,
                                        _wr_reason,
                                        breaker_config,
                                    )
                                loop_monitor.save_monitor_state(breaker, health_metrics)
                                logger.warning(
                                    f"[3/3 Execute] Worker failed: {_wid} — {_wr.get('reason', '?')}"
                                )
                                # WS-106-003: Record failure for pattern learning
                                try:
                                    from escalation_router import record_resolution

                                    _task_emp = _wr.get("assigned_to") or _wr.get(
                                        "executed_by"
                                    )
                                    if _task_emp:
                                        record_resolution(_wid, _task_emp, "failed")
                                except ImportError:
                                    pass
                                except Exception as _rec_err:
                                    logger.debug(
                                        f"WS-106-003: Failure record failed: {_rec_err}"
                                    )
                        elif _action != "idle":
                            logger.info(f"[3/3 Execute] Worker {_wid}: {_action}")

                        # Persist structured task result to .company/results/
                        if task_result_writer and _action in (
                            "executed",
                            "failed",
                            "escalated",
                        ):
                            try:
                                _worker_elapsed = _worker.get("elapsed_seconds", 0)
                                task_result_writer.write_task_result(
                                    _wr, _action, _worker_elapsed, company_dir
                                )
                            except Exception as _trw_exc:
                                logger.debug(f"Task result writer: {_trw_exc}")

                            # WS-106-005: Extract patterns from successful completions
                            if _action == "executed":
                                try:
                                    from pattern_extractor import extract_and_save

                                    _pat_result = extract_and_save(
                                        company_dir, task_id=_wid
                                    )
                                    if _pat_result.get("new_patterns", 0) > 0:
                                        logger.info(
                                            f"[Execute] WS-106-005: Extracted "
                                            f"{_pat_result['new_patterns']} new patterns "
                                            f"from {_wid}"
                                        )
                                except ImportError:
                                    pass
                                except Exception as _pat_err:
                                    logger.debug(
                                        f"WS-106-005: Pattern extraction failed: {_pat_err}"
                                    )

                        # Cleanup worktree
                        # Serialize removal to prevent index.lock contention
                        if _wt_path:
                            try:
                                with _worktree_creation_lock:
                                    subprocess.run(
                                        [
                                            "git",
                                            "worktree",
                                            "remove",
                                            "--force",
                                            _wt_path,
                                        ],
                                        capture_output=True,
                                        timeout=15,
                                    )
                                    # WS-057-005: Clean up local branch ref after worktree removal
                                    _wt_br = _worker.get("worktree_branch")
                                    if _wt_br:
                                        subprocess.run(
                                            ["git", "branch", "-D", _wt_br],
                                            capture_output=True,
                                            timeout=5,
                                        )
                            except Exception:
                                pass

                        del state._active_workers[_wid]

            # WS-057-004: Periodic worktree GC (every 30 cycles, >2h old)
            if cycle_number % 30 == 0 and cycle_number > 0:
                try:
                    _wt_gc_base = _worktree_base()
                    if _wt_gc_base.exists():
                        with _active_workers_lock:
                            _active_wt_paths = {
                                w.get("worktree_path")
                                for w in state._active_workers.values()
                            }
                        _gc_count = 0
                        _now_gc = time.time()
                        for _gc_entry in _wt_gc_base.iterdir():
                            if not _gc_entry.is_dir() or _gc_entry.is_symlink():
                                continue
                            _gc_path = str(_gc_entry)
                            if _gc_path in _active_wt_paths:
                                continue
                            try:
                                if _now_gc - _gc_entry.stat().st_mtime > 7200:
                                    # P1-R4: spare unpushed committed work
                                    if _worktree_has_unpushed_work(_gc_entry):
                                        logger.warning(
                                            f"WS-057-004: GC sparing {_gc_entry.name}"
                                            " — unpushed committed work"
                                        )
                                        continue
                                    # Serialize removal to prevent index.lock contention
                                    with _worktree_creation_lock:
                                        subprocess.run(
                                            [
                                                "git",
                                                "worktree",
                                                "remove",
                                                "--force",
                                                _gc_path,
                                            ],
                                            capture_output=True,
                                            timeout=15,
                                        )
                                    _gc_count += 1
                            except Exception:
                                pass
                        if _gc_count > 0:
                            # Serialize prune to prevent index.lock contention
                            with _worktree_creation_lock:
                                subprocess.run(
                                    ["git", "worktree", "prune"],
                                    capture_output=True,
                                    timeout=10,
                                )
                            logger.info(
                                f"[Execute] Worktree GC: cleaned {_gc_count} stale worktrees"
                            )
                    # WS-057-005: Periodic orphaned branch GC
                    # Delete up to 50 daemon/* branches with no upstream tracking per cycle
                    try:
                        _br_gc = subprocess.run(
                            [
                                "git",
                                "for-each-ref",
                                "--format=%(refname:short) %(upstream:track)",
                                "refs/heads/daemon/",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if _br_gc.returncode == 0:
                            _br_del_count = 0
                            for _br_line in _br_gc.stdout.strip().splitlines():
                                if _br_del_count >= 50:
                                    break
                                _br_parts = _br_line.strip().split()
                                _br_name = _br_parts[0] if _br_parts else ""
                                # WS-094 FIX: Only delete branches marked [gone] (remote deleted after PR merge).
                                # Do NOT delete branches with no tracking — they may be newly created by
                                # workers that haven't pushed yet. Deleting them causes PR creation to fail.
                                _br_track = _br_parts[1] if len(_br_parts) > 1 else ""
                                if _br_track == "[gone]":
                                    subprocess.run(
                                        ["git", "branch", "-D", _br_name],
                                        capture_output=True,
                                        timeout=5,
                                    )
                                    _br_del_count += 1
                            if _br_del_count > 0:
                                logger.info(
                                    f"[Execute] Branch GC: deleted {_br_del_count} orphaned daemon branches"
                                )
                        # Also prune stale remote tracking refs
                        subprocess.run(
                            ["git", "remote", "prune", "origin"],
                            capture_output=True,
                            timeout=15,
                        )
                    except Exception:
                        pass  # Non-fatal
                except Exception as _gc_err:
                    logger.debug(f"[Execute] Worktree GC failed (non-fatal): {_gc_err}")

            # Per-cycle stranded-task sweep: catches workers that died
            # while the daemon kept running (manual kill, OOM, silent crash).
            # Without this, the task is forever assigned to a live daemon
            # but with no worker subprocess actually executing it.
            try:
                _stranded = _cleanup_stranded_tasks(state, _daemon_agent_id)
                if _stranded.get("released", 0) > 0:
                    logger.info(
                        f"[Stranded] Released {_stranded['released']} stranded "
                        f"task(s) — worker subprocess(es) died while daemon ran"
                    )
            except Exception as _strand_err:
                logger.warning(
                    f"[Stranded] Per-cycle cleanup failed (non-fatal): {_strand_err}"
                )

            # Determine how many workers we can spawn
            _max_workers = 1
            if p25_scheduler:
                try:
                    _max_workers = p25_scheduler.get_worker_count()
                except Exception:
                    _max_workers = 1
            with _active_workers_lock:
                _available_slots = _max_workers - len(state._active_workers)

            if _available_slots <= 0:
                # All worker slots occupied — wait for completion (longer sleep to reduce log noise)
                if cycle_number % 6 == 0:
                    with _active_workers_lock:
                        _aw_count = len(state._active_workers)
                    logger.info(
                        f"[Execute] {_aw_count} workers active "
                        f"(max {_max_workers}), waiting for completion"
                    )
                time.sleep(min(poll_interval, 15))
                continue

            # P20: Check if planning is enabled and peek at next task
            planning_result = None
            use_planning = False
            next_task = None
            execution_plan = None

            # Peek at next task for routing/planning decisions
            try:
                with open(queue_path, encoding="utf-8") as f:
                    queue_data = json.load(f)
                pending = queue_data.get("pending", [])
                if pending:
                    # WS-057-003: Skip tasks that already have active workers
                    _active_ids = set(getattr(state, "_active_workers", {}).keys())
                    # WS-101: Track exhausted tasks to move to blocked
                    _DEFAULT_MAX_ATTEMPTS = 3
                    # 2026-09-04: selection also skips tasks whose respawn
                    # cooldown is still running, so a task that would be refused
                    # at spawn time no longer consumes the whole dispatch cycle.
                    # CONFLICT-AWARE-SCHEDULING (#727): hold back a goal build
                    # that shares a hotspot file with one in flight.
                    _pick_from = _conflict_gate(pending, _active_ids)
                    next_task, _exhausted_tasks = _pick_next_pending_task(
                        _pick_from,
                        _active_ids,
                        _recent_spawn_at,
                        time.time(),
                        _DEFAULT_MAX_ATTEMPTS,
                    )
                    # WS-101: Auto-move exhausted tasks to blocked queue
                    if _exhausted_tasks:
                        try:
                            _blocked = queue_data.get("blocked", [])
                            for _ex_task in _exhausted_tasks:
                                pending.remove(_ex_task)
                                _ex_task["status"] = "blocked"
                                _ex_task["blocked_reason"] = (
                                    f"Exhausted retries ({_ex_task.get('retry_count', 0)}/"
                                    f"{_ex_task.get('max_attempts', _DEFAULT_MAX_ATTEMPTS)})"
                                )
                                _blocked.append(_ex_task)
                            queue_data["pending"] = pending
                            queue_data["blocked"] = _blocked
                            # Atomic write
                            _fd, _tmp = tempfile.mkstemp(
                                dir=str(queue_path.parent), suffix=".json"
                            )
                            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                                json.dump(queue_data, _f, indent=2)
                            os.replace(_tmp, queue_path)
                            logger.info(
                                f"[Execute] WS-101: Moved {len(_exhausted_tasks)} exhausted "
                                f"tasks to blocked queue"
                            )
                            # WS-106-003: Smart escalation routing for blocked tasks
                            # Route each exhausted task to the best senior employee
                            try:
                                from escalation_router import route_escalation

                                for _ex_task in _exhausted_tasks:
                                    _ex_tid = _ex_task.get("task_id")
                                    if _ex_tid:
                                        _routing = route_escalation(_ex_tid)
                                        if _routing.action == "assign":
                                            logger.info(
                                                f"[Execute] WS-106-003: Routed {_ex_tid} "
                                                f"to {_routing.assigned_to}"
                                            )
                                        elif _routing.action == "notify_human":
                                            logger.warning(
                                                f"[Execute] WS-106-003: Human required "
                                                f"for {_ex_tid}: {_routing.reason}"
                                            )
                            except ImportError:
                                logger.debug(
                                    "[Execute] WS-106-003: escalation_router not available"
                                )
                            except Exception as _route_err:
                                logger.debug(
                                    f"[Execute] WS-106-003: Routing failed: {_route_err}"
                                )
                        except Exception as _ex_err:
                            logger.debug(
                                f"WS-101: Failed to move exhausted tasks: {_ex_err}"
                            )
                    if next_task is None and pending:
                        logger.debug(
                            f"[Execute] None of the {len(pending)} pending tasks is "
                            "dispatchable (active worker, respawn cooldown, or "
                            "exhausted retries)"
                        )
            except Exception as e:
                logger.debug(f"Queue peek failed: {e}")

            # WS-106: Reserve task IMMEDIATELY after selection to prevent race condition
            # Problem: Multiple cycles can select the same task during the ~5s planning phase
            # Fix: Add task_id to _active_workers as "reserving" before planning starts
            # This makes subsequent cycles skip it when checking _active_ids
            _reserved_task_id = None
            if next_task:
                _reserved_task_id = next_task.get("task_id")
                if _reserved_task_id:
                    with _active_workers_lock:
                        if _reserved_task_id not in state._active_workers:
                            state._active_workers[_reserved_task_id] = {
                                "thread": None,  # Will be set when worker spawns
                                "worktree_path": None,
                                "worktree_branch": None,
                                "result": {},
                                "started_at": time.time(),
                                "status": "reserving",  # Mark as reserved, not yet executing
                            }
                        else:
                            # Another cycle already reserved this task
                            logger.debug(
                                f"[Execute] Task {_reserved_task_id} already reserved, skipping"
                            )
                            next_task = None
                            _reserved_task_id = None

            # P28: Route task through central orchestrator if available
            if next_task and central_orchestrator is not None:
                try:
                    routing_start = time.time()

                    # Call orchestrator.route_task() for execution plan
                    execution_plan = central_orchestrator.route_task(
                        task_id=next_task.get("task_id", ""),
                        title=next_task.get("title", ""),
                        description=next_task.get("description"),
                        dependencies=next_task.get("dependencies", []),
                        wave=next_task.get("wave", 1),
                    )

                    routing_time_ms = (time.time() - routing_start) * 1000

                    # Convert to dict for storage
                    plan_dict = execution_plan.to_dict()

                    # Attach execution plan to task record
                    next_task["execution_plan"] = plan_dict

                    # Set employee hint for downstream matching
                    if execution_plan.employee_hint:
                        next_task["employee_hint"] = execution_plan.employee_hint

                    # Use orchestrator's complexity instead of detecting again
                    if plan_dict.get("complexity"):
                        complexity_level = plan_dict["complexity"].get(
                            "level", "standard"
                        )
                        next_task["estimated_complexity"] = complexity_level

                    # Track routing decision in metrics
                    if orchestrator_metrics_mod is not None:
                        orchestrator_metrics_mod.track_routing_decision(
                            plan=plan_dict,
                            routing_time_ms=routing_time_ms,
                        )

                    complexity = plan_dict.get("complexity", {}).get("level", "?")
                    exec_mode = plan_dict.get("execution_mode", "?")
                    routed_tid = (
                        next_task.get("task_id") or next_task.get("id") or "unknown"
                    )
                    logger.info(
                        f"[3/3 Execute] P28 routed task {routed_tid} "
                        f"(complexity: {complexity}, mode: {exec_mode}, "
                        f"time: {routing_time_ms:.1f}ms)"
                    )

                    # Update task in queue with execution plan
                    # Use QueueLock to prevent concurrent write races (P50 fix)
                    # WS-119 wave 1.5: instrument lock acquisition. The route -> plan
                    # gap was observed at ~21 minutes; this measures whether the
                    # lock acquisition is the culprit.
                    _ws119_q_t0 = time.time()
                    try:
                        lock_path = company_dir / "runtime/queue.lock"
                        logger.info(
                            f"[3/3 Execute] WS-119: acquiring queue lock for {routed_tid}"
                        )
                        with work_allocator.QueueLock(lock_path):
                            _ws119_q_lock_acquired = time.time()
                            logger.info(
                                f"[3/3 Execute] WS-119: queue lock acquired in "
                                f"{(_ws119_q_lock_acquired - _ws119_q_t0):.1f}s"
                            )
                            # Re-read queue inside lock to avoid clobbering
                            # concurrent writes from claim_task/release_task
                            with open(queue_path, encoding="utf-8") as f:
                                fresh_queue = json.load(f)
                            fresh_pending = fresh_queue.get("pending", [])
                            # Find and update the task we routed
                            routed_id = next_task.get("task_id")
                            for i, t in enumerate(fresh_pending):
                                if t.get("task_id") == routed_id:
                                    fresh_pending[i] = next_task
                                    break
                            fresh_queue["pending"] = fresh_pending
                            fd, tmp_path = tempfile.mkstemp(
                                dir=str(queue_path.parent),
                                prefix=".work_queue_",
                                suffix=".json.tmp",
                            )
                            try:
                                with os.fdopen(fd, "w", encoding="utf-8") as f:
                                    json.dump(fresh_queue, f, indent=2)
                                os.replace(tmp_path, queue_path)
                            except Exception:
                                if os.path.exists(tmp_path):
                                    os.unlink(tmp_path)
                                raise
                    except Exception as e:
                        logger.debug(f"P28: Failed to update queue with plan: {e}")

                except Exception as e:
                    # Fallback to old behavior on error
                    logger.debug(f"P28: Orchestrator routing failed (non-fatal): {e}")
                    execution_plan = None

            if config.planning_enabled and next_task:
                try:
                    task_complexity = next_task.get(
                        "complexity", next_task.get("estimated_complexity", "")
                    )

                    # Check if task needs planning
                    if _should_plan_task(next_task, config):
                        logger.info(
                            f"[3/3 Execute] Planning task {next_task.get('task_id')} "
                            f"(complexity: {task_complexity or 'detecting'})"
                        )

                        planning_result = _plan_and_validate_task(
                            task=next_task,
                            config=config,
                            state=state,
                        )

                        if planning_result.get("success"):
                            use_planning = True
                            logger.info(
                                f"[3/3 Execute] Plan validated (score: {planning_result.get('score')}/25)"
                            )
                        else:
                            logger.warning(
                                f"[3/3 Execute] Planning failed: {planning_result.get('reason')}"
                            )
                    else:
                        # Trivial task - direct execution
                        state.tasks_direct += 1
                except Exception as e:
                    logger.debug(f"Planning pre-check failed: {e}")

            # Execute task (either planned or direct)
            # P50 FIX: ALWAYS capture task_id from next_task, not just when planning.
            target_task_id = next_task.get("task_id") if next_task else None

            if use_planning and planning_result:
                logger.info(
                    f"[3/3 Execute] Executing wave {1}/{len(planning_result.get('waves', []))} "
                    f"({len(planning_result.get('plan', {}).get('subtasks', []))} subtasks planned)"
                )

            if target_task_id is None and next_task:
                logger.warning(
                    f"[3/3 Execute] Task identity lost - next_task exists but has no task_id. "
                    f"Title: {next_task.get('title', 'unknown')[:50]}"
                )

            # WS-092: Always use worktrees for isolation
            # Previously: only used worktrees when max_workers > 1.
            # Problem: when max_workers == 1, direct execution in main worktree
            # conflicts with human work (test sprints, interactive sessions).
            # Fix: Always use worktrees regardless of worker count. The minor
            # threading overhead is negligible vs. the cost of merge conflicts.
            if target_task_id:
                # WS-057-003: Skip if this task already has active worker (defense in depth)
                # WS-106: Allow "reserving" status to proceed (current cycle reserved it)
                with _active_workers_lock:
                    _worker_entry = state._active_workers.get(target_task_id)
                    _already_active = (
                        _worker_entry is not None
                        and _worker_entry.get("status") != "reserving"
                    )
                if _already_active:
                    logger.debug(
                        f"[Execute] Task {target_task_id} already has active worker, skipping"
                    )
                    # WS-106: Clean up reservation since we're not proceeding
                    with _active_workers_lock:
                        if target_task_id in state._active_workers:
                            if (
                                state._active_workers[target_task_id].get("status")
                                == "reserving"
                            ):
                                del state._active_workers[target_task_id]
                    time.sleep(min(poll_interval, 5))
                    continue

                # 2026-08-30: the in-memory `_active_workers` bookkeeping can
                # lose track of a still-running worker (its slot released by
                # a queue write that marked the task failed without
                # confirming the subprocess had actually exited — observed
                # when a torn-down worktree broke the worker's git commands
                # mid-session while the worker process itself stayed alive).
                # Cross-check the on-disk pid registry by PROCESS, not by a
                # queue field something else may have written, before ever
                # spawning a second worker for the same task_id.
                _live_pid = _dispatch_blocked_by_live_worker(target_task_id)
                if _live_pid:
                    logger.warning(
                        f"[Execute] Task {target_task_id} has a live worker "
                        f"process (pid={_live_pid}) not reflected in "
                        "_active_workers — skipping respawn"
                    )
                    time.sleep(min(poll_interval, 5))
                    continue

                # 2026-07-06: respawn cooldown — a task that just spawned a
                # worker must not spawn another within the window, whatever
                # state the active-worker bookkeeping is in (three duplicate
                # workers in 40s destroyed each other's worktree files).
                _cd_left = _spawn_cooldown_remaining(
                    _recent_spawn_at, target_task_id, time.time()
                )
                if _cd_left > 0:
                    logger.warning(
                        f"[Execute] Task {target_task_id} spawned a worker recently — "
                        f"respawn cooldown active ({_cd_left:.0f}s left), skipping"
                    )
                    with _active_workers_lock:
                        if (
                            state._active_workers.get(target_task_id, {}).get("status")
                            == "reserving"
                        ):
                            del state._active_workers[target_task_id]
                    time.sleep(min(poll_interval, 5))
                    continue

                # Respawn guard: never spawn a worker for a task that already
                # has an open PR (harvest or normal capture). If the task is in
                # pr_open, the merge-reconcile path owns it.
                try:
                    with open(queue_path, encoding="utf-8") as _f:
                        _q_snap = json.load(_f)
                    _pr_open_ids = {
                        t.get("task_id")
                        for t in _q_snap.get("pr_open", [])
                        if t.get("task_id")
                    }
                    if target_task_id in _pr_open_ids:
                        logger.warning(
                            f"[Execute] Task {target_task_id} already has an open PR "
                            f"— skipping respawn (harvest/capture already captured this work)"
                        )
                        with _active_workers_lock:
                            if (
                                state._active_workers.get(target_task_id, {}).get(
                                    "status"
                                )
                                == "reserving"
                            ):
                                del state._active_workers[target_task_id]
                        time.sleep(min(poll_interval, 5))
                        continue
                except Exception as _rg_err:
                    logger.debug(
                        f"[Execute] Respawn guard queue read failed: {_rg_err}"
                    )

                # E8: Cross-regeneration build ceiling — block spawn if task or title
                # has accumulated K total builds without succeeding.
                if _check_cross_regen_ceiling(
                    config,
                    state,
                    target_task_id,
                    next_task.get("title", "") if next_task else "",
                    company_dir,
                ):
                    state.cross_regen_ceiling_blocks += 1
                    with _active_workers_lock:
                        if (
                            state._active_workers.get(target_task_id, {}).get("status")
                            == "reserving"
                        ):
                            del state._active_workers[target_task_id]
                    time.sleep(min(poll_interval, 5))
                    continue

                # Create git worktree for isolated execution
                import uuid as _uuid

                _wt_suffix = _uuid.uuid4().hex[:6]
                # WS-057-003: Sanitize task_id to prevent path traversal
                import re as _re

                _safe_id = _re.sub(r"[^a-zA-Z0-9_\-]", "_", target_task_id)
                # WS-057-003: Validate worktree base and path in one atomic sequence
                # Check symlink FIRST to prevent TOCTOU race (security review CRITICAL-2)
                _wt_base = _worktree_base()
                _wt_dir = str(_wt_base / f"{_safe_id}-{_wt_suffix}")
                _wt_branch = f"daemon/wt-{_safe_id[-12:]}-{_wt_suffix}"

                if _wt_dir:
                    try:
                        os.makedirs(str(_wt_base), mode=0o700, exist_ok=True)
                        import stat as _stat

                        # Step 1: Verify base dir is not a symlink
                        if _wt_base.is_symlink():
                            logger.error(
                                "[3/3 Execute] Worktree base is a symlink — aborting"
                            )
                            _wt_dir = None
                        else:
                            # Step 2: Enforce permissions
                            _actual_mode = _stat.S_IMODE(os.stat(str(_wt_base)).st_mode)
                            if _actual_mode != 0o700:
                                os.chmod(str(_wt_base), 0o700)
                                logger.warning(
                                    f"Fixed worktree dir permissions: {oct(_actual_mode)} -> 0o700"
                                )
                            # Step 3: Validate resolved path stays under base
                            # (done AFTER symlink check to prevent TOCTOU)
                            _wt_resolved = Path(_wt_dir).resolve()
                            if not str(_wt_resolved).startswith(
                                str(_wt_base.resolve()) + "/"
                            ):
                                logger.error(
                                    f"[3/3 Execute] Path traversal detected for {target_task_id}, skipping"
                                )
                                _wt_dir = None
                    except Exception as _wte:
                        logger.warning(f"[3/3 Execute] Worktree setup error: {_wte}")
                        _wt_dir = None

                if _wt_dir:
                    # WS-119: Sweep stale `.git/worktrees/*/locked` files older
                    # than 1 hour BEFORE acquiring the lock. These files are
                    # plain filesystem state (not git commands) and removing
                    # them doesn't need serialization. Doing this outside the
                    # lock keeps the lock block tight and lets the test
                    # `test_branch_cleanup_inside_lock` keep its proximity
                    # invariant.
                    try:
                        _gwt_dir = Path(".git/worktrees")
                        if _gwt_dir.exists():
                            _lock_cutoff = time.time() - 3600
                            for _entry in _gwt_dir.iterdir():
                                _lock_file = _entry / "locked"
                                if (
                                    _lock_file.exists()
                                    and _lock_file.stat().st_mtime < _lock_cutoff
                                ):
                                    try:
                                        _lock_file.unlink()
                                        logger.debug(
                                            f"WS-119: removed stale lock {_lock_file}"
                                        )
                                    except OSError:
                                        pass
                    except Exception as _lockerr:
                        logger.debug(f"WS-119: stale-lock sweep failed: {_lockerr}")

                    try:
                        # WS-057-002: Serialize worktree creation to prevent git lock contention
                        with _worktree_creation_lock:
                            # Clean up stale branch if exists from previous run
                            # WS-102: Increased timeout from 5s to 15s — git can be slow
                            # when there are many branches or under concurrent load
                            subprocess.run(
                                ["git", "branch", "-d", _wt_branch],
                                capture_output=True,
                                timeout=15,
                            )
                            # WS-119: Retry worktree add once on timeout. Some
                            # intermittent timeouts resolve on the second attempt
                            # because the first run cleared any in-flight contention.
                            _wt_result = None
                            _wt_attempt = 0
                            # origin/main, not local main: the primary checkout
                            # can be stale for days when its pull is blocked.
                            _wt_base_ref = _preferred_base_ref()
                            while _wt_attempt < 2:
                                _wt_attempt += 1
                                try:
                                    _wt_result = subprocess.run(
                                        [
                                            "git",
                                            "worktree",
                                            "add",
                                            "--no-track",
                                            "-b",
                                            _wt_branch,
                                            _wt_dir,
                                            _wt_base_ref,
                                        ],
                                        capture_output=True,
                                        text=True,
                                        timeout=30,
                                    )
                                    break
                                except subprocess.TimeoutExpired:
                                    logger.warning(
                                        f"[3/3 Execute] WS-119: worktree add timeout "
                                        f"(attempt {_wt_attempt}/2) for {target_task_id}"
                                    )
                                    # Prune orphaned entries between attempts
                                    try:
                                        subprocess.run(
                                            ["git", "worktree", "prune"],
                                            capture_output=True,
                                            timeout=10,
                                        )
                                    except Exception:
                                        pass
                                    if _wt_attempt >= 2:
                                        raise
                        if _wt_result is None or _wt_result.returncode != 0:
                            # Worktree creation failed — fall back to direct execution
                            logger.warning(
                                f"[3/3 Execute] Worktree creation failed for {target_task_id}, "
                                f"falling back to direct execution: {_wt_result.stderr[:100]}"
                            )
                            # WS-057-005: Clean up branch ref created by failed worktree add
                            # WS-102: Increased timeout from 5s to 15s
                            subprocess.run(
                                ["git", "branch", "-D", _wt_branch],
                                capture_output=True,
                                timeout=15,
                            )
                            _wt_dir = None
                    except Exception as _wte:
                        logger.warning(f"[3/3 Execute] Worktree error: {_wte}")
                        # WS-057-005: Clean up branch ref on exception
                        # WS-102: Increased timeout from 5s to 15s
                        try:
                            subprocess.run(
                                ["git", "branch", "-D", _wt_branch],
                                capture_output=True,
                                timeout=15,
                            )
                        except Exception:
                            pass
                        _wt_dir = None

                if _wt_dir:
                    # SCOUT-20260813-1 — ChainDrop-class defense: verify
                    # nothing has touched this freshly created worktree's
                    # `.claude/settings.json`, `.mcp.json` or `.claude/hooks/`
                    # since checkout, BEFORE any session starts there. A
                    # SessionStart hook auto-executes the instant a session
                    # opens — before any prompt is sent — so a tampered
                    # worktree must never reach the worker spawn below.
                    try:
                        from worktree_integrity import (
                            log_integrity_violation as _log_wt_violation,
                        )
                        from worktree_integrity import (
                            verify_worktree_integrity as _verify_wt_integrity,
                        )

                        # Only needed to place the audit log inside the primary
                        # checkout's `.company/` — the integrity check itself
                        # anchors on the worktree's own HEAD and needs no
                        # reference to this directory.
                        try:
                            from company_resolver import find_company_root as _fcr

                            _primary_root = _fcr() or Path.cwd()
                        except Exception:
                            _primary_root = Path.cwd()

                        _wt_integrity = _verify_wt_integrity(Path(_wt_dir))
                    except Exception as _wt_integrity_err:
                        logger.error(
                            f"[3/3 Execute] SECURITY: worktree integrity check "
                            f"could not run for {target_task_id}, refusing to "
                            f"start a session: {_wt_integrity_err}"
                        )
                        _wt_dir = None
                        _wt_integrity = None

                    if _wt_dir and not _wt_integrity.ok:
                        logger.error(
                            f"[3/3 Execute] SECURITY: worktree integrity check "
                            f"failed for {target_task_id}: {_wt_integrity.reason} "
                            f"(diverging: {_wt_integrity.diverging_files})"
                        )
                        _log_wt_violation(
                            _primary_root, target_task_id, Path(_wt_dir), _wt_integrity
                        )
                        with _worktree_creation_lock:
                            subprocess.run(
                                ["git", "worktree", "remove", "--force", _wt_dir],
                                capture_output=True,
                                timeout=15,
                            )
                            subprocess.run(
                                ["git", "branch", "-D", _wt_branch],
                                capture_output=True,
                                timeout=15,
                            )
                        _wt_dir = None

                if _wt_dir:
                    # Spawn worker thread with worktree
                    _worker_info = {
                        "thread": None,
                        "worktree_path": _wt_dir,
                        "worktree_branch": _wt_branch,  # WS-057-005: track for cleanup
                        "result": {},
                        "started_at": time.time(),
                        "status": "executing",  # WS-106: Mark as executing (not just reserved)
                        "task_complexity": (next_task or {}).get(
                            "estimated_complexity", "standard"
                        ),
                        # CHALLENGER-STAGE: task-declared force-collect budget.
                        "task_timeout_seconds": (next_task or {}).get(
                            "worker_timeout_seconds"
                        ),
                        # E8: set below, next to the build increment, so the
                        # collection cycle can refund a build that did no work.
                        "e8_title_key": "",
                    }

                    def _worker_fn(
                        _qi=queue_path,
                        _aid=_daemon_agent_id,
                        _sp=state_path,
                        _tid=target_task_id,
                        _cwd=_wt_dir,
                        _info=_worker_info,
                    ):
                        try:
                            _info["result"] = operation_loop.poll_and_execute_once(
                                queue_path=_qi,
                                agent_id=_aid,
                                state_path=_sp,
                                target_task_id=_tid,
                                execution_cwd=_cwd,
                            )
                        except Exception as _e:
                            import traceback as _tb_mod

                            _tb_str = _tb_mod.format_exc()
                            logger.error(f"[Worker] {_tid} traceback:\n{_tb_str}")
                            _info["result"] = {
                                "action": "failed",
                                "reason": f"Worker exception: {_e}",
                                "task_id": _tid,
                            }
                            # WS-057: Release task to prevent in_progress leak
                            try:
                                operation_loop.release_task(
                                    queue_path=_qi,
                                    task_id=_tid,
                                    result="failed",
                                    error=f"Worker exception: {_e}",
                                )
                            except Exception as _release_err:
                                logger.error(
                                    f"[Worker] Failed to release task {_tid}: {_release_err}"
                                )

                    _t = _threading.Thread(
                        target=_worker_fn,
                        name=f"worker-{target_task_id[:12]}",
                        daemon=True,
                    )
                    _t.start()
                    _worker_info["thread"] = _t
                    _recent_spawn_at[target_task_id] = time.time()
                    # E8: record build toward cross-regen ceiling
                    state.task_total_builds[target_task_id] = (
                        state.task_total_builds.get(target_task_id, 0) + 1
                    )
                    _e8_title_key = (
                        _normalize_title(next_task.get("title", ""))
                        if next_task
                        else ""
                    )
                    _worker_info["e8_title_key"] = _e8_title_key
                    if _e8_title_key:
                        state.title_total_builds[_e8_title_key] = (
                            state.title_total_builds.get(_e8_title_key, 0) + 1
                        )
                        # Guard against unbounded growth (mirrors _recent_spawn_at cap).
                        # Drop lowest-count entries when dict exceeds 500 keys.
                        if len(state.title_total_builds) > 500:
                            _tb_cutoff = sorted(state.title_total_builds.values())[50]
                            for _tb_key in [
                                k
                                for k, v in state.title_total_builds.items()
                                if v <= _tb_cutoff
                            ][:100]:
                                del state.title_total_builds[_tb_key]
                    if len(_recent_spawn_at) > 200:
                        _cd_cutoff = time.time() - 600
                        for _cd_key in [
                            k for k, v in _recent_spawn_at.items() if v < _cd_cutoff
                        ]:
                            del _recent_spawn_at[_cd_key]
                    with _active_workers_lock:
                        state._active_workers[target_task_id] = _worker_info
                        _active_count = len(state._active_workers)
                    logger.info(
                        f"[3/3 Execute] Worker spawned in worktree for {target_task_id} "
                        f"({_active_count}/{_max_workers} active)"
                    )
                    consecutive_idle_polls = 0
                    last_activity_time = time.time()

                    # Skip the rest of result handling — workers are collected next cycle
                    cycle_elapsed = time.time() - cycle_start
                    with _active_workers_lock:
                        _wk_count = len(state._active_workers)
                    logger.info(
                        f"--- Cycle {cycle_number} done ({cycle_elapsed:.0f}s) | "
                        f"lifetime: {state.tasks_completed} completed, {state.tasks_failed} failed | "
                        f"workers: {_wk_count}/{_max_workers} active ---"
                    )
                    time.sleep(min(poll_interval, 5))
                    continue

            # Direct execution fallback (only when worktree creation fails or no task_id)
            # WS-092: This path should be rare - worktrees are now always preferred
            # Only warn if there WAS a task but worktree failed; normal idle is not a warning
            if target_task_id:
                logger.warning(
                    f"[3/3 Execute] WS-092: Falling back to direct execution (no worktree). "
                    f"task_id={target_task_id}, reason=worktree_creation_failed"
                )
                # WS-106: Clean up reservation since direct execution will claim from scratch
                with _active_workers_lock:
                    if target_task_id in state._active_workers:
                        if (
                            state._active_workers[target_task_id].get("status")
                            == "reserving"
                        ):
                            del state._active_workers[target_task_id]
            result = operation_loop.poll_and_execute_once(
                queue_path=queue_path,
                agent_id=_daemon_agent_id,
                state_path=state_path,
                target_task_id=target_task_id,
            )

            action = result.get("action", "error")
            task_id = result.get("task_id")
            cycle_elapsed = time.time() - cycle_start

            if action == "executed":
                state.tasks_completed += 1
                state.current_task = None
                consecutive_idle_polls = 0
                last_activity_time = time.time()

                # Record success in circuit breaker
                loop_monitor.record_success(breaker, health_metrics)
                loop_monitor.save_monitor_state(breaker, health_metrics)

                # P36: Record activity for adaptive scheduler (resets idle backoff)
                if p25_scheduler:
                    try:
                        p25_scheduler.record_activity()
                    except Exception as e:
                        logger.debug(f"P36: Failed to record activity: {e}")

                # P25: Record outcome for learning
                if (
                    config.p25_enabled
                    and config.p25_learning_loop_enabled
                    and learning_loop_mod is not None
                ):
                    try:
                        emp_activation = result.get("employee_activation", {}) or {}
                        actual_employee_id = emp_activation.get("employee_id", "")
                        task_details = result.get("task", {}) or {}
                        actual_complexity = task_details.get(
                            "estimated_complexity",
                            task_details.get("complexity", "standard"),
                        )
                        outcome = learning_loop_mod.OutcomeRecord(
                            task_id=task_id or "",
                            employee_id=actual_employee_id,
                            complexity=actual_complexity,
                            success=True,
                            first_pass=result.get("revision_count", 0) == 0,
                            revision_count=result.get("revision_count", 0),
                            plan_score=result.get("plan_score", 0),
                            execution_minutes=cycle_elapsed / 60.0,
                            tokens_used=result.get("tokens_used", 0),
                            recorded_at=datetime.now(timezone.utc),
                            task_type=task_details.get("source", ""),
                        )
                        learning_loop_mod.record_outcome(outcome)
                        state.p25_outcomes_recorded += 1
                    except Exception as e:
                        logger.debug(f"P25: Failed to record outcome: {e}")

                # P32: Track post-completion proposals
                post_comp = result.get("post_completion_proposal")
                if post_comp and post_comp.get("success"):
                    state.post_completion_proposals_submitted += 1
                    logger.info(
                        f"P32: Post-completion proposal submitted by "
                        f"{result.get('employee_id', 'unknown')}"
                    )

                # Log detailed task completion
                logger.info(f"[3/3 Execute] {format_task_log(result, action)}")

                # Persist structured task result to .company/results/
                if task_result_writer:
                    try:
                        task_result_writer.write_task_result(
                            result, action, cycle_elapsed, company_dir
                        )
                    except Exception as e:
                        logger.debug(f"Task result writer: {e}")

                    # WS-106-005: Extract patterns from successful completions
                    try:
                        from pattern_extractor import extract_and_save

                        _pat_result = extract_and_save(company_dir, task_id=task_id)
                        if _pat_result.get("new_patterns", 0) > 0:
                            logger.info(
                                f"[Execute] WS-106-005: Extracted "
                                f"{_pat_result['new_patterns']} new patterns from {task_id}"
                            )
                    except ImportError:
                        pass
                    except Exception as _pat_err:
                        logger.debug(
                            f"WS-106-005: Pattern extraction failed: {_pat_err}"
                        )

                # WS-106-003: Record successful resolution for pattern learning
                try:
                    from escalation_router import record_resolution

                    _seq_emp = (
                        result.get("employee_id")
                        or result.get("assigned_to")
                        or (result.get("employee_activation") or {}).get("employee_id")
                    )
                    if task_id and _seq_emp:
                        record_resolution(task_id, _seq_emp, "resolved")
                except ImportError:
                    pass
                except Exception as _rec_err:
                    logger.debug(f"WS-106-003: Resolution record failed: {_rec_err}")

            elif action == "idle":
                consecutive_idle_polls += 1
                state.current_task = None

                # P36: Record idle cycle for adaptive scheduler backoff
                if p25_scheduler:
                    try:
                        p25_scheduler.record_idle_cycle()
                        backoff_status = p25_scheduler.get_backoff_status()
                        if backoff_status.get("current_backoff_multiplier", 1) > 1:
                            logger.info(
                                f"P36: Backoff active - {backoff_status['consecutive_idle_cycles']} "
                                f"idle cycles, next poll in {backoff_status['effective_poll_interval']}s"
                            )
                    except Exception as e:
                        logger.debug(f"P36: Failed to record idle cycle: {e}")

                # Check idle timeout
                idle_seconds = time.time() - last_activity_time
                if (
                    config.idle_timeout_seconds > 0
                    and idle_seconds >= config.idle_timeout_seconds
                ):
                    logger.info(
                        f"Idle timeout reached ({idle_seconds:.0f}s >= {config.idle_timeout_seconds}s), "
                        f"shutting down"
                    )
                    break

                logger.info(
                    f"[3/3 Execute] No pending tasks — idle (cycle {cycle_number} took {cycle_elapsed:.0f}s)"
                )

            elif action == "failed":
                state.current_task = None
                consecutive_idle_polls = 0

                error_msg = result.get("reason", "Unknown error")

                # WS-105: Attempt autonomous failure recovery before counting as failure
                _seq_recovery_success = False
                if failure_recovery_mod and task_id:
                    try:
                        _seq_failed_task = result.get("task", {}) or {}
                        if not _seq_failed_task.get("task_id"):
                            _seq_failed_task["task_id"] = task_id
                        _seq_exit_code = result.get("exit_code")
                        _seq_recoverer = failure_recovery_mod.RecoveryOrchestrator(
                            company_dir
                        )
                        _seq_rec_result = _seq_recoverer.attempt_recovery(
                            task=_seq_failed_task,
                            error_msg=error_msg,
                            exit_code=_seq_exit_code,
                            queue_path=queue_path,
                        )
                        if _seq_rec_result.success:
                            _seq_recovery_success = True
                            logger.info(
                                f"[WS-105] Autonomous recovery for {task_id}: "
                                f"strategy={_seq_rec_result.strategy_used.value}, "
                                f"attempt={_seq_rec_result.recovery_attempt_num}"
                            )
                    except Exception as _seq_rec_exc:
                        logger.debug(
                            f"[WS-105] Recovery attempt failed: {_seq_rec_exc}"
                        )

                # If recovery succeeded, skip failure recording
                if _seq_recovery_success:
                    # WS-118: MARK_COMPLETE means task already succeeded — complete it
                    if (
                        _seq_rec_result
                        and _seq_rec_result.strategy_used
                        == failure_recovery_mod.RecoveryStrategy.MARK_COMPLETE
                    ):
                        state.tasks_completed += 1
                        loop_monitor.record_success(breaker, health_metrics)
                        loop_monitor.save_monitor_state(breaker, health_metrics)
                        try:
                            operation_loop.release_task(
                                queue_path=queue_path,
                                task_id=task_id,
                                result="completed",
                            )
                        except Exception as _rel_err:
                            logger.debug(f"[WS-118] release_task failed: {_rel_err}")
                        logger.info(
                            f"[3/3 Execute] Task {task_id} recovery: "
                            f"goal already achieved — marked complete"
                        )
                    else:
                        logger.info(
                            f"[3/3 Execute] Task {task_id} recovered - re-queued for retry"
                        )
                else:
                    # Normal failure path - recovery failed or not attempted
                    state.tasks_failed += 1

                    # Record failure in circuit breaker.
                    # P-G7: Skip circuit breaker for pr_test_failure and WS-119 1.8
                    # phantom-guard failures — these are task quality issues, not
                    # daemon health failures.
                    _is_pr_test_failure = "Tests failed" in error_msg and (
                        "PR creation failed" in error_msg
                        or "pr_test_failure" in error_msg
                    )
                    _is_phantom_guard = (
                        "no deliverable" in error_msg.lower()
                        or "WS-119 1.8" in error_msg
                        or "no PR was created" in error_msg
                    )
                    if not (_is_pr_test_failure or _is_phantom_guard):
                        loop_monitor.record_failure(
                            breaker, health_metrics, error_msg, breaker_config
                        )
                    loop_monitor.save_monitor_state(breaker, health_metrics)

                    # P25: Record failure outcome for learning
                    if (
                        config.p25_enabled
                        and config.p25_learning_loop_enabled
                        and learning_loop_mod is not None
                    ):
                        try:
                            # P53 FIX: Extract employee_id from nested employee_activation dict
                            emp_activation = result.get("employee_activation", {}) or {}
                            actual_employee_id = emp_activation.get("employee_id", "")

                            task_details = result.get("task", {}) or {}
                            actual_complexity = task_details.get(
                                "estimated_complexity",
                                task_details.get("complexity", "standard"),
                            )

                            outcome = learning_loop_mod.OutcomeRecord(
                                task_id=task_id or "",
                                employee_id=actual_employee_id,
                                complexity=actual_complexity,
                                success=False,
                                first_pass=False,
                                revision_count=result.get("revision_count", 0),
                                plan_score=result.get("plan_score", 0),
                                execution_minutes=cycle_elapsed / 60.0,
                                tokens_used=result.get("tokens_used", 0),
                                recorded_at=datetime.now(timezone.utc),
                                task_type=task_details.get("source", ""),
                            )
                            learning_loop_mod.record_outcome(outcome)
                            state.p25_outcomes_recorded += 1
                        except Exception as e:
                            logger.debug(f"P25: Failed to record failure outcome: {e}")

                    # Log detailed task failure
                    logger.error(f"[3/3 Execute] {format_task_log(result, action)}")

                    # Persist structured task result to .company/results/
                    if task_result_writer:
                        try:
                            task_result_writer.write_task_result(
                                result, action, cycle_elapsed, company_dir
                            )
                        except Exception as e:
                            logger.debug(f"Task result writer: {e}")

                    # WS-106-003: Record failure for pattern learning
                    try:
                        from escalation_router import record_resolution

                        _seq_emp = (
                            result.get("employee_id")
                            or result.get("assigned_to")
                            or (result.get("employee_activation") or {}).get(
                                "employee_id"
                            )
                        )
                        if task_id and _seq_emp:
                            record_resolution(task_id, _seq_emp, "failed")
                    except ImportError:
                        pass
                    except Exception as _rec_err:
                        logger.debug(f"WS-106-003: Failure record failed: {_rec_err}")

            elif action == "escalated":
                state.current_task = None
                consecutive_idle_polls = 0

                # Log detailed escalation
                logger.warning(f"[3/3 Execute] {format_task_log(result, action)}")

                # Persist structured task result to .company/results/
                if task_result_writer:
                    try:
                        task_result_writer.write_task_result(
                            result, action, cycle_elapsed, company_dir
                        )
                    except Exception as e:
                        logger.debug(f"Task result writer: {e}")

            elif action == "blocked":
                # Phase 2 honesty: the pre-merge deliverable gate held this task's
                # PR for manual review. The PR exists and is labelled
                # needs-manual-review. This is neither a success (must not inflate
                # completion metrics nor record a "resolved" pattern) nor a failure
                # (no retry, no failure-recovery — the work is fine; a human just
                # reviews/merges the held PR). Record it as neither.
                state.current_task = None
                consecutive_idle_polls = 0
                logger.warning(f"[3/3 Execute] {format_task_log(result, action)}")

                # Persist structured task result to .company/results/
                if task_result_writer:
                    try:
                        task_result_writer.write_task_result(
                            result, action, cycle_elapsed, company_dir
                        )
                    except Exception as e:
                        logger.debug(f"Task result writer: {e}")

            elif action == "claim_failed":
                logger.debug(
                    f"[3/3 Execute] Claim failed for task {task_id}: {result.get('reason')}"
                )

            else:
                logger.warning(f"[3/3 Execute] Unknown action: {action}")

            # Cycle summary
            logger.info(
                f"--- Cycle {cycle_number} done ({cycle_elapsed:.0f}s) | "
                f"lifetime: {state.tasks_completed} completed, {state.tasks_failed} failed | "
                f"planning: {state.tasks_planned} planned, {state.tasks_direct} direct ---"
            )

            # P25: Periodic session snapshot capture
            if (
                config.p25_enabled
                and config.p25_session_continuity_enabled
                and p25_continuity
                and time.time() - last_snapshot_time
                >= config.p25_snapshot_interval_seconds
            ):
                try:
                    p25_continuity.capture_snapshot(
                        daemon_state=state.to_dict(),
                        reason="periodic",
                    )
                    last_snapshot_time = time.time()
                    state.last_session_snapshot = datetime.now(timezone.utc).isoformat()
                    logger.debug("P25: Session snapshot captured")
                except Exception as e:
                    logger.debug(f"P25: Snapshot capture failed: {e}")

            # Sleep before next poll (use adaptive interval if available).
            # Interruptible: a stop must not wait out the idle backoff.
            _sleep_unless_shutdown(poll_interval)

        except Exception as e:
            logger.error(f"Loop iteration failed: {e}", exc_info=True)

            # Record failure in circuit breaker
            try:
                breaker, health_metrics = loop_monitor.load_monitor_state()
                loop_monitor.record_failure(
                    breaker, health_metrics, str(e), breaker_config
                )
                loop_monitor.save_monitor_state(breaker, health_metrics)
            except Exception:
                pass

            # Error backoff
            _sleep_unless_shutdown(config.poll_interval_seconds * 2)

    # WS-057-002: Wait for active workers to finish
    if hasattr(state, "_active_workers") and state._active_workers:
        with _active_workers_lock:
            _shutdown_workers = list(state._active_workers.items())
        logger.info(f"Waiting for {len(_shutdown_workers)} active workers to finish...")
        # E1: worker sessions run in their own process group, so they no
        # longer die with this process (or with launchd's teardown of the
        # job's group). One shared deadline for all of them, then every one
        # still running — or adopted, which has no thread — has its group
        # killed at once, BEFORE the worktree removal below pulls a worktree
        # out from under a live session.
        _stop_workers_for_shutdown(_shutdown_workers)
        for _wid, _worker in _shutdown_workers:
            _wt_path = _worker.get("worktree_path")
            if _wt_path:
                try:
                    # Serialize removal to prevent index.lock contention
                    with _worktree_creation_lock:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", _wt_path],
                            capture_output=True,
                            timeout=15,
                        )
                except Exception:
                    pass
        # Backstop: a second registry pass after the worktree removal, for a
        # session recorded after the first pass (the spawn gate should make
        # that impossible; a gate bypassed some other way is still caught).
        _late = _kill_surviving_worker_pids([_wid for _wid, _ in _shutdown_workers])
        if _late:
            logger.warning(
                f"Shutdown: backstop pass killed {_late} session group(s) "
                "started after the first pass"
            )
        logger.info("All workers finished or timed out")

    # WS-057: Wait for discovery thread to finish
    if _discovery_thread and _discovery_thread.is_alive():
        logger.info("Waiting for discovery thread to finish...")
        _discovery_thread.join(timeout=30)
        if _discovery_thread.is_alive():
            logger.warning("Discovery thread did not finish within 30s timeout")

    # P25: Capture shutdown snapshot
    if config.p25_enabled and config.p25_session_continuity_enabled and p25_continuity:
        try:
            p25_continuity.capture_snapshot(
                daemon_state=state.to_dict(),
                reason="shutdown",
            )
            logger.info("P25: Shutdown snapshot captured")
        except Exception as e:
            logger.debug(f"P25: Shutdown snapshot failed: {e}")

    # Graceful shutdown
    logger.info(
        f"Daemon loop shutdown complete "
        f"(tasks_completed={state.tasks_completed}, tasks_failed={state.tasks_failed}, "
        f"tasks_planned={state.tasks_planned}, tasks_direct={state.tasks_direct}, "
        f"planning_failures={state.planning_failures})"
    )

    # Log P25 metrics
    if config.p25_enabled:
        logger.info(
            f"P25 metrics: outcomes_recorded={state.p25_outcomes_recorded}, "
            f"auto_approvals={state.p25_auto_approvals}, "
            f"queue_reorders={state.p25_queue_reorders}, "
            f"mode={state.p25_schedule_mode}, throttle={state.p25_throttle_level}"
        )

    # Save final metrics
    try:
        breaker, health_metrics = loop_monitor.load_monitor_state()
        loop_monitor.save_monitor_state(breaker, health_metrics)
    except Exception as e:
        logger.warning(f"Failed to save final metrics: {e}")

    # Accumulate session uptime into session_state.json for G7 tracking.
    # This ensures uptime persists across daemon restarts so G7 can measure
    # cumulative uptime toward the 7-day target.
    try:
        session_uptime = 0.0
        try:
            started = datetime.fromisoformat(state.started_at.replace("Z", "+00:00"))
            session_uptime = (datetime.now(timezone.utc) - started).total_seconds()
        except Exception:
            pass

        if session_uptime > 0 and unified_state_mgr:
            unified_st = unified_state_mgr.load()
            unified_st.total_uptime_seconds += session_uptime
            unified_state_mgr.save(unified_st)
            logger.info(
                f"Accumulated {session_uptime:.0f}s session uptime → "
                f"total_uptime_seconds={unified_st.total_uptime_seconds:.0f}"
            )
    except Exception as e:
        logger.warning(f"Failed to accumulate session uptime: {e}")

    return 0


# -----------------------------------------------------------------------------
# LaunchAgent Helpers
# -----------------------------------------------------------------------------


def _get_launchagent_path(config: DaemonConfig) -> Path | None:
    """Get the LaunchAgent plist path for this project, if it exists.

    The plist directory comes from ``config.launchagent_dir`` (default
    ~/Library/LaunchAgents) rather than being hardcoded, so tests can point it
    at a fixture dir. With a fixture dir the plist never exists, this returns
    None, and every launchctl load/unload path is skipped — so running the test
    suite can never unload the operator's real production daemon.
    """
    try:
        from company_resolver import get_project_id

        project_root = Path(__file__).resolve().parent.parent.parent.parent
        project_id = get_project_id(project_root)
        plist_path = config.launchagent_dir / f"dev.gatesmith.daemon.{project_id}.plist"
        return plist_path if plist_path.exists() else None
    except ImportError:
        return None


def _is_launchagent_loaded(plist_path: Path) -> bool:
    """Check if a LaunchAgent is currently loaded."""
    project_id = plist_path.stem.split(".")[-1]  # Extract project ID from filename
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
    )
    return f"dev.gatesmith.daemon.{project_id}" in result.stdout


def _unload_launchagent(plist_path: Path) -> bool:
    """Unload LaunchAgent. Returns True if successful."""
    result = subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _load_launchagent(plist_path: Path) -> bool:
    """Load LaunchAgent. Returns True if successful."""
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# -----------------------------------------------------------------------------
# Daemon Commands
# -----------------------------------------------------------------------------


def _check_maintenance_mode() -> tuple[bool, str]:
    """Check if maintenance mode is enabled.

    Returns:
        Tuple of (is_enabled, reason)
    """
    maintenance_file = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".company"
        / "maintenance_mode.json"
    )
    if not maintenance_file.exists():
        return False, ""
    try:
        with open(maintenance_file) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return True, "Maintenance lock is malformed"
        # A lock that exists is ON unless it says "enabled": false, the same
        # rule as bin/gatesmith-maintenance status ({} kept status ON while the
        # daemon started; 2026-10-31).
        if data.get("enabled") is False:
            return False, ""
        return True, data.get("reason", "Maintenance mode active")
    except Exception:
        # A lock that exists but cannot be read keeps the daemon out, as in
        # bin/gatesmith-maintenance: fail closed, never open (book review 2026-10-30).
        return True, "Maintenance lock is unreadable"


def start_daemon(config: DaemonConfig) -> int:
    """Start the daemon process.

    If a LaunchAgent is installed, loads it instead of starting directly.
    This ensures proper auto-restart behavior.

    Args:
        config: Daemon configuration.

    Returns:
        Exit code (0 for success, 1 for error, 2 if already running).
    """
    # Check maintenance mode - block startup if core files are being modified
    in_maintenance, reason = _check_maintenance_mode()
    if in_maintenance:
        print(f"BLOCKED: {reason}", file=sys.stderr)
        print("Daemon cannot start while maintenance mode is enabled.", file=sys.stderr)
        print("To disable: ./bin/gatesmith-maintenance off", file=sys.stderr)
        return 1

    # Check if already running (with stale PID detection)
    state = read_pid_file(config)
    if state is not None:
        if is_daemon_state_alive(state):
            print(f"Daemon already running (pid={state.pid})", file=sys.stderr)
            return 2
        else:
            # Stale PID file — process gone, or its PID reused after a reboot
            reason = describe_stale_pid(state)
            logger.warning(f"Stale PID file detected ({reason}), cleaning up")
            print(
                f"Stale PID file detected ({reason}), cleaning up",
                file=sys.stderr,
            )
            remove_pid_file(config)
    elif config.pid_file.exists():
        # PID file exists but couldn't be parsed — clean up
        remove_pid_file(config)

    # Check for LaunchAgent — if installed, use it instead of direct start
    # (but not in foreground/non-daemonize mode)
    plist_path = _get_launchagent_path(config)
    if plist_path and config.daemonize:
        if _is_launchagent_loaded(plist_path):
            print("LaunchAgent already loaded, daemon should be running")
            return 2
        print(f"Loading LaunchAgent: {plist_path.name}")
        if _load_launchagent(plist_path):
            # Wait for daemon to start
            time.sleep(2)
            state = read_pid_file(config)
            if state and is_daemon_state_alive(state):
                print(f"Daemon started via LaunchAgent (pid={state.pid})")
                return 0
            else:
                print("LaunchAgent loaded but daemon didn't start - check logs")
                return 1
        else:
            print("Failed to load LaunchAgent, falling back to direct start")

    # Setup logging before daemonizing
    setup_logging(config)

    # Security: Refuse to run as root
    if os.getuid() == 0:
        logger.error("Gatesmith daemon cannot run as root (UID=0)")
        print("Error: Gatesmith daemon cannot run as root", file=sys.stderr)
        return 1

    logger.info("Starting Gatesmith daemon")

    if config.daemonize:
        # Daemonize process
        child_pid = daemonize()

        if child_pid > 0:
            # Parent process - wait briefly then verify child started
            time.sleep(0.5)
            if is_daemon_running(config):
                print(f"Daemon started (pid={child_pid})")
                return 0
            else:
                print("Daemon failed to start", file=sys.stderr)
                return 1

        # Daemon process continues below
        # Re-setup logging after daemonizing (file descriptors changed)
        setup_logging(config)

    # A marker left by an earlier `stop` must not make THIS process honour a
    # spurious launchd SIGTERM. Clear it, then install the handlers.
    stop_marker = _stop_marker_path(config)
    try:
        stop_marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not clear stale stop marker {stop_marker}: {e}")

    # Setup signal handlers
    setup_signal_handlers(stop_marker=stop_marker)

    # Write PID file
    pid = os.getpid()
    try:
        write_pid_file(pid, config)
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")
        return 1

    # Register cleanup on exit
    atexit.register(lambda: remove_pid_file(config))

    logger.info(f"Daemon process running (pid={pid})")

    # ProjectK root-cause fix (master switch): heal any bare-string employees
    # persisted in org.json BEFORE the supervised loop runs, so no autonomous
    # cycle (manager review, proactive scan, rebalancing, employee initiative,
    # …) crashes on an existing bad install. company_resolver.load_org(heal=True)
    # normalizes and re-persists dict records only when bare strings are present
    # (no-op otherwise), which also neutralizes the bypass-writer re-persist
    # path. Wrapped so a heal failure can never block daemon startup.
    try:
        _ensure_imports()
        company_resolver.load_org(heal=True)
    except Exception as _heal_err:
        logger.warning(f"Startup org.json normalization heal skipped: {_heal_err}")

    # Record daemon start in metrics (module-level so update_heartbeat() can checkpoint)
    global _g_metrics_tracker, _g_session_id
    _g_metrics_tracker = (
        _DaemonMetricsTracker(config.metrics_file)
        if _DaemonMetricsTracker is not None
        else None
    )
    _g_session_id = None
    if _g_metrics_tracker is not None:
        try:
            _g_session_id = _g_metrics_tracker.record_start(pid)
            logger.info(f"Daemon metrics: session {_g_session_id} started")
        except Exception as _e:
            logger.warning(f"Daemon metrics record_start failed: {_e}")

    # P22 Fix 4: Supervised execution with automatic restart
    restart_count = 0
    exit_code = 0

    while True:
        try:
            # Run main loop
            exit_code = run_daemon_loop(config)

            # Normal exit (shutdown requested) - don't restart
            if exit_code == 0:
                logger.info("Daemon loop exited normally")
                break

            # Check if we should restart
            if not config.restart_on_crash:
                logger.info("Restart on crash disabled, exiting")
                break

            if restart_count >= config.max_restarts:
                logger.error(f"Max restarts ({config.max_restarts}) reached, giving up")
                break

            # Restart with delay
            restart_count += 1
            logger.warning(
                f"Daemon loop crashed (exit={exit_code}), "
                f"restarting in {config.restart_delay_seconds}s "
                f"(attempt {restart_count}/{config.max_restarts})"
            )

            # Record restart event before sleeping
            if _g_metrics_tracker is not None and _g_session_id is not None:
                try:
                    _g_metrics_tracker.record_restart(
                        _g_session_id,
                        exit_code=exit_code,
                        delay_seconds=config.restart_delay_seconds,
                        reason="crash",
                    )
                    logger.info("Daemon metrics: restart event recorded (crash)")
                except Exception as _e:
                    logger.warning(f"Daemon metrics record_restart failed: {_e}")
                # Open a fresh session for the upcoming restart
                try:
                    _g_session_id = _g_metrics_tracker.record_start(pid)
                except Exception as _e:
                    logger.warning(
                        f"Daemon metrics record_start (post-restart) failed: {_e}"
                    )
                    _g_session_id = None

            time.sleep(config.restart_delay_seconds)

            # Reset shutdown flag for restart
            global _shutdown_requested
            _shutdown_requested = False

        except Exception as e:
            logger.error(f"Daemon loop exception: {e}")

            if not config.restart_on_crash:
                break

            if restart_count >= config.max_restarts:
                logger.error(f"Max restarts ({config.max_restarts}) reached, giving up")
                break

            restart_count += 1
            logger.warning(
                f"Restarting after exception in {config.restart_delay_seconds}s "
                f"(attempt {restart_count}/{config.max_restarts})"
            )

            # Record restart event before sleeping
            if _g_metrics_tracker is not None and _g_session_id is not None:
                try:
                    _g_metrics_tracker.record_restart(
                        _g_session_id,
                        exit_code=-1,
                        delay_seconds=config.restart_delay_seconds,
                        reason="exception",
                    )
                    logger.info("Daemon metrics: restart event recorded (exception)")
                except Exception as _e:
                    logger.warning(f"Daemon metrics record_restart failed: {_e}")
                # Open a fresh session for the upcoming restart
                try:
                    _g_session_id = _g_metrics_tracker.record_start(pid)
                except Exception as _e:
                    logger.warning(
                        f"Daemon metrics record_start (post-restart) failed: {_e}"
                    )
                    _g_session_id = None

            time.sleep(config.restart_delay_seconds)

            _shutdown_requested = False

    # Record clean shutdown
    if _g_metrics_tracker is not None and _g_session_id is not None:
        try:
            _g_metrics_tracker.record_stop(_g_session_id, reason="shutdown")
            logger.info("Daemon metrics: session stopped (shutdown)")
        except Exception as _e:
            logger.warning(f"Daemon metrics record_stop failed: {_e}")

    # Cleanup
    remove_pid_file(config)

    return exit_code


# How long `stop` waits for a graceful exit before SIGKILL. launchd's own
# ceiling is the plist's ExitTimeOut (300 s); the margin lets launchd's kill
# land first when the LaunchAgent is in play. An IDLE daemon exits within a
# couple of seconds — the wait only runs long while the shutdown sequence joins
# in-flight workers (up to 60 s each, WS-057-002).
STOP_GRACE_SECONDS: float = 330.0
STOP_PROGRESS_EVERY_SECONDS: float = 10.0
# Head start for `launchctl unload` before SIGUSR2: the unload must be
# registered before the daemon can exit, or KeepAlive respawns it.
STOP_UNLOAD_HEAD_START_SECONDS: float = 1.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_stop_marker(marker: Path) -> bool:
    """Leave the marker _handle_sigterm checks. Never creates directories: the
    marker sits beside the pid file, whose directory a running daemon made."""
    try:
        if not marker.parent.is_dir():
            return False
        marker.write_text(
            json.dumps(
                {
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "by_pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        return True
    except OSError as e:
        logger.warning(f"Could not write stop marker {marker}: {e}")
        return False


def _remove_stop_marker(marker: Path) -> None:
    try:
        marker.unlink()
    except OSError:
        pass


def _start_launchagent_unload(plist_path: Path) -> Any:
    """Start `launchctl unload` WITHOUT waiting for it.

    launchctl unload sends the job SIGTERM and blocks until the process exits
    or launchd SIGKILLs it at ExitTimeOut. Measured 2026-09-25 with a fake job
    that ignores SIGTERM (ExitTimeOut 20): unload returned after 20.05 s and
    the job died by SIGKILL; with a job that exits on SIGTERM it returned in
    2.2 s. Running it in the background lets `stop` report progress and send
    SIGUSR2 while launchd is already removing the job.
    """
    try:
        return subprocess.Popen(
            ["launchctl", "unload", str(plist_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        print(f"Warning: could not run launchctl unload: {e}", file=sys.stderr)
        return None


def stop_daemon(config: DaemonConfig, grace_seconds: float | None = None) -> int:
    """Stop the daemon process, gracefully and without a fixed 5-minute wait.

    Order (2026-09-25):
      1. Write the stop marker beside the pid file, so the daemon honours the
         SIGTERM launchd is about to send (it ignores SIGTERM otherwise, WS-110).
      2. Start `launchctl unload` in the background (LaunchAgent only), so
         KeepAlive cannot respawn the daemon once it exits.
      3. Send SIGUSR2 — the explicit stop for a daemon that predates the marker
         and for a daemon not run by launchd.
      4. Wait with progress output until the process is gone and the unload has
         returned, bounded by ``grace_seconds``; SIGKILL only past that bound.

    Before this order, the synchronous unload came first: launchd's SIGTERM was
    ignored, unload blocked for the plist's full ExitTimeOut (300 s), launchd
    SIGKILLed the daemon, and the graceful SIGUSR2 path never ran (no worker
    wait, no worktree cleanup, a stale pid file left behind).

    Args:
        config: Daemon configuration.
        grace_seconds: Bound on the graceful wait (default STOP_GRACE_SECONDS).

    Returns:
        Exit code (0 for success, 1 for error, 2 if not running).
    """
    grace = STOP_GRACE_SECONDS if grace_seconds is None else grace_seconds
    plist_path = _get_launchagent_path(config)
    agent_loaded = bool(plist_path) and _is_launchagent_loaded(plist_path)

    state = read_pid_file(config)
    pid: int | None = None
    if state is not None:
        if is_daemon_state_alive(state):
            pid = state.pid
        else:
            # Stale PID file — process gone, or its PID reused after a reboot
            reason = describe_stale_pid(state)
            logger.warning(f"Stale PID file detected ({reason}), cleaning up")
            print(
                f"Stale PID file detected ({reason}), cleaning up",
                file=sys.stderr,
            )
            remove_pid_file(config)

    if pid is None and not agent_loaded:
        if state is None:
            print("Daemon is not running", file=sys.stderr)
        return 2

    marker = _stop_marker_path(config)
    _write_stop_marker(marker)
    started = time.monotonic()

    unload_proc = None
    if agent_loaded:
        print(
            f"Unloading LaunchAgent {plist_path.name} (prevents auto-restart; "
            "the daemon shuts down gracefully on launchd's SIGTERM)..."
        )
        unload_proc = _start_launchagent_unload(plist_path)

    if pid is not None:
        print(f"Stopping daemon (pid={pid})...")
        if unload_proc is not None:
            time.sleep(STOP_UNLOAD_HEAD_START_SECONDS)
        try:
            os.kill(pid, signal.SIGUSR2)
        except ProcessLookupError:
            pass  # already gone — the wait below confirms it
        except PermissionError:
            print(f"Permission denied to stop daemon (pid={pid})", file=sys.stderr)
            _remove_stop_marker(marker)
            return 1

    # Bounded wait with progress: the process gone AND the unload returned.
    next_progress = STOP_PROGRESS_EVERY_SECONDS
    while True:
        pid_gone = pid is None or not _pid_alive(pid)
        unload_done = unload_proc is None or unload_proc.poll() is not None
        if pid_gone and unload_done:
            break
        elapsed = time.monotonic() - started
        if elapsed >= grace:
            break
        if elapsed >= next_progress:
            waiting_on = (
                "daemon shutdown (joins in-flight workers, up to 60 s each)"
                if not pid_gone
                else "launchctl unload"
            )
            print(f"  ... still stopping after {elapsed:.0f}s: waiting on {waiting_on}")
            next_progress += STOP_PROGRESS_EVERY_SECONDS
        time.sleep(0.5)

    forced = False
    if pid is not None and _pid_alive(pid):
        print(
            f"Graceful shutdown did not finish within {grace:.0f}s, sending SIGKILL..."
        )
        try:
            os.kill(pid, signal.SIGKILL)
            forced = True
            time.sleep(1)
        except ProcessLookupError:
            pass

    if unload_proc is not None:
        try:
            unload_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("Warning: launchctl unload has not returned", file=sys.stderr)
        if unload_proc.returncode == 0:
            print(f"LaunchAgent unloaded: {plist_path.name}")
        elif unload_proc.returncode is not None:
            print("Warning: Failed to unload LaunchAgent", file=sys.stderr)

    _remove_stop_marker(marker)
    if pid is None:
        print("Daemon is not running", file=sys.stderr)
        return 2
    remove_pid_file(config)
    elapsed = time.monotonic() - started
    if forced:
        print(f"Daemon stopped (forced, after {elapsed:.0f}s)")
    else:
        print(f"Daemon stopped gracefully ({elapsed:.0f}s)")
    return 0


def daemon_status(config: DaemonConfig, registry: Any = None) -> dict[str, Any]:
    """Get current daemon status.

    Args:
        config: Daemon configuration.
        registry: Optional SubsystemRegistry for aggregated status sections.
            When provided, nested sections (cross_project, strategic_planning,
            etc.) are generated from the registry. When absent, uses inline
            defaults for backward compatibility.

    Returns:
        Status dictionary with health information.
    """
    # Base fields (not owned by any subsystem)
    result: dict[str, Any] = {
        "running": False,
        "pid": None,
        "uptime_seconds": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "current_task": None,
        "last_heartbeat": None,
        "circuit_breaker_state": "unknown",
        "last_proactive_scan": None,
        "proactive_proposals_created": 0,
    }

    # Subsystem sections — from registry if available, otherwise inline
    if registry is not None:
        result.update(registry.aggregate_status())
        # Ensure pr_workflow section exists (not managed by a subsystem)
        if "pr_workflow" not in result:
            result["pr_workflow"] = {
                "enabled": True,
                "auto_push_branches": True,
                "auto_create_pr": True,
                "require_tests": True,
            }
    else:
        # Legacy fallback: inline section construction
        result.update(
            {
                "cross_project": {
                    "enabled": config.cross_project_enabled,
                    "tasks_routed": 0,
                    "tasks_queued_for_approval": 0,
                    "last_rebalance_check": None,
                    "rebalance_proposals": 0,
                    "cross_project_task_count": 0,
                    "employee_distribution": {},
                },
                "strategic_planning": {
                    "enabled": config.strategic_planning_enabled,
                    "last_run": None,
                    "initiatives_created": 0,
                    "tasks_queued": 0,
                },
                "document_approvals": {
                    "enabled": config.document_scan_enabled,
                    "last_scan": None,
                    "documents_discovered": 0,
                    "documents_pending": 0,
                },
                "roadmap_scheduling": {
                    "enabled": config.roadmap_scheduling_enabled,
                    "last_scan": None,
                    "tasks_scheduled": 0,
                    "tasks_completed": 0,
                    "current_wave": 1,
                },
                "pr_workflow": {
                    "enabled": True,
                    "auto_push_branches": True,
                    "auto_create_pr": True,
                    "require_tests": True,
                },
                "p25_autonomy": {
                    "enabled": config.p25_enabled,
                    "adaptive_scheduling": config.p25_adaptive_scheduling_enabled,
                    "session_continuity": config.p25_session_continuity_enabled,
                    "approval_learning": config.p25_approval_learning_enabled,
                    "goal_scheduling": config.p25_goal_scheduling_enabled,
                    "budget_governor": config.p25_budget_governor_enabled,
                    "learning_loop": config.p25_learning_loop_enabled,
                    "schedule_mode": "NORMAL",
                    "throttle_level": "NORMAL",
                    "last_snapshot": None,
                    "last_goal_refresh": None,
                    "last_queue_reorder": None,
                    "outcomes_recorded": 0,
                    "auto_approvals": 0,
                    "queue_reorders": 0,
                },
                "executive_loop": {
                    "enabled": config.executive_loop_enabled,
                    "interval_hours": config.executive_loop_interval_hours,
                    "last_run": None,
                    "sessions_run": 0,
                    "work_submitted": 0,
                },
                "improvement_cycle": {
                    "enabled": config.improvement_enabled,
                    "interval_hours": config.improvement_cycle_interval_hours,
                    "last_run": None,
                    "cycles_run": 0,
                    "next_scheduled": None,
                },
                "employee_ideation": {
                    "enabled": config.employee_ideation_enabled,
                    "interval_minutes": config.employee_ideation_interval_minutes,
                    "last_run": None,
                    "cycles_run": 0,
                    "ideas_generated": 0,
                },
                "auto_merge": {
                    "enabled": config.auto_merge_enabled,
                    "interval_minutes": config.auto_merge_interval_minutes,
                    "last_check": None,
                    "checks_run": 0,
                    "prs_merged": 0,
                },
                "scheduling_efficiency": {
                    "consecutive_idle_cycles": 0,
                    "wasted_polls_count": 0,
                    "current_backoff_multiplier": 1,
                    "effective_poll_interval": config.poll_interval_seconds,
                    "last_activity_time": None,
                    "is_business_hours": True,
                    "business_hours_mode_applied": False,
                    "ideation_cycles_total": 0,
                    "ideation_cycles_executed": 0,
                    "ideation_cycles_skipped": 0,
                    "ideation_skip_rate_percent": 0.0,
                    "ideation_ideas_generated": 0,
                    "ideation_ideas_per_cycle": 0.0,
                },
            }
        )

    # Check if running
    if not is_daemon_running(config):
        return result

    result["running"] = True

    # Get PID file state
    state = read_pid_file(config)
    if state:
        result["pid"] = state.pid
        result["tasks_completed"] = state.tasks_completed
        result["tasks_failed"] = state.tasks_failed
        result["current_task"] = state.current_task

        # Calculate uptime
        try:
            started = datetime.fromisoformat(state.started_at.replace("Z", "+00:00"))
            uptime = (datetime.now(timezone.utc) - started).total_seconds()
            result["uptime_seconds"] = int(uptime)
        except (ValueError, TypeError):
            pass

    # Get heartbeat info
    if config.heartbeat_file.exists():
        try:
            with open(config.heartbeat_file, encoding="utf-8") as f:
                heartbeat = json.load(f)
            result["last_heartbeat"] = heartbeat.get("last_heartbeat")
            result["circuit_breaker_state"] = heartbeat.get(
                "circuit_breaker_state", "unknown"
            )

            # Use heartbeat values if more recent than PID file
            if "tasks_completed_this_session" in heartbeat:
                result["tasks_completed"] = heartbeat["tasks_completed_this_session"]
            if "tasks_failed_this_session" in heartbeat:
                result["tasks_failed"] = heartbeat.get("tasks_failed_this_session", 0)
            if "current_task" in heartbeat:
                result["current_task"] = heartbeat["current_task"]
            if "uptime_seconds" in heartbeat:
                result["uptime_seconds"] = heartbeat["uptime_seconds"]
            if "last_proactive_scan" in heartbeat:
                result["last_proactive_scan"] = heartbeat["last_proactive_scan"]
            if "proactive_proposals_created" in heartbeat:
                result["proactive_proposals_created"] = heartbeat[
                    "proactive_proposals_created"
                ]

            # Cross-project metrics from heartbeat (P14)
            if "cross_project_tasks_routed" in heartbeat:
                result["cross_project"]["tasks_routed"] = heartbeat[
                    "cross_project_tasks_routed"
                ]
            if "cross_project_tasks_queued" in heartbeat:
                result["cross_project"]["tasks_queued_for_approval"] = heartbeat[
                    "cross_project_tasks_queued"
                ]
            if "last_rebalance_check" in heartbeat:
                result["cross_project"]["last_rebalance_check"] = heartbeat[
                    "last_rebalance_check"
                ]
            if "rebalance_proposals_created" in heartbeat:
                result["cross_project"]["rebalance_proposals"] = heartbeat[
                    "rebalance_proposals_created"
                ]

            # Strategic planning metrics from heartbeat (P15)
            if "last_strategic_planning" in heartbeat:
                result["strategic_planning"]["last_run"] = heartbeat[
                    "last_strategic_planning"
                ]
            if "strategic_initiatives_created" in heartbeat:
                result["strategic_planning"]["initiatives_created"] = heartbeat[
                    "strategic_initiatives_created"
                ]
            if "strategic_tasks_queued" in heartbeat:
                result["strategic_planning"]["tasks_queued"] = heartbeat[
                    "strategic_tasks_queued"
                ]

            # P29: Weekly/daily planning cycle metrics from heartbeat
            if "last_weekly_planning" in heartbeat:
                result["strategic_planning"]["last_weekly_planning"] = heartbeat[
                    "last_weekly_planning"
                ]
            if "last_daily_planning" in heartbeat:
                result["strategic_planning"]["last_daily_planning"] = heartbeat[
                    "last_daily_planning"
                ]
            if "weekly_planning_runs" in heartbeat:
                result["strategic_planning"]["weekly_planning_runs"] = heartbeat[
                    "weekly_planning_runs"
                ]
            if "daily_planning_runs" in heartbeat:
                result["strategic_planning"]["daily_planning_runs"] = heartbeat[
                    "daily_planning_runs"
                ]

            # Roadmap scheduling metrics from heartbeat (P27)
            if "last_roadmap_scan" in heartbeat:
                result["roadmap_scheduling"]["last_scan"] = heartbeat[
                    "last_roadmap_scan"
                ]
            if "roadmap_tasks_scheduled" in heartbeat:
                result["roadmap_scheduling"]["tasks_scheduled"] = heartbeat[
                    "roadmap_tasks_scheduled"
                ]
            if "roadmap_tasks_completed" in heartbeat:
                result["roadmap_scheduling"]["tasks_completed"] = heartbeat[
                    "roadmap_tasks_completed"
                ]
            if "roadmap_current_wave" in heartbeat:
                result["roadmap_scheduling"]["current_wave"] = heartbeat[
                    "roadmap_current_wave"
                ]

            # P25: Full Autonomous Operation metrics from heartbeat
            if "p25_schedule_mode" in heartbeat:
                result["p25_autonomy"]["schedule_mode"] = heartbeat["p25_schedule_mode"]
            if "p25_throttle_level" in heartbeat:
                result["p25_autonomy"]["throttle_level"] = heartbeat[
                    "p25_throttle_level"
                ]
            if "last_session_snapshot" in heartbeat:
                result["p25_autonomy"]["last_snapshot"] = heartbeat[
                    "last_session_snapshot"
                ]
            if "last_goal_refresh" in heartbeat:
                result["p25_autonomy"]["last_goal_refresh"] = heartbeat[
                    "last_goal_refresh"
                ]
            if "last_queue_reorder" in heartbeat:
                result["p25_autonomy"]["last_queue_reorder"] = heartbeat[
                    "last_queue_reorder"
                ]
            if "p25_outcomes_recorded" in heartbeat:
                result["p25_autonomy"]["outcomes_recorded"] = heartbeat[
                    "p25_outcomes_recorded"
                ]
            if "p25_auto_approvals" in heartbeat:
                result["p25_autonomy"]["auto_approvals"] = heartbeat[
                    "p25_auto_approvals"
                ]
            if "p25_queue_reorders" in heartbeat:
                result["p25_autonomy"]["queue_reorders"] = heartbeat[
                    "p25_queue_reorders"
                ]

            # P17: Executive loop metrics from heartbeat
            if "last_executive_loop" in heartbeat:
                result["executive_loop"]["last_run"] = heartbeat["last_executive_loop"]
            if "executive_sessions_run" in heartbeat:
                result["executive_loop"]["sessions_run"] = heartbeat[
                    "executive_sessions_run"
                ]
            if "work_submitted_by_executives" in heartbeat:
                result["executive_loop"]["work_submitted"] = heartbeat[
                    "work_submitted_by_executives"
                ]

            # P30: Self-improvement cycle metrics from heartbeat
            if "last_improvement_cycle" in heartbeat:
                result["improvement_cycle"]["last_run"] = heartbeat[
                    "last_improvement_cycle"
                ]
                # Calculate next scheduled run
                if heartbeat["last_improvement_cycle"]:
                    try:
                        last_run = datetime.fromisoformat(
                            heartbeat["last_improvement_cycle"].replace("Z", "+00:00")
                        )
                        next_run = last_run + timedelta(
                            hours=config.improvement_cycle_interval_hours
                        )
                        result["improvement_cycle"]["next_scheduled"] = (
                            next_run.isoformat()
                        )
                    except (ValueError, TypeError):
                        pass
            if "improvement_cycles_run" in heartbeat:
                result["improvement_cycle"]["cycles_run"] = heartbeat[
                    "improvement_cycles_run"
                ]

            # P34: Employee ideation metrics from heartbeat
            if "last_employee_ideation" in heartbeat:
                result["employee_ideation"]["last_run"] = heartbeat[
                    "last_employee_ideation"
                ]
            if "employee_ideation_cycles" in heartbeat:
                result["employee_ideation"]["cycles_run"] = heartbeat[
                    "employee_ideation_cycles"
                ]
            if "ideas_generated" in heartbeat:
                result["employee_ideation"]["ideas_generated"] = heartbeat[
                    "ideas_generated"
                ]

            # P35: Auto-merge metrics from heartbeat
            if "last_auto_merge_check" in heartbeat:
                result["auto_merge"]["last_check"] = heartbeat["last_auto_merge_check"]
            if "auto_merge_checks" in heartbeat:
                result["auto_merge"]["checks_run"] = heartbeat["auto_merge_checks"]
            if "prs_merged" in heartbeat:
                result["auto_merge"]["prs_merged"] = heartbeat["prs_merged"]

        except (json.JSONDecodeError, OSError):
            pass

    # P36: Scheduling efficiency metrics from adaptive_scheduler_state.json
    try:
        _ensure_imports()
        company_dir = company_resolver.get_company_dir()
        scheduler_state_path = company_dir / "state/adaptive_scheduler_state.json"

        if scheduler_state_path.exists():
            with open(scheduler_state_path, encoding="utf-8") as f:
                scheduler_state = json.load(f)

            result["scheduling_efficiency"]["consecutive_idle_cycles"] = (
                scheduler_state.get("consecutive_idle_cycles", 0)
            )
            result["scheduling_efficiency"]["wasted_polls_count"] = scheduler_state.get(
                "wasted_polls_count", 0
            )
            result["scheduling_efficiency"]["last_activity_time"] = scheduler_state.get(
                "last_activity_time"
            )

            # Calculate current backoff multiplier
            idle_cycles = scheduler_state.get("consecutive_idle_cycles", 0)
            if idle_cycles <= 2:
                multiplier = 1
            elif idle_cycles <= 5:
                multiplier = 2
            elif idle_cycles <= 10:
                multiplier = 4
            else:
                multiplier = 6

            result["scheduling_efficiency"]["current_backoff_multiplier"] = multiplier

            # Calculate effective poll interval (base * multiplier)
            base_interval = config.poll_interval_seconds
            current_mode = scheduler_state.get("current_mode", "NORMAL")
            if current_mode == "IDLE":
                base_interval = 300  # IDLE base
                result["scheduling_efficiency"]["effective_poll_interval"] = min(
                    base_interval * multiplier, 1800
                )
            else:
                result["scheduling_efficiency"]["effective_poll_interval"] = (
                    base_interval
                )

            # P36: Business hours awareness
            result["scheduling_efficiency"]["is_business_hours"] = scheduler_state.get(
                "is_business_hours", True
            )
            result["scheduling_efficiency"]["business_hours_mode_applied"] = (
                scheduler_state.get("business_hours_mode_applied", False)
            )

            # P36: Ideation efficiency metrics
            ideation_total = scheduler_state.get("ideation_cycles_total", 0)
            ideation_skipped = scheduler_state.get("ideation_cycles_skipped", 0)
            ideation_executed = ideation_total - ideation_skipped
            ideation_ideas = scheduler_state.get("ideation_ideas_generated", 0)

            result["scheduling_efficiency"]["ideation_cycles_total"] = ideation_total
            result["scheduling_efficiency"]["ideation_cycles_executed"] = (
                ideation_executed
            )
            result["scheduling_efficiency"]["ideation_cycles_skipped"] = (
                ideation_skipped
            )
            result["scheduling_efficiency"]["ideation_skip_rate_percent"] = (
                round(ideation_skipped / ideation_total * 100, 1)
                if ideation_total > 0
                else 0.0
            )
            result["scheduling_efficiency"]["ideation_ideas_generated"] = ideation_ideas
            result["scheduling_efficiency"]["ideation_ideas_per_cycle"] = (
                round(ideation_ideas / ideation_executed, 2)
                if ideation_executed > 0
                else 0.0
            )

    except Exception:
        pass

    # Get live cross-project metrics if enabled (P14)
    if config.cross_project_enabled:
        try:
            _ensure_imports()
            live_metrics = _get_cross_project_metrics()
            result["cross_project"]["cross_project_task_count"] = live_metrics.get(
                "cross_project_task_count", 0
            )
            result["cross_project"]["employee_distribution"] = live_metrics.get(
                "employee_distribution", {}
            )
            result["cross_project"]["recent_routing_activity"] = live_metrics.get(
                "routing_activity", []
            )
        except Exception:
            pass

    # Load PR workflow config from gatesmith.json (P27)
    try:
        _ensure_imports()
        pr_config = pr_output_manager.load_config()
        result["pr_workflow"]["enabled"] = pr_config.get("enabled", True)
        result["pr_workflow"]["auto_push_branches"] = pr_config.get(
            "autoPushFeatureBranches", True
        )
        result["pr_workflow"]["auto_create_pr"] = pr_config.get(
            "autoCreateDraftPR", True
        )
        result["pr_workflow"]["require_tests"] = pr_config.get("requireTestsPass", True)
    except Exception:
        pass

    return result


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(
        description="Gatesmith Daemon - Continuous autonomous operation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
    start     Start the daemon (daemonize by default)
    stop      Stop the daemon gracefully
    restart   Restart the daemon
    status    Show daemon status
    run       Run in foreground (alias for: start --foreground)
    install   Install daemon as macOS launchd service
    uninstall Uninstall daemon launchd service
    watchdog  Run one watchdog health-check cycle (safe for cron)

Examples:
    # Start daemon in background
    python forge_daemon.py start

    # Start in foreground (for debugging)
    python forge_daemon.py start --foreground
    python forge_daemon.py run

    # Stop daemon
    python forge_daemon.py stop

    # Check status
    python forge_daemon.py status

    # Install as macOS launchd service
    python forge_daemon.py install

    # Uninstall launchd service
    python forge_daemon.py uninstall

Configuration:
    Default configuration is loaded from gatesmith.json in the project root.
    Use --config to specify an alternate configuration file.
""",
    )

    parser.add_argument(
        "command",
        choices=[
            "start",
            "stop",
            "restart",
            "status",
            "run",
            "install",
            "uninstall",
            "watchdog",
        ],
        help="Command to execute",
    )

    parser.add_argument(
        "--foreground",
        "-f",
        action="store_true",
        help="Run in foreground (don't daemonize)",
    )

    parser.add_argument("--config", "-c", type=Path, help="Path to configuration file")

    args = parser.parse_args()

    # Load configuration
    if args.config:
        config = DaemonConfig.from_file(args.config)
    else:
        # Canonical root gatesmith.json, legacy .claude/ fallback
        config = DaemonConfig.from_file(DaemonConfig.default_config_path())

    # Refuse lifecycle commands when running inside a worker subprocess.
    # Workers set FORGE_WORKER_CONTEXT=1; allowing these from a worker would
    # silently unload/remove the LaunchAgent and cause a service outage.
    # Covers install/uninstall too — both touch launchctl and the plist on disk.
    # The daemon's own watchdog/self-restart paths call start_daemon() and
    # stop_daemon() directly as Python functions (never via main()), so they
    # are unaffected by this guard.
    # status and watchdog are read-only; they remain allowed from any context.
    _WORKER_BLOCKED_COMMANDS = {
        "start",
        "run",
        "stop",
        "restart",
        "install",
        "uninstall",
    }
    if args.command in _WORKER_BLOCKED_COMMANDS and os.environ.get(
        "FORGE_WORKER_CONTEXT"
    ):
        print(
            f"Error: forge_daemon.py {args.command} cannot be run from a worker "
            f"subprocess (FORGE_WORKER_CONTEXT is set). "
            f"Only humans and the daemon's own watchdog may change daemon lifecycle.",
            file=sys.stderr,
        )
        return 1

    # Handle foreground flag and run command
    if args.foreground or args.command == "run":
        config.daemonize = False

    # Execute command
    if args.command in ("start", "run"):
        return start_daemon(config)

    elif args.command == "stop":
        return stop_daemon(config)

    elif args.command == "restart":
        # Stop if running
        if is_daemon_running(config):
            stop_result = stop_daemon(config)
            if stop_result not in (0, 2):  # 2 = not running (ok for restart)
                return stop_result
            time.sleep(1)  # Brief pause before restart

        # Start
        return start_daemon(config)

    elif args.command == "status":
        status = daemon_status(config)

        if status["running"]:
            print(f"Daemon is running (pid={status['pid']})")
            print(f"  Uptime: {status['uptime_seconds']}s")
            print(f"  Tasks completed: {status['tasks_completed']}")
            print(f"  Tasks failed: {status['tasks_failed']}")
            print(f"  Current task: {status['current_task'] or 'idle'}")
            print(f"  Circuit breaker: {status['circuit_breaker_state']}")
            print(f"  Last heartbeat: {status['last_heartbeat'] or 'unknown'}")
            print(f"  Last proactive scan: {status['last_proactive_scan'] or 'never'}")
            print(f"  Proposals created: {status['proactive_proposals_created']}")

            # Cross-project metrics (P14)
            cross_project = status.get("cross_project", {})
            if cross_project.get("enabled"):
                print("  Cross-project orchestration:")
                print(f"    Tasks routed: {cross_project.get('tasks_routed', 0)}")
                print(
                    f"    Tasks pending approval: {cross_project.get('tasks_queued_for_approval', 0)}"
                )
                print(
                    f"    Cross-project task count: {cross_project.get('cross_project_task_count', 0)}"
                )
                print(
                    f"    Last rebalance check: {cross_project.get('last_rebalance_check') or 'never'}"
                )
                print(
                    f"    Rebalance proposals: {cross_project.get('rebalance_proposals', 0)}"
                )

            # Strategic planning metrics (P15)
            strategic = status.get("strategic_planning", {})
            strategic_enabled = strategic.get("enabled", False)
            print(
                f"  Strategic planning: {'enabled' if strategic_enabled else 'disabled'}"
            )
            if strategic_enabled:
                print(f"    Last run: {strategic.get('last_run') or 'never'}")
                print(
                    f"    Initiatives created: {strategic.get('initiatives_created', 0)}"
                )
                print(f"    Tasks queued: {strategic.get('tasks_queued', 0)}")

            # Document approval scan metrics (P23)
            doc_approvals = status.get("document_approvals", {})
            doc_enabled = doc_approvals.get("enabled", False)
            print(f"  Document approvals: {'enabled' if doc_enabled else 'disabled'}")
            if doc_enabled:
                print(f"    Last scan: {doc_approvals.get('last_scan') or 'never'}")
                print(
                    f"    Documents discovered: {doc_approvals.get('documents_discovered', 0)}"
                )
                print(
                    f"    Documents pending: {doc_approvals.get('documents_pending', 0)}"
                )

            # Roadmap scheduling metrics (P27)
            roadmap = status.get("roadmap_scheduling", {})
            roadmap_enabled = roadmap.get("enabled", False)
            print(
                f"  Roadmap scheduling: {'enabled' if roadmap_enabled else 'disabled'}"
            )
            if roadmap_enabled:
                print(f"    Last scan: {roadmap.get('last_scan') or 'never'}")
                print(f"    Tasks scheduled: {roadmap.get('tasks_scheduled', 0)}")
                print(f"    Tasks completed: {roadmap.get('tasks_completed', 0)}")
                print(f"    Current wave: {roadmap.get('current_wave', 1)}")

            # PR workflow metrics (P27)
            pr_workflow = status.get("pr_workflow", {})
            pr_enabled = pr_workflow.get("enabled", False)
            print(f"  PR workflow: {'enabled' if pr_enabled else 'disabled'}")
            if pr_enabled:
                print(
                    f"    Auto-push branches: {pr_workflow.get('auto_push_branches', True)}"
                )
                print(f"    Auto-create PR: {pr_workflow.get('auto_create_pr', True)}")
                print(f"    Require tests: {pr_workflow.get('require_tests', True)}")

            # P30: Self-improvement cycle metrics
            improvement = status.get("improvement_cycle", {})
            improvement_enabled = improvement.get("enabled", False)
            print(
                f"  Self-improvement: {'enabled' if improvement_enabled else 'disabled'}"
            )
            if improvement_enabled:
                print(f"    Last run: {improvement.get('last_run') or 'never'}")
                print(f"    Cycles run: {improvement.get('cycles_run', 0)}")
                print(
                    f"    Interval: {improvement.get('interval_hours', 168)} hours (weekly)"
                )
                print(
                    f"    Next scheduled: {improvement.get('next_scheduled') or 'pending first run'}"
                )

            # P34: Employee ideation metrics
            ideation = status.get("employee_ideation", {})
            ideation_enabled = ideation.get("enabled", False)
            print(
                f"  Employee ideation: {'enabled' if ideation_enabled else 'disabled'}"
            )
            if ideation_enabled:
                print(f"    Last run: {ideation.get('last_run') or 'never'}")
                print(f"    Cycles run: {ideation.get('cycles_run', 0)}")
                print(f"    Ideas generated: {ideation.get('ideas_generated', 0)}")
                print(f"    Interval: {ideation.get('interval_minutes', 60)} minutes")

            # P35: Auto-merge status
            auto_merge = status.get("auto_merge", {})
            auto_merge_enabled = auto_merge.get("enabled", False)
            print(f"  Auto-merge: {'enabled' if auto_merge_enabled else 'disabled'}")
            if auto_merge_enabled:
                print(f"    Last check: {auto_merge.get('last_check') or 'never'}")
                print(f"    Checks run: {auto_merge.get('checks_run', 0)}")
                print(f"    PRs merged: {auto_merge.get('prs_merged', 0)}")
                print(f"    Interval: {auto_merge.get('interval_minutes', 5)} minutes")
        else:
            print("Daemon is not running")

        # Output JSON for scripting
        print(f"\n{json.dumps(status, indent=2)}")

        return 0 if status["running"] else 1

    elif args.command == "watchdog":
        # WS-117: Run one watchdog health-check cycle
        try:
            from daemon_watchdog import (
                WatchdogConfig,
                run_watchdog_cycle,
            )

            wdg_cfg = WatchdogConfig(
                heartbeat_file=config.heartbeat_file,
                pid_file=config.pid_file,
            )
            action = run_watchdog_cycle(wdg_cfg)
            print(f"Watchdog cycle: {action.value}")
            return 0
        except Exception as e:
            print(f"Watchdog error: {e}")
            return 0  # Always exit 0 — safe for cron

    elif args.command == "install":
        import platform

        if platform.system() != "Darwin":
            print("Error: launchd install is only supported on macOS")
            sys.exit(2)

        import subprocess

        from company_resolver import get_project_id

        plist_src = Path(__file__).parent / "launchd" / "dev.gatesmith.daemon.plist"
        project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
        project_id = get_project_id(Path(project_root))
        plist_filename = f"dev.gatesmith.daemon.{project_id}.plist"
        plist_dest = Path.home() / "Library" / "LaunchAgents" / plist_filename

        if not plist_src.exists():
            print(f"Error: plist template not found at {plist_src}")
            sys.exit(1)

        # Migrate old-style global plist if present
        old_plist = (
            Path.home() / "Library" / "LaunchAgents" / "dev.gatesmith.daemon.plist"
        )
        if old_plist.exists():
            print(f"Migrating old-style plist: {old_plist}")
            subprocess.run(
                ["launchctl", "unload", str(old_plist)],
                capture_output=True,
                text=True,
            )
            old_plist.unlink()
            print("  Old service unloaded and removed.")

        if plist_dest.exists():
            print(f"Warning: {plist_dest} already exists. Overwriting.")
            subprocess.run(
                ["launchctl", "unload", str(plist_dest)],
                capture_output=True,
                text=True,
            )

        # Read plist and substitute placeholders
        import shutil

        label = f"dev.gatesmith.daemon.{project_id}"
        uv_path = shutil.which("uv") or "/usr/local/bin/uv"
        # Capture user's PATH so child processes (employee_activator) can find
        # uv, claude, and other tools that may live in ~/.local/bin etc.
        user_path = os.environ.get(
            "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        )
        content = plist_src.read_text()
        content = content.replace("__PROJECT_ROOT__", project_root)
        content = content.replace("__PROJECT_ID__", project_id)
        content = content.replace("__UV_PATH__", uv_path)
        content = content.replace("__USER_PATH__", user_path)

        # P84: Set Agent Teams env var based on gatesmith.json
        agent_teams_val = "0"
        try:
            config_path = Path(project_root) / ".claude" / "gatesmith.json"
            if config_path.exists():
                import json as _json

                _cfg = _json.loads(config_path.read_text())
                _at = _cfg.get("agentTeams", {})
                if _at.get("enabled") and _at.get("experimentalAcknowledged"):
                    agent_teams_val = "1"
        except Exception:
            pass
        content = content.replace("__AGENT_TEAMS_ENABLED__", agent_teams_val)

        # Issue #955: the plist runs the CI-health prestart wrapper from
        # Application Support, because macOS TCC blocks launchd from reading
        # scripts under ~/Documents where checkouts usually live. Copy it
        # there on every install so a moved or updated project stays current.
        app_support_dir = Path.home() / "Library" / "Application Support"
        wrapper_src = Path(__file__).parent / "daemon_prestart_wrapper.sh"
        wrapper_dest = app_support_dir / "Gatesmith" / "daemon_prestart_wrapper.sh"
        if not wrapper_src.exists():
            print(f"Error: prestart wrapper not found at {wrapper_src}")
            sys.exit(1)
        wrapper_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wrapper_src, wrapper_dest)
        wrapper_dest.chmod(0o755)
        content = content.replace("__APPLICATION_SUPPORT_DIR__", str(app_support_dir))

        # A placeholder left in the plist makes launchd run a path that does
        # not exist; refuse to write it rather than install a dead agent.
        import re

        leftover = sorted(set(re.findall(r"__[A-Z_]+__", content)))
        if leftover:
            print(f"Error: unsubstituted plist placeholders: {', '.join(leftover)}")
            sys.exit(1)

        # Ensure LaunchAgents dir exists
        plist_dest.parent.mkdir(parents=True, exist_ok=True)
        plist_dest.write_text(content)

        # Load via launchctl
        result = subprocess.run(
            ["launchctl", "load", str(plist_dest)], capture_output=True, text=True
        )
        if result.returncode == 0:
            print("Daemon installed and loaded.")
            print(f"  Plist: {plist_dest}")
            print(f"  Label: {label}")
            print(f"  Project: {project_root}")
            print(f"  Project ID: {project_id}")
            print(f"  Status: launchctl list | grep {project_id}")
        else:
            print(f"launchctl load failed: {result.stderr}")
            sys.exit(1)

        return 0

    elif args.command == "uninstall":
        import platform

        if platform.system() != "Darwin":
            print("Error: launchd uninstall is only supported on macOS")
            sys.exit(2)

        import subprocess

        from company_resolver import get_project_id

        project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
        project_id = get_project_id(Path(project_root))
        plist_filename = f"dev.gatesmith.daemon.{project_id}.plist"
        plist_dest = Path.home() / "Library" / "LaunchAgents" / plist_filename

        # Also check for old-style global plist as fallback
        old_plist = (
            Path.home() / "Library" / "LaunchAgents" / "dev.gatesmith.daemon.plist"
        )

        if plist_dest.exists():
            subprocess.run(
                ["launchctl", "unload", str(plist_dest)],
                capture_output=True,
                text=True,
            )
            plist_dest.unlink()
            print(f"Daemon uninstalled. Service {project_id} removed.")
        elif old_plist.exists():
            subprocess.run(
                ["launchctl", "unload", str(old_plist)],
                capture_output=True,
                text=True,
            )
            old_plist.unlink()
            print("Daemon uninstalled. Legacy global service removed.")
        else:
            print("Daemon is not installed via launchd.")
            sys.exit(2)

        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
