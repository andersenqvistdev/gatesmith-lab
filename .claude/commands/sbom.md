# /sbom — Generate Software Bill of Materials

Generate a Software Bill of Materials (SBOM) for compliance and security auditing. Uses Syft to produce CycloneDX or SPDX format output. Optionally scan for vulnerabilities using Grype.

## Input
$ARGUMENTS

Supported arguments:
- `--format cyclonedx|spdx` — Output format (default: cyclonedx)
- `--output FILE` — Output file path (default: sbom.json in current directory)
- `--scan` — Scan SBOM for vulnerabilities using Grype
- `--fail-on critical|high|medium|low` — Fail if vulnerabilities at this severity or above (default: critical)
- `--vuln-report FILE` — Output file for vulnerability report JSON (only with --scan)

## Step 1: Parse Arguments

Extract options from arguments:
- `--format` or `-f`: cyclonedx (default) or spdx
- `--output` or `-o`: custom output file path

If no arguments provided, use defaults:
- Format: cyclonedx
- Output: ./sbom.json

## Step 2: Check Syft Installation

Run the SBOM generator utility to check if syft is installed:

```bash
uv run .claude/hooks/sbom_generator.py --check
```

If exit code is 2 (syft not installed), proceed to Step 3.
If exit code is 0 (syft found), skip to Step 4.

## Step 3: Show Installation Instructions

If syft is not installed, display instructions and exit:

```
══════════════════════════════════════════
  SBOM Generation — Missing Dependency
══════════════════════════════════════════

Syft is required for SBOM generation but was not found.

Install syft:
  curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin

Or via Homebrew (macOS):
  brew install syft

After installation, run /sbom again.
══════════════════════════════════════════
```

Do NOT proceed further — the user must install syft first.

## Step 4: Generate SBOM

Run the SBOM generator with the parsed arguments:

```bash
uv run .claude/hooks/sbom_generator.py --format <FORMAT> --output <OUTPUT>
```

Capture the JSON output which contains:
- `success`: boolean
- `format`: the format used
- `output_file`: path to generated file
- `components`: count of components by type
- `error`: error message if failed

## Step 5: Display Results

On success, display the summary:

```
══════════════════════════════════════════
  SBOM Generated Successfully
══════════════════════════════════════════

Format: CycloneDX JSON
Output: ./sbom.json

Components Found:
  Libraries:     142
  Applications:    3
  ─────────────────
  Total:         145

The SBOM contains a complete inventory of:
  • Direct dependencies
  • Transitive dependencies
  • System packages (if applicable)

══════════════════════════════════════════
```

On failure, display the error:

```
══════════════════════════════════════════
  SBOM Generation Failed
══════════════════════════════════════════

Error: [error message from generator]

Troubleshooting:
  • Ensure syft is installed and in PATH
  • Check write permissions for output directory
  • Run 'syft --version' to verify installation

══════════════════════════════════════════
```

## Step 6: Vulnerability Scanning (if --scan)

If `--scan` flag is provided, scan the generated SBOM for vulnerabilities:

```bash
uv run .claude/hooks/sbom_generator.py --format <FORMAT> --output <OUTPUT> --scan --fail-on <SEVERITY>
```

On success with vulnerabilities found:

```
══════════════════════════════════════════
  Vulnerability Scan Results
══════════════════════════════════════════

Severity Summary:
  Critical:     2  ← Action Required
  High:         5  ← Action Required
  Medium:      12
  Low:         23
  ─────────────────
  Total:       42

Critical Vulnerabilities:
  CVE-2024-1234 | lodash@4.17.20 | Fix: 4.17.21
  CVE-2024-5678 | openssl@1.1.1  | Fix: 1.1.1k

High Vulnerabilities:
  CVE-2024-2345 | axios@0.21.1   | Fix: 0.21.2
  [...]

══════════════════════════════════════════
```

Exit codes with --scan:
- `0` — No vulnerabilities at or above threshold
- `4` — Vulnerabilities found at or above threshold

## Step 7: Mention Vulnerability Scanning (without --scan)

If --scan was NOT used, show the tip:

```
Tip: Scan for vulnerabilities:
  /sbom --scan

Or use Grype directly:
  grype sbom:./sbom.json
```

## Examples

```bash
# Generate CycloneDX SBOM (default)
/sbom

# Generate SPDX format
/sbom --format spdx

# Custom output file
/sbom --output reports/sbom-2024.json

# SPDX with custom output
/sbom --format spdx --output compliance/spdx-report.json

# Generate SBOM and scan for vulnerabilities
/sbom --scan

# Fail on high severity or above (CI/CD gating)
/sbom --scan --fail-on high

# Save vulnerability report for compliance
/sbom --scan --vuln-report reports/vulnerabilities.json
```

## Rules
- NEVER proceed without syft installed — show instructions and exit
- ALWAYS use the sbom_generator.py utility — don't call syft directly
- Display component counts by type for visibility
- With --scan, require grype installed — show instructions if missing
- Exit code 4 indicates blocking vulnerabilities (use for CI gating)
- Log SBOM generation to activity logs for audit trail
