# /health — Planning Directory Integrity Check (from GSD v1.22)

Validate the `.planning/` directory structure, detect corruption or drift, and optionally repair issues.

## Input
$ARGUMENTS

## Step 1: Parse Arguments

| Flag | Effect |
|------|--------|
| (none) | Check only — report issues without fixing |
| `--repair` | Attempt automatic repairs for fixable issues |
| `--verbose` | Show detailed check results including passing checks |

## Step 2: Run Integrity Checks

Perform all checks and collect results into a report.

### 2.1: Directory Structure

Check that `.planning/` exists and contains expected files:

| File | Required | Purpose |
|------|----------|---------|
| `PROJECT.md` | Yes | Tech stack, architecture, conventions |
| `REQUIREMENTS.md` | Yes | Goals, requirements, preferences |
| `ROADMAP.md` | Yes | Phases, XML tasks, dependency waves |
| `STATE.md` | Yes | Session state for pause/resume |
| `DISCUSS.md` | No | Discussion log (created by /discuss) |
| `quick/` | No | Quick task logs (created by /quick) |

**Check**: File exists and is non-empty (>10 bytes).
**Repair**: Create missing required files with minimal template content.

### 2.2: ROADMAP.md Task Format

Validate XML task blocks in ROADMAP.md:

- Each `<task>` has required attributes: `id`, `status`
- Each `<task>` has required children: `<name>`, `<description>`
- Task IDs follow `N.N` format (phase.task)
- Status values are valid: `pending`, `in-progress`, `complete`, `blocked`, `skipped`
- No duplicate task IDs

**Check**: Parse XML blocks and validate structure.
**Repair**: Flag malformed tasks but don't auto-fix (too risky).

### 2.3: STATE.md Consistency

Validate state tracking:

- STATE.md has a valid progress table
- Current phase in STATE.md matches ROADMAP.md active phase
- No "in-progress" phases that are actually complete in ROADMAP.md

**Check**: Cross-reference STATE.md with ROADMAP.md.
**Repair**: Update STATE.md to match ROADMAP.md reality.

### 2.4: Orphaned References

Check for broken references:

- Files referenced in ROADMAP.md tasks (`<file>` tags) — do they exist?
- Phases referenced in STATE.md — do they exist in ROADMAP.md?

**Check**: Validate file paths and phase references.
**Repair**: Flag orphans but don't auto-fix.

### 2.5: Git State

Check planning files are tracked:

```bash
git status .planning/
```

- Are there untracked planning files?
- Are there uncommitted changes to planning files?
- Is the planning directory in `.gitignore` (it shouldn't be)?

**Check**: Report git state of planning files.
**Repair**: Suggest `git add .planning/` for untracked files.

### 2.6: Config Consistency

If `.claude/gatesmith.json` exists:

- Check that planning-related config values are valid
- Verify complexity thresholds are in valid range
- Check model profiles reference valid models

**Check**: Validate config schema.
**Repair**: Reset invalid values to defaults.

### 2.7: Quick Task Logs

If `.planning/quick/` exists:

- Check log files follow `YYMMDD.md` naming
- Validate task IDs are sequential within each day
- Check for incomplete tasks (status != complete/failed)

**Check**: Validate quick task log format.
**Repair**: Flag issues only.

## Step 3: Generate Report

### Healthy Output

```
═══════════════════════════════════════════════════════════════
 PLANNING HEALTH CHECK                              [HEALTHY]
═══════════════════════════════════════════════════════════════

 Structure:     OK  (6/6 files present)
 ROADMAP:       OK  (N tasks, N phases, valid XML)
 STATE:         OK  (consistent with ROADMAP)
 References:    OK  (no orphaned refs)
 Git:           OK  (all tracked, no uncommitted)
 Config:        OK  (valid schema)
 Quick logs:    OK  (N tasks logged)

 All checks passed.
═══════════════════════════════════════════════════════════════
```

### Issues Found Output

```
═══════════════════════════════════════════════════════════════
 PLANNING HEALTH CHECK                             [N ISSUES]
═══════════════════════════════════════════════════════════════

 Structure:     WARN  Missing DISCUSS.md (optional)
 ROADMAP:       OK    (N tasks, N phases, valid XML)
 STATE:         FAIL  Phase mismatch (STATE says P5, ROADMAP active is P7)
 References:    WARN  2 file references point to missing files
 Git:           OK    (all tracked)
 Config:        OK    (valid schema)

 ─────────────────────────────────────────────────────────────
 ISSUES (N)
 ─────────────────────────────────────────────────────────────

 [FAIL] STATE.md phase mismatch
        STATE.md says current phase is P5
        ROADMAP.md active phase is P7
        Fix: Update STATE.md current phase to P7
        Auto-repair: Yes (use --repair)

 [WARN] Missing file references in ROADMAP.md
        Task 7.2 references src/old/handler.ts (not found)
        Task 7.3 references lib/deprecated.py (not found)
        Auto-repair: No (manual review needed)

═══════════════════════════════════════════════════════════════

 Run with --repair to fix auto-repairable issues.
═══════════════════════════════════════════════════════════════
```

### Repair Output

When `--repair` is used:

```
═══════════════════════════════════════════════════════════════
 PLANNING HEALTH CHECK                             [REPAIRED]
═══════════════════════════════════════════════════════════════

 Repairs applied:
   [FIXED] STATE.md current phase updated to P7
   [FIXED] Created missing REQUIREMENTS.md from template
   [SKIP]  Missing file references (manual review needed)

 Remaining issues: 1 (manual fix required)
═══════════════════════════════════════════════════════════════
```

## Rules

1. **Never auto-fix ROADMAP.md tasks.** Task definitions are too critical for automated repair.
2. **Always show a summary.** Even `--verbose` mode starts with the compact summary.
3. **Repair is conservative.** Only fix issues where the correct action is unambiguous.
4. **Don't create planning files from scratch if none exist.** Suggest `/new-project` instead.
5. **Cross-reference is key.** The most valuable checks are consistency between STATE.md and ROADMAP.md.
6. **Git awareness.** Always check if planning changes are committed.
