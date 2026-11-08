# /test-sprint — Focused Test Coverage Sprint

You are running a focused test coverage sprint. This command identifies coverage gaps and systematically generates tests to improve coverage.

## Input
$ARGUMENTS

Supported arguments:
- `<path>` — Target specific file or directory (e.g., `.claude/hooks/`)
- `--target=<percent>` — Aim for specific coverage percentage (e.g., `--target=60%`)
- No arguments — Auto-detect files with lowest coverage

## Step 0: Load Context & Current Coverage

Read `.planning/PROJECT.md` and `CLAUDE.md` for project context.

Run coverage report:
```bash
uv run pytest --cov=. --cov-report=term-missing -q 2>&1 | tail -50
```

If pytest-cov not available, estimate coverage from test file existence.

## Step 1: Identify Coverage Gaps

Analyze coverage report to find:
1. Files with 0% coverage (no tests)
2. Files with <50% coverage (partial tests)
3. Core files (hooks, commands) prioritized over utilities

Create priority list:
| Priority | File | Current Coverage | Target |
|----------|------|-----------------|--------|
| P1 | [core file] | 0% | 50% |
| P2 | [important file] | 25% | 50% |

## Step 2: Parse Arguments

If `$ARGUMENTS` contains a path:
- Filter priority list to that path only

If `$ARGUMENTS` contains `--target=X%`:
- Set target coverage to X% instead of 50%

If no arguments:
- Use auto-detected priority list
- Default target: 50%

## Step 3: Generate Tests Wave by Wave

Group files into waves (max 3 files per wave to avoid conflicts).

For each wave, spawn test writers in parallel:
```
Task(subagent_type="general-purpose", description="Write tests for [file]")
```

Pass each agent:
- The source file to test
- Existing test patterns from `tests/` directory
- Coverage target
- Instruction: Focus on public functions, edge cases, error paths

## Step 4: Run Tests & Verify Coverage

After each wave:
```bash
uv run pytest tests/ -q
uv run pytest --cov=. --cov-report=term-missing -q 2>&1 | tail -30
```

If tests fail, fix before proceeding.

## Step 5: Atomic Commits

After each test file:
```bash
uv run .claude/hooks/atomic_commit.py test-sprint <id> "add tests for <file>"
```

## Step 6: Coverage Report

Display final coverage delta:

```
═══════════════════════════════════════════════════════════════
 TEST SPRINT COMPLETE
═══════════════════════════════════════════════════════════════
 Coverage Before: [X]%
 Coverage After:  [Y]%
 Delta:          +[Z]%
───────────────────────────────────────────────────────────────
 Files Tested: [N]
 Tests Added:  [M]
 Tests Passing: [P]/[T]
═══════════════════════════════════════════════════════════════

### Coverage by File
| File | Before | After | Delta |
|------|--------|-------|-------|
| file1.py | 0% | 65% | +65% |
| file2.py | 30% | 55% | +25% |

### Next Steps
- Run `/test-sprint` again to continue improving coverage
- Current coverage: [Y]% (Q1 G1 target: 50%)
```

## Rules

1. **Focus on test files only** — do not modify source code
2. **Atomic commits** — one test file per commit
3. **Run tests frequently** — verify each wave before proceeding
4. **Report coverage delta** — always show before/after
5. **Respect existing patterns** — follow test conventions in the codebase
