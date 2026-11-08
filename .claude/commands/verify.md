# /verify — Verify Work Completeness (from GSD)

Run full verification that all planned work is actually complete and working.

## Input
$ARGUMENTS

## Step 1: Load Plan

Read `.planning/ROADMAP.md` to get all planned tasks.

## Step 2: Check Each Task

For every task in the roadmap:
1. Verify the file exists (for CREATE actions)
2. Verify the change was made (for MODIFY actions)
3. Verify acceptance criteria are met
4. Verify the atomic commit exists: `git log --oneline --grep="<task_id>"`

## Step 3: Run Quality Checks

```bash
# Run linter
npm run lint 2>&1 || python -m ruff check . 2>&1 || true

# Run tests
npm run test 2>&1 || python -m pytest 2>&1 || true

# Run build
npm run build 2>&1 || true
```

## Step 4: Security Quick-Scan

Spawn Security Auditor for a quick scan of changed files:
```
Task(subagent_type="general-purpose", description="Quick security scan of changes")
```

## Step 5: Report

```
## Verification Report

### Task Completion
| Task ID | Name | Status | Commit |
|---------|------|--------|--------|
| 1.1 | ... | DONE | abc123 |
| 1.2 | ... | DONE | def456 |

### Quality
- Lint: PASS/FAIL
- Tests: X/Y passing
- Build: PASS/FAIL

### Security
- Quick scan: PASS/WARNINGS

### Verdict: ALL CLEAR | ISSUES FOUND
[details of any issues]
```
