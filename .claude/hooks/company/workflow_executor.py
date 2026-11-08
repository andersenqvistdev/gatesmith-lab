#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Workflow executor — schema-validated per-task execution (autonomy "Phase 2").

Plan: `.planning/autonomy/workflow-execution-engine-plan.md`.

The daemon's default executor infers success from exit code + output-marker
heuristics (`employee_activator._detect_task_success`). That heuristic is the
phantom-completion root cause the rest of the autonomy stack has been working
around. This module replaces it, for opted-in tasks, with a DETERMINISTIC
verdict grounded in the real diff:

  Pass 1 — work:   run the proven `activate_employee_for_task` flow (full AGENT
                   mode, real file edits, git-capture → branch + PR, P95
                   deliverable enforcement). This produces ground truth, and the
                   work is committed/PR'd BEFORE we judge, so nothing is lost.
  Pass 2 — verdict: run the adversarial deliverable judge over the captured
                   diff (reusing `deliverable_judge`). `addresses_task=true` AND
                   `confidence >= threshold` is the success signal. The verdict
                   can only make success STRICTER — it demotes a phantom that the
                   work pass would have marked complete; it never resurrects a
                   failed task and never loses work.

Why not force schema output on the WORK pass: the CLI's `--json-schema` only
works with `--print`, the mode WS-121 removed because it made the worker a text
generator that had to be coerced into using tools — the original phantom source.
So the schema/judge pass runs separately over the diff (it uses `-p`, no tools).

Design invariants (see the plan):
  * Dual opt-in + fail-closed fallback. Default OFF. When disabled, return the
    `{"fell_back": True}` sentinel so the caller runs the legacy path. After the
    work pass has run we NEVER return that sentinel (that would double-execute);
    instead `onJudgeError="fallback"` keeps the legacy success signal.
  * Reuse the calibrate/deliverable judge as the single source of truth (framing,
    schema, prompt, CLI runner) — this module adds no new adversarial prompt.
  * All logic lives here (unprotected); the protected `operation_loop` takes only
    a minimal dispatch hook (see plan Phase 2).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Reuse the single-source-of-truth judge machinery and the proven activation
# flow. Support both package and direct-script import (mirrors operation_loop).
try:  # pragma: no cover - import shim
    from . import (
        company_resolver,
        deliverable_judge,
        employee_activator,
        task_admission,
    )
except ImportError:  # pragma: no cover - import shim
    import company_resolver  # type: ignore[no-redef]
    import deliverable_judge  # type: ignore[no-redef]
    import employee_activator  # type: ignore[no-redef]
    import task_admission  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _config_defaults() -> dict:
    return {
        # Master opt-in. OFF => never enter the workflow path.
        "enabled": False,
        # addresses_task=true is honoured only at/above this confidence.
        "confidenceThreshold": 0.7,
        # On a judge error (CLI failure / unparseable / no diff to judge):
        #   "fallback" => keep the legacy success signal from the work pass
        #                 (fail-closed-to-work — never lose completed work).
        #   "block"    => demote to failure so the task is retried.
        "onJudgeError": "fallback",
        # Judge model alias; null => CLI default. Judge timeout / diff cap.
        "model": None,
        "timeoutSeconds": 240,
        "maxDiffChars": 60000,
    }


def load_workflow_config() -> dict:
    """Load ``autonomy.workflowExecution`` from gatesmith-config, with safe defaults.

    Root ``gatesmith.json`` is canonical (#1052); the ``.claude/`` copy is the
    legacy fallback. Unknown keys are ignored; missing keys default. Any error
    yields the defaults (flag absent => OFF), so this module is a no-op until a
    human explicitly enables it.
    """
    defaults = _config_defaults()
    for config_path in (
        Path.cwd() / "gatesmith.json",
        Path.cwd() / ".claude" / "gatesmith.json",
    ):
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                block = config.get("autonomy", {}).get("workflowExecution", {})
                if isinstance(block, dict):
                    return {k: block.get(k, v) for k, v in defaults.items()}
            except (json.JSONDecodeError, OSError):
                pass
    return defaults


