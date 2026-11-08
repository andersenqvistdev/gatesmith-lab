# /prime — Load Full Project Context

Before starting any complex work, use this command to deeply understand the codebase and get guidance on next steps.

## Step 0: Detect Project State

First, determine what kind of project this is:

```bash
# Check for Gatesmith installation
ls .claude/settings.json 2>/dev/null && echo "FORGE_INSTALLED" || echo "NO_FORGE"

# Check for existing project context
ls .planning/PROJECT.md 2>/dev/null && echo "HAS_PROJECT_CONTEXT" || echo "NO_PROJECT_CONTEXT"

# Check for application code (beyond Gatesmith framework files)
find . -maxdepth 2 -name "*.py" -o -name "*.js" -o -name "*.ts" -o -name "*.go" -o -name "*.rs" 2>/dev/null | grep -v ".claude/" | grep -v "node_modules" | head -5

# Check for company mode
ls .company/org.json 2>/dev/null && echo "HAS_COMPANY" || echo "NO_COMPANY"

# Check for multi-project company root
uv run .claude/hooks/company/company_resolver.py mode 2>/dev/null || echo "SINGLE_OR_NONE"
```

**Store results as:**
- `$FORGE_INSTALLED`: true/false
- `$HAS_PROJECT_CONTEXT`: true/false
- `$HAS_APPLICATION_CODE`: true/false
- `$HAS_COMPANY`: true/false
- `$COMPANY_MODE`: "multi-project" / "single-project" / "none"

---

## Step 0.5: Detect Task Type (Token-Optimized Loading)

Analyze the user's recent message or conversation context to determine the task type. This enables selective document loading to minimize token usage.

**Keyword Detection:**

| Task Type | Keywords |
|-----------|----------|
| **planning** | plan, design, architect, requirements, discuss, scope, feature, roadmap, phase |
| **building** | build, implement, code, develop, create, make, add, write |
| **debugging** | debug, fix, bug, error, issue, problem, broken, crash, fail |
| **reviewing** | review, check, verify, audit, test, validate, inspect |
| **general** | (default if no keywords match) |

**Detection Logic:**
1. Check the user's most recent message for keywords
2. Check any `/prime` arguments for explicit task hints
3. If multiple types match, prioritize: debugging > building > reviewing > planning > general
4. Store result as `$TASK_TYPE`

**Document Loading Rules:**

| Document | Est. Tokens | planning | building | debugging | reviewing | general |
|----------|-------------|----------|----------|-----------|-----------|---------|
| PROJECT.md | ~1,000 | ✓ Always | ✓ Always | ✓ Always | ✓ Always | ✓ Always |
| STATE.md | ~500 | ✓ Always | ✓ Always | ✓ Always | ✓ Always | ✓ Always |
| ROADMAP.md | ~6,200 | ✓ Load | ✓ Load | ⊘ Skip | ⊘ Skip | ⊘ Skip |
| REQUIREMENTS.md | ~2,100 | ✓ Load | ⊘ Skip | ⊘ Skip | ⊘ Skip | ⊘ Skip |
| DISCUSS.md | ~3,600 | ✓ Load | ⊘ Skip | ⊘ Skip | ⊘ Skip | ⊘ Skip |

**Token Savings by Task Type:**
- **planning**: ~0% saved (full context needed) — ~13,400 tokens
- **building**: ~43% saved — ~7,700 tokens
- **debugging**: ~89% saved — ~1,500 tokens
- **reviewing**: ~89% saved — ~1,500 tokens (+ git diff)
- **general**: ~89% saved — ~1,500 tokens

**Store result as:** `$TASK_TYPE` (planning | building | debugging | reviewing | general)

---

## Step 1: Handle Fresh Install (No Application Code)

**If `$FORGE_INSTALLED` is true BUT `$HAS_APPLICATION_CODE` is false AND `$HAS_PROJECT_CONTEXT` is false:**

This is a fresh Gatesmith installation without any application code yet.

**Output:**

