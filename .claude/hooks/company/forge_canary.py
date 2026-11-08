#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
gatesmith-canary — One-command auto-merge gate drill.

Verifies preconditions and runs closed/open-gate smoke tests to confirm
the auto-merge pipeline is correctly wired before flipping allowMergeToMain.

Stages:
  PRECONDITIONS — config files + GitHub API
  CLOSED-GATE   — judge must block a phantom (comment-only) diff PR
  OPEN-GATE     — real diff PR must accept auto-merge (zero-touch)

Artifacts (printed after all stages):
  1. PR URL        — canary PR from open-gate
  2. Verdict JSONL — deliverable-judge output from closed-gate
  3. Merge commit  — SHA after auto-merge (or "pending" if CI still running)
  4. Reconcile     — autonomy_metrics reconcile --dry-run summary

Usage:
  gatesmith-canary [--project-dir PATH] [--mode MODE] [--dry-run] [--json]

Modes:
  full            Run all stages (default)
  preconditions   Only run precondition checks (no PRs created)
  closed-gate     Only run closed-gate judge drill
  open-gate       Only run open-gate PR drill

Exit codes:
  0  All stages PASS (or WARN/SKIP)
  1  One or more stages FAIL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Status = Literal["PASS", "FAIL", "WARN", "SKIP"]

# ── ANSI colors ───────────────────────────────────────────────────────────────
_NO_COLOR = (
    not sys.stdout.isatty()
    or bool(os.environ.get("NO_COLOR"))
    or bool(os.environ.get("FORGE_NO_COLOR"))
)

_ICONS: dict[str, str] = {
    "PASS": "\033[0;32m✓\033[0m",
    "FAIL": "\033[0;31m✗\033[0m",
    "WARN": "\033[1;33m⚠\033[0m",
    "SKIP": "\033[2m~\033[0m",
}
_TAGS: dict[str, str] = {
    "PASS": "[PASS]",
    "FAIL": "[FAIL]",
    "WARN": "[WARN]",
    "SKIP": "[SKIP]",
}


def _icon(status: Status) -> str:
    return _TAGS[status] if _NO_COLOR else _ICONS[status]


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class Stage:
    name: str
    status: Status
    detail: str
    artifact: str | None = None


@dataclass
class Artifacts:
    pr_url: str | None = None
    verdict_jsonl: str | None = None
    merge_commit: str | None = None
    reconcile: str | None = None


@dataclass
class CanaryReport:
    stages: list[Stage] = field(default_factory=list)
    artifacts: Artifacts = field(default_factory=Artifacts)

    @property
    def failed(self) -> bool:
        return any(s.status == "FAIL" for s in self.stages)

    @property
    def fail_count(self) -> int:
        return sum(1 for s in self.stages if s.status == "FAIL")

    @property
    def pass_count(self) -> int:
        return sum(1 for s in self.stages if s.status == "PASS")


# ── subprocess helper ─────────────────────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


# ── Config checks (pure file I/O, no network) ─────────────────────────────────


def check_config_governing(project_dir: Path) -> Stage:
    """Which file governs autonomy.autoMerge, and what it says.

    One rule for every reader (governing_config.py): the root config if it
    declares the block, else the install template, else absent (auto-merge
    off). An unreadable or malformed governing answer is a FAIL, because the
    merge gate refuses on it too.
    """
    import governing_config as gc

    got = gc.resolve("autonomy.autoMerge", project_dir)
    if got.error:
        return Stage(
            "config-governing",
            "FAIL",
            f"{got.error} — the merge gate refuses every merge until it is fixed",
        )
    if got.source is None:
        return Stage(
            "config-governing",
            "WARN",
            f"autonomy.autoMerge declared in neither {gc.ROOT_CONFIG} nor"
            f" {gc.TEMPLATE_CONFIG} — auto-merge is off",
        )
    block = got.value if isinstance(got.value, dict) else {}
    return Stage(
        "config-governing",
        "PASS",
        f"{got.source} governs autonomy.autoMerge:"
        f" enabled={block.get('enabled')!r},"
        f" allowMergeToMain={block.get('allowMergeToMain')!r},"
        f" requireApproval={block.get('requireApproval')!r}",
    )


