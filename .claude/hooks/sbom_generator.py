#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
SBOM Generator Utility: Generates Software Bill of Materials using Syft.

Supports CycloneDX and SPDX output formats.
Optionally scans for vulnerabilities using Grype.
Returns structured JSON output with component count and file location.

Exit codes:
  0 - Success (no critical/high vulnerabilities if scanning)
  1 - Error during generation
  2 - Syft not installed
  3 - Grype not installed (when --scan requested)
  4 - Critical/high vulnerabilities found
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def check_syft_installed() -> bool:
    """Check if syft is installed and available in PATH."""
    return shutil.which("syft") is not None


def check_grype_installed() -> bool:
    """Check if grype is installed and available in PATH."""
    return shutil.which("grype") is not None


def get_grype_install_instructions() -> str:
    """Return installation instructions for grype."""
    return """
Grype is not installed. Install it using one of these methods:

  macOS (Homebrew):
    brew install grype

  Linux/macOS (curl):
    curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /usr/local/bin

  Go install:
    go install github.com/anchore/grype/cmd/grype@latest

  Docker:
    docker pull anchore/grype:latest

For more options, visit: https://github.com/anchore/grype#installation
"""


def get_syft_install_instructions() -> str:
    """Return installation instructions for syft."""
    return """
Syft is not installed. Install it using one of these methods:

  macOS (Homebrew):
    brew install syft

  Linux/macOS (curl):
    curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin

  Go install:
    go install github.com/anchore/syft/cmd/syft@latest

  Docker:
    docker pull anchore/syft:latest

For more options, visit: https://github.com/anchore/syft#installation
"""


