# /docs — Fully Autonomous Documentation Generation

You are generating comprehensive documentation for the project or a specific area. You work autonomously — explore, analyze, write, and commit without asking the user.

## Input
$ARGUMENTS

If no arguments: generate full project documentation.
If arguments specify a scope: document only that area (e.g., "API endpoints", "database models", "authentication flow").

## Step 0: Load Context

Read `.planning/PROJECT.md`, `CLAUDE.md`, and any existing documentation (README.md, docs/, etc.).

## Step 1: Deep Codebase Analysis

Spawn parallel exploration agents to map what needs documenting:

```
Task(subagent_type="Explore", description="Map all public APIs, routes, and endpoints")
Task(subagent_type="Explore", description="Map all data models, schemas, and types")
Task(subagent_type="Explore", description="Map architecture patterns and key flows")
Task(subagent_type="Explore", description="Map configuration, environment variables, and setup requirements")
```

For scoped documentation, only spawn relevant explorers.

## Step 2: Check for Documentation Agent

Look for `.claude/agents/docs-writer.md`. If it doesn't exist, create it:

```
Task(subagent_type="general-purpose", description="Create documentation writer agent")
```

Pass to Meta-Agent:
"Read .claude/agents/meta-agent.md. Create a 'docs-writer' agent specialized in generating technical documentation. It should:
- Have Read-only + Write access (only to docs/ and *.md files)
- Analyze code to extract documentation (JSDoc, docstrings, type signatures)
- Follow the project's existing documentation style
- Produce: API references, architecture guides, setup guides, usage examples
- Include code examples extracted from actual source or tests
- Output structured markdown with tables, code blocks, and cross-references"

## Step 3: Generate Documentation

Based on scope, spawn documentation writer agent(s) in parallel:

### Full project docs:
```
Task(subagent_type="general-purpose", description="Write API reference documentation")
Task(subagent_type="general-purpose", description="Write architecture and setup guide")
Task(subagent_type="general-purpose", description="Write usage guide with examples")
```

### Scoped docs:
Spawn a single writer for the specific area.

Each writer receives:
- The exploration results from Step 1
- Instruction to read `.claude/agents/docs-writer.md` (created in Step 2)
- The project context from `.planning/PROJECT.md`
- Any existing docs to update (not duplicate)

## Step 4: Review Documentation

Spawn Reviewer agent to check docs quality:
```
Task(subagent_type="general-purpose", description="Review documentation accuracy")
```

The reviewer checks:
- Technical accuracy (do code examples actually match the codebase?)
- Completeness (are all public APIs documented?)
- Clarity (would a new developer understand this?)
- No stale references (do file paths and function names exist?)

Fix any issues found.

## Step 5: Commit & Report

Atomic commit:
```bash
uv run .claude/hooks/atomic_commit.py docs "docs" "generate [scope] documentation"
```

Report:
```
## Documentation Generated

### Scope
[what was documented]

### Files Created/Updated
| File | Type | Sections |
|------|------|----------|
| docs/api.md | API Reference | X endpoints |
| docs/architecture.md | Architecture | Y diagrams |
| README.md | Updated | Setup, usage |

### Coverage
- Public APIs documented: X/Y
- Data models documented: X/Y
- Configuration documented: X/Y

### Documentation Agent
[Created / Already existed]
```

## Autonomy Rules
1. **DO NOT ask** what format to use — analyze existing docs and match the style.
2. **DO NOT document** internal/private implementation details unless explicitly asked.
3. **DO create the docs-writer agent** if it doesn't exist — this is the smart agent spawning pattern.
4. **DO update** existing docs rather than creating duplicates.
5. **DO include** real code examples from the actual codebase, not made-up examples.
6. **Write to** `docs/` directory for detailed docs, update `README.md` for overview.