# ── GitHub API checks ─────────────────────────────────────────────────────────


def _get_repo(project_dir: Path) -> str | None:
    """Return 'owner/repo' string or None on failure."""
    r = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=project_dir,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def check_branch_protection(project_dir: Path) -> Stage:
    repo = _get_repo(project_dir)
    if repo is None:
        return Stage(
            "branch-protection",
            "FAIL",
            "gh repo view failed — check gh auth status and that this is a GitHub repo",
        )
    r = _run(["gh", "api", f"repos/{repo}/branches/main/protection"], cwd=project_dir)
    if r.returncode != 0:
        return Stage(
            "branch-protection",
            "FAIL",
            "No branch protection on main."
            " allowMergeToMain=true without required checks lets the daemon merge"
            " PRs that break the codebase (Project D finding D5)."
            " Fix: bin/gatesmith-protect-main --ensure-ci",
        )
    try:
        prot = json.loads(r.stdout)
    except json.JSONDecodeError:
        return Stage(
            "branch-protection", "WARN", "Unexpected gh api response (not JSON)"
        )
    checks = (prot.get("required_status_checks") or {}).get("contexts", [])
    if not checks:
        return Stage(
            "branch-protection",
            "WARN",
            "Branch protection exists but no required status checks configured —"
            " PRs could merge without CI. Fix: bin/gatesmith-protect-main --ensure-ci",
        )
    return Stage("branch-protection", "PASS", f"Required checks on main: {checks}")


def check_actions_liveness(project_dir: Path) -> Stage:
    r = _run(
        [
            "gh",
            "run",
            "list",
            "--branch",
            "main",
            "--status",
            "success",
            "--limit",
            "1",
            "--json",
            "databaseId,status",
        ],
        cwd=project_dir,
    )
    if r.returncode != 0:
        return Stage(
            "actions-liveness",
            "WARN",
            f"gh run list failed: {r.stderr.strip() or '(no output)'}",
        )
    try:
        runs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return Stage(
            "actions-liveness", "WARN", "Unexpected gh run list response (not JSON)"
        )
    if not runs:
        return Stage(
            "actions-liveness",
            "FAIL",
            "No successful Actions run found on main."
            " auto-merge will never arm (branch-protection requires a check that has never passed)."
            " Trigger one: gh workflow run ci.yml",
        )
    return Stage(
        "actions-liveness",
        "PASS",
        f"Latest successful run on main: id={runs[0].get('databaseId', '?')}",
    )


def run_preconditions(project_dir: Path) -> list[Stage]:
    return [
        check_config_governing(project_dir),
        check_branch_protection(project_dir),
        check_actions_liveness(project_dir),
    ]


# ── Verdict parsing ───────────────────────────────────────────────────────────

_JUDGE = Path(__file__).parent / "deliverable_judge.py"


def _parse_verdict(output: str) -> dict | None:
    """Extract the last JSON object with 'addresses_task' from judge output."""
    import re

    for match in reversed(list(re.finditer(r"\{[^{}]{10,}\}", output))):
        try:
            obj = json.loads(match.group())
            if "addresses_task" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


# ── Closed-gate drill ─────────────────────────────────────────────────────────

_PHANTOM_TITLE = "canary: phantom diff — expect blocked"
_PHANTOM_DESC = (
    "Gatesmith canary drill. This PR contains an empty commit that does NOT address"
    " any real task. The deliverable judge MUST return addresses_task=false."
)