```
══════════════════════════════════════════════════════════════════
  GATESMITH READY — Fresh Installation Detected
══════════════════════════════════════════════════════════════════

This is a fresh Gatesmith installation. No application code detected yet.

## Getting Started

To build something new, follow these steps:

  1. /discuss              Capture what you want to build
                           Talk through requirements, preferences, scope
                           Creates REQUIREMENTS.md, DISCUSS.md

  2. /plan                 Design the implementation
                           Architect creates tasks with checker review
                           Creates .planning/ and ROADMAP.md on first use

  3. /build                Execute the plan
                           Wave-by-wave implementation with atomic commits
                           Each task = one commit

  4. /review               Code review of changes
  5. /gate                 Security checkpoint before push

## Quick Alternative

For smaller features, skip the ceremony:

  /feature "description"   Fully autonomous: plan→build→review→test→commit

More commands are installed; they are unsupported and listed in EXPERIMENTAL.md.

══════════════════════════════════════════════════════════════════
```

**Then STOP — don't proceed to Step 2.**

---

## Step 1b: Handle Company Mode

**If `$HAS_COMPANY` is true, add one line to the output (single- or multi-project):**

```
## Company Extension

Your project has a .company/ directory. To see what needs you, run /pending;
to handle escalations, run /respond.

More company commands are installed; they are unsupported and listed in EXPERIMENTAL.md.
```

---

## Step 2: Project Discovery (For Existing Codebases)

**Only run this if `$HAS_APPLICATION_CODE` is true.**

Explore and catalog:

1. **Project type and structure:**
   - Read package.json / pyproject.toml / Cargo.toml / go.mod
   - Map the top-level directory structure
   - Identify src/, tests/, config/, docs/ equivalents

2. **Architecture:**
   - Find entry points (main files, index files, app files)
   - Map the dependency/import graph for key modules
   - Identify design patterns in use (MVC, hexagonal, etc.)

3. **Configuration:**
   - Read all config files (tsconfig, eslint, prettier, ruff, etc.)
   - Note CI/CD setup (.github/workflows, Dockerfile, etc.)
   - Check for environment variable usage

4. **Code conventions:**
   - Sample 3-5 source files to identify naming patterns, code style
   - Check test file conventions (naming, structure, assertion style)
   - Note import ordering, module organization

5. **Key documentation:**
   - Read README.md
   - Read CLAUDE.md / .claude/CLAUDE.md if present
   - Check for API docs, architecture docs

6. **Planning state (selective loading based on $TASK_TYPE):**

   **Always load (essential context):**
   - Read .planning/PROJECT.md for project context (~1,000 tokens)
   - Read .planning/STATE.md for session state (~500 tokens)

   **Conditionally load based on task type:**

   ```
   IF $TASK_TYPE == "planning":
       - Read .planning/ROADMAP.md (~6,200 tokens)
       - Read .planning/REQUIREMENTS.md (~2,100 tokens)
       - Read .planning/DISCUSS.md (~3,600 tokens)

   ELSE IF $TASK_TYPE == "building":
       - Read .planning/ROADMAP.md (~6,200 tokens)
       - SKIP REQUIREMENTS.md (not needed for implementation)
       - SKIP DISCUSS.md (decisions already captured in roadmap)

   ELSE IF $TASK_TYPE == "debugging":
       - SKIP ROADMAP.md (focus on current error)
       - SKIP REQUIREMENTS.md (not relevant to bugs)
       - SKIP DISCUSS.md (not relevant to bugs)

   ELSE IF $TASK_TYPE == "reviewing":
       - SKIP ROADMAP.md (review the code, not the plan)
       - SKIP REQUIREMENTS.md (review implementation quality)
       - SKIP DISCUSS.md (not needed)
       - Run: git diff HEAD~5 --stat (recent changes context)

   ELSE (general):
       - SKIP ROADMAP.md (load on demand)
       - SKIP REQUIREMENTS.md (load on demand)
       - SKIP DISCUSS.md (load on demand)
   ```

   **Track loading decisions for output:**
   - Store `$DOCS_LOADED`: list of documents loaded
   - Store `$DOCS_SKIPPED`: list of documents skipped with reasons
   - Store `$TOKENS_LOADED`: estimated tokens loaded
   - Store `$TOKENS_SAVED`: estimated tokens saved

---

## Step 2.5: Knowledge Search Suggestions

**Only run this if `$HAS_APPLICATION_CODE` is true.**

Check if the knowledge base exists and has content:

```bash
KNOWLEDGE_COUNT=$(find .company/knowledge -type f -name "*.md" ! -name "README.md" 2>/dev/null | wc -l)
```

**If `$KNOWLEDGE_COUNT` is 0:**
- Set `$HAS_KNOWLEDGE=false`
- Skip to Step 3

