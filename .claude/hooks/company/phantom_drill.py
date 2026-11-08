#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Phantom Drill backend — synthetic phantom scenarios for the deliverable-judge lab kit.

Imported by bin/gatesmith-phantom-drill and tested by tests/test_forge_phantom_drill.py.

Design invariants:
  * Never writes to the production .company/ directory or work queue.
  * All sandbox artifacts land in a caller-supplied or temp directory.
  * Offline mode (recorded verdict) works when no claude binary is present —
    deterministic for CI and student machines without API access.
  * Only calls deliverable_judge's pure helper functions (build_judge_prompt,
    parse_verdict_json, _decide); never calls judge_pr_deliverable() which
    hits GitHub APIs and writes to .company/state/.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from . import deliverable_judge as _dj
except ImportError:
    import deliverable_judge as _dj  # type: ignore[no-redef]


# =============================================================================
# Phantom scenarios
# =============================================================================

# Pre-recorded verdicts capture what the judge returns when run live against
# each phantom diff. Using them in offline mode makes the drill deterministic.

SCENARIOS: dict[str, dict] = {
    "docs-only": {
        "task_id": "DRILL-docs-001",
        "title": "Add user authentication with OAuth2 endpoints",
        "description": (
            "Implement OAuth2 user authentication. The feature must include "
            "working /login and /logout HTTP endpoints, token refresh logic, "
            "and a middleware layer that gates protected routes."
        ),
        "diff_text": """\
diff --git a/README.md b/README.md
index 0000001..0000002 100644
--- a/README.md
+++ b/README.md
@@ -8,6 +8,13 @@
 ## Features

+### Authentication
+
+The app supports OAuth2 user authentication. Users can log in at `/login`
+and log out at `/logout`. Protected routes require a valid session token.
+The token refresh endpoint is available at `/auth/refresh`.
+
 ## Installation
""",
        "diff_meta": {
            "title": "Add user authentication with OAuth2 endpoints",
            "files": ["README.md"],
            "additions": 7,
            "deletions": 0,
        },
        "scenario_label": "docs-only change claiming a feature",
        "recorded_verdict": {
            "task_id": "DRILL-docs-001",
            "addresses_task": False,
            "confidence": 0.96,
            "reason": (
                "Diff only edits README.md with prose describing the feature; "
                "zero implementation code — no /login endpoint, no /logout handler, "
                "no OAuth2 library usage, no middleware. Documentation of a feature "
                "is not delivery of a feature."
            ),
        },
    },
    "vague-title": {
        "task_id": "DRILL-vague-002",
        "title": "First task",
        "description": "Complete the first task in the backlog.",
        "diff_text": """\
diff --git a/tasks.md b/tasks.md
new file mode 100644
index 0000000..aabbcc1
--- /dev/null
+++ b/tasks.md
@@ -0,0 +1,6 @@
+# Task Log
+
+## Completed
+
+- [x] First task  <!-- done 2026-07-08 -->
+
""",
        "diff_meta": {
            "title": "First task",
            "files": ["tasks.md"],
            "additions": 6,
            "deletions": 0,
        },
        "scenario_label": "vague 'First task' pattern from the P15-G1 lineage",
        "recorded_verdict": {
            "task_id": "DRILL-vague-002",
            "addresses_task": False,
            "confidence": 0.93,
            "reason": (
                "Vague 'First task' pattern: the diff adds only a markdown checklist "
                "with no substantive deliverable. The task description provides no "
                "clarity on what was supposed to be delivered, and the diff itself "
                "delivers nothing concrete — it merely marks itself as done."
            ),
        },
    },
}


# =============================================================================
# Result
# =============================================================================


@dataclass
class DrillResult:
    """Outcome of running a phantom drill scenario."""

    scenario: str
    task_id: str
    title: str
    diff_text: str
    addresses_task: bool | None
    confidence: float | None
    reason: str
    blocked: bool
    needs_manual_review: bool
    offline: bool
    sandbox_dir: Path
    verdict_artifact: Path  # sandbox_dir/drill-verdict.json


# =============================================================================
# Core drill logic
# =============================================================================


def _simulate_quarantine_comment(task_id: str, reason: str, label: str) -> str:
    """Return the PR comment body that apply_manual_review_label would post.

    This is a simulation — no real gh commands are run.
    """
    return (
        f"🛑 **Pre-merge deliverable gate: needs manual review**\n\n"
        f"The deliverable judge determined that this PR does not substantively "
        f"address the task it claims to implement.\n\n"
        f"**Task:** {task_id}\n"
        f"**Reason:** {reason}\n\n"
        f"**Action required:** A human reviewer must inspect this PR and either "
        f"merge it manually if appropriate, or close it and re-open the task.\n\n"
        f"*Label applied: `{label}`*"
    )


