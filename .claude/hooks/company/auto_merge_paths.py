#!/usr/bin/env python3
"""Single source of truth for auto-merge path holds (PR 257/260, 2026-07-18).

Every auto-merge arming point — pr_output_manager.enable_github_auto_merge,
forge_daemon._check_security_gates, the legacy arming branch, and the CI
Auto-Merge job — must agree on which changed files hold a daemon PR for
operator review. That agreement is only guaranteed if they share ONE seed
list and ONE matching function, which is this module's entire job. Keep it
dependency-free: the CI job fetches this file from the BASE ref (origin/main)
and imports it standalone, so a PR editing this module cannot weaken the
gate that judges that same PR.

reviewPaths semantics: a daemon PR touching any matching file must NOT arm
GitHub auto-merge. It stays OPEN with normal CI and the
``awaiting-operator-review`` label; the operator merges manually after
review. This is a hold tier, not a block — distinct from blockedPaths
(exact-match files that must never auto-merge at all).
"""

from __future__ import annotations

import fnmatch

# The self-governance surface: gates, judges, assessors, arming code, the
# daemon core, and the shipped config template. Conventions (*audit*,
# *judge*, *admission*, *steering*) cover renames and new files in those
# families without list edits. This module protects itself (a PR editing it
# is by definition gate work). The operator's gatesmith.json can override
# via autonomy.autoMerge.reviewPaths; an ABSENT key means THIS list, never
# "no holds".
DEFAULT_REVIEW_PATHS: list[str] = [
    ".claude/hooks/company/*audit*",
    ".claude/hooks/company/*judge*",
    ".claude/hooks/company/*admission*",
    ".claude/hooks/company/*steering*",
    ".claude/hooks/company/goal_tracker.py",
    ".claude/hooks/company/board_governance.py",
    ".claude/hooks/company/pr_output_manager.py",
    ".claude/hooks/company/forge_daemon.py",
    ".claude/hooks/company/employee_activator.py",
    ".claude/hooks/company/operation_loop.py",
    ".claude/hooks/company/failure_recovery.py",
    ".claude/hooks/company/infra_failure.py",
    ".claude/hooks/company/auto_merge_paths.py",
    ".claude/hooks/company/acceptance_*.py",
    # The merge levers and the gates they consult (single-merge-gate,
    # 2026-10): a daemon PR that weakened the merge decision, the rebase
    # merge queue, the challenger stage or the verdict hold could otherwise
    # auto-merge through the gate it just weakened.
    ".claude/hooks/company/merge_gate.py",
    ".claude/hooks/company/pr_rebase.py",
    ".claude/hooks/company/challenger_stage.py",
    ".claude/hooks/company/verdict_hold.py",
    ".claude/hooks/company/pr_rollback_executor.py",
    # The safety hooks themselves (PR 600, 2026-09-05). A daemon PR that
    # rewrote block_dangerous.py and network_egress_guard.py auto-merged
    # with no hold because only company/ modules were listed here; the
    # incomplete fix then needed a follow-up (#602). fnmatch's `*` crosses
    # `/`, so `.claude/hooks/*.py` would also hold every company/ module —
    # hence explicit names: every hook registered in a denying event
    # (PreToolUse, UserPromptSubmit, Stop, SubagentStop, PermissionRequest),
    # the guards block_dangerous.py composes in-process, and the modules
    # those hooks import from .claude/hooks/ (config, canonicalization, the
    # denial audit, license checks). tests/test_auto_merge_paths.py derives
    # that set from .claude/settings.json and the hooks' own imports, so a
    # newly registered or newly imported guard cannot go unlisted.
    ".claude/hooks/block_dangerous.py",
    ".claude/hooks/network_egress_guard.py",
    ".claude/hooks/secrets_scanner.py",
    ".claude/hooks/git_guardian.py",
    ".claude/hooks/lint_on_edit.py",
    ".claude/hooks/slopsquat_check.py",
    ".claude/hooks/prompt_guard.py",
    ".claude/hooks/stop_validator.py",
    ".claude/hooks/subagent_validator.py",
    ".claude/hooks/permission_auto.py",
    ".claude/hooks/hook_utils.py",
    ".claude/hooks/hook_config.py",
    ".claude/hooks/hook_audit.py",
    # The whole ledger, not just manifest.json: relations/cases entries carry
    # `fixtures` (materialize_workdir writes wherever their keys point) and
    # `*_extra_args` (appended to an approved invoke argv, unchecked by the
    # argv[0]-only allowlist) — both need the same human hold as the manifest.
    # Root AND `*`-prefixed: the runner takes --project-dir, so a ledger can
    # live under a subproject, not just the repo root.
    "acceptance/*",
    "*acceptance/manifest.json",
    "*acceptance/relations/*",
    "*acceptance/cases/*",
    # The starter pack is the seed copied into every adopter's acceptance/ —
    # an unreviewed edit to its invoke template propagates to every project
    # that scaffolds from it.
    ".company/templates/acceptance/*",
    # goal_tracker.assess_generic_success_metric auto-completes a goal from a
    # ledger run when its success_metric names an acceptance-ledger entry id
    # as CONSISTENT "in the ledger run on main" (goal_crediting.py's sweep has
    # the same fallback). That text lives in vision.md, not in goal_tracker.py
    # or acceptance/ — so an unreviewed edit naming ANY already-passing entry
    # id (however unrelated) would instantly complete a goal with no work
    # done. `*` so a subproject's `.company/vision.md` is held too.
    "*.company/vision.md",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    # Workflow templates adopters copy into their own .github/workflows/
    # (LEDGER-AS-SHARDED-RUNNER-JOB: the sharded acceptance-ledger job). An
    # unreviewed edit (a GitHub-hosted runner, a non-main trigger) would
    # propagate to every product that adopts it.
    "templates/ci/*",
    ".claude/gatesmith.json",
    # Root config, not just .claude/gatesmith.json: acceptance_ledger.py's
    # _load_allowed_binaries() reads THIS path (matching adaptive_scheduler.py
    # and budget_governor.py's FORGE_CONFIG_FILE convention, not
    # board_governance.py's .claude/-scoped one). An unreviewed edit here can
    # widen acceptance.allowedInvokeBinaries past what any acceptance/*
    # review already approved.
    "gatesmith.json",
]