**If `$KNOWLEDGE_COUNT` is greater than 0:**
- Set `$HAS_KNOWLEDGE=true`
- Map `$TASK_TYPE` to suggested searches:

| Task Type | Suggestion 1 | Suggestion 2 |
|-----------|--------------|--------------|
| **planning** | "architecture patterns" | "design decisions" |
| **building** | "implementation patterns" | "code patterns" |
| **debugging** | "error handling" | "troubleshooting" |
| **reviewing** | "code review patterns" | "quality standards" |
| **general** | "workflow patterns" | "getting started" |

**If `$TASK_TYPE` is not in the table:** Default to "general" suggestions.

**Store as:**
- `$KNOWLEDGE_SUGGESTIONS`: List of 2 suggestion strings
- `$HAS_KNOWLEDGE`: true/false

---

## Step 3: Generate Context Summary

**For existing codebases, output:**

```
══════════════════════════════════════════════════════════════════
  PROJECT CONTEXT LOADED
══════════════════════════════════════════════════════════════════

## Project: [name]
- Type: [language/framework]
- Structure: [monorepo/single-package/etc]

## Key Paths
| Path | Purpose |
|------|---------|
| src/ | Source code |
| tests/ | Test files |

## Architecture
[Brief description of how the code is organized]

## Conventions
- Naming: [camelCase/snake_case/etc]
- Tests: [jest/pytest/etc] in [location]
- Linting: [tool and config]

## Entry Points
- [main entry point]
- [API routes file]

## Dependencies (notable)
- [key deps and their purpose]

## Current State
[From .planning/STATE.md if exists, or "No active work"]

══════════════════════════════════════════════════════════════════

## Context Loading (Token-Optimized)

| Document | Status | Est. Tokens | Reason |
|----------|--------|-------------|--------|
| PROJECT.md | [✓ Loaded / ⊘ Not found] | ~1,000 | Always loaded |
| STATE.md | [✓ Loaded / ⊘ Not found] | ~500 | Always loaded |
| ROADMAP.md | [✓ Loaded / ⊘ Skipped] | ~6,200 | [Needed for $TASK_TYPE / Not needed for $TASK_TYPE] |
| REQUIREMENTS.md | [✓ Loaded / ⊘ Skipped] | ~2,100 | [Needed for $TASK_TYPE / Not needed for $TASK_TYPE] |
| DISCUSS.md | [✓ Loaded / ⊘ Skipped] | ~3,600 | [Needed for $TASK_TYPE / Not needed for $TASK_TYPE] |

**Task Type Detected:** [$TASK_TYPE] (keywords: [matched keywords])
**Total Tokens:** ~[$TOKENS_LOADED] (saved ~[$TOKENS_SAVED] vs full load of ~13,400)
**Efficiency:** [$PERCENTAGE]% reduction

══════════════════════════════════════════════════════════════════

## Knowledge Suggestions
[Only display this section if $HAS_KNOWLEDGE is true]

Based on task type: $TASK_TYPE

  /knowledge-search "$SUGGESTION_1"
  /knowledge-search "$SUGGESTION_2"

Run a search to find relevant patterns and decisions.

══════════════════════════════════════════════════════════════════

## Ready for Work

  /plan <feature>          Design a new feature
  /build                   Execute current plan (if exists)
  /feature "desc"          Autonomous feature development
  /review                  Code review

More commands are installed; they are unsupported and listed in EXPERIMENTAL.md.

## Load More Context (if needed)

If the detected task type was incorrect or you need more context:

  /prime --full            Load all planning documents
  /prime --planning        Force planning mode (all docs)
  /prime --building        Force building mode (roadmap only)
  /prime --debugging       Force debugging mode (minimal)

══════════════════════════════════════════════════════════════════
```

---

## Rules

- Be thorough but concise. Read actual files, don't guess.
- This is READ-ONLY exploration. Don't modify anything.
- Focus on information that helps with future implementation tasks.
- Always detect project state first and provide appropriate guidance.
- For fresh installs, guide the user through the full workflow.
- **Token Efficiency:** Always detect task type and load only necessary documents.
- **Transparency:** Always show which documents were loaded/skipped and why.
- **Override Support:** Respect explicit mode flags (--full, --planning, --building, --debugging).
- **Accuracy:** If task type is ambiguous, prefer loading more context over missing information.
