# /security-status -- Three-Layer Security Health Check

Show the health of all three security defense layers (ADR-0002) at a glance.

## Input
$ARGUMENTS

## Instructions

<command name="security-status">
Execute the /security-status command to check all three security defense layers.

Run these checks in sequence, collecting results for each layer:

**Layer 1: Local Hook Protection (git_guardian.py)**

1. Check that git_guardian.py is registered as a PreToolUse hook in `.claude/settings.json`:
   ```bash
   grep -c "git_guardian" .claude/settings.json
   ```
   PASS if count > 0.

2. Verify the hook script exists:
   ```bash
   test -f .claude/hooks/git_guardian.py && echo "EXISTS" || echo "MISSING"
   ```

3. Verify it compiles (no syntax errors):
   ```bash
   python3 -m py_compile .claude/hooks/git_guardian.py 2>&1 && echo "VALID" || echo "INVALID"
   ```

**Layer 2: GitHub Branch Protection**

4. Check branch protection rules via GitHub API:
   ```bash
   uv run .claude/hooks/company/branch_protection_check.py 2>/dev/null || \
   python3 -c "
import subprocess, json
repo = subprocess.run(['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'], capture_output=True, text=True).stdout.strip()
result = subprocess.run(['gh', 'api', f'repos/{repo}/branches/main/protection'], capture_output=True, text=True)
if result.returncode != 0:
    print('STATUS: MISSING')
    print('No branch protection rules found')
else:
    data = json.loads(result.stdout)
    checks = data.get('required_status_checks', {})
    contexts = set(checks.get('contexts', []))
    for c in checks.get('checks', []):
        contexts.add(c.get('context', '') if isinstance(c, dict) else str(c))
    expected = ['Lint', 'Security', 'Hooks Validate']
    present = [c for c in expected if c in contexts]
    missing = [c for c in expected if c not in contexts]
    force_push = data.get('allow_force_pushes', {}).get('enabled', True)
    deletions = data.get('allow_deletions', {}).get('enabled', True)
    if not missing and not force_push and not deletions:
        print('STATUS: OK')
    else:
        print('STATUS: DEGRADED')
    print(f'Checks present: {present}')
    print(f'Checks missing: {missing}')
    print(f'Force push blocked: {not force_push}')
    print(f'Deletions blocked: {not deletions}')
"
   ```

5. Verify each required check is present:
   - Lint
   - Security
   - Hooks Validate

6. Verify force pushes are blocked.
7. Verify branch deletion is blocked.

**Layer 3: Auto-Merge Pipeline**

8. Check CI workflow has auto-merge job:
   ```bash
   grep -c "auto-merge" .github/workflows/ci.yml 2>/dev/null || echo "0"
   ```
   PASS if count > 0.

9. Check for open daemon PRs (potential stuck PRs):
   ```bash
   gh pr list --state open --json number,title,headRefName,createdAt 2>/dev/null
   ```
   Filter for branches starting with `daemon/`. Flag any open > 24 hours as stuck.

10. Check recent merged daemon PRs:
    ```bash
    gh pr list --state merged --limit 10 --json number,title,headRefName,mergedAt 2>/dev/null
    ```
    Filter for daemon branches. Show last 5 merges.

11. Check daemon auto-merge function exists in forge_daemon.py:
    ```bash
    grep -c "_run_auto_merge" .claude/hooks/company/forge_daemon.py
    ```

**Render the results as a status dashboard:**

```
===============================================================
 GATESMITH SECURITY STATUS                         [YYYY-MM-DD HH:MM]
 Defense-in-Depth: ADR-0002 Three-Layer Architecture
===============================================================

 LAYER 1: LOCAL HOOKS                                    [PASS]
   git_guardian.py registered in settings.json     [Y]
   git_guardian.py exists and valid Python          [Y]
   Blocks push to main (regex-enforced)            [Y]

 LAYER 2: BRANCH PROTECTION                             [PASS]
   Required check: Lint                            [Y]
   Required check: Security                        [Y]
   Required check: Hooks Validate                  [Y]
   Force pushes blocked                            [Y]
   Branch deletion blocked                         [Y]

 LAYER 3: AUTO-MERGE PIPELINE                           [PASS]
   CI auto-merge job exists                        [Y]
   Daemon _run_auto_merge() active                 [Y]
   Open daemon PRs: 0 (0 stuck)
   Recent merges: 3 in last 7 days

 OVERALL: 3/3 LAYERS HEALTHY
 Status: DEFENSE-IN-DEPTH OPERATIONAL
===============================================================
```

If any layer has failures, show:
```
 LAYER 2: BRANCH PROTECTION                             [FAIL]
   Required check: Lint                            [Y]
   Required check: Security                        [N] <-- MISSING
   Required check: Hooks Validate                  [Y]
   Force pushes blocked                            [N] <-- RISK
   Branch deletion blocked                         [Y]

   ACTION REQUIRED: Run the following to restore:
   gh api -X PUT repos/{owner}/{repo}/branches/main/protection --input - << 'EOF'
   { ... expected config ... }
   EOF
```

If `--fix` argument is provided and Layer 2 is degraded, offer to auto-restore branch protection rules using the BranchProtectionChecker.restore() method.
</command>

## Arguments
- `--fix` — Auto-restore degraded branch protection rules (Layer 2)
- `--verbose` — Show raw API responses and detailed check info
- `--json` — Output results as JSON (for scripting/automation)
