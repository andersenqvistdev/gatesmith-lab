# /validate — Mark Phase as Validated (WS-013-009)

Validate a completed phase by running actual tests and verifying success criteria.
Prevents false-positive "complete" claims by requiring proof that code works.

## Lifecycle

```
planning → in-progress → complete → validated
```

- `complete`: All tasks done (code written)
- `validated`: Tests pass, coverage acceptable, functionality verified

## Input
$ARGUMENTS

Optional arguments:
- `--phase=<id>` — Phase to validate (default: current phase from STATE.md)
- `--skip-tests` — Skip test execution (just mark as validated)
- `--coverage=<percent>` — Minimum coverage required (default: 40%)

## Step 1: Identify Phase

Read `.planning/STATE.md` to find the current phase.
Read `.planning/ROADMAP.md` to verify the phase status is `complete`.

**If phase is not `complete`:**
```
Phase [ID] is not marked complete. Status: [status]

Run /complete first to mark all tasks as done.
```
Exit without changes.

## Step 2: Run Validation Checks

### 2.1: Run Tests

```bash
uv run pytest -q --tb=short 2>&1
```

Record:
- Total tests run
- Tests passed
- Tests failed
- Test output summary

**If tests fail:**
```
Validation FAILED: [N] tests failing

Failed tests:
- test_xxx.py::test_name — assertion error
- test_yyy.py::test_other — timeout

Fix failing tests before validating phase.
```
Exit without changes (unless `--skip-tests` specified).

### 2.2: Check Coverage

```bash
uv run pytest --cov=. --cov-report=term-missing -q 2>&1
```

Compare coverage against threshold (default 40%).

**If coverage below threshold:**
```
Validation WARNING: Coverage at XX% (threshold: YY%)

Consider running /test-sprint to improve coverage.
Proceed anyway? [y/N]
```

Use AskUserQuestion if coverage is below threshold.

### 2.3: Verify Phase Tasks

Read `.planning/ROADMAP.md` and check:
- All tasks in the phase have `status="complete"`
- No tasks have `status="pending"` or `status="in-progress"`

## Step 3: Update Planning Docs

### 3.1: Update ROADMAP.md

Change phase status from `complete` to `validated`:

```markdown
## Phase P24: Employee Attribution Fix

**Status:** validated ✓
**Validated:** 2026-02-14
**Validation Results:**
- Tests: 116/116 passing
- Coverage: 49%
- Duration: 1 day
```

### 3.2: Update STATE.md Progress Table

Change status from `complete` to `validated`:

```markdown
| Phase | Tasks Total | Tasks Done | Status |
|-------|-------------|------------|--------|
| P24 Employee Attribution Fix | 3 | 3 | validated ✓ |
```

### 3.3: Update Workshop Action Items (if applicable)

If the phase relates to a workshop action item (WS-XXX), mark it complete.

## Step 4: Create Validation Commit

```bash
git add .planning/ROADMAP.md .planning/STATE.md
git commit -m "validate(phase): Mark [phase-id] as validated

Validation results:
- Tests: [passed]/[total] passing
- Coverage: [coverage]%
- All [n] tasks verified complete

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

## Step 5: Report

```
═══════════════════════════════════════════════════════════════
 PHASE VALIDATED                                     [phase-id]
═══════════════════════════════════════════════════════════════

 Validation Checks:
   ✓ All [N] tasks complete
   ✓ Tests: [passed]/[total] passing
   ✓ Coverage: [XX]% (threshold: [YY]%)

 Phase Lifecycle:
   planning → in-progress → complete → validated ✓

 Documentation Updated:
   - .planning/ROADMAP.md: Status → validated
   - .planning/STATE.md: Progress table updated

 Commit: [short-hash]

═══════════════════════════════════════════════════════════════

 Next Steps:
   - /gate — Security checkpoint before push
   - /dashboard — Check company health
```

## Rules

1. **Require `complete` status.** Only phases marked `complete` can be validated.
2. **Run actual tests.** Don't estimate or assume — run pytest.
3. **Report real numbers.** Show actual test counts and coverage.
4. **Allow override.** `--skip-tests` for edge cases (documented why).
5. **Update both files.** ROADMAP.md and STATE.md must be consistent.
6. **Atomic commit.** Validation changes go in one commit.
