# /lifecycle — Company Lifecycle Phase Management

Display and manage company lifecycle phases with metrics, transitions, and history.

## Input
$ARGUMENTS

Usage:
```
/lifecycle                    # Show current phase and metrics
/lifecycle status             # Detailed phase status
/lifecycle transition <phase> # Request phase transition
/lifecycle history            # Phase transition history
/lifecycle config             # Show/edit lifecycle config
```

---

## Step 0: Parse Arguments and Resolve Company

Parse the arguments to determine the subcommand:

```bash
# Find company root and mode
uv run .claude/hooks/company/company_resolver.py find
uv run .claude/hooks/company/company_resolver.py mode
```

Store:
- `company_root` — Path to company root
- `company_dir` — Path to `.company/` directory
- `subcommand` — One of: (empty), "status", "transition", "history", "config"
- `transition_target` — Target phase if subcommand is "transition"

### Check Prerequisites

```bash
# Check for .company directory
ls [company_dir]/org.json 2>/dev/null
```

**If not exists:**
```
No company initialized. Run /company-init or /company-bootstrap first.
```
Exit without further processing.

---

## Step 1: Gather Lifecycle Data

Fetch current phase and metrics using the phase detector:

```bash
# Get phase detection result
uv run .claude/hooks/company/phase_detector.py detect

# Get current phase from state tracker
uv run .claude/hooks/state_tracker.py get-company-phase

# Get metrics for display
uv run .claude/hooks/company/phase_detector.py metrics
```

Parse the JSON outputs and extract:
- `current_phase` — Current detected phase (startup, growth, scale, mature, decline_pivot)
- `phase_since` — Date when current phase started
- `confidence` — Detection confidence (0.0-1.0)
- `metrics` — Current metrics (employees, velocity, blocked_ratio, etc.)
- `transition_suggested` — Suggested next phase and requirements
- `transition_pending` — Any pending transition awaiting confirmation

---

## Step 2: Route by Subcommand

### Default View (no subcommand)

Display the compact lifecycle dashboard.

**Output:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE                                    [PHASE]
═══════════════════════════════════════════════════════════════
 Current Phase:  {phase} (since {phase_since})
 Confidence:     {confidence}%

 Metrics:
 ├── Employees:       {employees} / {target_employees} ({percent}% to {next_phase})
 ├── Completed Tasks: {completed_tasks}
 ├── Velocity:        {velocity} tasks/day ({velocity_trend})
 └── Blocked Ratio:   {blocked_ratio}%

 Next Milestone: {next_phase_upper}
 └── Need: {transition_requirements}
═══════════════════════════════════════════════════════════════
```

**Phase display mapping:**
| Phase | Display Name | Box Label |
|-------|--------------|-----------|
| startup | Startup | [STARTUP] |
| growth | Growth | [GROWTH] |
| scale | Scale | [SCALE] |
| mature | Mature | [MATURE] |
| decline_pivot | Pivot/Decline | [PIVOT] |

**Velocity trend indicators:**
- If velocity increased >5% from previous: `(↑{percent}%)`
- If velocity decreased >5% from previous: `(↓{percent}%)`
- If velocity stable (within +-5%): `(stable)`

**If transition is pending:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE                                    [PHASE]
═══════════════════════════════════════════════════════════════
 Current Phase:  {phase} (since {phase_since})
 Confidence:     {confidence}%

 ⚠ TRANSITION PENDING: {phase} → {pending_phase}
   Run `/lifecycle transition confirm` to complete transition.

 Metrics:
 ├── Employees:       {employees} / {target_employees} ({percent}% to {next_phase})
 ├── Completed Tasks: {completed_tasks}
 ├── Velocity:        {velocity} tasks/day ({velocity_trend})
 └── Blocked Ratio:   {blocked_ratio}%
═══════════════════════════════════════════════════════════════
```

### Status Subcommand (/lifecycle status)

Display detailed phase status with all metrics and phase analysis.

**Output:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE — DETAILED STATUS
═══════════════════════════════════════════════════════════════

## Current Phase: {PHASE}

| Attribute | Value |
|-----------|-------|
| Phase | {phase} |
| Since | {phase_since} |
| Duration | {days} days |
| Confidence | {confidence}% |
| Last Assessment | {last_assessment} |

## Metrics Snapshot

| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Employees | {employees} | {target} | {status_icon} |
| Completed Tasks | {completed} | - | - |
| Velocity (tasks/day) | {velocity} | {target_velocity} | {status_icon} |
| Velocity Variance | {variance}% | <{target}% | {status_icon} |
| Blocked Ratio | {blocked}% | <{target}% | {status_icon} |
| Stalled Ratio | {stalled}% | <{target}% | {status_icon} |
| Test Coverage | {coverage}% | >{target}% | {status_icon} |

## Phase Detection Analysis