def run_closed_gate(
    project_dir: Path,
    *,
    suffix: str,
    dry_run: bool = False,
) -> tuple[list[Stage], str | None]:
    """Create phantom PR, run judge, verify blocked. Returns (stages, verdict_jsonl)."""
    stages: list[Stage] = []
    verdict_jsonl: str | None = None
    branch = f"canary/closed-{suffix}"

    if dry_run:
        stages.append(
            Stage("closed-gate-create", "SKIP", "dry-run: would create phantom PR")
        )
        stages.append(
            Stage("closed-gate-judge", "SKIP", "dry-run: would run deliverable judge")
        )
        return stages, None

    # Create branch + empty commit
    for cmd in [
        ["git", "checkout", "-b", branch],
        ["git", "commit", "--allow-empty", "-m", f"canary: phantom — {suffix}"],
    ]:
        r = _run(cmd, cwd=project_dir)
        if r.returncode != 0:
            stages.append(
                Stage(
                    "closed-gate-create",
                    "FAIL",
                    f"{cmd[1]} failed: {r.stderr.strip()}",
                )
            )
            _git_cleanup(project_dir, branch)
            return stages, None

    r = _run(["git", "push", "origin", branch], cwd=project_dir)
    if r.returncode != 0:
        stages.append(
            Stage(
                "closed-gate-create",
                "FAIL",
                f"git push failed: {r.stderr.strip()}",
            )
        )
        _git_cleanup(project_dir, branch)
        return stages, None

    # Create PR
    r = _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            _PHANTOM_TITLE,
            "--body",
            _PHANTOM_DESC,
            "--head",
            branch,
            "--base",
            "main",
        ],
        cwd=project_dir,
    )
    if r.returncode != 0:
        stages.append(
            Stage(
                "closed-gate-create",
                "FAIL",
                f"gh pr create failed: {r.stderr.strip()}",
            )
        )
        _git_cleanup(project_dir, branch)
        return stages, None

    pr_url = r.stdout.strip().split("\n")[-1]
    pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else "0"
    stages.append(Stage("closed-gate-create", "PASS", f"Phantom PR: {pr_url}"))

    # Run judge
    if not _JUDGE.exists():
        stages.append(
            Stage("closed-gate-judge", "SKIP", "deliverable_judge.py not found")
        )
    else:
        jr = _run(
            [
                "uv",
                "run",
                str(_JUDGE),
                "judge",
                "--task-id",
                "canary-closed",
                "--pr-number",
                pr_number,
                "--title",
                _PHANTOM_TITLE,
                "--description",
                _PHANTOM_DESC,
            ],
            cwd=project_dir,
            timeout=120,
        )
        verdict = _parse_verdict(jr.stdout)
        if verdict is None:
            stages.append(
                Stage(
                    "closed-gate-judge",
                    "FAIL",
                    f"Judge returned unparseable output: {jr.stdout[:200]!r}",
                )
            )
        elif verdict.get("addresses_task") is False:
            verdict_jsonl = json.dumps(verdict)
            stages.append(
                Stage(
                    "closed-gate-judge",
                    "PASS",
                    f"Judge blocked phantom (addresses_task=false): {verdict.get('reason', '')}",
                    artifact=verdict_jsonl,
                )
            )
        else:
            verdict_jsonl = json.dumps(verdict)
            stages.append(
                Stage(
                    "closed-gate-judge",
                    "FAIL",
                    f"Judge DID NOT block phantom (addresses_task={verdict.get('addresses_task')!r})."
                    " Gate misconfigured — do NOT enable allowMergeToMain.",
                    artifact=verdict_jsonl,
                )
            )

    # Cleanup: close PR + delete branch
    _run(["gh", "pr", "close", pr_number, "--delete-branch"], cwd=project_dir)
    _git_cleanup(project_dir, branch)
    return stages, verdict_jsonl


def _git_cleanup(project_dir: Path, branch: str) -> None:
    """Best-effort: switch back to main and delete the canary branch."""
    _run(["git", "checkout", "main"], cwd=project_dir)
    _run(["git", "branch", "-D", branch], cwd=project_dir)
    _run(["git", "push", "origin", "--delete", branch], cwd=project_dir)


# ── Open-gate drill ───────────────────────────────────────────────────────────

_OPEN_TITLE = "canary: real diff — expect auto-merge"
_OPEN_DESC = (
    "Gatesmith canary drill. This PR contains a real, substantive change."
    " The auto-merge gate MUST merge this PR without human intervention."
)
_CANARY_FILE = ".gatesmith-canary"


