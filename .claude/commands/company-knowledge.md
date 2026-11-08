# /company-knowledge — Query Organizational Knowledge Base

Query the company knowledge base including Architecture Decision Records (ADRs) and Implementation Patterns. Supports listing, searching, filtering, and viewing recent entries.

**Multi-project mode:** When inside a multi-project company (`.forge-company-root` found), queries the company-level knowledge base and shows which project contributed each entry.

## Input
$ARGUMENTS

Supported subcommands:
- `list` — Show all decisions and patterns
- `search <keyword>` — Search knowledge base for keyword
- `semantic <query>` — Semantic search using Claude (see `/knowledge-search`)
- `category <type>` — Filter by category (architecture, code, testing, security, devops) or status (proposed, accepted, deprecated, superseded)
- `recent <N>` — Show last N entries (default: 5)

Options:
- `--project <project-id>` — Filter results to show only knowledge from a specific project (multi-project mode only)

Examples:
- `/company-knowledge list` — List all knowledge entries
- `/company-knowledge search authentication` — Search for "authentication"
- `/company-knowledge list --project forge-framework` — List entries from forge-framework project
- `/company-knowledge category security --project api-gateway` — Security patterns from api-gateway

## Step 0: Resolve Company Root (Multi-Project Support)

First, determine if we're in multi-project mode by searching upward for `.forge-company-root`:

```bash
uv run .claude/hooks/company/company_resolver.py find 2>/dev/null || echo "NO_ROOT_FOUND"
```

**If `.forge-company-root` found:**
- Set `$COMPANY_ROOT` to the directory containing `.forge-company-root`
- Set `$KNOWLEDGE_PATH` to `$COMPANY_ROOT/.company/knowledge/`
- Set `$MULTI_PROJECT_MODE` to `true`
- Read current project info:
  ```bash
  uv run .claude/hooks/company/company_resolver.py project 2>/dev/null
  ```

**If NO_ROOT_FOUND:**
- Set `$KNOWLEDGE_PATH` to `.company/knowledge/`
- Set `$MULTI_PROJECT_MODE` to `false`

## Step 0.1: Check Knowledge Base Exists

Check if knowledge directory exists at the resolved path:

```bash
ls -la $KNOWLEDGE_PATH 2>/dev/null
```

**If not exists:**
```
## Knowledge Base Not Found

No knowledge base found at `$KNOWLEDGE_PATH`.

[If multi-project mode:]
The company root was found at: $COMPANY_ROOT
But no knowledge base exists. This may indicate incomplete company setup.

To initialize the company knowledge base, run from the company root:
  /company-create --force

[If single-project mode:]
To initialize the company structure, run:
  /company-init

This will create:
- `$KNOWLEDGE_PATH/decisions.md` — Architecture Decision Records
- `$KNOWLEDGE_PATH/patterns.md` — Implementation Patterns
```

Exit without further processing.

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine the subcommand and options:

### Parse --project option first

If `--project <project-id>` is present in arguments:
- Extract the project ID value
- Set `$PROJECT_FILTER` to the extracted value
- Remove `--project <project-id>` from arguments before parsing subcommand

**If --project used but not in multi-project mode:**
```
## Project Filter Not Available

The --project filter requires multi-project mode.

Current mode: single-project

To use project filtering:
1. Upgrade to multi-project company: /company-init --upgrade
2. Or create a new multi-project company: /company-create

In single-project mode, all knowledge is scoped to this project.
```

Exit without further processing.

### Parse subcommand

| Input | Action |
|-------|--------|
| (empty) or `list` | Show all decisions and patterns |
| `search <keyword>` | Search for keyword in both files |
| `semantic <query>` | Redirect to `/knowledge-search` for Claude-powered search |
| `category <type>` | Filter by pattern category or decision status |
| `recent` or `recent <N>` | Show last N entries (default: 5) |

### Subcommand: `semantic <query>`

For semantic search, redirect to the specialized `/knowledge-search` command:

```
## Semantic Knowledge Search

For semantic (natural language) search, use the dedicated command:

/knowledge-search "<query>"

This command uses Claude's semantic understanding to:
- Find conceptually related entries (not just keyword matches)
- Rank results by relevance with scores
- Show reasoning for why each result matches

**Examples:**
- `/knowledge-search "authentication patterns"`
- `/knowledge-search "error handling" --type=pattern`
- `/knowledge-search "API design" --project=gatesmith`

See `/knowledge-search --help` for full options.
```

Exit after displaying this message.

## Step 2: Load Knowledge Files

