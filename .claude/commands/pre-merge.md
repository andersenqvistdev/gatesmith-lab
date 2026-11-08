# /pre-merge — Validate Before Push

Run all CI checks locally before pushing to ensure your PR will pass on the first try. This eliminates the "push → wait for CI → fix → push again" loop.

## Usage

```bash
/pre-merge                # Run all checks
/pre-merge --fix          # Auto-fix issues where possible
/pre-merge --quick        # Skip slow tests, just lint + format
```

## Instructions

<command name="pre-merge">
Execute a comprehensive pre-push validation matching GitHub CI configuration.

**Phase 1: Format Check (Ruff)**
```bash
uv tool run ruff format --check .
```
If `--fix` flag: run `uv tool run ruff format .` to auto-fix.

Exit codes:
- 0 = All files formatted correctly
- 1+ = Files need formatting (show file list)

**Phase 2: Lint Check (Ruff)**
```bash
uv tool run ruff check .
```
If `--fix` flag: run `uv tool run ruff check --fix .` to auto-fix.

Exit codes:
- 0 = No lint issues
- 1+ = Lint errors found (show summary)

**Phase 3: Hook Syntax Validation**
```bash
# Verify all hook files are valid Python
for f in .claude/hooks/*.py .claude/hooks/**/*.py; do
  python3 -m py_compile "$f" 2>&1 || echo "INVALID: $f"
done
```

**Phase 4: Tests (skip if --quick)**
```bash
uv run pytest tests/ .claude/tests/ -v --tb=short \
  --cov=.claude/hooks --cov=tests \
  --cov-report=term-missing \
  --cov-fail-under=35
```

Match the exact CI configuration:
- Coverage threshold: 35% (same as CI)
- Directories: tests/ and .claude/tests/

**Output Format:**

```
═══════════════════════════════════════════════════════════════
 PRE-MERGE VALIDATION
═══════════════════════════════════════════════════════════════

 [1/4] Format Check
   ✓ 664 files formatted correctly

 [2/4] Lint Check
   ✓ All checks passed

 [3/4] Hook Validation
   ✓ 34 hooks validated

 [4/4] Tests (35% coverage threshold)
   ✓ 11,499 tests passed
   ✓ Coverage: 42%

═══════════════════════════════════════════════════════════════
 RESULT: READY TO PUSH
═══════════════════════════════════════════════════════════════

All CI checks will pass. Safe to push:
  git push origin <branch>
```

**On Failure:**

```
═══════════════════════════════════════════════════════════════
 PRE-MERGE VALIDATION
═══════════════════════════════════════════════════════════════

 [1/4] Format Check
   ✗ 2 files need formatting:
     - .claude/hooks/company/forge_daemon.py
     - .claude/hooks/company/result_viewer.py

 [2/4] Lint Check
   ✗ 3 issues found:
     F541: f-string without placeholders (result_viewer.py:232)
     I001: Import unsorted (tests/test_result_viewer.py:17)
     ...

 [3/4] Hook Validation
   ✓ 34 hooks validated

 [4/4] Tests
   SKIPPED (fix format/lint first)

═══════════════════════════════════════════════════════════════
 RESULT: NOT READY
═══════════════════════════════════════════════════════════════

Fix issues:
  /pre-merge --fix    # Auto-fix format and lint

Or manually:
  uv tool run ruff format .
  uv tool run ruff check --fix .
```

**Quick Mode (--quick):**

Skip tests for fast iteration during development:
```
 [1/3] Format Check  ✓
 [2/3] Lint Check    ✓
 [3/3] Hook Check    ✓

 RESULT: READY (quick mode - tests skipped)
```

</command>

## Integration with Daemon

When the daemon creates a PR, it should run `/pre-merge` before pushing. This ensures daemon-created PRs don't fail CI.

Add to `forge_daemon.py` task execution:
```python
# Before git push in PR workflow
if not run_pre_merge_checks():
    escalate("PR would fail CI - needs manual review")
```

## CI Parity

This skill mirrors `.github/workflows/ci.yml`:

| CI Job | Pre-merge Check |
|--------|-----------------|
| lint/ruff check | Phase 2 |
| lint/ruff format --check | Phase 1 |
| hooks-validate | Phase 3 |
| test/pytest | Phase 4 |
| security | Not included (requires GitHub secrets) |

## Common Issues

| Issue | Fix |
|-------|-----|
| Format differences | `ruff format .` |
| Unused imports | `ruff check --fix .` |
| Test import errors | Check sys.path in test files |
| Coverage below 35% | Add more tests or lower threshold |
