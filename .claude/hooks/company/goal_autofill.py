#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Goal autofill — keeps the work queue fed from the active goals.

When pending work drops below a threshold, assesses the goals, picks the ones
furthest from their success metric and mints one brief-quality-gated
``[QUEUE-FILL]`` task per goal. Holds, rejection backoff, dedup against
recently completed twins and the per-goal escalation brake live here too.

Split out of strategic_planner (launch K2a): autofill is core, the planning
cycles are not. strategic_planner re-exports these names for its callers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Matches slash-commands embedded anywhere in action text (e.g. "Run /test-sprint to...")
_SLASH_CMD_RE = re.compile(r"(?<!\w)/[a-z][a-z0-9_-]+")

# Marks a next_action as deterministic infra work (e.g. a nightly LaunchAgent
# job) rather than something an LLM worker session can execute -- must never
# become a [QUEUE-FILL] task title. See goal_tracker.py's G1 next_actions.
_INFRA_ONLY_RE = re.compile(r"^\[INFRA-ONLY\]")

# Marks a next_action as requiring a HUMAN actor (owner decision, business
# development, sales) rather than something an LLM worker session can execute
# -- must never become a [QUEUE-FILL] task title. Same rationale as
# [INFRA-ONLY]: minting these produces either a reject-loop (the admission
# gate's semantic judge correctly rejects "close a sale" as non-software work,
# every cycle, forever) or an admit-then-block (the task burns retries and
# lands in blocked because no diff can satisfy it). See goal_tracker.py's
# G13 next_actions.
_OWNER_ONLY_RE = re.compile(r"^\[OWNER-ONLY\]")

# Rejection-log titles for gap_analysis-sourced queue-fill tasks always follow
# the f"[QUEUE-FILL] {goal_id}: ..." shape minted in autofill_queue_from_goals.
_QUEUE_FILL_GOAL_RE = re.compile(r"^\[QUEUE-FILL\]\s+([A-Za-z0-9_-]+):")

# Cap on how many trailing lines of task_admission_rejections.jsonl
# _recently_rejected_goal_ids scans. The log is append-only/chronological
# (see task_admission.py's rejection logger), so only the tail can ever fall
# within the (default 6h) backoff window -- 2000 lines is generous headroom
# even under the ~7min-cadence rejection thrashing incident that motivated
# this backoff (see docstring at _recently_rejected_goal_ids, ~line 2255).
_REJECTION_LOG_MAX_LINES = 2000

# Infrastructure file names excluded from greenfield source-file detection
_GREENFIELD_INFRA_NAMES = frozenset({"conftest.py", "setup.py", "__init__.py"})

# Brief-quality gate (2026-07-26): a non-greenfield QUEUE-FILL brief must carry
# at least one verifiable pointer AND one measured value, or the goal is
# skipped this cycle. Shape learned from the briefs that demonstrably execute
# well (PR #301/#307 evidence pattern) vs the vague-action phantoms in the
# 07-23 audit: a vague task costs a full worker execution and produces work
# nobody asked for; an empty queue slot costs nothing. Greenfield repos are
# exempt -- there is nothing to point at yet, the Success Metric IS the spec.
# The leading ``\.?`` is load-bearing: without it the match starts after the
# dot, so `.claude/hooks/company/forge_daemon.py` was captured as
# `claude/hooks/...` and the existence check failed for EVERY pointer into
# Gatesmith's own source tree -- the one directory fix briefs need to name.
_FILE_POINTER_RE = re.compile(
    r"\.?[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|json|ya?ml|sh|html|js|ts|toml|txt|css)\b"
)
_TASK_ID_POINTER_RE = re.compile(r"task-\d{14}-[0-9a-f]{6}")
_PR_POINTER_RE = re.compile(r"(?:PR\s*)?#\d{2,}\b")
_MEASURED_RE = re.compile(r"\d")

# Intent split for the brief-quality gate (2026-08-11). The gate used to demand
# an EXISTING pointer for every brief, which is the right test for fix work and
# the wrong test for build work: a brief describing work-not-yet-done names
# files that cannot exist yet. Evidence for both failure directions:
#   - forge-framework G7 passed the gate vacuously for weeks on "Improve success
#     rate from 75% to 90%+" -- the goal's OWN failure task-id satisfied
#     "pointer" and the percentages satisfied "measurement", so a pure metric
#     restatement minted every cycle, was admission-rejected, and re-minted.
#   - mdlint-sandbox (stranger repo) had ALL NINE goals skipped in one sweep on
#     2026-08-08 for missing verified_pointer, the moment it stopped being
#     greenfield -- real, enumerated build work, permanently unmintable.
# A fix brief must name the code to change; a build brief must name the artifact
# to create. Existence is required only for the former.
# The (?<![-\w]) guard keeps a CLI flag from reading as intent: "--fix applies
# safe autofixes" is a build brief naming a flag, not a request to fix anything.
_FIX_INTENT_RE = re.compile(
    r"(?<![-\w])(?:fix|fixes|fixing|repair|resolve|correct|debug|patch|refactor"
    r"|improve|raise|increase|decrease|reduce|restore|unblock|optimi[sz]e"
    r"|migrate)\b",
    re.IGNORECASE,
)

# Concrete-deliverable signals for build briefs. Existence is deliberately NOT
# checked here -- naming `src/mdlint/cli.py` or `--fix` is concrete whether or
# not it exists yet. A metric target ("90% success rate") matches none of these,
# which is exactly what separates buildable work from a restatement.
_CLI_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{1,}")
_CONFIG_NAME_RE = re.compile(r"(?<![\w./])\.[a-z][a-z0-9_-]*(?:rc|ignore|config)\b")
_WELL_KNOWN_ARTIFACT_RE = re.compile(
    r"\b(?:README|LICENSE|CHANGELOG|CONTRIBUTING|Makefile|Dockerfile)\b"
)
# A colon introducing a comma-separated list of >=2 named deliverables, e.g.
# "Three rules implemented with tests: first line is a top-level heading,
#  heading levels increment by one, no duplicate sibling headings".
_ENUMERATION_RE = re.compile(r":\s*[^,:]{3,},\s*[^,:]{3,}")

# Cap on trailing lines scanned in autofill_brief_skips.jsonl for dedup --
# same append-only/chronological rationale as _REJECTION_LOG_MAX_LINES.
_BRIEF_SKIP_LOG_MAX_LINES = 500

# Appended to every minted brief: the deliverable judge grades the diff
# against this description, so the requirements must live IN the brief.
# No backticks -- backtick-quoted identifiers trip the admission gate's
# target-existence check.
_TEST_REQUIREMENT_LINES = (
    "**Test Requirements:**",
    "- New or changed code ships with tests in the same PR",
    "- Lint passes: ruff check and ruff format --check",
    "- One atomic commit; branch + PR, never a direct push to main",
)