def should_use_workflow(task: dict, config: dict | None = None) -> bool:
    """Whether this task should run through the schema-validated workflow path.

    False unless the config flag is on. ``task`` is accepted for forward
    compatibility (per-task opt-in / capability gating) but is not used in v1.
    """
    if config is None:
        config = load_workflow_config()
    return bool(config.get("enabled", False))


# ---------------------------------------------------------------------------
# Verdict pass (reuses deliverable_judge)
# ---------------------------------------------------------------------------

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


def _pr_number_from_url(pr_url: str | None) -> str | None:
    if not pr_url:
        return None
    m = _PR_NUM_RE.search(pr_url)
    return m.group(1) if m else None


def judge_task_diff(
    task_id: str,
    title: str,
    description: str,
    pr_number: str | int,
    diff_text: str,
    meta_json: str,
    *,
    config: dict,
    product_name: str | None = None,
    product_description: str | None = None,
    goal_metric: str | None = None,
) -> deliverable_judge.DeliverableVerdict:
    """Judge whether ``diff_text`` substantively delivers the task.

    Reuses ``deliverable_judge``'s adversarial framing, schema, prompt, CLI
    runner and decision rule (single source of truth). Returns a
    ``DeliverableVerdict`` whose ``addresses_task`` is ``None`` on judge error
    (the caller maps that via ``onJudgeError``).

    Optional ``product_name``, ``product_description``, and ``goal_metric`` anchor
    the judgment to this specific product and goal. When omitted, product identity
    is auto-loaded from org.json by ``deliverable_judge.load_product_identity()``.
    """
    threshold = float(config.get("confidenceThreshold", 0.7))

    def _error(reason: str) -> deliverable_judge.DeliverableVerdict:
        return deliverable_judge.DeliverableVerdict(
            task_id=task_id,
            addresses_task=None,
            confidence=None,
            reason=reason,
            blocked=True,
            needs_manual_review=True,
            error=reason,
        )

    if not (diff_text or "").strip():
        return _error("no diff to judge — nothing delivered")

    # Auto-load product identity when caller does not provide it.
    if product_name is None and product_description is None:
        identity = deliverable_judge.load_product_identity()
        product_name = identity["product_name"]
        product_description = identity["product_description"]

    # Tripwire: hard-fail before calling the LLM. Exemption keyed on git remote.
    tripwire_msg = deliverable_judge._check_identity_tripwire(
        diff_text,
        product_name,
        is_framework_repo=deliverable_judge._is_framework_repo(),
    )
    if tripwire_msg:
        return deliverable_judge.DeliverableVerdict(
            task_id=task_id,
            addresses_task=False,
            confidence=1.0,
            reason=tripwire_msg,
            blocked=True,
            needs_manual_review=True,
            error=tripwire_msg,
        )

    prompt = deliverable_judge.build_judge_prompt(
        task_id,
        title,
        description,
        pr_number,
        meta_json,
        diff_text,
        product_name=product_name,
        product_description=product_description,
        goal_metric=goal_metric,
    )
    stdout, err = deliverable_judge._run_judge_cli(prompt, config)
    if err and not stdout:
        return _error(f"judge error: {err}")

    parsed = deliverable_judge.parse_verdict_json(stdout or "")
    if not parsed:
        klass, excerpt = deliverable_judge.classify_judge_failure(stdout, err)
        return _error(
            f"judge output had no parseable verdict JSON ({klass}): {excerpt}"
        )

    addresses = parsed.get("addresses_task")
    if not isinstance(addresses, bool):
        return _error("judge verdict missing boolean addresses_task")

    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    reason = str(parsed.get("reason", "")) or "(no reason given)"

    blocked, needs_review = deliverable_judge._decide(addresses, confidence, threshold)
    return deliverable_judge.DeliverableVerdict(
        task_id=task_id,
        addresses_task=addresses,
        confidence=confidence,
        reason=reason,
        blocked=blocked,
        needs_manual_review=needs_review,
    )


def _verdict_is_success(
    verdict: deliverable_judge.DeliverableVerdict, threshold: float
) -> bool:
    return verdict.addresses_task is True and (
        verdict.confidence is not None and verdict.confidence >= threshold
    )