def run_drill(
    scenario_name: str = "docs-only",
    *,
    offline: bool | None = None,
    sandbox_dir: Path | None = None,
) -> DrillResult:
    """Run the phantom drill for the given scenario.

    Args:
        scenario_name: "docs-only" or "vague-title".
        offline: Force offline mode (pre-recorded verdict). None = auto-detect:
            offline when ``claude`` is not on PATH.
        sandbox_dir: Write artifacts here. When None a temporary directory is
            created — the caller is responsible for cleanup.

    Returns:
        DrillResult with the verdict and the path to the sandbox artifact.

    Raises:
        ValueError: If ``scenario_name`` is not recognised.
    """
    if scenario_name not in SCENARIOS:
        raise ValueError(
            f"Unknown scenario {scenario_name!r}. Available: {sorted(SCENARIOS)}"
        )

    scenario = SCENARIOS[scenario_name]
    task_id: str = scenario["task_id"]
    title: str = scenario["title"]
    description: str = scenario["description"]
    diff_text: str = scenario["diff_text"]
    diff_meta: dict = scenario["diff_meta"]
    recorded: dict = scenario["recorded_verdict"]

    # Auto-detect offline mode: no claude binary → offline.
    if offline is None:
        offline = shutil.which("claude") is None

    # Create sandbox dir when caller did not supply one.
    if sandbox_dir is None:
        sandbox_dir = Path(tempfile.mkdtemp(prefix="gatesmith-phantom-drill-"))
    else:
        sandbox_dir = Path(sandbox_dir)
        sandbox_dir.mkdir(parents=True, exist_ok=True)

    # Build the judge prompt (pure function — no network, no .company writes).
    meta_json = json.dumps(diff_meta)
    prompt = _dj.build_judge_prompt(
        task_id,
        title,
        description,
        pr_number=0,
        meta_json=meta_json,
        diff_text=diff_text,
    )

    # Resolve the verdict.
    if offline:
        raw_verdict = dict(recorded)
    else:
        # Live mode: call the judge CLI.  spawn_guard blocks this path under
        # pytest, so tests must always use offline=True or mock _run_judge_cli.
        live_config: dict = {"timeoutSeconds": 120, "model": None}
        stdout, err = _dj._run_judge_cli(prompt, live_config)
        if err and not stdout:
            # Judge error → fall back to recorded verdict annotated with the error.
            raw_verdict = dict(recorded)
            raw_verdict["reason"] = (
                f"[live-judge error: {err}] — fallback: {raw_verdict['reason']}"
            )
        else:
            parsed = _dj.parse_verdict_json(stdout or "")
            raw_verdict = parsed if parsed else dict(recorded)

    addresses_task = raw_verdict.get("addresses_task")
    confidence_raw = raw_verdict.get("confidence")
    try:
        confidence: float | None = (
            float(confidence_raw) if confidence_raw is not None else None
        )
    except (TypeError, ValueError):
        confidence = None
    reason = str(raw_verdict.get("reason", ""))

    blocked, needs_review = _dj._decide(addresses_task, confidence, threshold=0.7)

    # Write the verdict artifact to the sandbox (never to .company/).
    verdict_artifact = sandbox_dir / "drill-verdict.json"
    artifact_data = {
        "scenario": scenario_name,
        "task_id": task_id,
        "title": title,
        "addresses_task": addresses_task,
        "confidence": confidence,
        "reason": reason,
        "blocked": blocked,
        "needs_manual_review": needs_review,
        "offline": offline,
    }
    verdict_artifact.write_text(
        json.dumps(artifact_data, indent=2) + "\n", encoding="utf-8"
    )

    return DrillResult(
        scenario=scenario_name,
        task_id=task_id,
        title=title,
        diff_text=diff_text,
        addresses_task=addresses_task,
        confidence=confidence,
        reason=reason,
        blocked=blocked,
        needs_manual_review=needs_review,
        offline=offline,
        sandbox_dir=sandbox_dir,
        verdict_artifact=verdict_artifact,
    )


# =============================================================================
# Display
# =============================================================================