# Lazy imports for sibling modules
goal_tracker = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global goal_tracker
    if goal_tracker is not None:
        return

    try:
        from . import goal_tracker as gt

        goal_tracker = gt
    except ImportError:
        import goal_tracker as gt  # type: ignore[no-redef]

        goal_tracker = gt


def _incomplete_dependencies(assessment: Any, status_by_goal: dict[str, str]) -> list:
    """The goals ``assessment`` depends on that are not yet complete.

    ONE dependency predicate for every consumer: identify_gaps, the queue
    autofill and goal_scheduler.compute_goal_priorities (which zeroes the same
    goals as "dep_blocked"). A dependency missing from the assessed set counts
    as incomplete, exactly as the scheduler treats it.
    """
    depends_on = getattr(assessment, "depends_on", None) or []
    if not isinstance(depends_on, (list, tuple)):
        return []
    return [dep for dep in depends_on if status_by_goal.get(dep) != "complete"]


def _should_defer_fill_to_ideation(last_ideation: str, defer_to_ideation: bool) -> bool:
    """Return True when ideation-first ordering is active and ideation hasn't run yet.

    Args:
        last_ideation: ISO timestamp of last ideation cycle ("" = never ran).
        defer_to_ideation: Config knob value from fill.deferToIdeation.
    """
    if not defer_to_ideation:
        return False
    return not last_ideation


def _recently_rejected_goal_ids(
    company_dir: Path, *, window_hours: float = 6.0
) -> set[str]:
    """Goal IDs whose auto-filled task was admission-rejected within window_hours.

    Prevents the reject-loop where autofill re-mints the same unbuildable task
    every cycle right after the admission gate rejects it (observed ~7 min
    cadence for G13, 2026-07-19 -- 20 rejections logged before the goal was
    manually retired). Reads only gap_analysis-sourced rejection records (the
    source autofill mints with) so an unrelated rejection with a coincidentally
    similar title never spuriously suppresses fill for an unaffiliated goal.

    Fails open (empty set) on any error -- a missing or corrupt rejection log
    must never block the queue from filling.
    """
    path = company_dir / "state" / "task_admission_rejections.jsonl"
    goal_ids: set[str] = set()
    try:
        if not path.exists():
            return goal_ids
        cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines[-_REJECTION_LOG_MAX_LINES:]:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("source") != "gap_analysis":
                continue
            try:
                rec_time_dt = datetime.fromisoformat(str(record.get("ts")))
                if rec_time_dt.tzinfo is None:
                    rec_time_dt = rec_time_dt.replace(tzinfo=timezone.utc)
                rec_time = rec_time_dt.timestamp()
            except (TypeError, ValueError):
                continue
            if rec_time < cutoff:
                continue
            match = _QUEUE_FILL_GOAL_RE.match(record.get("title") or "")
            if match:
                goal_ids.add(match.group(1))
    except Exception:
        return set()
    return goal_ids


def _verified_pointers(text: str, project_root: Path) -> list[str]:
    """Pointers in text that verifiably refer to something real.

    File paths count only when they exist under project_root at mint time;
    task IDs and PR refs are system-generated and count as-is. Backticks are
    stripped first so quoted paths verify like bare ones. Order-preserving
    de-dup so the brief's Verified Pointers section reads naturally.
    """
    cleaned = text.replace("`", "")
    pointers: list[str] = []
    for match in _FILE_POINTER_RE.finditer(cleaned):
        candidate = match.group(0)
        if candidate.startswith("./"):
            candidate = candidate[2:]
        try:
            if (project_root / candidate).exists():
                pointers.append(candidate)
        except OSError:
            continue
    pointers.extend(m.group(0) for m in _TASK_ID_POINTER_RE.finditer(cleaned))
    pointers.extend(m.group(0) for m in _PR_POINTER_RE.finditer(cleaned))
    seen: set[str] = set()
    unique: list[str] = []
    for pointer in pointers:
        if pointer not in seen:
            seen.add(pointer)
            unique.append(pointer)
    return unique


def _existing_file_pointers(text: str, project_root: Path) -> list[str]:
    """Subset of _verified_pointers that are real FILES on disk.

    A fix brief must name the code to change. Task IDs and PR refs tell a worker
    what failed, not what to edit -- accepting them alone is what let
    forge-framework's G7 restatement ("Improve success rate from 75% to 90%+",
    carrying only its own failure task-id) pass the gate every cycle.
    """
    cleaned = text.replace("`", "")
    found: list[str] = []
    for match in _FILE_POINTER_RE.finditer(cleaned):
        candidate = match.group(0)
        if candidate.startswith("./"):
            candidate = candidate[2:]
        try:
            if (project_root / candidate).exists() and candidate not in found:
                found.append(candidate)
        except OSError:
            continue
    return found


def _is_fix_intent(text: str) -> bool:
    """True when the action describes repairing/moving something that exists."""
    return bool(_FIX_INTENT_RE.search(text))


def _concrete_targets(text: str) -> list[str]:
    """Named deliverables in text, WITHOUT requiring them to exist yet.

    Order-preserving de-dup. Used for build briefs: the question is "does this
    name something specific to produce", not "does it already exist".
    """
    cleaned = text.replace("`", "")
    targets: list[str] = []
    for pattern in (
        _FILE_POINTER_RE,
        _CLI_FLAG_RE,
        _CONFIG_NAME_RE,
        _WELL_KNOWN_ARTIFACT_RE,
        _TASK_ID_POINTER_RE,
        _PR_POINTER_RE,
    ):
        targets.extend(m.group(0) for m in pattern.finditer(cleaned))
    if _ENUMERATION_RE.search(cleaned):
        targets.append("enumerated-deliverables")
    seen: set[str] = set()
    unique: list[str] = []
    for target in targets:
        if target not in seen:
            seen.add(target)
            unique.append(target)
    return unique


def _has_measurement(text: str) -> bool:
    """True when text carries a number that is not part of a pointer.

    Pointers are stripped first so a task-id's timestamp digits or a file
    name like test_pr933.py never masquerade as measured evidence.
    """
    cleaned = text.replace("`", "")
    cleaned = _TASK_ID_POINTER_RE.sub(" ", cleaned)
    cleaned = _PR_POINTER_RE.sub(" ", cleaned)
    cleaned = _FILE_POINTER_RE.sub(" ", cleaned)
    return bool(_MEASURED_RE.search(cleaned))


