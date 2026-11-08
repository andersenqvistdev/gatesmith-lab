# /scout-status — View Opportunity Scout Pipeline Status

Show the current state of all tasks proposed by the opportunity scout, including their pipeline status, recent admission rejections, and summary statistics.

## Step 1: Gather Scout Data

Run the scout status helper from the repo root:

```bash
uv run .claude/hooks/company/scout_status.py
```

The script searches upward from the hooks directory for the nearest `.company` directory (containing `org.json`) and the corresponding `.planning/ROADMAP-SCOUT.md` intake file. It outputs JSON to stdout.

Capture the JSON output. If the command fails or produces no output, display:

```
═══════════════════════════════════════════════════════════════
 SCOUT STATUS                                        [ERROR]
═══════════════════════════════════════════════════════════════

Could not read scout data. Ensure you are in a Gatesmith project
with a .company/ directory.

  Check: ls .company/
  Check: cat .planning/ROADMAP-SCOUT.md
═══════════════════════════════════════════════════════════════
```

## Step 2: Parse the JSON

The output has this structure:

```json
{
  "scout_tasks": [
    {"id": "SCOUT-...", "title": "...", "complexity": "...", "status": "...", "queue_id": null}
  ],
  "recent_rejections": [
    {"ts": "...", "task_id": "SCOUT-...", "title": "...", "reason": "..."}
  ],
  "stats": {
    "total": 5,
    "pending": 2,
    "queued": 1,
    "in_progress": 0,
    "completed": 1,
    "rejected": 1
  },
  "last_scan": "2026-07-14T...",
  "current_wave": 1
}
```

Status values for each task:
- `pending` — in intake file, not yet scheduled
- `queued (WQ-XXX)` — admitted to the work queue
- `in_progress` — actively being worked on by an employee
- `completed` — merged and done
- `rejected` — blocked by the admission gate

## Step 3: Display the Report

Format and display the report using this layout:

```
═══════════════════════════════════════════════════════════════
 SCOUT STATUS                          Wave 1 · 5 tasks total
═══════════════════════════════════════════════════════════════

 ──────────────────────────────────────────────────────────────
 SCOUT INTAKE  (.planning/ROADMAP-SCOUT.md)
 ──────────────────────────────────────────────────────────────

 SCOUT-20260713-1  [pending]     standard  Harden reviewer agents...
 SCOUT-20260713-2  [pending]     standard  Fix stale model pricing...
 SCOUT-20260714-1  [queued]      trivial   Adopt /doctor checkup...
 SCOUT-20260714-2  [completed]   standard  Close rm -rf bypass...
 SCOUT-20260714-3  [rejected]    standard  Require human signal...

 ──────────────────────────────────────────────────────────────
 PIPELINE SUMMARY
 ──────────────────────────────────────────────────────────────

 Total:       5
 Pending:     2  (awaiting scheduling)
 Queued:      1  (admitted to work queue)
 In Progress: 0
 Completed:   1
 Rejected:    1

 Last Scan:   5m ago    Current Wave: 1

 ──────────────────────────────────────────────────────────────
 RECENT REJECTIONS
 ──────────────────────────────────────────────────────────────

 SCOUT-20260714-3  Require human signal before compliance merge
   Reason: target not found: compliance-report.json

═══════════════════════════════════════════════════════════════
```

### Display Rules

**Scout Intake table**: one row per task from `scout_tasks`. Truncate titles to fit 60 characters. Show the status in brackets. Color-code status where supported:
- `[pending]` — dim/grey
- `[queued ...]` — cyan/blue
- `[in_progress]` — yellow
- `[completed]` — green
- `[rejected]` — red

**Last Scan**: convert ISO timestamp to relative time (e.g. "5m ago", "2h ago"). Show "never" if `last_scan` is null.

**If no intake file found**: show "No scout intake file found at .planning/ROADMAP-SCOUT.md" in the intake section instead of the table.

**If no rejections**: show "No recent rejections." in the rejections section.

## Step 4: Show Next Steps

After the report, show:

```
### Next Steps

• Review pending tasks:  cat .planning/ROADMAP-SCOUT.md
• Scout PRs waiting:     gh pr list --search "head:scout/"
• Force roadmap scan:    /run-loop
• Submit manual task:    /company-request "description"
```

## Rules

- **Always run from repo root** — the script auto-finds .company from the hooks directory
- **Graceful degradation** — if .company is missing or roadmap_state.json is absent, show what is known from the intake file with all tasks as "pending"
- **Never error out** — if the script fails, show the error section from Step 1 instead of a stack trace
- **No arguments needed** — `/scout-status` requires no arguments; ignore any supplied
