# /build — Full Autonomous Pipeline with Atomic Commits (Gatesmith + GSD + BMAD)

You are orchestrating the **complete autonomous build pipeline**. This runs the full cycle: build → verify → gate → complete — stopping only for security issues, failures, or explicit configuration.

Every task produces one atomic commit. State is tracked for pause/resume.

## Input
$ARGUMENTS

If no plan exists, run `/plan` first.

## Step 0: Load State & Config

### 0.1 Load Autonomy Config

Read `.claude/gatesmith.json` to determine autonomy level:

```json
{
  "autonomy": {
    "level": "full|standard|supervised",
    "pausePoints": { ... }
  },
  "gate": {
    "autoApprove": { "enabled": true, "maxCritical": 0, "maxHigh": 0 },
    "alwaysRequireHuman": false
  }
}
```

**Autonomy Levels:**
| Level | Behavior |
|-------|----------|
| `full` | Runs entire pipeline (build→verify→gate→complete) automatically. Pauses only for security findings, failures, or misalignment. |
| `standard` | Runs build→verify automatically. Pauses at gate for human approval. |
| `supervised` | Pauses after each major step for human approval. |

If no config exists, default to `full` autonomy.

### 0.2 Load Plan & State

Read `.planning/ROADMAP.md` for the plan and `.planning/STATE.md` for resume context.

If resuming a previous session, pick up from the last incomplete task.

## Step 0.5: Executive Context Load (NEW)

Before starting implementation, establish organizational context for phase-aware execution.

### 1. Detect Current Phase

```bash
uv run .claude/hooks/company/phase_detector.py detect
```

Parse the JSON output to extract:
- `phase`: Current organizational phase (startup/growth/scale/mature/decline_pivot)
- `confidence`: Detection confidence (0.0-1.0)
- `transition_suggested`: If non-null, a phase transition may be appropriate

### 2. Display Phase Context

Show the phase status at build start:

```
┌─ Executive Context ───────────────────────────────────────┐
│ Phase: [phase] | Confidence: [X]% | Build Starting       │
└───────────────────────────────────────────────────────────┘
```

### 3. Transition Advisory

If `transition_suggested` is not null, display a notification:

```
┌─ Phase Transition Advisory ───────────────────────────────┐
│ Current: [current_phase] → Suggested: [suggested_phase]  │
│ Progress: [progress_percent]%                            │
│ Requirements:                                            │
│   • [requirement 1]                                      │
│   • [requirement 2]                                      │
└───────────────────────────────────────────────────────────┘
```

Store the phase context for use in subsequent steps (CEO validation, CTO review).

## Step 1: Implementation by Waves (Builder Phase)

Execute tasks wave by wave from the ROADMAP.

### For each wave:

**Foreground execution requirement:** Claude Code v2.1.232 (2026-08-13) made
non-teammate `Task()` spawns run in the **background by default** in
interactive sessions (previously synchronous/foreground). Every Task call in
this file is followed by a step that consumes that agent's result immediately
(an atomic commit, a verdict branch, a pass/fail gate) — none of that works if
the call returns before the subagent finishes. Every Task() call below passes
`run_in_background=false` explicitly for this reason; do not drop it when
adding new call sites to this file.

**Parallel tasks** — launch multiple Implementer agents simultaneously, capped
at `gatesmith.json`'s `subagentSpawnBudget.maxParallelPerWave` (default 8):
```
Task(subagent_type="general-purpose", description="Implement task 1.1", run_in_background=false)
Task(subagent_type="general-purpose", description="Implement task 1.2", run_in_background=false)
Task(subagent_type="general-purpose", description="Implement task 1.3", run_in_background=false)
```
Wait for all three to complete before proceeding — `run_in_background=false`
makes each call block, but they still run concurrently with each other.

If a wave has more tasks than `maxParallelPerWave`, launch them in sequential
batches of that size instead of all at once — Claude Code caps concurrently-
running subagents per session (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, default
20), and headroom must be left for other in-flight tool calls.

**Cumulative spawn budget:** Claude Code v2.1.212 added a per-session cap of
200 total Task spawns, and **v2.1.223 (2026-08-06) removed it** — long-running
sessions no longer refuse new agents on that basis. The concurrency limit above
is unaffected and still applies.