def _record_brief_quality_skip(
    company_dir: Path,
    goal_id: str,
    missing: list[str],
    action_preview: str,
    *,
    dedup_hours: float = 6.0,
) -> bool:
    """Append a skip record so silent non-minting stays observable.

    A quality gate that hides its own skips reads as "autofill broken" the
    next time the queue sits empty -- the record says exactly which goal was
    held back and what evidence its brief lacked. Best-effort: any error must
    never block the fill loop. Returns True when a record was written; a
    record for the same goal within dedup_hours suppresses the write (the
    fill cycle runs every few minutes, one record per window is enough).
    """
    path = company_dir / "state" / "autofill_brief_skips.jsonl"
    now = datetime.now(timezone.utc)
    try:
        if path.exists():
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            for line in reversed(lines[-_BRIEF_SKIP_LOG_MAX_LINES:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("goal_id") != goal_id:
                    continue
                try:
                    rec_time = datetime.fromisoformat(str(record.get("ts")))
                    if rec_time.tzinfo is None:
                        rec_time = rec_time.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    break
                if (now - rec_time).total_seconds() < dedup_hours * 3600:
                    return False
                break
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "goal_id": goal_id,
                        "missing": missing,
                        "action": action_preview[:200],
                    }
                )
                + "\n"
            )
        return True
    except Exception:
        return False


# Fallback only. The live value is backlog_generator's, imported below so the
# two mint paths cannot drift apart -- this repo has been bitten by a constant
# duplicated across two modules before.
_QUEUE_FILL_DEDUP_FALLBACK_HOURS = 26.0


def _queue_fill_dedup_hours() -> float:
    """Snapshot dedup horizon, sourced from backlog_generator when available."""
    try:
        from . import backlog_generator as bg
    except ImportError:
        try:
            import backlog_generator as bg  # type: ignore[no-redef]
        except ImportError:
            return _QUEUE_FILL_DEDUP_FALLBACK_HOURS
    value = getattr(
        bg, "COVERAGE_SNAPSHOT_DEDUP_HOURS", _QUEUE_FILL_DEDUP_FALLBACK_HOURS
    )
    return (
        float(value)
        if isinstance(value, (int, float))
        else (_QUEUE_FILL_DEDUP_FALLBACK_HOURS)
    )


# Fallbacks only, same reasoning as _QUEUE_FILL_DEDUP_FALLBACK_HOURS above: the
# live values are backlog_generator's and challenger_stage's, imported below so
# this module's cross-lane saturation guard cannot drift from what actually
# names a repair/divergence or challenger-stage task.
_DIVERGENCE_TITLE_PREFIX_FALLBACK = "Repair required acceptance divergence "
_CHALLENGER_STAGE_SOURCE_FALLBACK = "challenger"


def _divergence_title_prefix() -> str:
    """Title prefix of a repair task minted for an open acceptance divergence."""
    try:
        from . import backlog_generator as bg
    except ImportError:
        try:
            import backlog_generator as bg  # type: ignore[no-redef]
        except ImportError:
            return _DIVERGENCE_TITLE_PREFIX_FALLBACK
    value = getattr(bg, "DIVERGENCE_TITLE_PREFIX", _DIVERGENCE_TITLE_PREFIX_FALLBACK)
    return (
        value if isinstance(value, str) and value else _DIVERGENCE_TITLE_PREFIX_FALLBACK
    )


def _challenger_stage_source() -> str:
    """``source`` value of a challenger-stage follow-up (fix) task."""
    try:
        from . import challenger_stage as cs
    except ImportError:
        try:
            import challenger_stage as cs  # type: ignore[no-redef]
        except ImportError:
            return _CHALLENGER_STAGE_SOURCE_FALLBACK
    value = getattr(cs, "STAGE_SOURCE", _CHALLENGER_STAGE_SOURCE_FALLBACK)
    return (
        value if isinstance(value, str) and value else _CHALLENGER_STAGE_SOURCE_FALLBACK
    )


def _is_repair_or_challenger_task(task: Any) -> bool:
    """True for a repair/divergence task or a challenger-stage follow-up task.

    NO-DIVERGENCE-FOR-UNBUILT-ENTRIES (2026-10-06, webapp): six repair-task
    PRs sat in ``pr_open`` at once and the cross-lane saturation guard below
    (``active_count >= threshold``) held goal auto-fill off for ~40h -- it
    counted them the same as goal-building work. These tasks are themselves
    repair work the ledger or the challenger stage already identified, not
    goal-building work competing for the same workers in spirit; goal autofill
    must not starve because several of them happen to be open as PRs at once.
    A non-dict (a bare placeholder, as in some callers' tests) is never
    recognised as one of these and still counts, unchanged from before this
    guard existed.
    """
    if not isinstance(task, dict):
        return False
    title = task.get("title")
    if isinstance(title, str) and title.startswith(_divergence_title_prefix()):
        return True
    # A pr_rebase task (AUTO-REBASE-CONFLICTING-PRS) rebases an existing PR:
    # maintenance of work already built, not goal-building work.
    return task.get("source") in (_challenger_stage_source(), "rebase")


def _normalize_title(title: str) -> str:
    """Casefolded, whitespace-collapsed title for exact comparison."""
    return " ".join(str(title or "").split()).casefold()


