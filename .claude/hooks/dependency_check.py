# /// script
# requires-python = ">=3.10"
# ///
"""
PostToolUse Hook: Check dependencies after install commands and file changes.
Runs npm audit / pip-audit to detect known vulnerabilities with CVE details.
Warns on suspicious packages and provides remediation guidance.

Security Profile Aware:
- strict/standard: Warns on vulnerabilities with detailed CVE info
- minimal: Disabled

Enhanced Features:
- Detailed CVE ID reporting (e.g., CVE-2021-12345)
- Package version information in findings
- Remediation guidance with update recommendations
- Requirements file change detection (requirements.txt, pyproject.toml)
- Severity-based filtering (CRITICAL, HIGH, MEDIUM)
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# Import hook_config for profile-aware behavior
try:
    from hook_config import is_enabled
except ImportError:
    # Fallback if hook_config not available
    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "dependency_check"

# Known typosquat patterns (common misspellings of popular packages)
TYPOSQUAT_INDICATORS = [
    # Patterns that suggest typosquatting
    (r"^(?:expresss|exprss|expres|ecpress)$", "express"),
    (r"^(?:lodassh|lodahs|lod4sh)$", "lodash"),
    (r"^(?:reacct|raect|reactt)$", "react"),
    (r"^(?:requets|reqeusts|requestes)$", "requests"),
    (r"^(?:djnago|dajngo|djangoo)$", "django"),
    (r"^(?:flaskk|flaask|flsk)$", "flask"),
]

# Install commands we monitor
NPM_INSTALL_PATTERNS = [
    r"npm\s+install\s+",
    r"npm\s+i\s+",
    r"yarn\s+add\s+",
    r"pnpm\s+add\s+",
    r"pnpm\s+install\s+",
]

PIP_INSTALL_PATTERNS = [
    r"pip\s+install\s+",
    r"pip3\s+install\s+",
    r"uv\s+pip\s+install\s+",
    r"uv\s+add\s+",
    r"poetry\s+add\s+",
]

# Requirements file patterns to trigger audits
REQUIREMENTS_FILE_PATTERNS = [
    r"requirements.*\.txt$",
    r"pyproject\.toml$",
    r"setup\.py$",
    r"setup\.cfg$",
    r"Pipfile$",
    r"poetry\.lock$",
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"pnpm-lock\.yaml$",
]


def extract_package_names(command: str) -> list[str]:
    """Extract package names from an install command."""
    packages = []

    # Remove the install command prefix
    for pattern in NPM_INSTALL_PATTERNS + PIP_INSTALL_PATTERNS:
        command = re.sub(pattern, "", command, count=1)

    # Split remaining args and filter flags
    parts = command.split()
    for part in parts:
        if part.startswith("-"):
            continue
        # Strip version specifiers
        name = re.split(r"[@>=<~^]", part)[0]
        if name and not name.startswith("-"):
            packages.append(name)

    return packages


def check_typosquat(package_name: str) -> str | None:
    """Check if a package name looks like a typosquat."""
    for pattern, real_package in TYPOSQUAT_INDICATORS:
        if re.match(pattern, package_name, re.IGNORECASE):
            return (
                f"Package '{package_name}' looks like a typosquat of '{real_package}'"
            )
    return None


def format_npm_audit_findings(data: dict) -> list[str]:
    """Format npm audit JSON output into detailed findings with CVE info.

    Returns list of formatted vulnerability strings with:
    - CVE IDs
    - Affected packages and versions
    - Severity levels
    - Remediation steps
    """
    findings = []

    try:
        vulns = data.get("vulnerabilities", {})
        for pkg_name, pkg_data in vulns.items():
            if isinstance(pkg_data, dict):
                severity = pkg_data.get("severity", "UNKNOWN").upper()
                version = (
                    pkg_data.get("from", ["?"])[-1] if pkg_data.get("from") else "?"
                )
                cves = pkg_data.get("cves", [])
                remediation = pkg_data.get("fixAvailable", {})

                # Format CVE string
                cve_str = ""
                if cves:
                    cve_ids = [
                        c.get("id", "UNKNOWN") for c in cves if isinstance(c, dict)
                    ]
                    cve_str = f" ({', '.join(cve_ids)})"

                # Format remediation
                remedy = ""
                if remediation and remediation.get("name"):
                    fix_version = remediation.get("version", "latest")
                    remedy = f"\n    Remediation: Update {pkg_name} to {fix_version}"

                finding = f"[{severity}] {pkg_name}@{version}{cve_str}{remedy}"
                findings.append(finding)

        return findings
    except (AttributeError, KeyError, TypeError):
        return []


def parse_pip_audit_json(data: list | dict) -> list[str]:
    """Parse pip-audit JSON output into detailed findings.

    pip-audit output format (as of v2.0):
    [
      {
        "vulnerability_id": "CVE-2021-12345",
        "package_name": "package",
        "installed_version": "1.0.0",
        "fixed_version": "1.0.1",
        "source": "NVD|PyPA",
        "description": "...",
        "vulnerabilities": ["CVE-2021-12345"]
      }
    ]

    Returns list of formatted vulnerability strings.
    """
    findings = []

    # Handle both list and dict formats
    if isinstance(data, dict):
        # Some versions return a dict with "vulnerabilities" key
        items = data.get("vulnerabilities", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    for vuln in items:
        if not isinstance(vuln, dict):
            continue

        try:
            pkg_name = vuln.get("package_name", "unknown")
            installed = vuln.get("installed_version", "?")
            fixed = vuln.get("fixed_version", None)
            cve_id = vuln.get("vulnerability_id", "")
            description = vuln.get("description", "")

            # Truncate long descriptions
            if description and len(description) > 100:
                description = description[:100] + "..."

            cve_str = f" {cve_id}" if cve_id else ""
            remedy = f"\n    Remediation: Update {pkg_name} to {fixed}" if fixed else ""

            desc_str = f"\n    {description}" if description else ""

            finding = f"[MEDIUM] {pkg_name}@{installed}{cve_str}{desc_str}{remedy}"
            findings.append(finding)
        except (AttributeError, KeyError, TypeError):
            continue

    return findings


def run_npm_audit() -> tuple[list[str], bool]:
    """Run npm audit and return detailed findings with CVE info.

    Returns:
        Tuple of (findings_list, has_vulnerabilities)
    """
    try:
        result = subprocess.run(
            ["npm", "audit", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            data = json.loads(result.stdout)

            # Get vulnerability summary
            metadata = data.get("metadata", {})
            vulns = metadata.get("vulnerabilities", {})
            critical = vulns.get("critical", 0)
            high = vulns.get("high", 0)

            if critical > 0 or high > 0:
                findings = format_npm_audit_findings(data)
                vuln_count = len(findings)
                summary = (
                    f"npm audit: {critical} CRITICAL, {high} HIGH, {vuln_count} total"
                )
                return ([summary] + findings, True)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
        pass

    return ([], False)


def run_pip_audit() -> tuple[list[str], bool]:
    """Run pip-audit and return detailed findings with CVE info and remediation.

    Returns:
        Tuple of (findings_list, has_vulnerabilities)
    """
    try:
        result = subprocess.run(
            ["pip-audit", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            try:
                # Try to parse as JSON
                data = json.loads(result.stdout)
                if data:
                    findings = parse_pip_audit_json(data)
                    summary = f"pip-audit: {len(findings)} vulnerabilities found"
                    return ([summary] + findings, True)
            except json.JSONDecodeError:
                # Fallback to text parsing
                if "vulnerability" in result.stdout.lower():
                    msg = "pip-audit: vulnerabilities detected"
                    return ([msg], True)
    except FileNotFoundError:
        # pip-audit not installed - this is fine, it's optional
        pass
    except subprocess.TimeoutExpired:
        pass

    return ([], False)


def check_requirements_file_modified(file_path: str) -> bool:
    """Check if the modified file is a requirements/dependency file."""
    path = Path(file_path)
    filename = path.name

    for pattern in REQUIREMENTS_FILE_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            return True

    return False


def main():
    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    warnings = []
    has_vulnerabilities = False

    # Check for package install commands (Bash tool)
    if tool_name == "Bash":
        command = tool_input.get("command", "")

        # Check if this is a package install command
        is_npm = any(re.search(p, command) for p in NPM_INSTALL_PATTERNS)
        is_pip = any(re.search(p, command) for p in PIP_INSTALL_PATTERNS)

        if is_npm or is_pip:
            # Check for typosquats
            packages = extract_package_names(command)
            for pkg in packages:
                typo_warning = check_typosquat(pkg)
                if typo_warning:
                    warnings.append(f"[TYPOSQUAT] {typo_warning}")

            # Run vulnerability audit
            if is_npm:
                npm_findings, npm_has_vulns = run_npm_audit()
                warnings.extend(npm_findings)
                has_vulnerabilities = has_vulnerabilities or npm_has_vulns

            if is_pip:
                pip_findings, pip_has_vulns = run_pip_audit()
                warnings.extend(pip_findings)
                has_vulnerabilities = has_vulnerabilities or pip_has_vulns

    # Check for requirements file modifications (Write/Edit tool)
    elif tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if file_path and check_requirements_file_modified(file_path):
            # Requirements file changed - should trigger audit
            msg = (
                "⚠️  Requirements file modified. "
                "Run 'pip-audit' or 'npm audit' to verify for new vulnerabilities."
            )
            warnings.append(msg)

    # Output warnings if any
    if warnings:
        report = "DEPENDENCY CHECK REPORT:\n"
        for warning in warnings:
            # Add proper indentation
            lines = warning.split("\n")
            report += f"  {lines[0]}\n"
            for line in lines[1:]:
                report += f"  {line}\n"

        report += "\nAction: Review vulnerabilities and run remediation commands above."

        # Print to stderr (warnings don't block)
        print(report, file=sys.stderr)

        # Exit with warning code (1) if vulnerabilities found
        sys.exit(1 if has_vulnerabilities else 0)

    sys.exit(0)


if __name__ == "__main__":
    main()