| Phase | Matches | Confidence | Key Reasons |
|-------|---------|------------|-------------|
| startup | {yes/no} | {conf}% | {reasons} |
| growth | {yes/no} | {conf}% | {reasons} |
| scale | {yes/no} | {conf}% | {reasons} |
| mature | {yes/no} | {conf}% | {reasons} |
| decline_pivot | {yes/no} | {conf}% | {reasons} |

## Transition Progress

Next Phase: {next_phase}
Progress: [{progress_bar}] {progress}%

Requirements:
{requirements_list}

═══════════════════════════════════════════════════════════════
```

**Status icons:**
- Meeting target: `[OK]`
- Close to target (within 10%): `[~]`
- Not meeting target: `[!]`

### Transition Subcommand (/lifecycle transition <phase>)

Request or confirm a phase transition.

#### /lifecycle transition <phase> (request new transition)

**Step 1: Validate the target phase**

Valid phases: startup, growth, scale, mature, decline_pivot

**If invalid phase:**
```
Invalid phase: {phase}

Valid phases:
  - startup      Early stage, small team, initial development
  - growth       Expanding team, increasing velocity
  - scale        Large team, high test coverage, efficient
  - mature       Stable velocity, optimized operations
  - decline_pivot  Declining metrics, needs intervention

Usage: /lifecycle transition <phase>
```

**Step 2: Check current phase**

**If target phase equals current phase:**
```
Already in {phase} phase. No transition needed.

Current metrics:
  Employees: {employees}
  Velocity: {velocity} tasks/day
  Blocked Ratio: {blocked}%
```

**Step 3: Display transition confirmation prompt**

```
═══════════════════════════════════════════════════════════════
 PHASE TRANSITION REQUEST
═══════════════════════════════════════════════════════════════

 From: {current_phase}
 To:   {target_phase}

## Current Metrics
| Metric | Value | {target_phase} Threshold |
|--------|-------|--------------------------|
| Employees | {val} | {threshold} |
| Velocity | {val} | {threshold} |
| Blocked Ratio | {val}% | {threshold}% |

## Transition Readiness

{readiness_analysis}

## Confirmation Required

This will:
1. Update the company phase in STATE.md
2. Record the transition in history
3. Adjust phase-specific behaviors

To confirm this transition, run:
  /lifecycle transition confirm

To cancel, simply don't confirm.
═══════════════════════════════════════════════════════════════
```

**Step 4: Set pending transition**

```bash
uv run .claude/hooks/state_tracker.py set-company-phase {current_phase} --transition {target_phase}
```

#### /lifecycle transition confirm

**Step 1: Check for pending transition**

```bash
uv run .claude/hooks/state_tracker.py get-company-phase
```

**If no pending transition:**
```
No pending transition to confirm.

To request a transition, run:
  /lifecycle transition <phase>

Available phases: startup, growth, scale, mature, decline_pivot
```

**Step 2: Confirm the transition**

```bash
uv run .claude/hooks/state_tracker.py confirm-transition
```

**Output:**
```
═══════════════════════════════════════════════════════════════
 PHASE TRANSITION CONFIRMED
═══════════════════════════════════════════════════════════════

 {previous_phase} → {new_phase}

 Transitioned at: {timestamp}

 The company is now operating in {new_phase} phase.

 Phase characteristics:
 {phase_description}

 Recommended actions:
 {recommendations}

═══════════════════════════════════════════════════════════════
```

**Phase descriptions:**

| Phase | Description |
|-------|-------------|
| startup | Small team, rapid iteration, establishing foundations |
| growth | Expanding capabilities, hiring, increasing velocity |
| scale | Optimizing processes, high coverage, stable operations |
| mature | Peak efficiency, stable velocity, minimal friction |
| decline_pivot | Intervention needed, reassess strategy, unblock work |

### History Subcommand (/lifecycle history)

Display phase transition history.

**Step 1: Read history from STATE.md and git log**

```bash
# Get phase information from STATE.md
uv run .claude/hooks/state_tracker.py get-company-phase

# Search git log for phase transition commits
git log --oneline --grep="phase transition" --grep="company phase" --all-match -20 2>/dev/null || echo "No git history"
```

**Output:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE — PHASE HISTORY
═══════════════════════════════════════════════════════════════

## Current Phase
  Phase: {phase}
  Since: {since}
  Duration: {days} days

## Transition History

| Date | From | To | Trigger | Duration in Phase |
|------|------|-----|---------|-------------------|
| {date} | {from} | {to} | {trigger} | {duration} |
| {date} | {from} | {to} | {trigger} | {duration} |
| {date} | - | startup | Initial | - |

## Phase Duration Summary

| Phase | Total Time | Entries | Avg Duration |
|-------|------------|---------|--------------|
| startup | {time} | {count} | {avg} |
| growth | {time} | {count} | {avg} |
| scale | {time} | {count} | {avg} |
| mature | {time} | {count} | {avg} |
| decline_pivot | {time} | {count} | {avg} |

═══════════════════════════════════════════════════════════════
```