Read the knowledge base files from the resolved knowledge path:
- `$KNOWLEDGE_PATH/decisions.md` — Architecture Decision Records
- `$KNOWLEDGE_PATH/patterns.md` — Implementation Patterns

Parse entries from each file:

**For decisions.md:**
- Each ADR starts with `## ADR-NNNN:` header
- Extract: ID, Title, Status, Date
- **Extract Project/Source:** Look for `**Project:**` or `**Source:**` field (e.g., `**Project:** forge-framework` or `**Project:** company-wide`)
- Skip the template section (before first `---`)

**For patterns.md:**
- Each pattern starts with `### [Pattern Name]` header
- Extract: Name, Category
- **Extract Source:** Look for `**Source:**` field (e.g., `**Source:** api-gateway` or `**Source:** company-wide`)
- Skip the template section (before first `---`)

### Project Filtering Logic

If `$PROJECT_FILTER` is set:
- Only include entries where Project/Source matches the filter
- Include entries marked as `company-wide` (they apply everywhere)
- Exclude entries from other projects

If `$PROJECT_FILTER` is not set:
- Include all entries from all projects

## Step 3: Execute Subcommand

### Subcommand: `list`

Display all entries in formatted tables.

**Single-project mode output:**

```
## Knowledge Base Overview

═══════════════════════════════════════════════════════════════════════════════
 ORGANIZATIONAL KNOWLEDGE                                          [query: list]
═══════════════════════════════════════════════════════════════════════════════

### Architecture Decision Records (ADRs)

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| ADR-0001 | Use ADR Format for Architectural Decisions | Accepted | 2024-01-01 |
| ADR-0002 | ... | ... | ... |

Total: X decision(s)

### Implementation Patterns

| Pattern | Category |
|---------|----------|
| Builder-Validator Loop | Architecture |
| Atomic Commits | DevOps |

Total: X pattern(s)

═══════════════════════════════════════════════════════════════════════════════
 SUMMARY: X ADRs, X Patterns
═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode output (includes Source column):**

```
## Knowledge Base Overview

═══════════════════════════════════════════════════════════════════════════════
 ORGANIZATIONAL KNOWLEDGE                                          [query: list]
═══════════════════════════════════════════════════════════════════════════════
 Company: [Company Name from .forge-company-root]
 Mode: multi-project
 Knowledge Path: $COMPANY_ROOT/.company/knowledge/
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

### Architecture Decision Records (ADRs)

| ADR | Title | Status | Date | Source |
|-----|-------|--------|------|--------|
| ADR-0001 | Use ADR Format for Architectural Decisions | Accepted | 2024-01-01 | company-wide |
| ADR-0002 | Use JWT for API Auth | Accepted | 2024-02-15 | api-gateway |
| ADR-0003 | Adopt TypeScript | Proposed | 2024-03-01 | forge-framework |

Total: X decision(s) from Y project(s)

### Implementation Patterns

| Pattern | Category | Source |
|---------|----------|--------|
| Builder-Validator Loop | Architecture | company-wide |
| Atomic Commits | DevOps | company-wide |
| Rate Limiting | Security | api-gateway |

Total: X pattern(s) from Y project(s)

═══════════════════════════════════════════════════════════════════════════════
 SUMMARY: X ADRs, X Patterns | Sources: company-wide, api-gateway, forge-framework
═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode with --project filter:**

```
## Knowledge Base Overview

═══════════════════════════════════════════════════════════════════════════════
 ORGANIZATIONAL KNOWLEDGE                     [query: list --project api-gateway]
═══════════════════════════════════════════════════════════════════════════════
 Company: [Company Name]
 Mode: multi-project
 Filter: project=api-gateway (includes company-wide entries)
═══════════════════════════════════════════════════════════════════════════════

### Architecture Decision Records (ADRs)

| ADR | Title | Status | Date | Source |
|-----|-------|--------|------|--------|
| ADR-0001 | Use ADR Format for Architectural Decisions | Accepted | 2024-01-01 | company-wide |
| ADR-0002 | Use JWT for API Auth | Accepted | 2024-02-15 | api-gateway |

Total: 2 decision(s) (1 company-wide, 1 api-gateway)

### Implementation Patterns

| Pattern | Category | Source |
|---------|----------|--------|
| Builder-Validator Loop | Architecture | company-wide |
| Atomic Commits | DevOps | company-wide |
| Rate Limiting | Security | api-gateway |

Total: 3 pattern(s) (2 company-wide, 1 api-gateway)

═══════════════════════════════════════════════════════════════════════════════
 SHOWING: Entries relevant to api-gateway (project-specific + company-wide)
═══════════════════════════════════════════════════════════════════════════════
```

### Subcommand: `search <keyword>`

