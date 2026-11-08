# /// script
# requires-python = ">=3.10"
# ///
"""
Merge Gatesmith sections into an existing CLAUDE.md.

Behavior:
- Reads the existing CLAUDE.md
- Appends Gatesmith sections ONLY if they don't already exist
- Detects existing Gatesmith content by looking for marker comments
- Never removes or modifies existing content

Usage:
    uv run merge_claude_md.py <existing_claude_md> <forge_claude_md> [--dry-run]
"""

import sys

FORGE_MARKER = "<!-- FORGE:START -->"
FORGE_END_MARKER = "<!-- FORGE:END -->"

# The sections we inject — these are the minimum security/workflow context
FORGE_SECTIONS = """
<!-- FORGE:START — Do not edit between these markers. Managed by Gatesmith. -->

## Gatesmith: Security & Workflow

### Trust Tiers

| Tier | Operations | Permission | Why |
|------|-----------|------------|-----|
| **Free** | Read, Glob, Grep, WebSearch, Task, ls, git status/diff/log | Auto-approved | Cannot cause harm — read-only |
| **Guarded** | Write, Edit, mkdir, git add/commit, lint, test, build | Auto-approved + logged + hook-validated | Modifies local state but reversible. Hooks enforce quality + secret scanning |
| **Gated** | git push, rm, docker, deploy | Requires human confirmation | External consequences, harder to reverse |
| **Forbidden** | rm -rf, sudo rm, chmod 777, curl\\|bash, push --force main | Blocked unconditionally | No legitimate dev reason |

### Active Hooks (automatic on every action)

| Trigger | Hook | What It Does |
|---------|------|-------------|
| Every file write | `secrets_scanner.py` | Blocks API keys, tokens, passwords in code |
| Every file write | `validate_quality.py` | Runs linter/formatter after changes |
| Every bash command | `block_dangerous.py` | Blocks rm -rf, sudo rm, chmod 777, etc. |
| Every git operation | `git_guardian.py` | Blocks commits with secrets, pushes to main |
| Every package install | `dependency_check.py` | Typosquat detection, CVE audit |
| Every tool action | `log_activity.py` | Full audit trail to logs/ |

### Available Commands

| Command | Purpose |
|---------|---------|
| `/prime` | Explore and load project context |
| `/plan <feature>` | Multi-agent architecture design |
| `/build` | Full build/review/test pipeline |
| `/review` | Code review on changes |
| `/gate` | Security checkpoint — pause, scan, approve |
| `/security-audit` | Full OWASP Top 10 audit |
| `/add-agent` | Create new specialist agent |

### Agent Team

| Agent | Role | Access Level |
|-------|------|-------------|
| Architect | Designs implementation plans | Read-only |
| Implementer | Executes plans, writes code | Full |
| Reviewer | Validates quality + security | Read-only + lint |
| Tester | Writes and runs tests | Full |
| Security Auditor | OWASP audit, secrets, deps | Read-only + audit tools |
| Meta-Agent | Generates new agents | Read + Write |

### Constraints (Gatesmith-enforced)

- NEVER commit .env files or secrets (enforced by git_guardian hook)
- NEVER hardcode API keys (enforced by secrets_scanner hook)
- NEVER run destructive commands (enforced by block_dangerous hook)
- ALWAYS use the builder/validator pattern for non-trivial changes
- ALWAYS run /gate before pushing code

<!-- FORGE:END -->
"""


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: merge_claude_md.py <existing_claude_md> [--dry-run]",
            file=sys.stderr,
        )
        sys.exit(1)

    existing_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    with open(existing_path) as f:
        existing_content = f.read()

    # Check if Gatesmith sections already exist
    if FORGE_MARKER in existing_content:
        # Replace existing Gatesmith section
        start = existing_content.index(FORGE_MARKER)
        end = existing_content.index(FORGE_END_MARKER) + len(FORGE_END_MARKER)
        merged = (
            existing_content[:start] + FORGE_SECTIONS.strip() + existing_content[end:]
        )
        action = "UPDATED"
    else:
        # Append Gatesmith sections
        merged = existing_content.rstrip() + "\n\n" + FORGE_SECTIONS.strip() + "\n"
        action = "APPENDED"

    if dry_run:
        print(f"Action: {action} Gatesmith sections", file=sys.stderr)
        print(f"Existing content: {len(existing_content)} chars", file=sys.stderr)
        print(f"Merged content: {len(merged)} chars", file=sys.stderr)
        print("", file=sys.stderr)

    print(merged)


if __name__ == "__main__":
    main()