`.claude/hooks/subagent_spawn_budget.py` still counts spawns per session, but
only pauses when a cap is actually in effect: it enforces whenever
`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` is set, and otherwise only on a CLI
older than v2.1.223 (or one whose version it cannot determine, which fails
safe). On a current install with the variable unset it never pauses a build —
see "Between waves" below for how to handle the pause if you are on an older
CLI or have set an explicit cap.

Each Implementer receives:
- The specific XML task definition
- Instruction to read `.claude/agents/implementer.md`
- Context about parallel work (to avoid file conflicts)
- The project context from `.planning/PROJECT.md`

**After each task completes:**

1. Create an atomic commit:
```bash
uv run .claude/hooks/atomic_commit.py <phase> <task_id> "<task_name>"
```

2. Update state:
```bash
uv run .claude/hooks/state_tracker.py <phase> <task_id> "<task_name>" "complete" "<next_task>"
```

3. Mark the task as complete in `.planning/ROADMAP.md`

### Between waves:
- Verify all tasks in the wave completed successfully
- Run lint/test to catch issues early
- If any task failed, fix before proceeding to next wave
- **IMPORTANT**: Do NOT ask for user confirmation to continue — proceed to next wave automatically
- Only pause if there's a failure, BLOCK verdict, MISALIGNMENT, or a subagent
  spawn budget exhaustion (see below)

#### Handling subagent spawn budget exhaustion

If a Task call's tool result is a block whose reason mentions "subagent spawn
budget exhausted": this is NOT a failure — it is a designed pause, exactly
like the pause/resume this pipeline already supports. `.planning/STATE.md` is
already current (updated after every completed task), so:
1. Do not retry the blocked Task call.
2. Finish any remaining bookkeeping for the current wave that needs no more
   Task spawns (state update, marking completed tasks in ROADMAP.md).
3. Display a pause notice telling the user the session's subagent-spawn
   budget is exhausted and that they should run `/clear` then re-invoke
   `/build` to resume from `.planning/STATE.md`'s next-task pointer.
4. Stop — do not attempt any further wave or step in this session.

### CEO Validation (After Each Wave)

After each wave completes, perform CEO-level validation. This is **logged, not spawned** — the coordinator logic runs inline.

#### 1. Log Wave Completion

```bash
# Log the wave completion to activity log
uv run .claude/hooks/log_activity.py wave_complete --wave [N] --tasks [completed_count] --phase [current_phase]
```

#### 2. Validate Phase Alignment

Check that the completed work aligns with phase priorities:

| Phase | Expected Focus | Misalignment Indicators |
|-------|---------------|------------------------|
| startup | Core functionality, MVP | Non-essential features, over-engineering |
| growth | Feature expansion, scaling | Technical debt without business value |
| scale | Reliability, performance | Shortcuts that compromise quality |
| mature | Stability, optimization | Risky changes without clear ROI |
| decline_pivot | Pivot execution, survival | Work not supporting pivot goals |

#### 3. Display CEO Validation Status

```
┌─ CEO Validation ──────────────────────────────────────────┐
│ Phase: [phase] | Wave [N] complete | Alignment: [OK/WARN]│
└───────────────────────────────────────────────────────────┘
```

#### 4. Handle Misalignment

If work does not align with phase priorities:

```
┌─ CEO Validation ──────────────────────────────────────────┐
│ Phase: [phase] | Wave [N] complete | Alignment: MISALIGN │
│ ⚠ PAUSED: Work may not align with phase priorities       │
│ Reason: [specific misalignment reason]                   │
│ Action: Notify user for decision                         │
└───────────────────────────────────────────────────────────┘
```

**On misalignment:**
- Pause the build pipeline
- Notify the user with the misalignment details
- Wait for user decision before proceeding to next wave

### Visual Progress (display after each task and between waves)

Show progress using this format:

```
═══════════════════════════════════════════════════════════════
 WAVE 1                                              [3/4 tasks]
═══════════════════════════════════════════════════════════════
 ✓ 1.1  Setup auth module                              complete
 ✓ 1.2  Add middleware layer                           complete
 ⏳ 1.3  Implement route handlers                     in progress
 ○ 1.4  Add error boundaries                            pending
───────────────────────────────────────────────────────────────
 Progress: ████████████░░░░ 75%
═══════════════════════════════════════════════════════════════
```

Status icons:
- `✓` = complete (green)
- `⏳` = in progress
- `○` = pending
- `✗` = failed (red)

