# /company-standup — Daily Standup View

Display a compact daily standup snapshot: queue counts, last 24h completions, open escalations, and top goal progress deltas. Read-only and fast.

## Input
$ARGUMENTS

No arguments required. The standup view always covers the last 24 hours.

---

## Step 1: Get Work Queue Counts

Fetch the full queue to extract status counts:

```bash
uv run .claude/hooks/company/work_allocator.py list
```

Parse the JSON response. Extract the `counts` object:

```json
{
  "success": true,
  "counts": {
    "pending": 4,
    "in_progress": 2,
    "blocked": 1,
    "review": 0,
    "completed": 18,
    "proposed": 0
  }
}
```

Store: `pending_count`, `active_count` (in_progress), `blocked_count`, `review_count`.

---

## Step 2: Get Last 24h Completions

Fetch completed tasks:

```bash
uv run .claude/hooks/company/work_allocator.py list --status completed
```

Parse the JSON response. The `completed` array contains task objects with `updated_at` (ISO timestamp).

Filter to tasks where `updated_at` is within the last 24 hours. Store as `recent_completions`.

For each recent completion, retain: `task_id`, `title`, `updated_at` (as relative time, e.g. "3h ago"), `department`.

---

## Step 3: Get Open Escalations

Fetch Tier 4 (human) escalations:

```bash
uv run .claude/hooks/company/input_channel.py pending
```

Parse the JSON response:

```json
{
  "success": true,
  "escalations": [...],
  "count": 2
}
```

Store `escalation_count` and the `escalations` list. For each escalation, retain: `task_id`, `trigger`, `created_at` (as relative time), `original_agent`.

---

## Step 4: Get Goal Deltas

Read the strategic state file to compute goal progress deltas between the two most recent snapshots:

Read `.company/state/strategic_state.json`

The file has this structure:

```json
{
  "goal_snapshots": [
    {
      "timestamp": "2026-07-02T18:00:00Z",
      "assessments": [
        {"goal_id": "G1", "progress_percent": 42, "status": "on_track", "velocity_trend": "improving"},
        ...
      ]
    },
    {
      "timestamp": "2026-07-03T06:00:00Z",
      "assessments": [
        {"goal_id": "G1", "progress_percent": 45, "status": "on_track", "velocity_trend": "improving"},
        ...
      ]
    }
  ]
}
```

**If fewer than 2 snapshots exist**, skip this section (show "No baseline snapshot yet for goal deltas").

**If 2+ snapshots exist**, compare the **last two** snapshots:
- For each goal, compute `delta = latest_progress - previous_progress`
- Sort goals by `abs(delta)` descending
- Keep top 5 goals with the largest absolute delta (positive or negative)
- For goals with `delta == 0`, include them only if velocity_trend is "regressing" in the latest snapshot

Store as `goal_deltas` list with fields: `goal_id`, `delta`, `latest_progress`, `status`, `velocity_trend`.

**If no strategic_state.json file exists**, skip goal deltas section entirely.

---

## Step 5: Render Output

### 5.1: Header

```
================================================================================
  DAILY STANDUP                                          [YYYY-MM-DD HH:MM UTC]
================================================================================
```

### 5.2: Queue Status

```
### Queue

| Status   | Count |
|----------|-------|
| Active   | X     |
| Pending  | X     |
| Blocked  | X     |
| Review   | X     |
```

If `blocked_count > 0`, append after the table:
```
⚠  [X] task(s) blocked — run /pending to review
```

If `review_count > 0`, append:
```
◉  [X] task(s) awaiting review
```

### 5.3: Completed Last 24h

**If `recent_completions` is non-empty:**

```
### Completed Last 24h  ([N] tasks)

| # | Task | Department | Completed |
|---|------|------------|-----------|
| 1 | [title] | [dept] | [Xh ago] |
| 2 | [title] | [dept] | [Xh ago] |
```

Cap display at 10 rows. If more than 10, append: `… and [N-10] more.`

**If empty:**

```
### Completed Last 24h

No tasks completed in the last 24 hours.
```

### 5.4: Open Escalations

**If `escalation_count > 0`:**

```
### Escalations  ([N] awaiting human response)

| # | Task ID | Reason | Escalated |
|---|---------|--------|-----------|
| 1 | [task_id] | [trigger] | [Xh ago] |
| 2 | [task_id] | [trigger] | [Xh ago] |

Use /respond [task-id] "message" to resolve.
```

**If `escalation_count == 0`:**

```
### Escalations

No escalations awaiting human response.
```

### 5.5: Goal Deltas

**If `goal_deltas` is available and non-empty:**

```
### Goal Deltas  (since last snapshot)

| Goal | Progress | Δ | Trend |
|------|----------|---|-------|
| G1: Quality | 45% | +3% | improving ↑ |
| G5: Autonomy | 61% | 0% | stalled → |
| G3: Stability | 88% | -2% | regressing ↓ |
```