Use Grep to search both knowledge files at the resolved knowledge path:

```bash
grep -i "<keyword>" $KNOWLEDGE_PATH/decisions.md
grep -i "<keyword>" $KNOWLEDGE_PATH/patterns.md
```

If `$PROJECT_FILTER` is set, filter results to only show entries from that project or company-wide.

**Single-project mode output:**

```
## Knowledge Base Search

═══════════════════════════════════════════════════════════════════════════════
 SEARCH RESULTS                                          [query: search <keyword>]
═══════════════════════════════════════════════════════════════════════════════

### Matching Decisions

| ADR | Title | Status | Match Context |
|-----|-------|--------|---------------|
| ADR-0001 | Use ADR Format | Accepted | "...architectural decisions..." |

### Matching Patterns

| Pattern | Category | Match Context |
|---------|----------|---------------|
| Builder-Validator Loop | Architecture | "...quality standards..." |

═══════════════════════════════════════════════════════════════════════════════
 FOUND: X decision(s), X pattern(s) matching "<keyword>"
═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode output (includes Source column):**

```
## Knowledge Base Search

═══════════════════════════════════════════════════════════════════════════════
 SEARCH RESULTS                                          [query: search <keyword>]
═══════════════════════════════════════════════════════════════════════════════
 Company: [Company Name]
 Mode: multi-project
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

### Matching Decisions

| ADR | Title | Status | Source | Match Context |
|-----|-------|--------|--------|---------------|
| ADR-0001 | Use ADR Format | Accepted | company-wide | "...architectural decisions..." |
| ADR-0002 | JWT Authentication | Accepted | api-gateway | "...authentication tokens..." |

### Matching Patterns

| Pattern | Category | Source | Match Context |
|---------|----------|--------|---------------|
| Builder-Validator Loop | Architecture | company-wide | "...quality standards..." |

═══════════════════════════════════════════════════════════════════════════════
 FOUND: X decision(s), X pattern(s) matching "<keyword>"
 Sources: company-wide, api-gateway
═══════════════════════════════════════════════════════════════════════════════
```

**If no matches:**

```
## Knowledge Base Search

═══════════════════════════════════════════════════════════════════════════════
 SEARCH RESULTS                                          [query: search <keyword>]
═══════════════════════════════════════════════════════════════════════════════
[If multi-project mode:]
 Company: [Company Name]
 Mode: multi-project
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

No entries found matching "<keyword>".

### Suggestions
- Try a broader search term
- Check available categories: architecture, code, testing, security, devops
- Use `/company-knowledge list` to see all entries
[If $PROJECT_FILTER set:]
- Remove the --project filter to search across all projects
- Try: `/company-knowledge search <keyword>` (no filter)

═══════════════════════════════════════════════════════════════════════════════
```

### Subcommand: `category <type>`

Filter entries by category (patterns) or status (decisions).

**Valid categories for patterns:** architecture, code, testing, security, devops
**Valid statuses for decisions:** proposed, accepted, deprecated, superseded

If `$PROJECT_FILTER` is set, additionally filter by project source.

**Single-project mode output:**

```
## Knowledge Base Filter

═══════════════════════════════════════════════════════════════════════════════
 FILTERED RESULTS                                    [query: category <type>]
═══════════════════════════════════════════════════════════════════════════════

### Decisions with Status: <type>

| ADR | Title | Date |
|-----|-------|------|
| ADR-0001 | Use ADR Format | 2024-01-01 |

### Patterns in Category: <type>

| Pattern | Description |
|---------|-------------|
| Builder-Validator Loop | Agent produces output reviewed by validator |
| Atomic Commits | One task = one commit |

═══════════════════════════════════════════════════════════════════════════════
 FOUND: X decision(s), X pattern(s) in category "<type>"
═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode output (includes Source column):**

```
## Knowledge Base Filter

═══════════════════════════════════════════════════════════════════════════════
 FILTERED RESULTS                                    [query: category <type>]
═══════════════════════════════════════════════════════════════════════════════
 Company: [Company Name]
 Mode: multi-project
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

### Decisions with Status: <type>

| ADR | Title | Date | Source |
|-----|-------|------|--------|
| ADR-0001 | Use ADR Format | 2024-01-01 | company-wide |
| ADR-0002 | Use JWT for Auth | 2024-02-15 | api-gateway |

### Patterns in Category: <type>

| Pattern | Description | Source |
|---------|-------------|--------|
| Builder-Validator Loop | Agent produces output reviewed by validator | company-wide |
| Atomic Commits | One task = one commit | company-wide |
| Rate Limiting | Prevent abuse via request limits | api-gateway |

═══════════════════════════════════════════════════════════════════════════════
 FOUND: X decision(s), X pattern(s) in category "<type>"
 Sources: company-wide, api-gateway
═══════════════════════════════════════════════════════════════════════════════
```