Update and display this progress block:
1. Before starting each wave
2. After each task completes
3. After each wave completes (with wave summary)

## Step 1.5: CTO Technical Validation (NEW)

Before code review, spawn the CTO agent for technical oversight.

### 1. Gather Inputs for CTO

Collect the required context:

```bash
# Get git diff of all changes in this build
git diff --stat HEAD~[N]..HEAD  # Where N = number of commits in this build

# Get current phase and metrics
uv run .claude/hooks/company/phase_detector.py detect
```

### 2. Spawn CTO Agent

```
Task(subagent_type="general-purpose", description="CTO Technical Review", run_in_background=false)
```

Pass to the CTO agent:
- **Git diff:** Full diff of changes since build started (`git diff HEAD~[N]..HEAD`)
- **Current phase:** From phase_detector.py output (startup/growth/scale/mature/decline_pivot)
- **Phase metrics:** Key metrics from detection (velocity, blocked_ratio, test_coverage)
- **Task context:** Task IDs and descriptions from ROADMAP.md that were implemented
- **Instruction:** Read `.claude/agents/company/cto.md` for review protocol

`run_in_background=false` makes this call block, so the verdict below is
available immediately — this section does not describe an async poll loop.

### 3. Display CTO Review Status

The review runs synchronously; display this while it completes:

```
┌─ CTO Technical Review ────────────────────────────────────┐
│ Phase: [phase] | Files: [N] changed | Status: IN REVIEW  │
└───────────────────────────────────────────────────────────┘
```

### 4. Handle CTO Verdict

The CTO returns one of three verdicts:

#### APPROVED
```
┌─ CTO Technical Review ────────────────────────────────────┐
│ Phase: [phase] | Files: [N] changed | Verdict: APPROVED  │
│ ✓ Technical standards met. Proceeding to Code Review.    │
└───────────────────────────────────────────────────────────┘
```
**Action:** Continue to Step 2 (Code Review).

#### CONCERNS
```
┌─ CTO Technical Review ────────────────────────────────────┐
│ Phase: [phase] | Files: [N] changed | Verdict: CONCERNS  │
│ ⚠ Caution flags set for Reviewer:                        │
│   • [concern 1]                                          │
│   • [concern 2]                                          │
└───────────────────────────────────────────────────────────┘
```
**Action:** Continue to Step 2, but pass the concern flags to the Reviewer agent. These are not blockers but require extra attention during code review.

#### BLOCK
```
┌─ CTO Technical Review ────────────────────────────────────┐
│ Phase: [phase] | Files: [N] changed | Verdict: BLOCK     │
│ ✗ BLOCKED: Technical review failed                       │
│ Reason: [blocking reason from CTO]                       │
│ Required: [what must change before approval]             │
│ Action: Build paused. User decision required.            │
└───────────────────────────────────────────────────────────┘
```
**Action:**
- Pause the build pipeline immediately
- Display the blocking reason and required resolution
- Escalate to user for decision
- Do NOT proceed to Code Review until user approves or CTO re-reviews

### 5. Log CTO Review

```bash
uv run .claude/hooks/log_activity.py cto_review --verdict [APPROVED|CONCERNS|BLOCK] --phase [phase] --files [N]
```

## Step 2: Code Review (Validator Phase)

Spawn the Reviewer agent:
```
Task(subagent_type="general-purpose", description="Review all changes", run_in_background=false)
```

Pass it:
- `git diff` of all changes since build started
- The original plan from `.planning/ROADMAP.md`
- Instruction to read `.claude/agents/reviewer.md`

### Review outcomes:
- **PASS** → Step 3
- **NEEDS CHANGES** → spawn Implementer with fixes, atomic commit, re-review (max 3 cycles)
- **BLOCK** → escalate to user

## Step 3: Testing (Verification Phase)

Spawn the Tester agent:
```
Task(subagent_type="general-purpose", description="Write and run tests", run_in_background=false)
```

Atomic commit for test files:
```bash
uv run .claude/hooks/atomic_commit.py <phase> "test" "add tests for phase <N>"
```

## Step 4: Final Validation

Run the full quality suite:
1. Linter
2. Tests
3. Build (if applicable)

## Step 5: Update Planning State

Update `.planning/STATE.md` with completion status.
Update `.planning/ROADMAP.md` marking phase complete.

