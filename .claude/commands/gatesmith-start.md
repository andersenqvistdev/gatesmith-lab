# /gatesmith-start — Quick Start Onboarding

Interactive onboarding for new Gatesmith users. Guides through initial setup and suggests first tasks based on project state.

## Input

$ARGUMENTS

Optional arguments:
- `--skip-check` — Skip health checks
- `--minimal` — Show only essential commands

## Step 1: Detect Project State

Check what exists:

```bash
# Check for company structure
ls -la .company/ 2>/dev/null

# Check for planning docs
ls -la .planning/ 2>/dev/null

# Count the workers: the entries in org.json's "employees" list (the installer writes one)
python3 -c "import json; print(len(json.load(open('.company/org.json')).get('employees', [])))" 2>/dev/null || echo 0

# Check daemon status
cat .company/daemon.heartbeat 2>/dev/null | head -1
```

Workers are the entries in `.company/org.json` → `employees`. Do not count files in
`.company/employees/`: that folder is empty on a single-project install even though the
installer has set up a worker.

Determine state:
- **NEW**: No `.company/` directory
- **INITIALIZED**: `.company/` exists but `org.json` lists no employees
- **READY**: `org.json` lists at least one employee, daemon not running
- **RUNNING**: Daemon is active

## Step 2: Display Welcome

```
═══════════════════════════════════════════════════════════════════════════════
 GATESMITH — Structured Autonomy                                    [Quick Start]
═══════════════════════════════════════════════════════════════════════════════

 Welcome to Gatesmith — AI-powered software development with safety controls.

 Project State: [NEW | INITIALIZED | READY | RUNNING]

═══════════════════════════════════════════════════════════════════════════════
```

## Step 3: Next Steps

Every state gets the same core next steps. A fresh install has a `.company/`
directory, so do not send the reader to company setup.

```
 NEXT STEPS — Start Working

 1. Load context for this project:
    /prime

 2. Plan a change:
    /plan "describe what you want built"

 3. Build it, then review it:
    /build
    /review

 For a small, well-defined change, skip planning:
    /quick "Fix null pointer in user.getProfile()"

 ──────────────────────────────────────────────────────────────────────────────
 QUICK REFERENCE

 | Command | Purpose |
 |---------|---------|
 | /plan | Create an implementation plan |
 | /build | Execute the plan with atomic commits |
 | /review | Code review |
 | /verify | Check work completeness |
 | /continue | Resume where you left off |

 More commands are installed; they are unsupported and listed in EXPERIMENTAL.md.

═══════════════════════════════════════════════════════════════════════════════
```

### If READY or RUNNING (the daemon is set up)

Add the queue and daemon commands to the list above:

```
 | /submit "..." | Queue a task for the worker |
 | /run-loop | Run one cycle by hand: the worker takes a task and opens a PR |
 | /daemon status | Check the daemon |
 | /pending | Items needing your attention |
 | /respond | Handle escalations |
```

If READY and the daemon is not running, `/daemon start` begins autonomous work.

## Step 4: Health Check (unless --skip-check)

Run a quick health check:

```bash
./bin/gatesmith-doctor
```

Show each `FAIL` and `WARN` line with its `Remedy:` line, as printed. A WARN
on a fresh install (no goals yet, workspace trust not accepted) is expected.
For a blocked task point to `/pending`, for a stale heartbeat to `/daemon status`.

## Step 5: Suggest First Action

Suggest ONE clear next action:

| State | Suggestion |
|-------|------------|
| NEW, INITIALIZED | `Run /plan "your first change"` |
| READY | `Run /daemon start to begin autonomous work` |
| RUNNING | `Run /pending to see what needs you` |

## Rules

- **Be concise** — New users are overwhelmed by 80 commands
- **Core commands only** — First-run output names only commands in export/manifest.toml `[core]`; everything else goes behind one line pointing to EXPERIMENTAL.md
- **One clear action** — Always end with a single suggested next step
- **Check health** — Warn about obvious issues unless skipped
- **Link to docs** — Reference `docs/` for deeper learning