def run_open_gate(
    project_dir: Path,
    *,
    suffix: str,
    dry_run: bool = False,
) -> tuple[list[Stage], str | None, str | None]:
    """Create real-diff PR and enable auto-merge. Returns (stages, pr_url, merge_commit)."""
    stages: list[Stage] = []
    branch = f"canary/open-{suffix}"

    if dry_run:
        stages.append(
            Stage("open-gate-create", "SKIP", "dry-run: would create real-diff PR")
        )
        stages.append(
            Stage("open-gate-automerge", "SKIP", "dry-run: would enable auto-merge")
        )
        return stages, None, None

    # Create branch
    r = _run(["git", "checkout", "-b", branch], cwd=project_dir)
    if r.returncode != 0:
        stages.append(
            Stage(
                "open-gate-create",
                "FAIL",
                f"git checkout -b failed: {r.stderr.strip()}",
            )
        )
        return stages, None, None

    # Write real change to .gatesmith-canary
    canary_path = project_dir / _CANARY_FILE
    existing = canary_path.read_text().splitlines() if canary_path.exists() else []
    existing.append(f"canary-{suffix}")
    canary_path.write_text("\n".join(existing) + "\n")

    for cmd in [
        ["git", "add", _CANARY_FILE],
        ["git", "commit", "-m", f"canary: real diff — {suffix}"],
        ["git", "push", "origin", branch],
    ]:
        r = _run(cmd, cwd=project_dir)
        if r.returncode != 0:
            stages.append(
                Stage(
                    "open-gate-create",
                    "FAIL",
                    f"{cmd[1]} failed: {r.stderr.strip()}",
                )
            )
            _git_cleanup(project_dir, branch)
            return stages, None, None

    # Create PR
    r = _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            _OPEN_TITLE,
            "--body",
            _OPEN_DESC,
            "--head",
            branch,
            "--base",
            "main",
        ],
        cwd=project_dir,
    )
    if r.returncode != 0:
        stages.append(
            Stage(
                "open-gate-create",
                "FAIL",
                f"gh pr create failed: {r.stderr.strip()}",
            )
        )
        _git_cleanup(project_dir, branch)
        return stages, None, None

    pr_url = r.stdout.strip().split("\n")[-1]
    pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else "0"
    stages.append(
        Stage("open-gate-create", "PASS", f"PR created: {pr_url}", artifact=pr_url)
    )

    # Enable auto-merge
    r = _run(
        ["gh", "pr", "merge", "--auto", "--squash", pr_number],
        cwd=project_dir,
    )
    if r.returncode != 0:
        stages.append(
            Stage(
                "open-gate-automerge",
                "FAIL",
                f"gh pr merge --auto failed: {r.stderr.strip()}"
                " — check allowMergeToMain=true in the governing config (governing_config.py get autonomy.autoMerge)"
                " and that branch protection has required checks",
            )
        )
        return stages, pr_url, None

    stages.append(
        Stage(
            "open-gate-automerge",
            "PASS",
            "Auto-merge enabled — PR will merge when CI passes (zero-touch)",
        )
    )

    # Attempt to read merge commit (may be pending if CI hasn't run)
    mc_r = _run(
        [
            "gh",
            "pr",
            "view",
            pr_number,
            "--json",
            "mergeCommit",
            "-q",
            ".mergeCommit.oid",
        ],
        cwd=project_dir,
    )
    mc = mc_r.stdout.strip()
    merge_commit = mc if (mc and mc != "null") else f"pending (PR #{pr_number})"
    return stages, pr_url, merge_commit


# ── Reconcile check ───────────────────────────────────────────────────────────

_METRICS = Path(__file__).parent / "autonomy_metrics.py"


def check_reconcile(
    project_dir: Path, company_dir: Path | None = None
) -> tuple[Stage, str | None]:
    """Run autonomy_metrics reconcile --dry-run. Returns (stage, summary_text)."""
    if not _METRICS.exists():
        return Stage("reconcile", "SKIP", "autonomy_metrics.py not found"), None

    if company_dir is None:
        company_dir = project_dir / ".company"

    r = _run(
        [
            "uv",
            "run",
            str(_METRICS),
            "reconcile",
            "--dry-run",
            "--company-dir",
            str(company_dir),
        ],
        cwd=project_dir,
        timeout=60,
    )
    if r.returncode != 0:
        return (
            Stage(
                "reconcile",
                "WARN",
                f"reconcile --dry-run failed: {r.stderr.strip()[:120]}",
            ),
            None,
        )
    summary = r.stdout.strip() or "0 pending flips"
    return Stage("reconcile", "PASS", summary[:120]), summary