## Step 6: Build Summary (Checkpoint)

Display build completion status:

```
═══════════════════════════════════════════════════════════════
 BUILD COMPLETE                                  [X/X tasks]
═══════════════════════════════════════════════════════════════
 Wave 1: ████████████████ 100%  [4/4 complete]
 Wave 2: ████████████████ 100%  [2/2 complete]
 Wave 3: ████████████████ 100%  [1/1 complete]
───────────────────────────────────────────────────────────────
 Total:  ████████████████ 100%  ALL TASKS COMPLETE
═══════════════════════════════════════════════════════════════
 → Proceeding to autonomous verification...
```

**DO NOT STOP HERE** — continue immediately to Step 7.

## Step 7: Auto-Verify (Autonomous)

Run verification inline without spawning the /verify command. Execute these checks:

### 7.1 Task Completion Check

For every task in ROADMAP.md:
1. Verify the atomic commit exists: `git log --oneline --grep="<task_id>"`
2. Verify file changes were made

### 7.2 Quality Checks

```bash
# Run linter
npm run lint 2>&1 || python -m ruff check . 2>&1 || echo "No linter configured"

# Run tests
npm run test 2>&1 || python -m pytest 2>&1 || echo "No tests configured"

# Run build
npm run build 2>&1 || echo "No build configured"
```

### 7.3 Display Verification Status

```
┌─ Auto-Verify ─────────────────────────────────────────────────┐
│ Tasks: [X/X verified] | Commits: [Y found]                    │
│ Lint: [PASS/FAIL] | Tests: [X/Y] | Build: [PASS/FAIL/SKIP]    │
│ Status: VERIFIED / ISSUES FOUND                               │
└───────────────────────────────────────────────────────────────┘
```

**If ISSUES FOUND:** Pause and report issues. Wait for user decision.
**If VERIFIED:** Continue immediately to Step 8.

## Step 8: Auto-Gate (Conditional Autonomous)

Run security gate inline. Behavior depends on `gatesmith.json`:

- If `gate.alwaysRequireHuman: true` → Always pause for human approval
- If `gate.autoApprove.enabled: true` → Auto-approve if findings below thresholds
- If `autonomy.level: supervised` → Always pause for human approval

### 8.1 Security Quick-Scan

Spawn Security Auditor for changed files:
```
Task(subagent_type="general-purpose", description="Quick security scan of build changes", run_in_background=false)
```

Pass it:
- `git diff HEAD~[N]..HEAD` (all changes in this build)
- Instruction: "Quick checkpoint scan. Flag only CRITICAL and HIGH findings."

### 8.2 Evaluate Gate Decision

**If 0 CRITICAL and 0 HIGH findings:**

```
┌─ Auto-Gate ───────────────────────────────────────────────────┐
│ Security Scan: CLEAN                                          │
│ Critical: 0 | High: 0 | Medium: [N] | Low: [N]                │
│ Decision: AUTO-APPROVED (no blocking issues)                  │
└───────────────────────────────────────────────────────────────┘
```

Log the auto-approval and continue to Step 9.

**If CRITICAL or HIGH findings exist:**

```
┌─ Auto-Gate ───────────────────────────────────────────────────┐
│ Security Scan: ISSUES FOUND                                   │
│ Critical: [N] | High: [N]                                     │
│ ⚠ PAUSED: Human approval required                             │
│ [List of findings]                                            │
│                                                               │
│ Actions:                                                      │
│   [1] APPROVE — Continue despite findings                     │
│   [2] FIX     — Address security issues first                 │
│   [3] STOP    — End build pipeline                            │
└───────────────────────────────────────────────────────────────┘
```

Wait for user decision before proceeding.

### 8.3 Log Gate Event

```bash
# Log to gates.jsonl
echo '{"timestamp":"[ISO]","phase":[N],"findings":{"critical":[N],"high":[N]},"decision":"auto-approved|user-approved|blocked"}' >> logs/gates.jsonl
```

## Step 9: Auto-Complete (Autonomous)

Run completion inline without spawning the /complete command.

### 9.1 Update Planning Docs

Update `.planning/ROADMAP.md`:
- Mark current phase as **COMPLETE**
- Set status date

Update `.planning/STATE.md`:
- Update progress table
- Set next phase as current
- Record completion timestamp

### 9.2 Summary Commit

