# /knowledge-search — Semantic Knowledge Search

Search the organizational knowledge base using natural language. Uses Claude's semantic understanding to find relevant patterns, decisions, and learnings.

## Input
$ARGUMENTS

## Usage

```
/knowledge-search "authentication patterns"
/knowledge-search "error handling best practices"
/knowledge-search "API design" --type=pattern
/knowledge-search "database choice" --type=decision
/knowledge-search "security" --project=gatesmith
```

## Step 1: Parse Arguments

Parse the input to extract:
- **query**: The natural language search query (required)
- **--type**: Filter by entry type (pattern, decision, workshop, learning)
- **--project**: Filter by project ID
- **--limit**: Maximum results to return (default: 5)

If no query provided:
```
## Knowledge Search

Usage: /knowledge-search "your query" [--type=TYPE] [--project=ID] [--limit=N]

Examples:
  /knowledge-search "authentication patterns"
  /knowledge-search "error handling" --type=pattern
  /knowledge-search "API design" --project=gatesmith --limit=10
```

## Step 2: Load Knowledge Entries

Load knowledge entries from the knowledge base:

```bash
uv run .claude/hooks/company/semantic_search.py list
```

This loads entries from:
- `.company/knowledge/patterns.md` — Architectural patterns
- `.company/knowledge/decisions.md` — ADRs and decisions
- `.company/knowledge/workshops.md` — Workshop records
- `.company/knowledge/alignment.md` — Alignment checks

Apply any type or project filters specified.

## Step 3: Semantic Ranking

Spawn a subagent to semantically rank the entries:

```
Task(subagent_type="general-purpose", description="Rank knowledge entries by relevance")
```

Pass the subagent:
- The formatted knowledge entries (from semantic_search.py)
- The search query
- Instructions to return top N results with relevance scores

**Subagent Prompt:**
```
You are evaluating knowledge base entries for semantic relevance.

**Query:** [query]

**Task:** Rank the following knowledge entries by how well they match the query semantically.
Consider:
- Direct keyword matches
- Conceptual similarity (e.g., "auth" matches "login", "session")
- Contextual relevance
- Actionable value for someone searching this query

Return the top [limit] most relevant entries as JSON:
```json
[
  {"id": "entry-id", "score": 85, "reason": "Brief reason"}
]
```

[Formatted entries from semantic_search.py]
```

## Step 4: Display Results

Format and display the ranked results:

```
═══════════════════════════════════════════════════════════════
 KNOWLEDGE SEARCH                                  [N results]
═══════════════════════════════════════════════════════════════
 Query: "[query]"
 Filters: [type=X] [project=Y]
═══════════════════════════════════════════════════════════════

 1. [95%] Pattern: JWT Token Refresh Strategy
    📁 patterns.md:145 | Project: forge
    ─────────────────────────────────────────────────────────
    Implement refresh token rotation with short-lived access
    tokens (15min) and long-lived refresh tokens (7 days)...

 2. [82%] Decision: Session Storage Choice
    📁 decisions.md:78 | Project: forge
    ─────────────────────────────────────────────────────────
    Chose Redis for session storage because of built-in
    expiration and cluster support...

 3. [71%] Workshop: Authentication Deep Dive
    📁 workshops.md:234
    ─────────────────────────────────────────────────────────
    Team discussed OAuth2 vs session-based auth. Decided
    to use OAuth2 for external APIs...

═══════════════════════════════════════════════════════════════
 Tip: Use --type=pattern to filter by type
═══════════════════════════════════════════════════════════════
```

## Step 5: No Results Handling

If no relevant results found (all scores < 30):

```
═══════════════════════════════════════════════════════════════
 KNOWLEDGE SEARCH                                  [0 results]
═══════════════════════════════════════════════════════════════
 Query: "[query]"

 No semantically relevant entries found.

 Suggestions:
 • Try broader terms (e.g., "auth" instead of "JWT refresh")
 • Check available types: /company-knowledge list
 • Add new knowledge: /company-knowledge add pattern "..."

═══════════════════════════════════════════════════════════════
```

## Rules

1. **Always use semantic matching** — Don't just do keyword search
2. **Show relevance scores** — Users should see why results matched
3. **Include file:line references** — Enable navigation to source
4. **Truncate long content** — Show preview, not full entry
5. **Respect filters** — Type and project filters are strict
6. **Limit token usage** — Cap entries sent to subagent at 20