# ── Report output ─────────────────────────────────────────────────────────────

_WIDTH = 70


def print_report(
    report: CanaryReport,
    *,
    json_out: bool = False,
    no_color: bool = False,
) -> None:
    if json_out:
        out = {
            "stages": [
                {"name": s.name, "status": s.status, "detail": s.detail}
                for s in report.stages
            ],
            "artifacts": {
                "pr_url": report.artifacts.pr_url,
                "verdict_jsonl": report.artifacts.verdict_jsonl,
                "merge_commit": report.artifacts.merge_commit,
                "reconcile": report.artifacts.reconcile,
            },
            "pass": report.pass_count,
            "fail": report.fail_count,
            "ok": not report.failed,
        }
        print(json.dumps(out, indent=2))
        return

    print(f"\n{'═' * _WIDTH}")
    print(" FORGE CANARY DRILL")
    print(f"{'═' * _WIDTH}")
    for s in report.stages:
        icon = _icon(s.status)
        # Truncate detail to keep lines readable
        detail = (
            s.detail[: _WIDTH - 34] + "…" if len(s.detail) > _WIDTH - 34 else s.detail
        )
        print(f" {icon}  {s.name:<28} {detail}")
    print(f"{'─' * _WIDTH}")

    result = "PASS" if not report.failed else "FAIL"
    print(
        f" {report.pass_count} passed, {report.fail_count} failed,"
        f" {len(report.stages)} total  [{result}]"
    )

    arts = report.artifacts
    if any([arts.pr_url, arts.verdict_jsonl, arts.merge_commit, arts.reconcile]):
        print(f"\n{'─' * _WIDTH}")
        print(" ARTIFACTS")
        print(f"{'─' * _WIDTH}")
        if arts.pr_url:
            print(f" pr_url:        {arts.pr_url}")
        if arts.verdict_jsonl:
            print(f" verdict_jsonl: {arts.verdict_jsonl}")
        if arts.merge_commit:
            print(f" merge_commit:  {arts.merge_commit}")
        if arts.reconcile:
            print(f" reconcile:     {arts.reconcile}")
    print(f"{'═' * _WIDTH}\n")


# ── CLI entry point ───────────────────────────────────────────────────────────


def _suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gatesmith-canary",
        description="Auto-merge gate drill: preconditions + closed/open-gate smoke tests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Project directory (default: cwd)",
    )
    p.add_argument(
        "--mode",
        choices=["full", "preconditions", "closed-gate", "open-gate"],
        default="full",
        help="Which stages to run (default: full)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help="Machine-readable JSON output",
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip gh calls that modify state (no PRs created)",
    )
    args = p.parse_args(argv)

    project_dir = (args.project_dir or Path.cwd()).resolve()
    report = CanaryReport()
    suffix = _suffix()

    if args.mode in ("full", "preconditions"):
        report.stages.extend(run_preconditions(project_dir))

    if args.mode in ("full", "closed-gate"):
        closed_stages, verdict = run_closed_gate(
            project_dir, suffix=suffix, dry_run=args.dry_run
        )
        report.stages.extend(closed_stages)
        report.artifacts.verdict_jsonl = verdict

    if args.mode in ("full", "open-gate"):
        open_stages, pr_url, merge_commit = run_open_gate(
            project_dir, suffix=suffix, dry_run=args.dry_run
        )
        report.stages.extend(open_stages)
        report.artifacts.pr_url = pr_url
        report.artifacts.merge_commit = merge_commit

    if args.mode == "full":
        reconcile_stage, reconcile_summary = check_reconcile(project_dir)
        report.stages.append(reconcile_stage)
        report.artifacts.reconcile = reconcile_summary

    print_report(report, json_out=args.json_out, no_color=args.no_color)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