```bash
uv run .claude/hooks/atomic_commit.py <phase> "milestone" "complete phase <N>"
```

### 9.3 Display Final Report

```
## Phase [N] Complete — Full Autonomous Pipeline

═══════════════════════════════════════════════════════════════
 FINAL STATUS                                    [X/X tasks]
═══════════════════════════════════════════════════════════════
 Wave 1: ████████████████ 100%  [4/4 complete]
 Wave 2: ████████████████ 100%  [2/2 complete]
 Wave 3: ████████████████ 100%  [1/1 complete]
───────────────────────────────────────────────────────────────
 Total:  ████████████████ 100%  ALL TASKS COMPLETE
═══════════════════════════════════════════════════════════════

### Pipeline Results
| Phase | Status | Details |
|-------|--------|---------|
| Plan | DONE | X tasks in Y waves |
| Build | DONE | X files changed, Y commits |
| CTO Review | PASS | [verdict] |
| Code Review | PASS | Round N |
| Test | PASS | X/Y tests passing |
| Verify | PASS | All tasks confirmed |
| Gate | AUTO-APPROVED | 0 critical, 0 high |
| Complete | DONE | Docs updated |

### Atomic Commits
| Commit | Task | Files |
|--------|------|-------|
| feat(phase-1): ... | 1.1 | 3 files |
| feat(phase-1): ... | 1.2 | 2 files |
| chore(phase-1): milestone | complete | - |

### Files Changed
[table]

### Next Phase
[What's coming next, or "Project complete" if all phases done]

### Ready for Push
Run `git push` when ready to publish changes.
```

## Step 9.4: Auto-Restart Daemon (if needed)

After build completion, check if daemon-related files changed and restart automatically:

```bash
uv run .claude/hooks/daemon_auto_restart.py --commits [N]
```

Where N = total commits in this build phase.

**If action = "restarted":**
```
┌─ Daemon Auto-Restart ─────────────────────────────────────────┐
│ Daemon-critical files changed: [list]                          │
│ Stopped PID [N] — launchd KeepAlive will restart with new code │
└───────────────────────────────────────────────────────────────┘
```

**If action = "skip":** No display needed, continue silently.

**If action = "error":** Display warning but do NOT block the pipeline.

## Step 9.5: Post-PR Merge Prompt (Optional)

If a PR was created during this build and `flowMode.autoMergeAfterApproval` is false, offer a quick merge option:

```
┌─ PR Created ──────────────────────────────────────────────────┐
│ PR #[N]: [title]                                              │
│ URL: [github URL]                                             │
├───────────────────────────────────────────────────────────────┤
│ Quick Actions:                                                │
│   "merge" — Merge PR and sync local main                      │
│   "later" — Leave PR open, continue working                   │
└───────────────────────────────────────────────────────────────┘
```

**If user replies "merge":**
1. Run: `gh pr merge [N] --squash --delete-branch`
2. Run: `git checkout main && git pull`
3. Confirm: "Merged and synced. Main is at [commit]."

**If user replies "later" or no response:**
- Continue without merging
- PR remains open for manual review

**If `flowMode.autoMergeAfterApproval` is true:**
- Skip the prompt entirely
- Auto-merge after successful gate (if PR has approvals)
- Log the auto-merge to audit trail

This step reduces the manual workflow of: push → PR → wait → merge → sync → delete branch.

## Rules
- ONE task = ONE commit. Never batch multiple tasks into one commit.
- ALWAYS update state after each task (enables pause/resume).
- Use parallel Task spawning for independent tasks within a wave.
- If any phase fails, fix before moving on.
- **FULL AUTONOMOUS PIPELINE**: The build command runs the COMPLETE pipeline (build → verify → gate → complete) without stopping unless:
  - BLOCK verdict from CTO technical review
  - MISALIGNMENT detected by CEO validation
  - Task failure requiring user decision
  - CRITICAL or HIGH security findings in gate
  - Verification failures
  - Subagent spawn budget exhausted (see "Handling subagent spawn budget
    exhaustion" in Step 1) — pause cleanly and tell the user to `/clear` then
    resume, same as any other designed pause point
- Do NOT ask "Would you like me to continue?" between steps — just proceed.
- Do NOT suggest running /verify, /gate, or /complete separately — run them inline.
- Display progress updates between steps, but continue executing immediately.
- The only human touchpoints are: security findings, build failures, and phase misalignment.