def _recently_completed_twin(work_allocator, queue: dict, title: str):
    """A task with the SAME title that COMPLETED inside the snapshot horizon.

    Exact normalized-title match, deliberately not fuzzy, and completed-only.

    These titles are machine-generated from one template --
    ``[QUEUE-FILL] <goal>: Add tests for <path> (<n>% covered, ...)`` -- and
    truncated to 60 characters, so the only distinguishing part (the file) is a
    short tail on a long shared prefix. work_allocator.find_duplicate_task
    scores two such siblings at 0.80, well above its 0.70 duplicate threshold,
    so every G1 coverage title reads as a duplicate of every other one.

    Worse, that matcher also scans non-completed buckets, where
    max_completed_age_hours does not apply. Measured 2026-08-23 -> 26: a single
    BLOCKED task about forge_daemon.py matched a performance_trends.py title and
    vetoed the entire G1 lane for 60 hours -- zero tasks, zero PRs -- with no
    log line, because the skip was only reported in a return value nobody read.

    Blocked and in-flight tasks are excluded here on purpose: this check asks
    "was this exact work just DONE", and add_task still runs its own
    active-duplicate check for everything else.

    Never raises: a dedup failure must cost one wasted task, not the work source.
    """
    horizon_hours = _queue_fill_dedup_hours()
    wanted = _normalize_title(title)
    if not wanted:
        return None

    try:
        now = datetime.now(timezone.utc)
        for task in queue.get("completed") or []:
            if not isinstance(task, dict):
                continue
            if _normalize_title(task.get("title", "")) != wanted:
                continue
            completed_at = task.get("completed_at")
            if not completed_at:
                continue
            try:
                finished = datetime.fromisoformat(
                    str(completed_at).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if (now - finished).total_seconds() <= horizon_hours * 3600:
                return task
    except Exception:
        return None

    return None


# -----------------------------------------------------------------------------
# Mint brake for goals the generic assessor cannot see finish
# -----------------------------------------------------------------------------
#
# assess_generic_success_metric never derives COMPLETE from a product's goal
# text (by design: a wrong "complete" is a silent lie), so a product goal that
# is actually met keeps reading not_started and autofill keeps minting it. The
# mdlint sandbox minted G5 203 times; 32 of those re-mints were parked by the
# daemon's build ceiling, one every ~31 min, and nobody was told once.
#
# Two signals say "further mints of this goal will not move it":
#
# * the build ceiling -- the daemon already refused another build of this
#   goal's title after 5 attempts; and
# * GOAL_REJECTION_STREAK consecutive negative deliverable verdicts on this
#   goal's tasks. K = 3: one or two rejections are within judge variance
#   (sandbox verdicts sit at 0.70-0.92 confidence), three in a row without a
#   pass in between is a pattern -- and in the sandbox the rejections read
#   "the rules already exist, this PR only pads their tests", i.e. the goal
#   was done. 3 is also task_admission's repeatRejectionThreshold default, and
#   it trips before the 5-build ceiling on a loop the judge can see.
#
# Either signal places a HOLD on the goal (state/goal_mint_holds.json) and
# raises ONE escalation, goal-may-be-met-<goal>. The hold is what stops minting.
#
# Exits, in order of preference:
#
# * `goal_tracker.py verify --goal Gn --confirm --evidence "..."` (or `--pr N`)
#   -- the definitive one. The goal reads COMPLETE, is never minted again, and
#   the hold and its escalation are closed automatically.
# * Resolving the escalation ("not met, keep going"). The hold is released and
#   only evidence newer than the release can place the next one. The daemon's
#   build ceiling counts builds per normalized TITLE and never forgets, so the
#   same title would be ceiling-blocked on its first build; each release
#   therefore starts a new ROUND and the next mints carry a "(round N)" suffix,
#   a new title with a fresh ceiling.
#
# While held the escalation never goes silent: the stale archiver moves a
# record untouched for 48 h (and resolve_escalation then cannot find it), so a
# held goal's record is refreshed every ESCALATION_REFRESH_HOURS, and re-raised
# if it has gone missing anyway.

GOAL_REJECTION_STREAK = 3
ESCALATION_REFRESH_HOURS = 24
_BUILD_CEILING_PREFIX = "Build ceiling"
_GOAL_HOLDS_FILE = "state/goal_mint_holds.json"
_GENERIC_ASSESSOR = "generic_success_metric"


def _goal_escalation_id(goal_id: str) -> str:
    return f"goal-may-be-met-{goal_id}"


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_goal_holds(company_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads((company_dir / _GOAL_HOLDS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_goal_holds(company_dir: Path, holds: dict[str, dict]) -> None:
    path = company_dir / _GOAL_HOLDS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(holds, indent=2), encoding="utf-8")
    tmp.replace(path)


def _hold_active(hold: dict | None) -> bool:
    return bool(hold) and bool(hold.get("held_at")) and not hold.get("released_at")


def _goal_round(holds: dict[str, dict], goal_id: str) -> int:
    try:
        return max(1, int((holds.get(goal_id) or {}).get("round", 1)))
    except (TypeError, ValueError):
        return 1


def _round_suffix(holds: dict[str, dict], goal_id: str) -> str:
    """Title suffix for a goal's mints after a released hold ("" in round 1)."""
    n = _goal_round(holds, goal_id)
    return f" (round {n})" if n > 1 else ""


def _escalation_paths(company_dir: Path, goal_id: str) -> tuple[Path, Path]:
    name = f"{_goal_escalation_id(goal_id)}.json"
    return (
        company_dir / "escalations" / name,
        company_dir / "archive" / "escalations" / name,
    )


def _read_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _goal_escalation_resolved_at(company_dir: Path, goal_id: str) -> datetime | None:
    """When the operator resolved this goal's escalation, or None if they have not.

    Looks in the live escalations directory and in the archive the stale
    archiver moves records into -- an archived-as-stale record is NOT resolved.
    """
    for path in _escalation_paths(company_dir, goal_id):
        record = _read_record(path)
        if not record or record.get("status") != "resolved":
            continue
        resolved = None
        for event in reversed(record.get("events") or []):
            if isinstance(event, dict) and event.get("resolved_at"):
                resolved = _parse_ts(event["resolved_at"])
                break
        return resolved or _parse_ts(record.get("updated_at"))
    return None


def _goal_queue_fill_tasks(queue: dict, goal_id: str) -> list[dict]:
    """Every autofill-minted task for ``goal_id``, in any lane."""
    tasks: list[dict] = []
    for bucket in (queue or {}).values():
        if not isinstance(bucket, list):
            continue
        for task in bucket:
            if not isinstance(task, dict) or task.get("source") != "gap_analysis":
                continue
            match = _QUEUE_FILL_GOAL_RE.match(str(task.get("title") or ""))
            if match and match.group(1) == goal_id:
                tasks.append(task)
    return tasks


def _list_open_prs_for(project_root: Path) -> list[dict] | None:
    """Open PRs of the repo at ``project_root`` (None: not a checkout / no gh)."""
    if not (project_root / ".git").exists():
        return None
    try:
        from . import pr_reality_check
    except ImportError:
        import pr_reality_check  # type: ignore[no-redef]
    return pr_reality_check.list_open_prs(project_root=project_root)


def _goal_pr_in_flight(queue: dict, goal_id: str, load_open_prs: Any) -> str | None:
    """Task id of an autofill task for ``goal_id`` whose PR is already under review.

    Two ways a goal is in progress although nothing is pending: a task parked in
    ``pr_open``, and a task the queue still shows as blocked / failed /
    in_progress while an open PR that owns it exists (a delivery the daemon
    failed to record). Minting again builds the same goal twice — measured
    2026-09-28: a second G7 worker started while the first's PR was in review.
    ``load_open_prs`` is only called when the second case needs it.
    """
    lanes = queue or {}
    parked = _goal_queue_fill_tasks({"pr_open": lanes.get("pr_open") or []}, goal_id)
    if parked:
        return str(parked[0].get("task_id") or "unknown")
    unrecorded = _goal_queue_fill_tasks(
        {lane: lanes.get(lane) or [] for lane in ("in_progress", "blocked", "failed")},
        goal_id,
    )
    task_ids = [str(t["task_id"]) for t in unrecorded if t.get("task_id")]
    if not task_ids:
        return None
    try:
        from . import pr_reality_check
    except ImportError:
        import pr_reality_check  # type: ignore[no-redef]
    # Check the state here, not only in the loader's gh query: a CLOSED PR
    # owned by a blocked task would otherwise veto its goal forever (the
    # 2026-08-26 permanent-veto class).
    open_prs = [
        pr
        for pr in load_open_prs() or []
        if isinstance(pr, dict) and str(pr.get("state") or "").upper() == "OPEN"
    ]
    for task_id in task_ids:
        if any(pr_reality_check.pr_owns_task(pr, task_id) for pr in open_prs):
            return task_id
    return None


def _goal_mint_exhaustion(
    company_dir: Path,
    queue: dict,
    goal_id: str,
    *,
    since: datetime | None = None,
    verdicts: dict[str, dict] | None = None,
) -> dict | None:
    """Evidence that minting ``goal_id`` again cannot advance it, or None.

    Only evidence newer than ``since`` (the last release of this goal's hold)
    counts; undated evidence counts only when there has been no release.
    """

    def _after_since(value: Any) -> bool:
        if since is None:
            return True
        when = _parse_ts(value)
        return when is not None and when > since

    tasks = _goal_queue_fill_tasks(queue, goal_id)
    if not tasks:
        return None

    ceiling = [
        t
        for t in tasks
        if str(t.get("blocked_reason") or "").startswith(_BUILD_CEILING_PREFIX)
        and _after_since(t.get("ceiling_blocked_at"))
    ]
    if ceiling:
        return {
            "trigger": "build_ceiling",
            "detail": str(ceiling[-1].get("blocked_reason") or "")[:200],
            "task_ids": [t.get("task_id") for t in ceiling[-5:]],
            "count": len(ceiling),
        }

    if verdicts is None:
        verdicts = goal_tracker.latest_task_verdicts(company_dir)
    judged = []
    for task in tasks:
        record = verdicts.get(task.get("task_id") or "")
        if not record or not _after_since(record.get("timestamp")):
            continue
        judged.append((str(record.get("timestamp") or ""), task.get("task_id"), record))
    judged.sort(key=lambda item: item[0])
    streak = judged[-GOAL_REJECTION_STREAK:]
    if len(streak) == GOAL_REJECTION_STREAK and all(
        item[2].get("addresses_task") is False for item in streak
    ):
        return {
            "trigger": "judge_rejections",
            "detail": str(streak[-1][2].get("reason") or "")[:300],
            "task_ids": [item[1] for item in streak],
            "count": GOAL_REJECTION_STREAK,
        }
    return None


def _write_goal_escalation(
    company_dir: Path, goal_id: str, hold: dict, now: datetime
) -> str:
    """(Re)write the live escalation for an active hold. Returns its id."""
    record_id = _goal_escalation_id(goal_id)
    evidence = hold.get("evidence") or {}
    goal_name = hold.get("goal_name") or goal_id
    next_round = _goal_round({goal_id: hold}, goal_id) + 1
    verify = (
        f'`goal_tracker.py verify --goal {goal_id} --confirm --evidence "..."` '
        f"(or `--pr N` for a merged PR that meets the metric)"
    )
    if evidence.get("trigger") == "build_ceiling":
        why = (
            f"the daemon's build ceiling refused further builds of {goal_id} "
            f"({evidence.get('count')} ceiling-blocked re-mints)"
        )
        on_resolve = (
            "Resolving this escalation releases the hold and autofill resumes "
            f'under a fresh task title ending "(round {next_round})" -- the build '
            "ceiling counts builds per title, so the old title stays blocked and "
            "the new one starts a new 5-build budget."
        )
    else:
        why = (
            f"the deliverable judge rejected the last {evidence.get('count')} "
            f"{goal_id} tasks in a row"
        )
        on_resolve = (
            "Resolving this escalation releases the hold and autofill resumes "
            f'(new titles end "(round {next_round})"); '
            f"{GOAL_REJECTION_STREAK} more rejections in a row hold it again."
        )
    title = f"Goal {goal_id} ({goal_name}) may be met -- verify with {verify}"
    message = (
        f"Autofill stopped minting {goal_id}: {why}. Its generic assessor cannot "
        "tell a met product goal from an unstarted one, so without a verification "
        f"record it would be re-minted forever. If the goal is met, run {verify}: "
        "that is the definitive exit and closes this escalation automatically. "
        f"If it is not met: {on_resolve} This record is kept live (refreshed "
        f"every {ESCALATION_REFRESH_HOURS} h) for as long as the goal is held."
    )
    created = hold.get("held_at") or now.isoformat()
    record = {
        "task_id": record_id,
        "current_tier": 4,
        "status": "pending",
        "created_at": created,
        "updated_at": now.isoformat(),
        "original_agent": "strategic_planner",
        "current_agent": None,
        "trigger": "goal_may_be_met",
        "trigger_details": {
            "goal_id": goal_id,
            "goal_name": goal_name,
            "success_metric": hold.get("success_metric"),
            "evidence": evidence,
            "round": _goal_round({goal_id: hold}, goal_id),
            "message": message,
        },
        "events": [
            {
                "timestamp": created,
                "tier": 4,
                "trigger": "goal_may_be_met",
                "action_taken": title,
                "resolved": False,
            }
        ],
        "metadata": {"title": title, "goal_id": goal_id},
    }
    live, _ = _escalation_paths(company_dir, goal_id)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record_id


def _keep_goal_escalation_live(
    company_dir: Path, goal_id: str, hold: dict, now: datetime
) -> bool:
    """Refresh (or re-raise) an active hold's escalation. Returns True on re-raise."""
    live, _ = _escalation_paths(company_dir, goal_id)
    record = _read_record(live)
    if record is None:
        _write_goal_escalation(company_dir, goal_id, hold, now)
        return True
    updated = _parse_ts(record.get("updated_at"))
    if updated is None or now - updated >= timedelta(hours=ESCALATION_REFRESH_HOURS):
        record["updated_at"] = now.isoformat()
        live.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return False


def _close_holds_for_complete_goals(
    company_dir: Path, assessments: list, holds: dict[str, dict], now: datetime
) -> bool:
    """A held goal that now reads COMPLETE (verified): release it and resolve
    its escalation, so nobody is paged about a goal that is done."""
    changed = False
    for assessment in assessments:
        goal_id = getattr(assessment, "goal_id", None)
        hold = holds.get(goal_id) if isinstance(goal_id, str) else None
        if not _hold_active(hold) or assessment.status.value != "complete":
            continue
        hold["released_at"] = now.isoformat()
        hold["release_reason"] = "goal_verified"
        changed = True
        live, _ = _escalation_paths(company_dir, goal_id)
        record = _read_record(live)
        if record and record.get("status") != "resolved":
            record["status"] = "resolved"
            record["updated_at"] = now.isoformat()
            for event in record.get("events") or []:
                if isinstance(event, dict):
                    event.update(
                        resolved=True,
                        resolution="goal verified; autofill hold closed",
                        resolved_at=now.isoformat(),
                        resolved_by="strategic_planner",
                    )
            live.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return changed


def _goal_mint_held(
    company_dir: Path,
    queue: dict,
    assessment: Any,
    holds: dict[str, dict],
    now: datetime,
) -> tuple[bool, bool]:
    """Should autofill skip this generic-assessed goal? Returns (held, holds_changed).

    Places a new hold (and raises its escalation) on fresh exhaustion evidence;
    keeps an active hold's escalation live; releases a hold whose escalation
    the operator resolved (starting a new round) or whose goal text changed.
    """
    goal_id = assessment.goal_id
    metric = str(assessment.success_metric or "")
    hold = holds.get(goal_id)
    changed = False
    since: datetime | None = None

    if hold:
        if hold.get("success_metric") != metric:
            # The goal was redefined: old evidence is about a different goal.
            hold = {
                "released_at": now.isoformat(),
                "release_reason": "metric_changed",
                "success_metric": metric,
                "round": _goal_round(holds, goal_id) + 1,
            }
            holds[goal_id] = hold
            changed = True
        elif _hold_active(hold):
            held_at = _parse_ts(hold.get("held_at"))
            resolved_at = _goal_escalation_resolved_at(company_dir, goal_id)
            if resolved_at and (held_at is None or resolved_at > held_at):
                hold["released_at"] = resolved_at.isoformat()
                hold["release_reason"] = "escalation_resolved"
                hold["round"] = _goal_round(holds, goal_id) + 1
                changed = True
            else:
                if _keep_goal_escalation_live(company_dir, goal_id, hold, now):
                    hold["reraised"] = int(hold.get("reraised", 0)) + 1
                    changed = True
                return True, changed
        since = _parse_ts(hold.get("released_at"))

    evidence = _goal_mint_exhaustion(company_dir, queue, goal_id, since=since)
    if evidence is None:
        return False, changed

    new_hold = {
        "held_at": now.isoformat(),
        "success_metric": metric,
        "goal_name": assessment.goal_name,
        "evidence": evidence,
        "round": _goal_round(holds, goal_id),
    }
    new_hold["escalation_id"] = _write_goal_escalation(
        company_dir, goal_id, new_hold, now
    )
    holds[goal_id] = new_hold
    return True, True


def autofill_queue_from_goals(
    company_dir: Path,
    *,
    threshold: int = 3,
    max_tasks_to_add: int = 3,
    lightweight: bool = True,
    last_ideation: str = "",
    defer_to_ideation: bool = False,
    rejection_backoff_hours: float = 6.0,
    open_prs_loader: Any = None,
) -> dict[str, Any]:
    """WS-120: Auto-fill work queue when pending tasks fall below threshold.

    When the number of pending tasks drops below threshold, assesses all
    active goals, finds those with the lowest progress metric, and generates
    one task per goal until the queue reaches the threshold.

    Args:
        company_dir: Path to the .company directory.
        threshold: Minimum desired pending task count (default 3).
        max_tasks_to_add: Upper bound on tasks created in a single call.
        lightweight: Pass to assess_all_goals to skip expensive ops (pytest).
        rejection_backoff_hours: Skip a goal whose gap_analysis-sourced task
            was admission-rejected within this many hours, instead of
            re-minting it every fill cycle (default 6h).
        open_prs_loader: ``callable(project_root) -> list[dict] | None`` giving
            the repo's open PRs (injectable for tests; defaults to ``gh``).
            A goal with a task in ``pr_open``, or whose blocked / failed /
            in_progress task has an open PR, is in progress and mints nothing.

    Returns:
        Dict with keys:
          - tasks_created: int -- number of tasks added
          - pending_before: int -- queue depth before fill
          - goals_targeted: list[str] -- goal IDs that received tasks
          - goals_duplicate_skipped: list[str] -- goal IDs whose brief was
            already completed inside the snapshot dedup horizon, so re-minting
            it would only reproduce work already merged
          - goals_quality_skipped: list[str] -- goal IDs held back by the
            brief-quality gate (brief lacked a verified pointer and/or a
            measured value; recorded in state/autofill_brief_skips.jsonl)
          - skipped: bool -- True when queue was already at/above threshold
    """
    _ensure_imports()

    # Load work allocator to read queue and add tasks
    try:
        try:
            from . import work_allocator
        except ImportError:
            import work_allocator  # type: ignore[no-redef]
    except ImportError:
        return {
            "tasks_created": 0,
            "skipped": True,
            "error": "work_allocator unavailable",
        }

    # Count current pending tasks and check cross-lane saturation
    queue: dict = {}
    try:
        # Promote any satisfied blocked tasks via update_dependencies.
        # This runs even on idle cycles (when pull_next_task is skipped),
        # fixing the "stranded blocked task" gap where a task could wait
        # indefinitely if its dependencies landed via pr_open->completed
        # reconciliation path (not via update_task_status status=completed).
        lock_path = work_allocator.get_lock_path()
        with work_allocator.QueueLock(lock_path):
            queue = work_allocator.load_queue()
            if work_allocator.update_dependencies(queue):
                work_allocator.save_queue(queue)

        # Reload to get updated pending count after promotion
        queue = work_allocator.load_queue()
        pending_before = len(queue.get("pending", []))
        # E7: Cross-lane saturation guard — skip fill if workers are already busy.
        # Counts tasks actively being worked (in_progress + pr_open) so fill doesn't
        # saturate the pipeline when PRs are queued up waiting for review. Repair/
        # divergence and challenger-stage follow-up tasks are excluded (NO-
        # DIVERGENCE-FOR-UNBUILT-ENTRIES): they are not goal-building work, and
        # goal autofill must not starve just because several of them are open as
        # PRs at once -- see _is_repair_or_challenger_task.
        active_tasks = queue.get("in_progress", []) + queue.get("pr_open", [])
        active_count = sum(
            1 for t in active_tasks if not _is_repair_or_challenger_task(t)
        )
    except Exception:
        pending_before = 0
        active_count = 0

    if pending_before >= threshold:
        return {
            "tasks_created": 0,
            "pending_before": pending_before,
            "goals_targeted": [],
            "skipped": True,
        }

    if active_count >= threshold:
        return {
            "tasks_created": 0,
            "pending_before": pending_before,
            "goals_targeted": [],
            "skipped": True,
            "reason": "cross_lane_saturation",
        }

    # E7-ext: Ideation-first ordering — defer until the direction pipeline has had a turn.
    if _should_defer_fill_to_ideation(last_ideation, defer_to_ideation):
        return {
            "tasks_created": 0,
            "pending_before": pending_before,
            "goals_targeted": [],
            "skipped": True,
            "reason": "ideation_first",
        }

    # Greenfield detection: if no source files exist yet, mint build-the-deliverable tasks
    # based on the goal's Success Metric rather than tooling/slash-command actions.
    project_root = company_dir.parent
    try:
        is_greenfield = not any(
            True
            for f in list(project_root.glob("*.py"))
            + list(project_root.glob("src/**/*.py"))
            if f.name not in _GREENFIELD_INFRA_NAMES and ".claude" not in str(f)
        )
    except Exception:
        is_greenfield = False

    # Assess all active goals (lightweight to avoid blocking)
    try:
        assessments = goal_tracker.assess_all_goals(
            company_dir, lightweight=lightweight
        )
    except Exception:
        return {
            "tasks_created": 0,
            "pending_before": pending_before,
            "goals_targeted": [],
            "skipped": True,
            "error": "goal assessment failed",
        }

    # Filter out completed goals; sort by progress ascending (lowest first)
    incomplete = [a for a in assessments if a.status.value != "complete"]
    incomplete.sort(key=lambda a: a.progress_percent)

    # Dependencies gate minting exactly as they gate scheduling and gap
    # analysis. Before this, autofill read neither depends_on nor priorities:
    # the mdlint sandbox minted G2..G10 (all "dependsOn: G1") hundreds of times
    # while goal_scheduler logged every one of them at priority 0.0.
    status_by_goal = {a.goal_id: a.status.value for a in assessments}
    goals_dependency_skipped: list[str] = []
    ready = []
    for assessment in incomplete:
        blocking = _incomplete_dependencies(assessment, status_by_goal)
        if blocking:
            _record_brief_quality_skip(
                company_dir,
                assessment.goal_id,
                [f"dependency_incomplete:{','.join(blocking)}"],
                f"{assessment.goal_id} waits on {', '.join(blocking)}",
            )
            goals_dependency_skipped.append(assessment.goal_id)
            continue
        ready.append(assessment)

    # Mint brake: a generic-assessed goal whose re-mints hit the build ceiling
    # or GOAL_REJECTION_STREAK judge rejections is held, with ONE escalation.
    goals_held: list[str] = []
    holds = _load_goal_holds(company_dir)
    now = datetime.now(timezone.utc)
    try:
        holds_changed = _close_holds_for_complete_goals(
            company_dir, assessments, holds, now
        )
    except Exception:
        holds_changed = False
    incomplete = []
    for assessment in ready:
        raw = getattr(assessment, "raw_data", None)
        if isinstance(raw, dict) and raw.get("assessor") == _GENERIC_ASSESSOR:
            try:
                held, changed = _goal_mint_held(
                    company_dir, queue, assessment, holds, now
                )
            except Exception:
                held, changed = False, False  # a broken brake must not stop the fill
            holds_changed = holds_changed or changed
            if held:
                hold = holds.get(assessment.goal_id) or {}
                _record_brief_quality_skip(
                    company_dir,
                    assessment.goal_id,
                    [f"mint_held:{hold.get('escalation_id', 'unknown')}"],
                    f"{assessment.goal_id} held: may be met, awaiting verification",
                )
                goals_held.append(assessment.goal_id)
                continue
        incomplete.append(assessment)
    if holds_changed:
        try:
            _save_goal_holds(company_dir, holds)
        except OSError:
            pass

    # Determine how many tasks we need to fill the queue
    needed = min(threshold - pending_before, max_tasks_to_add, len(incomplete))
    if needed <= 0:
        return {
            "tasks_created": 0,
            "pending_before": pending_before,
            "goals_targeted": [],
            "goals_dependency_skipped": goals_dependency_skipped,
            "goals_held": goals_held,
            "skipped": True,
        }

    rejected_goal_ids = _recently_rejected_goal_ids(
        company_dir, window_hours=rejection_backoff_hours
    )

    tasks_created = 0
    goals_targeted: list[str] = []
    goals_quality_skipped: list[str] = []
    goals_duplicate_skipped: list[str] = []

    open_prs_cache: list[list[dict]] = []

    def _open_prs() -> list[dict]:
        """Open PRs, listed at most once per fill run; unreachable reads as none."""
        if not open_prs_cache:
            try:
                open_prs_cache.append(
                    (open_prs_loader or _list_open_prs_for)(project_root) or []
                )
            except Exception:
                open_prs_cache.append([])
        return open_prs_cache[0]

    # Iterate the FULL incomplete list (not incomplete[:needed]): goals sort
    # lowest-progress-first, so a NOT_STARTED owner/infra-only or recently-
    # rejected goal would otherwise sort to the front and consume the slice's
    # slots via `continue`, starving healthy buildable goals just past the
    # boundary. Break once `needed` tasks are actually created instead.
    for assessment in incomplete:
        if tasks_created >= needed:
            break

        # Build task title and description from goal data
        actions = assessment.next_actions or []
        executable_actions: list[str] = []

        # A goal whose actions carry an [INFRA-ONLY] marker has explicitly
        # declared that no worker session can advance it right now (e.g.
        # G1's nightly coverage job); [OWNER-ONLY] marks an action that only
        # a human can perform (owner decision, business development/sales —
        # e.g. G13's "close a sale"). Either way, skip the goal entirely, on
        # every branch including greenfield. Minting from the success_metric
        # instead would re-mint the same unbuildable work under a different
        # title (PR 263 review: treadmill softened, not stopped).
        if any(_INFRA_ONLY_RE.match(a) or _OWNER_ONLY_RE.match(a) for a in actions):
            continue

        # The admission gate already rejected this goal's auto-filled task
        # recently (e.g. its semantic judge correctly ruled it non-software
        # business-dev work) — re-minting it every cycle is the reject-loop
        # this backoff exists to stop. Skip until the backoff window elapses.
        if assessment.goal_id in rejected_goal_ids:
            continue

        if is_greenfield and assessment.success_metric:
            # On a greenfield repo the deliverable doesn't exist yet — mint a
            # build task directly from the Success Metric, never a slash-command
            # invocation or a docs/tutorial task that presupposes working code.
            action_text = assessment.success_metric
        else:
            # Filter slash-commands anywhere in action text (e.g. "Run /test-sprint to...")
            # not just those that start with '/'.
            executable_actions = [a for a in actions if not _SLASH_CMD_RE.search(a)]
            if executable_actions:
                action_text = executable_actions[0]
            elif assessment.success_metric:
                action_text = assessment.success_metric
            else:
                action_text = f"Advance {assessment.goal_name} goal"

        # Brief-quality gate: a non-greenfield brief must be concrete enough to
        # act on, or it doesn't ship. The skip is recorded (observable) and the
        # goal is retried next cycle, so a brief that gains evidence later
        # passes automatically.
        #
        # What "concrete" means depends on the intent (see _FIX_INTENT_RE):
        #   fix   -> name an EXISTING file to change, plus a measured value.
        #            Existence matters: a fix pointing at a path that isn't
        #            there is a hallucinated pointer.
        #   build -> name the artifact to produce. Existence must NOT be
        #            required, because the artifact is the thing being created.
        #            No measurement is demanded either: task_admission.py (#325)
        #            already rejects gradient claims that lack a baseline, and
        #            duplicating that here is what blocked every build goal.
        verified = []
        if not is_greenfield:
            evidence_text = " ".join([action_text, *executable_actions[1:4]])
            verified = _verified_pointers(evidence_text, project_root)
            missing = []
            if _is_fix_intent(evidence_text):
                if not _existing_file_pointers(evidence_text, project_root):
                    missing.append("verified_pointer")
                if not _has_measurement(evidence_text):
                    missing.append("measured_evidence")
            elif not _concrete_targets(evidence_text):
                missing.append("concrete_target")
            if missing:
                _record_brief_quality_skip(
                    company_dir, assessment.goal_id, missing, action_text
                )
                goals_quality_skipped.append(assessment.goal_id)
                continue

        # Sanitize action_text: strip newlines before embedding in task title
        # (goal data comes from vision.md parsing and should have no embedded
        # newlines, but this defensive measure prevents accidental markdown injection)
        sanitized_action = action_text.replace("\n", " ")

        description_parts = [
            f"[Queue Auto-Fill] Task generated because queue dropped below {threshold} pending tasks.",
            "",
            f"**Goal:** {assessment.goal_id} -- {assessment.goal_name}",
            f"**Progress:** {assessment.progress_percent}% (target: 100%)",
            f"**Status:** {assessment.status.value}",
            "",
            f"**Action Required:** {action_text}",
        ]
        if len(executable_actions) > 1:
            description_parts.append("")
            description_parts.append("**Additional Actions:**")
            for i, act in enumerate(executable_actions[1:4], 2):  # Max 3 extras
                description_parts.append(f"{i}. {act}")

        description_parts.extend(
            [
                "",
                f"**Goal Metric:** {assessment.success_metric}",
            ]
        )
        if verified:
            description_parts.extend(
                [
                    "",
                    "**Verified Pointers** (existence checked at mint time): "
                    + ", ".join(verified[:8]),
                ]
            )
        description_parts.extend(["", *_TEST_REQUIREMENT_LINES])

        # After a released hold the goal mints under a NEW title: the daemon's
        # build ceiling counts per normalized title and would block the old
        # one on its first build (see the mint-brake notes above).
        title = (
            f"[QUEUE-FILL] {assessment.goal_id}: {sanitized_action[:60]}"
            f"{_round_suffix(holds, assessment.goal_id)}"
        )

        # A goal reading a nightly snapshot re-proposes the SAME work until
        # that snapshot is rewritten, so the dedup horizon has to cover the
        # evidence's staleness, not the operator's attention span.
        # add_task's own check uses RECENT_COMPLETION_HOURS (4h), which these
        # mints walk straight past: measured 2026-08-22/23, one identical G1
        # title was minted at 15:45, again at 20:13 (+4h28m) and again at
        # 02:17 (+6h04m). Seven QUEUE-FILL PRs resulted and four were
        # auto-closed on irreconcilable conflicts, because each worker was
        # writing overlapping tests into the same two files.
        # backlog_generator learned this in #433; this mint path never got it.
        try:
            in_flight = _goal_pr_in_flight(queue, assessment.goal_id, _open_prs)
        except Exception:
            in_flight = None  # a broken check must not stop the fill
        if in_flight:
            _record_brief_quality_skip(
                company_dir,
                assessment.goal_id,
                [f"pr_in_flight:{in_flight}"],
                title,
            )
            goals_duplicate_skipped.append(assessment.goal_id)
            continue

        twin = _recently_completed_twin(work_allocator, queue, title)
        if twin:
            # Record it. Reporting this only in the return value made the skip
            # invisible: when a false positive vetoed the whole G1 lane for 60
            # hours, the queue simply read "0 pending" and the daemon logged
            # nothing at all. A gate that hides its own skips is indistinguishable
            # from a caught-up queue -- the same trap as the brief-quality gate,
            # which is why this lands in the same file operators already read.
            _record_brief_quality_skip(
                company_dir,
                assessment.goal_id,
                [f"recent_duplicate:{twin.get('task_id', 'unknown')}"],
                title,
            )
            goals_duplicate_skipped.append(assessment.goal_id)
            continue

        try:
            result = work_allocator.add_task(
                title=title,
                description="\n".join(description_parts),
                priority=3,  # P3-Normal: filler must not outrank the goal work it supports
                source="gap_analysis",
                estimated_complexity="standard",
                # Templated titles: siblings score 0.80 on the fuzzy matcher (#450).
                exact_duplicate_only=True,
            )
            if result.get("success"):
                tasks_created += 1
                goals_targeted.append(assessment.goal_id)
            else:
                # A refusal here used to vanish: only successes were counted,
                # so add_task's own duplicate check could reject every mint for
                # a goal and the run still reported "0 created" with an empty
                # skip list. That is how a blocked-task veto went unnoticed for
                # 60 hours. Record it where the other skips already land.
                _record_brief_quality_skip(
                    company_dir,
                    assessment.goal_id,
                    [f"add_task_refused:{result.get('error', 'unknown')}"],
                    title,
                )
                goals_duplicate_skipped.append(assessment.goal_id)
        except Exception:
            pass  # Don't fail the entire fill on a single task error

    return {
        "tasks_created": tasks_created,
        "pending_before": pending_before,
        "goals_targeted": goals_targeted,
        "goals_quality_skipped": goals_quality_skipped,
        "goals_duplicate_skipped": goals_duplicate_skipped,
        "goals_dependency_skipped": goals_dependency_skipped,
        "goals_held": goals_held,
        "skipped": False,
    }