def scan_vulnerabilities(sbom_path: str, output: str | None = None) -> dict:
    """
    Scan an SBOM for vulnerabilities using Grype.

    Args:
        sbom_path: Path to the SBOM file to scan
        output: Optional path for vulnerability report JSON

    Returns:
        dict with keys:
          - success: bool
          - message: str
          - vulnerabilities: dict with counts by severity
          - critical: list of critical vulnerabilities
          - high: list of high vulnerabilities
          - report_file: str (if output specified)
          - error: str (error message if failed)
    """
    sbom_file = Path(sbom_path)
    if not sbom_file.exists():
        return {
            "success": False,
            "message": f"SBOM file not found: {sbom_path}",
            "error": "Generate an SBOM first with --format cyclonedx or --format spdx",
        }

    try:
        # Run grype on the SBOM
        cmd = ["grype", f"sbom:{sbom_file}", "--output", "json"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        # Grype returns non-zero if vulnerabilities found, but that's expected
        # We only fail on actual errors (no JSON output)
        try:
            vuln_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "success": False,
                "message": "Failed to parse Grype output",
                "error": result.stderr.strip() or "Invalid JSON output from grype",
            }

        # Count vulnerabilities by severity
        matches = vuln_data.get("matches", [])
        severity_counts = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "negligible": 0,
            "unknown": 0,
        }
        critical_vulns = []
        high_vulns = []

        for match in matches:
            vuln = match.get("vulnerability", {})
            severity = vuln.get("severity", "unknown").lower()
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

            vuln_info = {
                "id": vuln.get("id", "unknown"),
                "severity": severity,
                "package": match.get("artifact", {}).get("name", "unknown"),
                "version": match.get("artifact", {}).get("version", "unknown"),
                "fix_versions": vuln.get("fix", {}).get("versions", []),
            }

            if severity == "critical":
                critical_vulns.append(vuln_info)
            elif severity == "high":
                high_vulns.append(vuln_info)

        # Write report if output specified
        report_file = None
        if output:
            output_path = Path(output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                json.dump(vuln_data, f, indent=2)
            report_file = str(output_path)

        total = sum(severity_counts.values())
        return {
            "success": True,
            "message": f"Scanned {len(matches)} vulnerabilities",
            "total": total,
            "vulnerabilities": severity_counts,
            "critical": critical_vulns,
            "high": high_vulns,
            "report_file": report_file,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Vulnerability scan timed out",
            "error": "Grype took longer than 5 minutes",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "message": "Grype executable not found",
            "error": "Grype is not in PATH",
        }
    except Exception as e:
        return {
            "success": False,
            "message": "Unexpected error during vulnerability scan",
            "error": str(e),
        }


def generate_sbom(format: str, output: str) -> dict:
    """
    Generate an SBOM using syft.

    Args:
        format: Output format - 'cyclonedx' or 'spdx'
        output: Output file path

    Returns:
        dict with keys:
          - success: bool
          - message: str
          - file: str (output path if successful)
          - component_count: int (number of components if successful)
          - error: str (error message if failed)
    """
    # Map format names to syft format strings
    format_mapping = {
        "cyclonedx": "cyclonedx-json",
        "spdx": "spdx-json",
    }

    syft_format = format_mapping.get(format.lower())
    if not syft_format:
        return {
            "success": False,
            "message": f"Invalid format: {format}",
            "error": "Supported formats: cyclonedx, spdx",
        }

    # Resolve output path
    output_path = Path(output).resolve()

    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Run syft to generate SBOM
    try:
        result = subprocess.run(
            [
                "syft",
                ".",
                "--output",
                f"{syft_format}={output_path}",
            ],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout for large projects
            cwd=os.getcwd(),
        )

        if result.returncode != 0:
            return {
                "success": False,
                "message": "Syft failed to generate SBOM",
                "error": result.stderr.strip() or result.stdout.strip(),
            }

        # Parse the generated SBOM to count components
        if not output_path.exists():
            return {
                "success": False,
                "message": "SBOM file was not created",
                "error": "Syft completed but output file is missing",
            }

        try:
            with open(output_path) as f:
                sbom_data = json.load(f)

            # Extract component count based on format
            if format.lower() == "cyclonedx":
                components = sbom_data.get("components", [])
                component_count = len(components)
            else:  # spdx
                packages = sbom_data.get("packages", [])
                component_count = len(packages)

        except json.JSONDecodeError:
            # SBOM was created but couldn't parse it
            component_count = -1

        return {
            "success": True,
            "message": "SBOM generated successfully",
            "file": str(output_path),
            "format": format.lower(),
            "component_count": component_count,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "SBOM generation timed out",
            "error": "Syft took longer than 5 minutes",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "message": "Syft executable not found",
            "error": "Syft is not in PATH",
        }
    except Exception as e:
        return {
            "success": False,
            "message": "Unexpected error during SBOM generation",
            "error": str(e),
        }


def main() -> int:
    """
    Main entry point for the SBOM generator.

    Returns:
        0 on success (no critical/high vulnerabilities if scanning)
        1 on error
        2 if syft is not installed
        3 if grype is not installed (when --scan requested)
        4 if critical/high vulnerabilities found
    """
    parser = argparse.ArgumentParser(
        description="Generate Software Bill of Materials (SBOM) using Syft",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Generate CycloneDX SBOM to sbom.json
  %(prog)s --format spdx                # Generate SPDX SBOM
  %(prog)s --output my-sbom.json        # Custom output file
  %(prog)s --format spdx --output out/  # SPDX to directory (uses sbom.json name)
  %(prog)s --scan                       # Generate SBOM and scan for vulnerabilities
  %(prog)s --scan --fail-on high        # Fail if high or critical vulns found
""",
    )

    parser.add_argument(
        "--format",
        choices=["cyclonedx", "spdx"],
        default="cyclonedx",
        help="SBOM format (default: cyclonedx)",
    )

    parser.add_argument(
        "--output",
        default="sbom.json",
        help="Output file path (default: sbom.json in current directory)",
    )

    parser.add_argument(
        "--scan",
        action="store_true",
        help="Scan SBOM for vulnerabilities using Grype",
    )

    parser.add_argument(
        "--fail-on",
        choices=["critical", "high", "medium", "low"],
        default="critical",
        help="Exit with code 4 if vulnerabilities at this severity or above are found (default: critical)",
    )

    parser.add_argument(
        "--vuln-report",
        help="Output file for vulnerability report JSON (only with --scan)",
    )

    args = parser.parse_args()

    # Check if syft is installed
    if not check_syft_installed():
        print(get_syft_install_instructions(), file=sys.stderr)
        result = {
            "success": False,
            "message": "Syft is not installed",
            "error": "Install syft to generate SBOMs",
        }
        print(json.dumps(result, indent=2))
        return 2

    # Check if grype is installed (only if --scan requested)
    if args.scan and not check_grype_installed():
        print(get_grype_install_instructions(), file=sys.stderr)
        result = {
            "success": False,
            "message": "Grype is not installed",
            "error": "Install grype to scan for vulnerabilities",
        }
        print(json.dumps(result, indent=2))
        return 3

    # Handle directory output path
    output = args.output
    if os.path.isdir(output) or output.endswith("/"):
        output = os.path.join(output, "sbom.json")

    # Generate SBOM
    result = generate_sbom(args.format, output)

    if not result["success"]:
        print(json.dumps(result, indent=2))
        return 1

    # Scan for vulnerabilities if requested
    if args.scan:
        scan_result = scan_vulnerabilities(output, args.vuln_report)
        result["scan"] = scan_result

        if scan_result["success"]:
            # Check if we should fail based on severity threshold
            severity_order = ["critical", "high", "medium", "low"]
            fail_index = severity_order.index(args.fail_on)
            vulns = scan_result.get("vulnerabilities", {})

            failing_vulns = sum(
                vulns.get(sev, 0) for sev in severity_order[: fail_index + 1]
            )

            if failing_vulns > 0:
                result["scan"]["has_blocking_vulnerabilities"] = True
                result["scan"]["blocking_threshold"] = args.fail_on
                print(json.dumps(result, indent=2))
                return 4

    # Output JSON result
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
