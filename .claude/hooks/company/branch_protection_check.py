# /// script
# requires-python = ">=3.10"
# ///
"""Branch Protection Health Check — Layer 2 validation for ADR-0002.

Verifies that GitHub branch protection rules exist and match expected
configuration. Part of the three-layer security-by-design defense.

Usage:
    from branch_protection_check import BranchProtectionChecker
    checker = BranchProtectionChecker()
    result = checker.check()
    # result: {"status": "ok"|"degraded"|"missing", "checks": [...], ...}
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("forge_daemon")

# Default expected branch protection rules (ADR-0002)
DEFAULT_EXPECTED_CHECKS = ["Lint", "Security", "Hooks Validate"]


@dataclass
class ProtectionCheckResult:
    """Result of a branch protection health check."""

    status: str = "unknown"  # ok, degraded, missing, error
    checks_present: list[str] = field(default_factory=list)
    checks_missing: list[str] = field(default_factory=list)
    force_push_blocked: bool = False
    deletions_blocked: bool = False
    issues: list[str] = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks_present": self.checks_present,
            "checks_missing": self.checks_missing,
            "force_push_blocked": self.force_push_blocked,
            "deletions_blocked": self.deletions_blocked,
            "issues": self.issues,
        }


class BranchProtectionChecker:
    """Checks GitHub branch protection rules against expected config.

    Args:
        expected_checks: List of required status check names.
        require_no_force_push: Whether force pushes must be blocked.
        require_no_deletion: Whether branch deletion must be blocked.
        config_path: Path to gatesmith.json (overrides defaults).
    """

    def __init__(
        self,
        expected_checks: list[str] | None = None,
        require_no_force_push: bool = True,
        require_no_deletion: bool = True,
        config_path: Path | None = None,
    ):
        # Load from config if available
        if config_path and config_path.exists():
            self._load_from_config(config_path)
        else:
            self.expected_checks = expected_checks or DEFAULT_EXPECTED_CHECKS
            self.require_no_force_push = require_no_force_push
            self.require_no_deletion = require_no_deletion

    def _load_from_config(self, config_path: Path) -> None:
        """Load expected rules from gatesmith.json."""
        try:
            with open(config_path) as f:
                data = json.load(f)
            bp = data.get("branchProtection", {})
            self.expected_checks = bp.get("expectedChecks", DEFAULT_EXPECTED_CHECKS)
            self.require_no_force_push = bp.get("requireNoForcePush", True)
            self.require_no_deletion = bp.get("requireNoDeletion", True)
        except (json.JSONDecodeError, OSError):
            self.expected_checks = DEFAULT_EXPECTED_CHECKS
            self.require_no_force_push = True
            self.require_no_deletion = True

    def _get_repo_name(self) -> str | None:
        """Get owner/repo from gh CLI."""
        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".nameWithOwner",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def check(self, branch: str = "main") -> ProtectionCheckResult:
        """Check branch protection rules against expected configuration.

        Args:
            branch: Branch name to check (default: main).

        Returns:
            ProtectionCheckResult with status and details.
        """
        result = ProtectionCheckResult()

        repo = self._get_repo_name()
        if not repo:
            result.status = "error"
            result.issues.append("Could not determine repo name via gh CLI")
            return result

        try:
            api_result = subprocess.run(
                ["gh", "api", f"repos/{repo}/branches/{branch}/protection"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            result.status = "error"
            result.issues.append(f"gh api call failed: {e}")
            return result

        if api_result.returncode != 0:
            # 404 means no protection rules at all
            if "404" in api_result.stderr or "Not Found" in api_result.stderr:
                result.status = "missing"
                result.checks_missing = list(self.expected_checks)
                result.issues.append(f"No branch protection rules found on {branch}")
            else:
                result.status = "error"
                result.issues.append(f"API error: {api_result.stderr[:200]}")
            return result

        try:
            protection = json.loads(api_result.stdout)
            result.raw_response = protection
        except json.JSONDecodeError:
            result.status = "error"
            result.issues.append("Failed to parse protection API response")
            return result

        # Check required status checks
        required_checks = protection.get("required_status_checks", {})
        if required_checks:
            contexts = required_checks.get("contexts", [])
            # Also check checks array (newer API format)
            checks_array = required_checks.get("checks", [])
            check_names = set(contexts)
            for chk in checks_array:
                if isinstance(chk, dict):
                    check_names.add(chk.get("context", ""))
                else:
                    check_names.add(str(chk))

            for expected in self.expected_checks:
                if expected in check_names:
                    result.checks_present.append(expected)
                else:
                    result.checks_missing.append(expected)
                    result.issues.append(
                        f"Required check '{expected}' not in branch protection"
                    )
        else:
            result.checks_missing = list(self.expected_checks)
            result.issues.append("No required status checks configured")

        # Check force push setting
        force_push = protection.get("allow_force_pushes", {})
        result.force_push_blocked = not force_push.get("enabled", True)
        if self.require_no_force_push and not result.force_push_blocked:
            result.issues.append("Force pushes are allowed (should be blocked)")

        # Check deletion setting
        deletions = protection.get("allow_deletions", {})
        result.deletions_blocked = not deletions.get("enabled", True)
        if self.require_no_deletion and not result.deletions_blocked:
            result.issues.append("Branch deletion is allowed (should be blocked)")

        # Determine overall status
        if not result.issues:
            result.status = "ok"
        elif result.checks_missing or not result.force_push_blocked:
            result.status = "degraded"
        else:
            result.status = "degraded"

        return result

    def restore(self, branch: str = "main") -> dict:
        """Restore expected branch protection rules via GitHub API.

        This is a powerful operation — only call when autoRestore is enabled.

        Args:
            branch: Branch name to protect.

        Returns:
            dict with success status and details.
        """
        repo = self._get_repo_name()
        if not repo:
            return {"success": False, "error": "Could not determine repo name"}

        protection_body = {
            "required_status_checks": {
                "strict": False,
                "contexts": self.expected_checks,
            },
            "enforce_admins": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }

        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{repo}/branches/{branch}/protection",
                    "--input",
                    "-",
                ],
                input=json.dumps(protection_body),
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode == 0:
                logger.info(f"Branch protection restored on {branch}")
                return {"success": True, "restored_checks": self.expected_checks}
            else:
                return {"success": False, "error": result.stderr[:200]}

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"success": False, "error": str(e)}
