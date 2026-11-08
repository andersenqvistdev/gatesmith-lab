#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
venture_scope_monitor.py — Scan ventures for compliance gate status.

Foundation slice follow-up: makes `venture-scope.json` load-bearing. A
venture marked `regulated: true` in its scope file must have a matching
`compliance-report.json` before engineering may proceed. This module:

1. Walks the company tree for ventures with scope files
2. Flags any regulated venture that lacks an approved compliance report
3. For each flagged venture, creates (via work_allocator) a review task
   assigned to the `legal-compliance-officer` agent
4. Emits a terse status report

Called from:
- CLI: `uv run venture_scope_monitor.py scan [--dry-run]`
- Daemon hook: `ensure_pending_reviews(company_root)` on each cycle
- Pre-merge gate: `is_merge_allowed(venture_dir)` returns bool

Design notes:
- Non-destructive: never rewrites a venture's scope or report files.
  Only creates queue tasks and reads status.
- Dedup: uses a deterministic task_id per venture so re-runs don't
  spam the queue (same pattern as `_escalate_stuck_pr`).
- Fail-open on missing infrastructure: if work_allocator isn't
  importable (e.g. running from a customer venture without the full
  Gatesmith install), the monitor logs and exits cleanly.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Lazy import guard — same pattern as employee_initiative.py
work_allocator = None
company_resolver = None
compliance_report_generator = None


def _ensure_imports() -> None:
    """Lazily import sibling modules so this file is CLI-usable standalone."""
    global work_allocator, company_resolver, compliance_report_generator
    if work_allocator is not None:
        return
    try:
        from . import company_resolver as cr
        from . import compliance_report_generator as crg
        from . import work_allocator as wa
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
        import compliance_report_generator as crg  # type: ignore[no-redef]
        import work_allocator as wa  # type: ignore[no-redef]
    work_allocator = wa
    company_resolver = cr
    compliance_report_generator = crg


# ---------------------------------------------------------------------------
# Scope discovery
# ---------------------------------------------------------------------------