def format_output(result: DrillResult, *, verbose: bool = False) -> str:
    """Return a human-readable, optionally coloured drill report."""
    use_color = sys.stdout.isatty()
    RED = "\033[31m" if use_color else ""
    GREEN = "\033[32m" if use_color else ""
    YELLOW = "\033[33m" if use_color else ""
    BOLD = "\033[1m" if use_color else ""
    DIM = "\033[2m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    scenario_meta = SCENARIOS[result.scenario]
    label = "needs-manual-review"
    lines: list[str] = []

    lines.append(f"{BOLD}=== Gatesmith Phantom Drill ==={RESET}")
    lines.append(
        f"Scenario: {YELLOW}{result.scenario}{RESET} "
        f"({scenario_meta['scenario_label']})"
    )
    lines.append("")

    # Step 1 — task
    lines.append(f"{BOLD}[1/4] Synthetic task{RESET}")
    lines.append(f"  Task ID  : {result.task_id}")
    lines.append(f"  Title    : {result.title}")
    desc_preview = scenario_meta["description"][:80]
    if len(scenario_meta["description"]) > 80:
        desc_preview += "…"
    lines.append(f"  Desc     : {desc_preview}")
    lines.append("")

    # Step 2 — phantom diff
    lines.append(f"{BOLD}[2/4] Phantom diff{RESET}")
    meta = scenario_meta["diff_meta"]
    files_str = ", ".join(meta.get("files", []))
    lines.append(
        f"  Type     : {YELLOW}phantom{RESET} — {scenario_meta['scenario_label']}"
    )
    lines.append(f"  Files    : {files_str} (+{meta.get('additions', 0)} lines)")
    if verbose:
        lines.append("")
        for diff_line in result.diff_text.splitlines():
            if diff_line.startswith("+") and not diff_line.startswith("+++"):
                lines.append(f"  {GREEN}{diff_line}{RESET}")
            elif diff_line.startswith("-") and not diff_line.startswith("---"):
                lines.append(f"  {RED}{diff_line}{RESET}")
            else:
                lines.append(f"  {DIM}{diff_line}{RESET}")
    lines.append("")

    # Step 3 — judge
    mode_tag = (
        f"{DIM}[OFFLINE — pre-recorded verdict]{RESET}" if result.offline else "[LIVE]"
    )
    lines.append(f"{BOLD}[3/4] Deliverable judge{RESET}  {mode_tag}")
    if verbose:
        lines.append(f"  Prompt   : {len(_build_sample_prompt(result))} chars")
    verdict_tag = (
        f"{RED}PHANTOM BLOCKED{RESET}" if result.blocked else f"{GREEN}PASSED{RESET}"
    )
    lines.append(f"  Verdict  : {verdict_tag}")
    at_str = str(result.addresses_task).lower()
    conf_str = f"{result.confidence:.2f}" if result.confidence is not None else "n/a"
    lines.append(f"  addresses_task = {at_str}  (confidence: {conf_str})")
    wrapped_reason = _wrap(result.reason, width=72, indent="             ")
    lines.append(f"  Reason   : {wrapped_reason}")
    lines.append("")

    # Step 4 — quarantine label simulation
    lines.append(
        f"{BOLD}[4/4] Quarantine label flow (simulated — no real gh calls){RESET}"
    )
    comment = _simulate_quarantine_comment(result.task_id, result.reason, label)
    lines.append(f"  → Would apply label : {YELLOW}{label}{RESET}")
    lines.append("  → Would post comment:")
    for cline in comment.splitlines():
        lines.append(f"      {DIM}{cline}{RESET}")
    outcome = (
        f"{RED}Auto-merge BLOCKED ✗{RESET}"
        if result.blocked
        else f"{GREEN}Auto-merge ALLOWED ✓{RESET}"
    )
    lines.append(f"  → {outcome}")
    lines.append("")

    # Summary
    lines.append(f"{BOLD}=== Result ==={RESET}")
    lines.append(f"Verdict artifact : {result.verdict_artifact}")
    lines.append(f"Sandbox          : {result.sandbox_dir}")
    lines.append("")
    if result.blocked:
        lines.append(
            f"{GREEN}✓ The deliverable judge correctly identified this as a phantom "
            f"and blocked auto-merge.{RESET}"
        )
    else:
        lines.append(
            f"{RED}✗ The judge did not block this phantom — check the scenario "
            f"configuration.{RESET}"
        )

    return "\n".join(lines)


def _build_sample_prompt(result: DrillResult) -> str:
    """Re-build the judge prompt for the verbose display (no side-effects)."""
    scenario = SCENARIOS[result.scenario]
    return _dj.build_judge_prompt(
        result.task_id,
        result.title,
        scenario["description"],
        pr_number=0,
        meta_json=json.dumps(scenario["diff_meta"]),
        diff_text=result.diff_text,
    )


def _wrap(text: str, width: int, indent: str) -> str:
    """Wrap long text to width, indenting continuation lines."""
    if len(text) <= width:
        return text
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).lstrip()
    if current:
        lines.append(current)
    return ("\n" + indent).join(lines)
