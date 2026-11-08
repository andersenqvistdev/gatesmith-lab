# /audit-export — Export Audit Trail for Compliance

Export comprehensive audit data for compliance, security reviews, and regulatory requirements. Aggregates activity logs, session history, security configuration, and SBOM into a single compliance-ready package.

## Input
$ARGUMENTS

Supported arguments:
- `--since=YYYY-MM-DD` — Start date filter (default: 30 days ago)
- `--until=YYYY-MM-DD` — End date filter (default: today)
- `--format=json|csv|both` — Output format (default: json)
- `--output=DIR` — Output directory (default: ./audit-export/)
- `--include-sbom` — Include SBOM in export (requires syft)
- `--redact-paths` — Redact absolute file paths for privacy

## Step 1: Parse Arguments

Extract options from arguments:
- `--since` or `-s`: Start date (default: 30 days ago)
- `--until` or `-u`: End date (default: today)
- `--format` or `-f`: json, csv, or both
- `--output` or `-o`: Output directory
- `--include-sbom`: Flag to include SBOM
- `--redact-paths`: Flag to redact file paths

Display parsing result:
```
═══════════════════════════════════════════════════════════════
 AUDIT EXPORT
═══════════════════════════════════════════════════════════════
 Period: YYYY-MM-DD to YYYY-MM-DD
 Format: JSON / CSV / Both
 Output: ./audit-export/
 Options: [include-sbom] [redact-paths]
═══════════════════════════════════════════════════════════════
```

## Step 2: Run Audit Exporter

Execute the audit exporter utility:

```bash
uv run .claude/hooks/audit_exporter.py \
  --since <START_DATE> \
  --until <END_DATE> \
  --format <FORMAT> \
  --output <OUTPUT_DIR> \
  [--include-sbom] \
  [--redact-paths]
```

The exporter collects:
1. **Activity Logs** (`logs/activity.jsonl`) — Tool invocations, agent actions
2. **Session Logs** (`logs/sessions.jsonl`) — Session metadata, git state
3. **Prompt Logs** (`logs/prompts.jsonl`) — Prompt guard triggers
4. **Gate Logs** (`logs/gates.jsonl`) — Human approval decisions
5. **Security Config** — Hooks enabled, trust tier settings
6. **SBOM** (optional) — Software bill of materials

## Step 3: Display Results

On success, display the export summary:

```
═══════════════════════════════════════════════════════════════
 AUDIT EXPORT COMPLETE
═══════════════════════════════════════════════════════════════

 Export Directory: ./audit-export/

 Files Generated:
 ────────────────────────────────────────────────────────────
 │ audit-manifest.json      │ Export metadata and checksums │
 │ activity-log.json        │ 1,234 tool invocations        │
 │ sessions.json            │ 45 sessions                   │
 │ security-config.json     │ 26 hooks, trust tier: guarded │
 │ sbom.json                │ 145 components (if included)  │
 ────────────────────────────────────────────────────────────

 Summary:
 ────────────────────────────────────────────────────────────
 │ Period           │ 2026-01-09 to 2026-02-08             │
 │ Total Events     │ 1,324                                │
 │ Sessions         │ 45                                   │
 │ Unique Tools     │ 12                                   │
 │ Blocked Actions  │ 3                                    │
 │ Gate Approvals   │ 7                                    │
 ────────────────────────────────────────────────────────────

═══════════════════════════════════════════════════════════════
```

On failure, display the error:

```
═══════════════════════════════════════════════════════════════
 AUDIT EXPORT FAILED
═══════════════════════════════════════════════════════════════

 Error: [error message]

 Troubleshooting:
   • Ensure logs/ directory exists with log files
   • Check write permissions for output directory
   • Verify date format is YYYY-MM-DD

═══════════════════════════════════════════════════════════════
```

## Step 4: Verify Export Integrity

After export, verify the manifest checksums:

```bash
uv run .claude/hooks/audit_exporter.py --verify <OUTPUT_DIR>
```

Display verification result:
```
 Integrity Check: ✓ All files verified
```

## Export Package Contents

### audit-manifest.json
```json
{
  "version": "1.0",
  "generated_at": "2026-02-08T12:00:00Z",
  "generated_by": "forge-audit-export",
  "period": {
    "start": "2026-01-09",
    "end": "2026-02-08"
  },
  "files": [
    {"name": "activity-log.json", "sha256": "...", "records": 1234},
    {"name": "sessions.json", "sha256": "...", "records": 45}
  ],
  "summary": {
    "total_events": 1324,
    "sessions": 45,
    "blocked_actions": 3,
    "gate_approvals": 7
  }
}
```

### security-config.json
```json
{
  "hooks": {
    "total": 26,
    "by_event": {
      "PreToolUse": 5,
      "PostToolUse": 4,
      "UserPromptSubmit": 1
    }
  },
  "trust_tier": "guarded",
  "autonomy_level": "full",
  "flow_mode_enabled": false,
  "protected_branches": ["main", "master"],
  "forbidden_patterns": ["rm -rf /", "sudo rm", "chmod 777"]
}
```

## Examples

```bash
# Default export (last 30 days, JSON format)
/audit-export

# Export specific date range
/audit-export --since=2026-01-01 --until=2026-01-31

# Export as CSV for spreadsheet analysis
/audit-export --format=csv

# Full compliance package with SBOM
/audit-export --include-sbom --format=both

# Redacted export for external sharing
/audit-export --redact-paths --output=./compliance-report/
```

## Use Cases

### SOC 2 Compliance
```bash
/audit-export --since=2026-01-01 --format=both --include-sbom
```
Provides: Activity audit trail, access logs, security controls, dependency inventory.

### Security Incident Investigation
```bash
/audit-export --since=2026-02-05 --until=2026-02-06
```
Provides: Detailed activity during incident window with tool invocations and outcomes.

### Quarterly Security Review
```bash
/audit-export --since=2026-01-01 --until=2026-03-31 --include-sbom
```
Provides: Full quarter activity with software inventory for review.

## Rules
- ALWAYS include audit-manifest.json with checksums for integrity verification
- ALWAYS filter by date range — never export unbounded data
- Include security configuration to show what controls were in place
- Redact absolute paths when --redact-paths is specified (replace with relative paths)
- Log the export action itself to the activity log
- SBOM requires syft — show installation instructions if missing