def _prime_deliverable_gate_cache(
    pr_number: str | int,
    verdict: deliverable_judge.DeliverableVerdict,
    judged_sha: str | None = None,
) -> bool:
    """Write the workflow verdict into the deliverable gate's sha-keyed cache.

    The workflow verdict and the downstream pre-merge ``run_deliverable_gate``
    judge the SAME PR diff against the SAME task with the SAME judge — so the
    gate can reuse this verdict instead of making a second (expensive) judge
    call. We prime the gate's existing verdict cache (keyed by PR head sha +
    task_id, see ``deliverable_judge.judge_pr_deliverable``); the gate then takes
    its cache-hit path. Best-effort: on any failure the gate simply re-judges, so
    correctness never depends on this — it only removes the double-judge cost.
    Returns True if the cache was primed.

    ``judged_sha`` is the head commit read BEFORE the diff was fetched. The
    cache is keyed by commit, so the verdict may only be filed under the commit
    it judged: when the head moved while the judge ran (a retry pushed to the
    same branch), nothing is primed and the new head gets a verdict of its own.
    """
    try:
        sha = deliverable_judge.get_pr_head_sha(pr_number)
        if not sha:
            return False
        if judged_sha is not None and sha != judged_sha:
            _log.warning(
                "PR #%s head moved %s -> %s while judging; not caching the verdict",
                pr_number,
                judged_sha[:12],
                sha[:12],
            )
            return False
        verdict.sha = sha
        cache = deliverable_judge._load_cache()
        cache[sha] = {
            "task_id": verdict.task_id,
            "addresses_task": verdict.addresses_task,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "blocked": verdict.blocked,
            "needs_manual_review": verdict.needs_manual_review,
            "error": verdict.error,
        }
        deliverable_judge._save_cache(cache)
        return True
    except Exception as exc:
        _log.debug("cache prime failed (best-effort): %s", exc)
        return False


def _hold_rejected_pr(
    task: dict,
    pr_url: str | None,
    verdict: deliverable_judge.DeliverableVerdict,
) -> dict:
    """Label + record a PR whose task this executor just failed on its verdict.

    The pre-merge gate in ``operation_loop`` runs only on a SUCCESSFUL task, so
    a PR the workflow judge rejected was left open, unlabeled, and with no row
    in ``deliverable_gate.jsonl`` — and both merge levers (the daemon's
    full-trust merge and ci.yml's Auto-Merge job) read only the hold label.
    Best-effort: a failure here never changes the task result.
    """
    try:
        try:
            from . import pr_output_manager
        except ImportError:
            import pr_output_manager  # type: ignore[no-redef]
        return pr_output_manager.hold_pr_for_verdict(
            str(task.get("task_id", "unknown")), pr_url, verdict
        )
    except Exception as exc:  # noqa: BLE001 — the hold must never break the task
        _log.warning("could not hold rejected PR %s: %s", pr_url, exc)
        return {"label_applied": False, "recorded": False, "error": str(exc)}