def find_venture_scopes(company_root: Path) -> list[dict[str, Any]]:
    """Find every venture-scope.json under the company's projects tree.

    Expected layout:
        <company_root>/projects/<venture-id>/.company/venture-scope.json

    Returns a list of dicts, each with:
      - venture_id: str (the directory name)
      - venture_dir: Path (project root)
      - scope_path: Path (path to scope file)
      - scope: dict (parsed contents) or None if invalid
      - report_path: Path (where the compliance report would/should live)
      - report_exists: bool
    """
    projects_root = company_root / "projects"
    results: list[dict[str, Any]] = []

    if not projects_root.is_dir():
        return results

    for venture_dir in sorted(projects_root.iterdir()):
        if not venture_dir.is_dir():
            continue
        scope_path = venture_dir / ".company" / "venture-scope.json"
        if not scope_path.exists():
            continue

        report_path = venture_dir / ".company" / "compliance-report.json"
        try:
            scope = json.loads(scope_path.read_text())
        except (json.JSONDecodeError, OSError):
            scope = None

        results.append(
            {
                "venture_id": venture_dir.name,
                "venture_dir": venture_dir,
                "scope_path": scope_path,
                "scope": scope,
                "report_path": report_path,
                "report_exists": report_path.exists(),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def _has_human_signoff(report: dict[str, Any]) -> bool:
    """Return True only if the report carries a valid human-authored signoff.

    The signoff block must have both ``approved_by`` (non-empty string
    identifying the human who confirmed) and ``approved_at`` (non-empty ISO
    timestamp).  Only a /gate-style human-confirmation step should write this
    block — the reviewing LLM agent must NOT populate it.
    """
    signoff = report.get("human_signoff")
    if not isinstance(signoff, dict):
        return False
    approved_by = signoff.get("approved_by")
    approved_at = signoff.get("approved_at")
    return (
        isinstance(approved_by, str)
        and bool(approved_by)
        and isinstance(approved_at, str)
        and bool(approved_at)
    )


def _report_is_approved(report_path: Path) -> bool:
    """Return True only if the report exists, is explicitly approved, AND
    carries a human signoff record.

    Any read/parse error yields False — we never consider a damaged report
    "approved" by accident.
    """
    if not report_path.exists():
        return False
    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return bool(report.get("approved")) and _has_human_signoff(report)


def needs_compliance_review(venture_info: dict[str, Any]) -> bool:
    """True iff the venture is regulated and does not yet have an approved report.

    Checks actual approval status, not just file existence. This is how we
    avoid the silent dead-end where a scaffold gets written but never
    approved — the loop must keep noticing and re-dispatching. Duplicate
    tasks are prevented by ensure_pending_reviews()'s active-bucket dedup.
    """
    scope = venture_info.get("scope")
    if not isinstance(scope, dict):
        return False
    if not scope.get("regulated"):
        return False
    report_path = venture_info.get("report_path")
    if report_path is None:
        # Legacy callers: fall back to the older semantics.
        return not venture_info.get("report_exists", False)
    return not _report_is_approved(report_path)


def is_merge_allowed(venture_dir: Path) -> tuple[bool, str]:
    """Pre-merge gate: is this venture cleared to merge?

    Returns (allowed, reason). `allowed=True` when:
      - the venture has no scope file (non-venture or unscoped), OR
      - the scope declares `regulated: false`, OR
      - the scope declares `regulated: true` AND a compliance-report.json
        exists with `approved: true`.

    `allowed=False` with a human-readable reason otherwise.
    """
    scope_path = Path(venture_dir) / ".company" / "venture-scope.json"
    if not scope_path.exists():
        return True, "no venture-scope.json — not a scoped venture"

    try:
        scope = json.loads(scope_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, f"venture-scope.json present but unreadable: {e}"

    if not scope.get("regulated"):
        return True, "scope is not flagged regulated"

    report_path = Path(venture_dir) / ".company" / "compliance-report.json"
    if not report_path.exists():
        frameworks = (
            ", ".join(scope.get("regulatory_frameworks", [])) or "(none listed)"
        )
        return False, (
            f"regulated venture ({scope.get('vertical', '?')}; frameworks: {frameworks}) "
            "requires an approved compliance-report.json before merge — "
            "route to legal-compliance-officer"
        )

    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, f"compliance-report.json present but unreadable: {e}"

    if not report.get("approved"):
        return False, (
            f"compliance-report.json exists but is not approved "
            f"(status={report.get('status', 'unknown')}; "
            f"reviewer={report.get('reviewer', 'none')})"
        )

    if not _has_human_signoff(report):
        return False, (
            "compliance-report.json has approved=true but lacks a human signoff — "
            "an LLM agent cannot self-approve a regulated venture; "
            "a human must confirm via /gate and write human_signoff.approved_by + "
            "human_signoff.approved_at before merge is allowed"
        )

    return True, "compliance report approved with human signoff"


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


def _review_task_id(venture_id: str) -> str:
    """Deterministic task_id so re-runs dedup (same pattern as _escalate_stuck_pr)."""
    return f"compliance-review-{venture_id}"


def build_review_task(venture_info: dict[str, Any]) -> dict[str, Any]:
    """Construct the task payload for a compliance review without submitting it.

    Args:
        venture_info: Entry from ``find_venture_scopes()`` containing at minimum
            ``venture_id`` (str), ``scope`` (dict or None), and ``report_path`` (Path).

    Returns:
        Task dict ready to pass to ``work_allocator.add_task()`` with fields:
        ``task_id``, ``title``, ``description``, ``priority``,
        ``estimated_complexity``, ``source``, ``assigned_to``,
        ``required_capabilities``, ``requires_deliverable``,
        ``created_at``, and ``venture_id``.
    """
    scope = venture_info["scope"] or {}
    venture_id = venture_info["venture_id"]
    frameworks = ", ".join(scope.get("regulatory_frameworks", [])) or "(unspecified)"

    description = (
        f"Compliance-gate review for venture `{venture_id}`.\n\n"
        f"**Vertical:** {scope.get('vertical', 'unknown')}\n"
        f"**Regulatory frameworks:** {frameworks}\n"
        f"**Acceptance criteria:** {scope.get('acceptance_criteria', '(unspecified)')}\n"
        f"**Integrations:** {', '.join(scope.get('integrations', [])) or '(none)'}\n\n"
        f"Deliverable: write `{venture_info['report_path']}` with fields "
        f"`approved: bool`, `status`, `reviewer`, `findings`, `blockers`, "
        f"`must_not_ship`, `captured_at`. Engineering PRs on this venture "
        f"remain merge-blocked until that file exists with `approved: true` "
        f"AND a separate human signoff (human_signoff.approved_by + "
        f"human_signoff.approved_at) written by a /gate human-confirmation "
        f"step. Do NOT write human_signoff yourself — only a human can."
    )

    return {
        "task_id": _review_task_id(venture_id),
        "title": f"Compliance review: {venture_id} ({scope.get('vertical', 'unknown')})",
        "description": description,
        "priority": 2,
        "estimated_complexity": "standard",
        "source": "compliance-gate",
        "assigned_to": "legal-compliance-officer",
        "required_capabilities": [
            "regulatory-compliance",
            "legal-review",
            "compliance-gating",
        ],
        "requires_deliverable": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "venture_id": venture_id,
    }


# ---------------------------------------------------------------------------
# End-to-end: ensure pending reviews are queued
# ---------------------------------------------------------------------------


def ensure_pending_reviews(company_root: Path, dry_run: bool = False) -> dict[str, Any]:
    """Scan for regulated ventures needing review, queue tasks if missing.

    Idempotent: tasks are deduped by deterministic task_id. A venture
    that already has a review task in the queue (pending, in_progress, or
    completed) will NOT get a duplicate.

    Returns a status dict with counts and any issues for the caller to log.
    """
    _ensure_imports()

    ventures = find_venture_scopes(company_root)
    pending = [v for v in ventures if needs_compliance_review(v)]
    queued: list[str] = []
    skipped_existing: list[str] = []
    scaffolds_written: list[str] = []
    errors: list[str] = []

    # Load the queue once to check for existing review tasks
    try:
        queue = work_allocator.load_queue()
    except Exception as e:
        errors.append(f"load_queue failed: {e}")
        queue = {"pending": [], "in_progress": [], "completed": [], "blocked": []}

    # Dedup by title match rather than task_id: work_allocator.add_task
    # generates its own task_id (no way to pass ours in), so comparing
    # against our deterministic `compliance-review-<venture>` id would
    # never match. Two compliance reviews for the same venture would
    # share a title prefix, so we match on that instead.
    #
    # Only ACTIVE buckets count — a completed-or-failed review whose
    # report is still unapproved must be re-queued (silent dead-end fix).
    # needs_compliance_review() handles the other half: it now returns
    # True when the report exists but is not approved.
    # Also check "blocked" — a task parked there (e.g. halted by operator)
    # must not be re-queued every cycle, which produces a spurious log line.
    existing_review_venture_ids: set[str] = set()
    for bucket in ("pending", "in_progress", "blocked"):
        for t in queue.get(bucket, []) or []:
            title = t.get("title") or ""
            if title.startswith("Compliance review: "):
                # Title format: "Compliance review: <venture_id> (<vertical>)"
                rest = title[len("Compliance review: ") :]
                venture_id = rest.split(" (", 1)[0].strip()
                if venture_id:
                    existing_review_venture_ids.add(venture_id)

    for venture_info in pending:
        task = build_review_task(venture_info)
        if venture_info["venture_id"] in existing_review_venture_ids:
            skipped_existing.append(venture_info["venture_id"])
            continue

        if dry_run:
            queued.append(venture_info["venture_id"])
            continue

        try:
            # add_task signature defined in work_allocator
            work_allocator.add_task(
                title=task["title"],
                priority=task["priority"],
                description=task["description"],
                estimated_complexity=task["estimated_complexity"],
                source=task["source"],
                required_capabilities=task["required_capabilities"],
            )
            queued.append(venture_info["venture_id"])
        except Exception as e:
            errors.append(f"{venture_info['venture_id']}: add_task failed: {e}")
            # Task queueing failed — don't leave an orphan scaffold behind.
            continue

        # Drop a scaffold report alongside the task so the legal agent has a
        # strict schema to enrich rather than a blank file. Never clobber an
        # existing report (shouldn't exist — needs_compliance_review guards —
        # but defensive). Scaffold failures must not block queueing: the
        # agent can still fill the report from scratch.
        try:
            compliance_report_generator.write_scaffold(
                venture_info["venture_dir"], overwrite=False
            )
            scaffolds_written.append(venture_info["venture_id"])
        except FileExistsError:
            # Race: report appeared between the needs_compliance_review check
            # and now. Leave the existing report alone.
            pass
        except Exception as e:
            errors.append(
                f"{venture_info['venture_id']}: scaffold write failed "
                f"(task still queued): {e}"
            )

    return {
        "ventures_scanned": len(ventures),
        "pending_reviews": len(pending),
        "queued": queued,
        "skipped_existing": skipped_existing,
        "scaffolds_written": scaffolds_written,
        "errors": errors,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_main() -> int:
    _ensure_imports()
    args = sys.argv[1:]
    dry_run = "--dry-run" in args

    if not args or args[0] not in {"scan", "check"}:
        print("Usage: venture_scope_monitor.py <command> [options]")
        print("")
        print("Commands:")
        print("  scan [--dry-run]       Scan company tree, queue compliance reviews")
        print("  check <venture-dir>    Exit 0 if merge allowed, 1 otherwise")
        print("")
        print("Examples:")
        print("  uv run venture_scope_monitor.py scan --dry-run")
        print("  uv run venture_scope_monitor.py check ./projects/my-trading-app")
        return 1

    if args[0] == "check":
        if len(args) < 2:
            print("error: 'check' requires a venture directory path", file=sys.stderr)
            return 2
        allowed, reason = is_merge_allowed(Path(args[1]))
        print(f"{'ALLOW' if allowed else 'BLOCK'}: {reason}")
        return 0 if allowed else 1

    # scan
    company_root = company_resolver.get_company_dir().parent
    result = ensure_pending_reviews(company_root, dry_run=dry_run)
    print(json.dumps(result, indent=2, default=str))
    if result["errors"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