**If no history available:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE — PHASE HISTORY
═══════════════════════════════════════════════════════════════

## Current Phase
  Phase: {phase}
  Since: {since}

## Transition History

No transition history recorded yet.

Phase transitions are recorded when you run:
  /lifecycle transition <phase>

═══════════════════════════════════════════════════════════════
```

### Config Subcommand (/lifecycle config)

Display and optionally edit lifecycle configuration.

**Step 1: Load current config**

```bash
# Get current thresholds
uv run .claude/hooks/company/phase_detector.py metrics
```

**Output:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE — CONFIGURATION
═══════════════════════════════════════════════════════════════

## Phase Detection Thresholds

Configuration file: .company/config.json
Key: "phase_detection"

### Startup Phase
| Threshold | Value | Description |
|-----------|-------|-------------|
| max_employees | {val} | Below this = startup |
| max_completed_tasks | {val} | Below this = startup |

### Growth Phase
| Threshold | Value | Description |
|-----------|-------|-------------|
| min_employees | {val} | At least this many |
| max_employees | {val} | Below this (not yet scale) |
| min_velocity | {val} | Tasks/day minimum |

### Scale Phase
| Threshold | Value | Description |
|-----------|-------|-------------|
| min_employees | {val} | At least this many |
| min_test_coverage | {val}% | Coverage requirement |
| max_blocked_ratio | {val}% | Must be below this |

### Mature Phase
| Threshold | Value | Description |
|-----------|-------|-------------|
| min_employees | {val} | At least this many |
| max_velocity_variance | {val}% | Stability requirement |
| max_blocked_ratio | {val}% | Must be below this |
| velocity_window_days | {val} | Window for variance calc |

### Decline/Pivot Phase (any triggers)
| Threshold | Value | Description |
|-----------|-------|-------------|
| velocity_decline_threshold | {val}% | Decline triggers pivot |
| max_blocked_ratio | {val}% | Above triggers pivot |
| max_stalled_ratio | {val}% | Above triggers pivot |

## Customizing Thresholds

To customize thresholds, add a "phase_detection" key to .company/config.json:

```json
{
  "phase_detection": {
    "startup": {
      "max_employees": 5,
      "max_completed_tasks": 20
    },
    "growth": {
      "min_employees": 5,
      "max_employees": 15
    }
  }
}
```

Only include the thresholds you want to override. Defaults are used for the rest.

═══════════════════════════════════════════════════════════════
```

---

## Step 3: Handle No Phase Set

If no company phase has been set yet:

```bash
# Check if phase exists
uv run .claude/hooks/state_tracker.py get-company-phase
```

**If no phase:**
```
═══════════════════════════════════════════════════════════════
 COMPANY LIFECYCLE — NOT INITIALIZED
═══════════════════════════════════════════════════════════════

No company phase has been set yet.

## Auto-Detect Phase

Running phase detection based on current metrics...

{Run phase_detector.py detect and show results}

Detected Phase: {phase} ({confidence}% confidence)

## Set Initial Phase

To set the initial phase, run:
  /lifecycle transition {detected_phase}

Or manually select a phase:
  /lifecycle transition startup     # Just getting started
  /lifecycle transition growth      # Team is growing
  /lifecycle transition scale       # Operating at scale
  /lifecycle transition mature      # Stable operations

═══════════════════════════════════════════════════════════════
```

---

## Helper Functions

### Render Progress Bar

```python
def render_progress_bar(percent: float, width: int = 20) -> str:
    filled = int(percent / 100 * width)
    return "█" * filled + "░" * (width - filled)
```

Example: `[████████████░░░░░░░░] 60%`

### Calculate Days Since

```python
from datetime import datetime

def days_since(iso_date: str) -> int:
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days
```

### Format Velocity Trend

```python
def format_velocity_trend(current: float, previous: float) -> str:
    if previous == 0:
        return "(no history)"
    change = ((current - previous) / previous) * 100
    if change > 5:
        return f"(↑{abs(change):.0f}%)"
    elif change < -5:
        return f"(↓{abs(change):.0f}%)"
    else:
        return "(stable)"
```

---

## Rules

- **Always run phase detection.** Even for simple status display, use fresh metrics.
- **Transitions require confirmation.** Never auto-transition; always require explicit confirm.
- **Parse JSON carefully.** Handle missing fields gracefully with sensible defaults.
- **Use company_resolver for path detection.** Works in multi-project mode.
- **Visual consistency.** Use the box-drawing characters consistently with other commands.
- **Show actionable next steps.** Always tell the user what they can do next.
- **Handle empty/new state gracefully.** If no phase is set, guide the user to set one.