def _record_deliverable_rejection(task: dict, reason: str) -> None:
    """Log a workflow-phantom demotion into the admission rejection log.

    ``strategic_planner._recently_rejected_goal_ids`` already backs off
    re-minting a ``[QUEUE-FILL] <goal>: ...`` autofill task for
    ``window_hours`` after that goal's task was rejected — but it only reads
    records written by the pre-execution admission gate
    (``task_admission.log_rejection``), never this post-execution
    deliverable-judge demotion. Without this, a goal whose PR the judge just
    rejected has no backoff at all and the very next autofill cycle can
    immediately re-mint the same task, racing a still-in-flight recovery
    retry of the original task_id. Reusing the existing log/reader pair
    (same ``source=="gap_analysis"`` + title-regex filter, no changes needed
    to ``strategic_planner.py``) closes that gap. Best-effort: never allowed
    to affect the workflow result.
    """
    try:
        repo_root = company_resolver.get_company_dir().parent
    except Exception:
        repo_root = Path.cwd()
    try:
        task_admission.log_rejection(repo_root, task, reason)
    except Exception as exc:
        _log.debug("deliverable-rejection log failed (best-effort): %s", exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_task_workflow(
    task: dict,
    *,
    execution_cwd: str | None = None,
    fallback_agent_id: str | None = None,
) -> dict[str, Any]:
    """Run a task through the schema-validated workflow path.

    Returns the SAME dict shape as ``activate_employee_for_task`` (so the
    dispatch hook is a drop-in), plus ``execution_mode="workflow"`` and, when a
    verdict ran, ``workflow_verdict``.

    Returns the ``{"fell_back": True}`` sentinel ONLY before any work has run
    (the workflow path is disabled). Once the work pass has executed we never
    return that sentinel — the work is already captured/PR'd, so a judge problem
    is handled in-place via ``onJudgeError`` rather than by re-running the task.
    """
    config = load_workflow_config()
    if not config.get("enabled", False):
        return {"fell_back": True}

    # Pass 1 — work. Reuse the proven activation flow (employee match, context,
    # execute, git-capture → PR, P95). This commits/PRs the work before we judge.
    result = employee_activator.activate_employee_for_task(
        task, fallback_agent_id, execution_cwd
    )
    result["execution_mode"] = "workflow"

    # If the work pass already failed, there is nothing to judge — keep the
    # failure (the verdict can only make success stricter, never resurrect).
    if not result.get("success"):
        return result

    # Pass 2 — verdict over the captured diff.
    git_capture = result.get("git_capture") or {}
    pr_url = git_capture.get("pr_url")
    pr_number = _pr_number_from_url(pr_url)
    on_error = config.get("onJudgeError", "fallback")
    threshold = float(config.get("confidenceThreshold", 0.7))

    def _on_judge_problem(reason: str) -> dict[str, Any]:
        # "fallback" => trust the legacy success signal (fail-closed-to-work).
        # "block"    => demote so the task is retried. Never re-run here.
        result["workflow_verdict"] = {
            "addresses_task": None,
            "confidence": None,
            "reason": reason,
            "errored": True,
        }
        if on_error == "block":
            result["success"] = False
            result["reason"] = "workflow_judge_error"
            result["message"] = f"Workflow judge could not verify delivery: {reason}"
            # The task is failed, so operation_loop's gate will not run — hold
            # the PR here, or it sits open and unlabeled for both merge levers.
            if pr_number:
                result["workflow_hold"] = _hold_rejected_pr(
                    task,
                    pr_url,
                    deliverable_judge.DeliverableVerdict(
                        task_id=task.get("task_id", "unknown"),
                        addresses_task=None,
                        confidence=None,
                        reason=reason,
                        blocked=True,
                        needs_manual_review=True,
                        error=reason,
                        sha=judged_sha,
                        pr_url=pr_url,
                    ),
                )
        return result

    judged_sha: str | None = None
    if not pr_number:
        return _on_judge_problem("no PR/diff produced to judge")

    # Read the head BEFORE the diff: the verdict belongs to this commit only.
    judged_sha = deliverable_judge.get_pr_head_sha(pr_number)
    ctx = deliverable_judge.fetch_pr_context(
        pr_number, int(config.get("maxDiffChars", 60000))
    )
    if ctx is None:
        return _on_judge_problem("could not fetch diff for workflow judgment")
    meta_json, diff_text = ctx

    _MAX_TITLE = 500
    _MAX_DESC = 4000
    verdict = judge_task_diff(
        task.get("task_id", "unknown"),
        (task.get("title") or "")[:_MAX_TITLE],
        (task.get("description") or "")[:_MAX_DESC],
        pr_number,
        diff_text,
        meta_json,
        config=config,
    )

    if verdict.addresses_task is None:
        return _on_judge_problem(verdict.error or "judge produced no verdict")

    # Forward the verdict to the pre-merge deliverable gate so it reuses this
    # judgment (same PR diff, same task, same judge) instead of re-judging.
    _prime_deliverable_gate_cache(pr_number, verdict, judged_sha)

    success = _verdict_is_success(verdict, threshold)
    result["success"] = success
    result["workflow_verdict"] = {
        "addresses_task": verdict.addresses_task,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "errored": False,
    }
    if not success:
        result["reason"] = "workflow_phantom"
        result["message"] = (
            f"Workflow verdict: diff does not deliver the task "
            f"(confidence {verdict.confidence}): {verdict.reason}"
        )
        _record_deliverable_rejection(task, result["message"])
        # Hold the PR: the task is failed, so no later gate will label it.
        verdict.pr_url = pr_url
        if verdict.sha is None:
            verdict.sha = judged_sha
        result["workflow_hold"] = _hold_rejected_pr(task, pr_url, verdict)
    return result