**If category not found:**

```
## Knowledge Base Filter

═══════════════════════════════════════════════════════════════════════════════
 FILTERED RESULTS                                    [query: category <type>]
═══════════════════════════════════════════════════════════════════════════════
[If multi-project mode:]
 Company: [Company Name]
 Mode: multi-project
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

No entries found for category/status "<type>".
[If $PROJECT_FILTER set:]
(Note: This includes entries from $PROJECT_FILTER and company-wide)

### Valid Categories (for patterns)
| Category | Description |
|----------|-------------|
| architecture | System design patterns |
| code | Coding conventions |
| testing | Test strategies |
| security | Security practices |
| devops | Operations patterns |

### Valid Statuses (for decisions)
| Status | Description |
|--------|-------------|
| proposed | Under consideration |
| accepted | Approved and active |
| deprecated | No longer recommended |
| superseded | Replaced by newer ADR |

═══════════════════════════════════════════════════════════════════════════════
```

### Subcommand: `recent <N>`

Show the most recent N entries (default: 5), sorted by date/order.

If `$PROJECT_FILTER` is set, only show recent entries from that project or company-wide.

**Single-project mode output:**

```
## Knowledge Base Recent

═══════════════════════════════════════════════════════════════════════════════
 RECENT ENTRIES                                           [query: recent <N>]
═══════════════════════════════════════════════════════════════════════════════

### Recent Decisions

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| ADR-0003 | Most Recent Decision | Accepted | 2024-03-15 |
| ADR-0002 | Second Most Recent | Accepted | 2024-02-10 |
| ADR-0001 | Use ADR Format | Accepted | 2024-01-01 |

### Recent Patterns

| Pattern | Category | Added |
|---------|----------|-------|
| Atomic Commits | DevOps | - |
| Builder-Validator Loop | Architecture | - |

═══════════════════════════════════════════════════════════════════════════════
 SHOWING: Last <N> entries
═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode output (includes Source column):**

```
## Knowledge Base Recent

═══════════════════════════════════════════════════════════════════════════════
 RECENT ENTRIES                                           [query: recent <N>]
═══════════════════════════════════════════════════════════════════════════════
 Company: [Company Name]
 Mode: multi-project
[If $PROJECT_FILTER set:]
 Filter: project=$PROJECT_FILTER
═══════════════════════════════════════════════════════════════════════════════

### Recent Decisions

| ADR | Title | Status | Date | Source |
|-----|-------|--------|------|--------|
| ADR-0003 | Most Recent Decision | Accepted | 2024-03-15 | api-gateway |
| ADR-0002 | Second Most Recent | Accepted | 2024-02-10 | forge-framework |
| ADR-0001 | Use ADR Format | Accepted | 2024-01-01 | company-wide |

### Recent Patterns

| Pattern | Category | Source | Added |
|---------|----------|--------|-------|
| Rate Limiting | Security | api-gateway | - |
| Atomic Commits | DevOps | company-wide | - |
| Builder-Validator Loop | Architecture | company-wide | - |

═══════════════════════════════════════════════════════════════════════════════
 SHOWING: Last <N> entries across all projects
 Sources: company-wide, api-gateway, forge-framework
═══════════════════════════════════════════════════════════════════════════════
```

**Note:** Patterns don't have dates, so they're shown in reverse file order (most recently added at top).

## Step 4: Handle Empty Results

If knowledge base exists but has no entries beyond templates:

```
## Knowledge Base

═══════════════════════════════════════════════════════════════════════════════
 KNOWLEDGE BASE                                              [minimal entries]
═══════════════════════════════════════════════════════════════════════════════

The knowledge base is initialized but contains minimal entries.

### Current State
| Type | Count |
|------|-------|
| Architecture Decisions | 1 (ADR-0001 template example) |
| Implementation Patterns | 2 (core patterns) |

### How to Add Knowledge

**Add a new ADR:**
Edit `.company/knowledge/decisions.md` and add:

```markdown
## ADR-NNNN: [Title]

**Status:** Proposed

**Date:** YYYY-MM-DD

### Context
Why is this decision needed?

### Decision
What was decided?

### Consequences
What are the implications?
```

**Add a new Pattern:**
Edit `.company/knowledge/patterns.md` and add:

```markdown
### [Pattern Name]

**Category:** [Architecture | Code | Testing | Security | DevOps]

**Context:** When to use this pattern

**Pattern:**
Description of the approach

**Example:**
Code or diagram example

**See also:** Related patterns or ADRs
```

