# /gate — Human Security Checkpoint

You are implementing a security checkpoint in the current workflow. This command pauses all agent work and presents a structured security review for human approval before continuing.

**Gate Passing:** When the user approves (APPROVE), this unlocks GitHub operations for the session:
- `gh pr create` — Create pull requests
- `gh pr merge` — Merge pull requests
- `git push origin <feature-branch>` — Push to feature branches

Note: Direct push to main/master remains blocked. PRs are the professional path to main.

**Also unlocks:** the same `.claude/gate_passed` file (4-hour TTL) additionally lets
`network_egress_guard.py` allow outbound network requests to non-allowlisted
destinations for the rest of that window — approving this checkpoint for any
reason opens that window, not just the GitHub operations above. See
`SECURITY.md` § Network Egress Guard.

## Input
$ARGUMENTS

## Step 1: Gather Current State

Collect everything that has changed since the last checkpoint (or session start):

```bash
git diff --stat                    # What files changed
git diff                           # The actual changes
git diff --cached --stat           # What's staged
```

Also read the activity log to understand the sequence of operations:
```bash
tail -50 logs/activity.jsonl
```

## Step 2: Security Quick-Scan

Spawn the Security Auditor agent to review the changes:

```
Task(subagent_type="general-purpose", description="Security quick-scan of changes")
```

The Security Auditor receives:
- List of changed files
- Instruction to read `.claude/agents/security-auditor.md`
- Instruction: "Focus on the diff only. This is a quick checkpoint, not a full audit. Flag only CRITICAL and HIGH findings."

## Step 3: Present Checkpoint to User

Format the checkpoint clearly:

```
══════════════════════════════════════════
  SECURITY GATE — Human Checkpoint
══════════════════════════════════════════

Files Changed (since last gate):
  M src/auth/service.ts
  A src/auth/middleware.ts
  M src/routes/api.ts

Quick Security Scan:
  CRITICAL: 0
  HIGH: 0
  [or list any findings]

Operations Performed:
  - 12 file reads
  - 4 file writes
  - 3 bash commands (lint, test, build)
  - 0 git pushes

Actions Available:
  [1] APPROVE — Continue with current changes
  [2] REVIEW  — Show full diff for manual inspection
  [3] AUDIT   — Run full security audit (/security-audit)
  [4] REVERT  — Undo changes since last checkpoint
  [5] STOP    — End session, keep changes unstaged

══════════════════════════════════════════
```

## Step 4: Execute User's Decision

Wait for the user's choice and act accordingly:
- **APPROVE**: Log the approval, set gate-passed state, and continue the workflow
- **REVIEW**: Display the full git diff and wait for follow-up
- **AUDIT**: Run the full security audit agent
- **REVERT**: Run `git checkout -- .` to undo changes (confirm first)
- **STOP**: End gracefully

### On APPROVE: Set Gate-Passed State

When the user approves, create the gate-passed marker:

```bash
mkdir -p .claude && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .claude/gate_passed
```

This unlocks GitHub operations for the next 4 hours:
- PR creation and merging via `gh` CLI
- Pushing to feature branches

**Note:** This does NOT unlock:
- Direct push to main/master (always requires PR)
- Force push operations

Display confirmation:
```
══════════════════════════════════════════
  GATE PASSED ✓
══════════════════════════════════════════

GitHub operations unlocked for this session:
  • gh pr create
  • gh pr merge
  • git push origin <feature-branch>

Direct push to main/master still requires PR.

Continuing workflow...
══════════════════════════════════════════
```

## Rules
- NEVER skip the security quick-scan
- ALWAYS show the operation count from activity logs
- This command should feel like a calm checkpoint, not an alarm — unless there ARE security findings
- Log the gate event and decision to `logs/gates.jsonl`
- On APPROVE, always set the gate-passed state
- Gate-passed expires after 4 hours (session-scoped security)
