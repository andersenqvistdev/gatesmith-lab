# /security-audit — Full Security Audit

Perform a comprehensive security audit on the project (or specified files).

## Input
$ARGUMENTS

If arguments specify files or directories, audit those. Otherwise, audit the entire project.

## Step 1: Determine Scope

If no specific scope given:
- Identify all source code files (exclude node_modules, .git, build artifacts)
- Prioritize: auth code, API routes, database queries, file handling, config

If scope is given:
- Focus on the specified files/directories
- Also check files that import from or are imported by the target files

## Step 2: Launch Security Auditor

```
Task(subagent_type="general-purpose", description="Full security audit")
```

Pass the Security Auditor:
- The list of files to audit
- Instruction to read `.claude/agents/security-auditor.md` for full rules
- Full OWASP Top 10 checklist
- Instruction to also run dependency audit tools

## Step 3: Present Results

Show the full audit report to the user.

If CRITICAL findings exist:
```
╔══════════════════════════════════════╗
║  CRITICAL SECURITY ISSUES FOUND     ║
║  X critical findings require        ║
║  immediate attention.               ║
╚══════════════════════════════════════╝
```

Ask: "Want me to fix the critical issues? I'll use the builder/validator pattern to ensure fixes are correct."

## Step 4: Optional Fix Cycle

If the user wants fixes:
1. For each CRITICAL/HIGH finding, spawn an Implementer with the specific remediation
2. Re-run the Security Auditor to verify fixes
3. Maximum 3 fix/verify cycles
4. Present final state

## Rules
- ALWAYS run actual tools (npm audit, pip-audit) — don't guess
- ALWAYS check git history for accidentally committed secrets
- Audit is READ-ONLY unless the user explicitly asks for fixes
- Never downplay findings. CRITICAL means CRITICAL.