═══════════════════════════════════════════════════════════════════════════════
```

## Step 5: Display View Details (for specific entry)

If an ADR or pattern name is specified directly (e.g., `ADR-0001` or `Builder-Validator Loop`), show the full entry:

**Single-project mode:**

```
## ADR-0001: Use ADR Format for Architectural Decisions

═══════════════════════════════════════════════════════════════════════════════

**Status:** Accepted
**Date:** 2024-01-01

### Context

Teams need a consistent way to document significant architectural decisions...

### Decision

Adopt Architecture Decision Records (ADRs) as the standard format...

### Consequences

- **Positive:** Decisions are discoverable and searchable
- **Positive:** New team members can understand historical context
- **Negative:** Requires discipline to document decisions

═══════════════════════════════════════════════════════════════════════════════

### Related
- Patterns using this: Builder-Validator Loop

### Navigation
- Previous: (none)
- Next: ADR-0002

═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode (includes Source metadata):**

```
## ADR-0001: Use ADR Format for Architectural Decisions

═══════════════════════════════════════════════════════════════════════════════
 Source: company-wide                            [applies to all projects]
═══════════════════════════════════════════════════════════════════════════════

**Status:** Accepted
**Date:** 2024-01-01
**Source:** company-wide

### Context

Teams need a consistent way to document significant architectural decisions...

### Decision

Adopt Architecture Decision Records (ADRs) as the standard format...

### Consequences

- **Positive:** Decisions are discoverable and searchable
- **Positive:** New team members can understand historical context
- **Negative:** Requires discipline to document decisions

═══════════════════════════════════════════════════════════════════════════════

### Related
- Patterns using this: Builder-Validator Loop

### Navigation
- Previous: (none)
- Next: ADR-0002

### Source Information
- **Contributed by:** company-wide
- **Applies to:** All projects in the company
- **Location:** $COMPANY_ROOT/.company/knowledge/decisions.md

═══════════════════════════════════════════════════════════════════════════════
```

**Multi-project mode with project-specific entry:**

```
## ADR-0002: Use JWT for API Authentication

═══════════════════════════════════════════════════════════════════════════════
 Source: api-gateway                      [project-specific knowledge]
═══════════════════════════════════════════════════════════════════════════════

**Status:** Accepted
**Date:** 2024-02-15
**Project:** api-gateway

### Context

The API gateway needs a stateless authentication mechanism...

### Decision

Use JSON Web Tokens (JWT) for API authentication...

### Consequences

- **Positive:** Stateless authentication scales horizontally
- **Negative:** Token revocation requires additional infrastructure

═══════════════════════════════════════════════════════════════════════════════

### Related
- Related patterns: Rate Limiting
- Related ADRs: ADR-0003 (Token Refresh Strategy)

### Navigation
- Previous: ADR-0001
- Next: ADR-0003

### Source Information
- **Contributed by:** api-gateway
- **Applies to:** api-gateway project (may be useful for similar projects)
- **Location:** $COMPANY_ROOT/.company/knowledge/decisions.md

═══════════════════════════════════════════════════════════════════════════════
```

## Rules

- **Handle missing files gracefully.** If knowledge files don't exist, guide user to initialize.
- **Case-insensitive search.** All searches should be case-insensitive.
- **Parse entries accurately.** Use the established header patterns to identify entries.
- **Skip template sections.** Content before the first `---` separator is template/documentation.
- **Show context in search.** Include snippet of matching text to help identify relevant entries.
- **Validate categories.** Only accept known categories; show available options for invalid input.
- **Default to list.** If no subcommand provided, treat as `list`.
- **Limit recent output.** Default to 5 entries for `recent` to avoid overwhelming output.

### Multi-Project Mode Rules

- **Always resolve company root first.** Use `company_resolver.py find` to determine the company root before accessing knowledge.
- **Use resolved knowledge path.** In multi-project mode, knowledge is at `$COMPANY_ROOT/.company/knowledge/`, not the local directory.
- **Show Source column in multi-project mode.** Always include the Source/Project column when operating in multi-project mode.
- **Include company-wide with --project filter.** When filtering by project, always include entries marked `company-wide` since they apply to all projects.
- **Validate --project in multi-project mode only.** The `--project` option should error in single-project mode.
- **Works from any project directory.** When inside a registered project, queries should resolve upward to the company knowledge base.
- **Parse Source/Project fields.** Look for `**Source:**` or `**Project:**` metadata in entries to determine attribution.
- **Default Source is company-wide.** If an entry has no Source/Project field, treat it as `company-wide`.