REVIEW_HOLD_LABEL = "awaiting-operator-review"


def resolve_review_paths(configured: object) -> list[str]:
    """Resolve the effective reviewPaths from a config value.

    - ``None`` (key absent) → the built-in default seed.
    - A list of strings → that list (operator override; an explicit ``[]``
      is honored as "operator disabled holds" — visible in config review,
      unlike a silent code fallback).
    - Anything else (malformed) → the default seed. Malformed config must
      never fail open into "no holds" on this surface.
    """
    if configured is None:
        return list(DEFAULT_REVIEW_PATHS)
    if isinstance(configured, list) and all(isinstance(p, str) for p in configured):
        return list(configured)
    return list(DEFAULT_REVIEW_PATHS)


def approval_paths(human_protected: object) -> list[str]:
    """``humanProtected.requireApprovalFor`` as review holds.

    Those paths stay human-writable, but a daemon PR that touches one is held
    for the operator exactly like a reviewPaths match. The setting shipped in
    the template and the docs for months while no code read it.

    - ``None`` (no humanProtected section), ``enabled: false``, or no
      ``requireApprovalFor`` key → no extra holds.
    - A list of strings → those patterns.
    - Anything else (a malformed section or list) → ``["*"]``: hold every
      PR. A typo never turns a hold off.
    """
    if human_protected is None:
        return []
    if not isinstance(human_protected, dict):
        return ["*"]
    if human_protected.get("enabled", True) is False:
        return []
    raw = human_protected.get("requireApprovalFor")
    if raw is None:
        return []
    if isinstance(raw, list) and all(isinstance(p, str) for p in raw):
        return list(raw)
    return ["*"]


def _normalize(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def match_review_paths(
    changed_files: list[str], patterns: list[str] | None = None
) -> list[str]:
    """Changed files that match a reviewPaths glob (posix paths, fnmatch)."""
    pats = DEFAULT_REVIEW_PATHS if patterns is None else patterns
    return [
        f for f in changed_files if any(fnmatch.fnmatch(_normalize(f), p) for p in pats)
    ]