Trend symbols:
- `improving` → `↑`
- `stalled` → `→`
- `regressing` → `↓`

Delta formatting:
- Positive: `+N%`
- Negative: `-N%`
- Zero: `0%`

**If no snapshots / file missing:**

```
### Goal Deltas

No snapshot data available. Run /company-status to assess goals.
```

### 5.6: Footer

```
================================================================================
  NEXT ACTIONS
================================================================================

| Need | Command |
|------|---------|
| Resolve escalations | /respond [task-id] "message" |
| Unblock tasks | /pending --type blocked |
| Submit new work | /company-request "description" |
| Full status | /company-status |
| Goal details | /company-status (goals section) |

================================================================================
```

---

## Step 6: Error Handling

### Company Not Initialized

If `.company/` directory does not exist:

```
## Company Not Initialized

No company structure found. Run /company-init first.
```

### Work Allocator Error

If `work_allocator.py list` fails:

```
## Queue Unavailable

Could not read work queue: [error]

Partial data is shown where available.
```

Still proceed with escalations and goal deltas.

### Escalation Read Error

If `input_channel.py pending` fails, skip the escalations section and note:

```
(Escalation data unavailable — input channel error)
```

### Missing strategic_state.json

Skip the goal deltas section silently (no error shown).

---

## Rules

1. **Read-only.** This command never modifies any file or queue.
2. **Fast.** Use `work_allocator.py list` (not full status), read strategic_state.json directly (no goal assessors). Do NOT run `goal_tracker.py assess`.
3. **Compact.** Cap completions at 10 rows. Cap goal deltas at 5 rows. No deep detail — link to /company-status for that.
4. **Graceful degradation.** If any data source fails, show what's available and note the gap.
5. **Relative times.** Always convert ISO timestamps to relative human-readable form (e.g., "3h ago", "just now", "yesterday").
6. **Show blocked prominently.** Blocked tasks and open escalations are the highest-priority standup signals.
7. **No goal assessor execution.** Goal deltas come from pre-existing snapshots in strategic_state.json, not from running assessors (which are slow).

---

## Examples

### Example 1: Normal Day

```
================================================================================
  DAILY STANDUP                                          [2026-07-03 09:00 UTC]
================================================================================

### Queue

| Status   | Count |
|----------|-------|
| Active   | 3     |
| Pending  | 5     |
| Blocked  | 1     |
| Review   | 0     |

⚠  1 task(s) blocked — run /pending to review

### Completed Last 24h  (4 tasks)

| # | Task | Department | Completed |
|---|------|------------|-----------|
| 1 | Add audit export endpoint | engineering | 2h ago |
| 2 | Fix CI race condition in coverage | engineering | 5h ago |
| 3 | Update homebrew formula sha256 | devops | 8h ago |
| 4 | Write tests for secrets_scanner | engineering | 11h ago |

### Escalations

No escalations awaiting human response.

### Goal Deltas  (since last snapshot)

| Goal | Progress | Δ | Trend |
|------|----------|---|-------|
| G1: Quality | 45% | +3% | improving ↑ |
| G3: Stability | 88% | +1% | improving ↑ |
| G5: Autonomy | 61% | 0% | stalled → |

================================================================================
  NEXT ACTIONS
================================================================================

| Need | Command |
|------|---------|
| Resolve escalations | /respond [task-id] "message" |
| Unblock tasks | /pending --type blocked |
| Submit new work | /company-request "description" |
| Full status | /company-status |
| Goal details | /company-status (goals section) |

================================================================================
```

### Example 2: Escalation Active

```
================================================================================
  DAILY STANDUP                                          [2026-07-03 09:00 UTC]
================================================================================

### Queue

| Status   | Count |
|----------|-------|
| Active   | 2     |
| Pending  | 3     |
| Blocked  | 2     |
| Review   | 1     |

⚠  2 task(s) blocked — run /pending to review
◉  1 task(s) awaiting review

### Completed Last 24h

No tasks completed in the last 24 hours.

### Escalations  (1 awaiting human response)

| # | Task ID | Reason | Escalated |
|---|---------|--------|-----------|
| 1 | task-abc123 | capability_mismatch | 3h ago |

Use /respond task-abc123 "message" to resolve.

### Goal Deltas  (since last snapshot)

No snapshot data available. Run /company-status to assess goals.

================================================================================
  NEXT ACTIONS
================================================================================

| Need | Command |
|------|---------|
| Resolve escalations | /respond [task-id] "message" |
| Unblock tasks | /pending --type blocked |
| Submit new work | /company-request "description" |
| Full status | /company-status |
| Goal details | /company-status (goals section) |

================================================================================
```
