# /agent — Unified Agent Management with Company Awareness

Create, list, activate, or archive agents with intelligent company-aware lifecycle management. This command replaces `/add-agent` with full company integration when available, falling back to ephemeral mode for standalone use.

## Input
$ARGUMENTS

## Command Syntax

```
/agent [description]              # Create new agent from description
/agent --list                     # List all auto-consultants (active + archived)
/agent --activate [consultant-id] # Re-activate an archived consultant
/agent --archive [consultant-id]  # Archive an active consultant
```

## Step 0: Determine Operating Mode

Check for company context using `company_resolver`:

```bash
uv run .claude/hooks/company/company_resolver.py dir 2>/dev/null && echo "COMPANY_EXISTS" || echo "NO_COMPANY"
```

Also check if org.json exists:

```bash
ls "$(uv run .claude/hooks/company/company_resolver.py dir 2>/dev/null)/org.json" 2>/dev/null && echo "ORG_EXISTS" || echo "NO_ORG"
```

**Store the results:**
- `$HAS_COMPANY`: true if both COMPANY_EXISTS and ORG_EXISTS, false otherwise
- `$COMPANY_DIR`: Path to `.company/` directory (if exists)

**Decision:**
- If `$HAS_COMPANY` is false → **EPHEMERAL MODE** (behave like `/add-agent`)
- If `$HAS_COMPANY` is true → **COMPANY MODE** (full lifecycle management)

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine the operation:

| Pattern | Operation | Go to |
|---------|-----------|-------|
| `--list` | LIST | Step 2A |
| `--activate [id]` | ACTIVATE | Step 2B |
| `--archive [id]` | ARCHIVE | Step 2C |
| `[description]` | CREATE | Step 2D (company) or Step 3 (ephemeral) |
| (empty) | HELP | Display usage |

**If empty arguments:**
```
## /agent — Unified Agent Management

**Usage:**
  /agent [description]              Create new agent from description
  /agent --list                     List all auto-consultants
  /agent --activate [consultant-id] Re-activate archived consultant
  /agent --archive [consultant-id]  Archive active consultant

**Examples:**
  /agent A GraphQL specialist for schema design and resolver optimization
  /agent Database migration expert for PostgreSQL
  /agent --list
  /agent --activate graphql-specialist
  /agent --archive db-migration-expert

**Mode:** [COMPANY / EPHEMERAL based on $HAS_COMPANY]
```

---

## Step 2A: LIST Operation (Company Mode Only)

**If in EPHEMERAL mode:**
```
## List Not Available

Agent listing requires company context.

To enable company features:
1. Run `/company-init` to create a single-project company
2. Or run `/company-create` for multi-project setup

In ephemeral mode, agents are created in `.claude/agents/` and not tracked.
Use `ls .claude/agents/*.md` to see existing agent files.
```

**If in COMPANY mode:**

Get consultant data via lifecycle utility:

```bash
# Get active auto-consultants from org.json
uv run .claude/hooks/company/consultant_lifecycle.py context --id "*" 2>/dev/null || echo "[]"
```

Read org.json directly and filter for auto-consultants:

```bash
cat "$COMPANY_DIR/org.json" | python3 -c "
import json, sys
org = json.load(sys.stdin)
employees = org.get('employees', org.get('agents', []))
consultants = [e for e in employees if e.get('type') == 'auto-consultant']
for c in consultants:
    print(f\"ACTIVE|{c['id']}|{c.get('name', c['id'])}|{c.get('status', 'unknown')}|{','.join(c.get('capabilities', [])[:3])}|{c.get('activationCount', 0)}\")
"
```

List archived consultants:

```bash
ls "$COMPANY_DIR/archive/consultants/"*.md 2>/dev/null | while read f; do
  id=$(basename "$f" .md)
  # Skip reactivated archives
  [[ "$id" == *".reactivated"* ]] && continue
  echo "ARCHIVED|$id"
done
```

**Display:**
```
## Auto-Consultants

### Active Consultants

| ID | Name | Status | Top Skills | Activations |
|----|------|--------|------------|-------------|
| [id] | [name] | [status] | [skill1, skill2, skill3] | [count] |

### Archived Consultants

| ID | Available For |
|----|---------------|
| [id] | Re-activation via `/agent --activate [id]` |

**Total:** [X] active, [Y] archived

### Commands
- `/agent --activate [id]` — Re-activate an archived consultant
- `/agent --archive [id]` — Archive an active consultant
- `/agent [description]` — Create new consultant
```

---

## Step 2B: ACTIVATE Operation (Company Mode Only)

**If in EPHEMERAL mode:**
```
## Activation Not Available

Consultant activation requires company context.
Run `/company-init` or `/company-create` to enable company features.
```

**If in COMPANY mode:**

Extract consultant ID from arguments (after `--activate`).

**If no ID provided:**
```
## Missing Consultant ID

Usage: /agent --activate [consultant-id]

Example: /agent --activate graphql-specialist

To see available archived consultants:
  /agent --list
```

Reactivate via lifecycle utility:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py reactivate --id "[consultant-id]" --request "Reactivated via /agent command"
```

**On success:**
```
## Consultant Reactivated

**ID:** [consultant-id]
**Name:** [name]
**Activation #:** [count]
**Skills:** [skill1, skill2, ...]

The consultant's memory has been preserved. Previous context and learnings
are available for the new engagement.

### Next Steps
- Use the consultant: "Use [consultant-id] to [task]"
- Or spawn directly in code via Task tool with consultant context
```

**On failure (not found):**
```
## Consultant Not Found

No archived consultant with ID "[consultant-id]" found.

**Available archived consultants:**
[list from /agent --list]

Did you mean one of these?
```

---

## Step 2C: ARCHIVE Operation (Company Mode Only)

**If in EPHEMERAL mode:**
```
## Archival Not Available

Consultant archival requires company context.
Run `/company-init` or `/company-create` to enable company features.
```

**If in COMPANY mode:**

Extract consultant ID from arguments (after `--archive`).

**If no ID provided:**
```
## Missing Consultant ID

Usage: /agent --archive [consultant-id]

Example: /agent --archive graphql-specialist

To see active consultants:
  /agent --list
```

Archive via lifecycle utility:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py archive --id "[consultant-id]" --reason "Archived via /agent command" --by "user"
```

**On success:**
```
## Consultant Archived

**ID:** [consultant-id]
**Archive File:** $COMPANY_DIR/archive/consultants/[consultant-id].md

### Preserved
- Memory snapshot (working context)
- Extracted learnings (patterns, decisions, lessons)
- Activation history
- Skill profile

### Reactivation
To bring this consultant back:
  /agent --activate [consultant-id]

The consultant's accumulated knowledge will be restored.
```

**On failure:**
```
## Archive Failed

[Error details from lifecycle utility]

**Possible reasons:**
- Consultant ID not found
- Consultant is not an auto-consultant (cannot archive permanent employees)
- File system error
```

---

## Step 2D: CREATE Operation (Company Mode)

This is the main flow for creating a new agent with company awareness.

### Step 2D.1: Extract Skills from Request

Analyze the description to extract skills:

```bash
echo "$ARGUMENTS" | python3 -c "
import sys
import re

# Domain skill mapping (subset of consultant_lifecycle.py)
DOMAIN_SKILLS = {
    'database': ['database', 'sql', 'orm', 'postgres', 'mysql', 'mongodb', 'migration', 'schema'],
    'api': ['api', 'rest', 'graphql', 'endpoint', 'swagger', 'openapi'],
    'frontend': ['frontend', 'react', 'vue', 'angular', 'ui', 'css', 'component'],
    'backend': ['backend', 'server', 'service', 'microservice'],
    'devops': ['devops', 'docker', 'kubernetes', 'ci/cd', 'pipeline', 'deploy', 'terraform'],
    'testing': ['testing', 'test', 'spec', 'coverage', 'qa', 'unit', 'integration'],
    'security': ['security', 'auth', 'encryption', 'owasp', 'vulnerability'],
    'performance': ['performance', 'optimize', 'latency', 'profiling', 'cache'],
}

TECH_TERMS = [
    'python', 'javascript', 'typescript', 'java', 'go', 'rust',
    'react', 'vue', 'angular', 'django', 'flask', 'fastapi', 'express',
    'postgres', 'mysql', 'mongodb', 'redis', 'elasticsearch',
    'docker', 'kubernetes', 'aws', 'gcp', 'azure',
]

desc = sys.stdin.read().lower()
skills = set()

for domain, terms in DOMAIN_SKILLS.items():
    for term in terms:
        if re.search(rf'\b{re.escape(term)}\b', desc):
            skills.add(term)
            skills.add(domain)

for term in TECH_TERMS:
    if re.search(rf'\b{re.escape(term)}\b', desc):
        skills.add(term)

print(','.join(sorted(skills)) if skills else 'general')
"
```

Store as `$EXTRACTED_SKILLS`.

### Step 2D.2: Search for Matching Consultant

Use the lifecycle utility to find a match:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py find --request "$ARGUMENTS" --skills "$EXTRACTED_SKILLS"
```

Parse the JSON output to check for matches.

**If match found in ARCHIVES:**
```
## Matching Archived Consultant Found

An archived consultant matches your request:

| Field | Value |
|-------|-------|
| ID | [consultant-id] |
| Name | [name] |
| Skill Match | [X]% |
| Previous Activations | [count] |

### Recommendation
Re-activate this consultant to preserve accumulated knowledge:
  /agent --activate [consultant-id]

Or create a new consultant anyway:
  /agent --force [description]

**Skills overlap:**
- Requested: [skill1, skill2, ...]
- Consultant has: [skill1, skill2, ...]
```

**If match found ACTIVE and AVAILABLE:**
```
## Matching Consultant Available

An active consultant matches your request:

| Field | Value |
|-------|-------|
| ID | [consultant-id] |
| Name | [name] |
| Status | available |
| Skill Match | [X]% |

### Use This Consultant
"Use [consultant-id] to [your task]"

Or spawn via Task tool with:
```
Task(subagent_type="general-purpose", description="[task]")
```
Include consultant context from: $COMPANY_DIR/employees/[consultant-id]/memory.md

To create a new consultant anyway:
  /agent --force [description]
```

**If match found ACTIVE but BUSY:**
```
## Similar Consultant Exists (Busy)

A similar consultant exists but is currently busy:

| Field | Value |
|-------|-------|
| ID | [consultant-id] |
| Name | [name] |
| Status | busy |

### Options
1. Wait for [consultant-id] to become available
2. Create a new consultant with similar skills (will have suffix):
   Proceeding with creation...
```

Continue to Step 2D.3 to create new consultant.

**If no match found:**
Continue to Step 2D.3 to create new consultant.

### Step 2D.3: Generate Consultant ID

Create a unique consultant ID:

```bash
# Extract key words and generate ID
echo "$ARGUMENTS" | python3 -c "
import sys
import re

desc = sys.stdin.read().strip()
# Extract key nouns/adjectives
words = re.findall(r'\b[a-z]+\b', desc.lower())
# Filter common words
stop_words = {'a', 'an', 'the', 'for', 'and', 'or', 'to', 'with', 'that', 'this', 'is', 'be', 'of', 'in', 'on'}
key_words = [w for w in words if w not in stop_words and len(w) > 2][:3]
print('-'.join(key_words) if key_words else 'specialist')
"
```

Check if ID exists and add suffix if needed:

```bash
base_id="[generated-id]"
id="$base_id"
suffix=2
while grep -q "\"id\": \"$id\"" "$COMPANY_DIR/org.json" 2>/dev/null; do
  id="${base_id}-${suffix}"
  suffix=$((suffix + 1))
done
echo "$id"
```

Store as `$CONSULTANT_ID`.

### Step 2D.4: Determine Department

Auto-detect department from skills:

| Skills Include | Department |
|----------------|------------|
| database, api, backend, frontend, devops, testing, security, performance | engineering |
| product, strategy, roadmap, requirements | product |
| design, ux, ui, visual, prototype | design |
| (default) | engineering |

Store as `$DEPARTMENT`.

### Step 2D.5: Invoke Meta-Agent

Spawn the Meta-Agent to generate the agent definition:

```
Task(subagent_type="general-purpose", description="Generate auto-consultant agent definition")
```

**Pass to Meta-Agent:**
1. Read `.claude/agents/meta-agent.md` for generation rules
2. Read existing agents in `.claude/agents/` for patterns
3. Read existing company agents in `$COMPANY_DIR/employees/` for company-specific patterns

**Agent Requirements:**
- Role: [description from user]
- ID: $CONSULTANT_ID
- Type: auto-consultant (specialist created on demand)
- Skills: $EXTRACTED_SKILLS
- Output: Write to `.claude/agents/$CONSULTANT_ID.md`

**The agent definition must include:**
- Clear role description
- Capabilities section with skills
- Process for completing tasks
- Structured output format
- Rules for working within company context
- Reference to memory path for context accumulation

### Step 2D.6: Register Consultant

Use lifecycle utility to register:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py register \
  --id "$CONSULTANT_ID" \
  --name "[Display Name from Meta-Agent]" \
  --request "$ARGUMENTS" \
  --skills "$EXTRACTED_SKILLS" \
  --department "$DEPARTMENT"
```

This will:
- Add entry to org.json with type="auto-consultant"
- Create employee directory at `$COMPANY_DIR/employees/$CONSULTANT_ID/`
- Initialize memory.md with creation context

### Step 2D.7: Display Success

```
## Auto-Consultant Created

| Field | Value |
|-------|-------|
| ID | [consultant-id] |
| Name | [Display Name] |
| Type | auto-consultant |
| Department | [department] |
| Status | available |

### Skills
- [skill1]
- [skill2]
- [skill3]

### Files Created

| File | Purpose |
|------|---------|
| .claude/agents/[consultant-id].md | Agent definition |
| $COMPANY_DIR/employees/[consultant-id]/memory.md | Working memory |

### Metadata for SubagentStop Hook

When spawning this consultant, include in Task metadata:
```json
{
  "is_auto_consultant": true,
  "consultant_id": "[consultant-id]"
}
```

This enables the SubagentStop hook to:
- Update consultant status
- Capture knowledge/learnings
- Track activation metrics

### Usage

**Direct invocation:**
"Use [consultant-id] to [task description]"

**Via Task tool:**
```
Task(
  subagent_type="general-purpose",
  description="[task]",
  metadata={
    "is_auto_consultant": true,
    "consultant_id": "[consultant-id]"
  }
)
```
Include context from: $COMPANY_DIR/employees/[consultant-id]/memory.md

### Lifecycle Commands
- `/agent --list` — View all consultants
- `/agent --archive [consultant-id]` — Archive when no longer needed
```

---

## Step 3: EPHEMERAL MODE (No Company)

When no company context exists, behave exactly like `/add-agent`.

### Step 3.1: Invoke Meta-Agent

Spawn the Meta-Agent:

```
Task(subagent_type="general-purpose", description="Generate new agent")
```

**Pass to Meta-Agent:**
- The user's description: $ARGUMENTS
- Instruction to read `.claude/agents/meta-agent.md` for full rules
- Instruction to read ALL existing agents in `.claude/agents/` first to maintain consistency

### Step 3.2: Verify and Display

Verify the generated agent file was written to `.claude/agents/`.

**Display:**
```
## Agent Created (Ephemeral Mode)

**Agent:** [name]
**File:** .claude/agents/[name].md
**Role:** [one-line description from Meta-Agent]
**Tools:** [list of tools the agent can use]

### Ephemeral Mode Notice

This agent was created without company tracking:
- No memory persistence across sessions
- No knowledge capture on completion
- No skill-based matching for reuse

**To enable full lifecycle management:**
1. Run `/company-init` to create company structure
2. Future agents will have persistent memory and tracking

### Usage

To use this agent:
"Use the [name] agent to [task description]"

Or reference it when spawning Task sub-agents for relevant work.
```

---

## Error Handling

### Invalid Arguments

```
## Invalid Arguments

Could not parse: $ARGUMENTS

**Valid formats:**
- /agent [description] — Create new agent
- /agent --list — List consultants
- /agent --activate [id] — Reactivate archived consultant
- /agent --archive [id] — Archive active consultant

**Examples:**
- /agent A GraphQL specialist for schema design
- /agent --activate graphql-specialist
```

### Meta-Agent Failure

```
## Agent Generation Failed

The Meta-Agent could not create an agent definition.

**Error:** [error details]

**Suggestions:**
1. Provide a more specific description
2. Verify `.claude/agents/meta-agent.md` exists
3. Check that existing agents in `.claude/agents/` are valid

**Try again with:**
/agent [more detailed description]
```

### Lifecycle Utility Errors

```
## Operation Failed

**Command:** [lifecycle command]
**Error:** [error message]

**Debugging:**
```bash
uv run .claude/hooks/company/consultant_lifecycle.py help
```

If the error persists, check:
- Company structure: $COMPANY_DIR/org.json
- Permissions on company directory
- Python environment: `uv --version`
```

---

## Rules

1. **Always check company context first.** The operating mode determines the entire flow.

2. **Prefer reuse over creation.** In company mode, always search for matching consultants before creating new ones.

3. **Preserve knowledge.** When reactivating archived consultants, their memory and learnings are restored.

4. **Use Meta-Agent for definitions.** Never hardcode agent definitions. The Meta-Agent ensures consistency.

5. **Register before spawning.** In company mode, always register the consultant via lifecycle utility before first use.

6. **Include metadata for hooks.** When spawning auto-consultants, include `is_auto_consultant` and `consultant_id` in metadata for the SubagentStop hook.

7. **Ephemeral mode is backward-compatible.** Without company context, behave exactly like `/add-agent`.

8. **Skill extraction is best-effort.** If no skills can be extracted, use "general" as a fallback.

9. **IDs must be unique.** If a consultant ID already exists, append a numeric suffix.

10. **Suggest next steps.** Always show the user how to use the created/activated consultant.

11. **Always inject memory context.** When spawning a consultant, always include their accumulated memory, learnings, and preferences in the subagent prompt.

---

## Step 4: Spawn Consultant with Memory Context Injection

This step handles the actual activation of a consultant with their full accumulated context. This is called after creation (Step 2D), activation (Step 2B), or when an existing match is used.

### Step 4.1: Load Full Consultant Context

Use the lifecycle utility to get complete consultant context:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py context --id "$CONSULTANT_ID"
```

This returns a JSON object with:
- `consultant`: Full consultant object from org.json
- `name`: Display name
- `capabilities`: Skills list
- `source_request`: Original creation request
- `activation_count`: Number of previous activations
- `last_active`: Last activation timestamp
- `hire_date`: Creation date
- `memory`: Object with `path`, `content`, and `exists` fields
- `learnings`: Object with `path`, `content`, and `exists` fields

### Step 4.2: Parse Memory Content for Context Building

Read the memory file and extract structured sections:

```python
import re

def parse_memory_sections(memory_content: str) -> dict:
    """Extract sections from memory.md content."""
    sections = {
        "assignment_history": "",
        "learnings": "",
        "preferences": "",
    }

    if not memory_content:
        return sections

    # Extract assignment history from "## Recent Interactions" or "## Reactivation"
    history_match = re.search(
        r'## Recent Interactions\s*\n(.*?)(?=\n## |\Z)',
        memory_content, re.DOTALL
    )
    if history_match:
        sections["assignment_history"] = history_match.group(1).strip()

    # Also capture reactivation notes
    reactivation_matches = re.findall(
        r'## Reactivation #\d+:.*?\n(.*?)(?=\n## |\n---|\Z)',
        memory_content, re.DOTALL
    )
    if reactivation_matches:
        reactivation_history = "\n\n".join(m.strip() for m in reactivation_matches)
        if sections["assignment_history"]:
            sections["assignment_history"] += "\n\n" + reactivation_history
        else:
            sections["assignment_history"] = reactivation_history

    # Extract preferences
    pref_match = re.search(
        r'## Preferences\s*\n(.*?)(?=\n## |\Z)',
        memory_content, re.DOTALL
    )
    if pref_match:
        sections["preferences"] = pref_match.group(1).strip()

    return sections
```

### Step 4.3: Parse Learnings File

If a separate learnings file exists at `$COMPANY_DIR/employees/$CONSULTANT_ID/learnings.md`:

```bash
cat "$COMPANY_DIR/employees/$CONSULTANT_ID/learnings.md" 2>/dev/null || echo ""
```

Parse learnings into a summary:

```python
def parse_learnings(learnings_content: str) -> str:
    """Extract learnings summary from learnings.md content."""
    if not learnings_content:
        return "No learnings recorded yet."

    # Extract bullet points or patterns
    learnings = []
    for line in learnings_content.split('\n'):
        line = line.strip()
        if line.startswith('- ') or line.startswith('* '):
            learnings.append(line)
        elif line.startswith('### '):
            # Include section headers
            learnings.append(f"\n{line}")

    return '\n'.join(learnings) if learnings else "No learnings recorded yet."
```

### Step 4.4: Build Memory-Injected Prompt

Construct the full prompt that will be passed to the subagent:

```markdown
You are being activated as consultant "[name]" ([id]).

## Your Accumulated Context

### Previous Work
[assignment_history OR "No previous assignments."]

### Learnings
[learnings OR "No learnings recorded yet."]

### Preferences
[preferences OR "No preferences established yet."]

## Current Request
[request]

## Instructions
1. Read your agent definition for capabilities and process
2. Apply your accumulated learnings to this task
3. Follow established preferences where applicable
4. Document new learnings discovered during this work

Your agent definition is at: [agent_def_path]
Your memory file is at: [memory_path]
```

**Template Variables:**
- `[name]`: Consultant display name from context
- `[id]`: Consultant ID
- `[assignment_history]`: Parsed from memory, or "No previous assignments."
- `[learnings]`: Parsed from learnings file, or "No learnings recorded yet."
- `[preferences]`: Parsed from memory preferences section, or "No preferences established yet."
- `[request]`: The current task/request
- `[agent_def_path]`: Path to agent definition (see Step 4.5)
- `[memory_path]`: Path to consultant memory file

### Step 4.5: Determine Agent Definition Path

Locate the agent definition file for this consultant:

```bash
# Check for consultant-specific agent definition
if [ -f ".claude/agents/company/$CONSULTANT_ID.md" ]; then
  AGENT_DEF_PATH=".claude/agents/company/$CONSULTANT_ID.md"
# Check for agent in main agents directory
elif [ -f ".claude/agents/$CONSULTANT_ID.md" ]; then
  AGENT_DEF_PATH=".claude/agents/$CONSULTANT_ID.md"
# Fallback to implementer
else
  AGENT_DEF_PATH=".claude/agents/implementer.md"
fi
```

### Step 4.6: Update Consultant Status to Busy

Mark the consultant as busy before spawning:

```bash
uv run .claude/hooks/company/consultant_lifecycle.py status \
  --id "$CONSULTANT_ID" \
  --status busy \
  --context "Activated for: $REQUEST"
```

### Step 4.7: Display Pre-Spawn Summary

```
=====================================================================
 CONSULTANT ACTIVATION                                      [spawning]
=====================================================================

### Consultant Profile

| Field | Value |
|-------|-------|
| ID | [consultant-id] |
| Name | [consultant-name] |
| Type | auto-consultant |
| Department | [department] |
| Activation # | [count + 1] |
| Previous Activations | [count] |
| Skills | [skill1, skill2, ...] |

### Memory Context Injected

| Context | Content |
|---------|---------|
| Previous Assignments | [summary or "None - first activation"] |
| Learnings | [count] items injected |
| Preferences | [count] preferences applied |

### Current Request

[request text]

### Agent Definition

Reading from: [agent_def_path]

=====================================================================

Spawning consultant subagent with accumulated context...
```

### Step 4.8: Spawn the Subagent

Spawn the consultant as a subagent with the memory-injected prompt:

```
Task(
  subagent_type="general-purpose",
  description="Consultant activation: [consultant-name] - [request summary]",
  metadata={
    "is_auto_consultant": true,
    "consultant_id": "[consultant-id]",
    "activation_number": [count + 1],
    "memory_path": "[memory_path]"
  }
)
```

**Pass to the subagent:**
1. The complete memory-injected prompt from Step 4.4
2. The current request
3. Instructions to read the agent definition file
4. Instructions to:
   - Apply learnings from accumulated context
   - Follow established preferences
   - Document new learnings discovered during work
   - Update memory with interaction summary when complete

### Step 4.9: Post-Activation Handling (SubagentStop Hook)

When the subagent completes, the `SubagentStop` hook will automatically:

1. **Update consultant status** back to "available":
   ```bash
   uv run .claude/hooks/company/consultant_lifecycle.py status \
     --id "[consultant-id]" \
     --status available \
     --context "Completed activation #[count]"
   ```

2. **Extract new learnings** from the work performed using `knowledge_capture`:
   ```bash
   uv run .claude/hooks/company/knowledge_capture.py extract-from-session \
     --agent-id "[consultant-id]"
   ```

3. **Append interaction summary** to memory file:
   ```bash
   uv run .claude/hooks/agent_memory.py append "[consultant-id]" \
     "### Activation #[count]: [timestamp]
     **Request:** [request]
     **Outcome:** [summary from subagent]
     **New Learnings:** [extracted learnings if any]"
   ```

4. **Update metrics**:
   - Increment `activationCount` in org.json
   - Update `lastActive` timestamp

---

## Example: Full Memory Context Injection Flow

### Scenario: Reactivating an Archived GraphQL Specialist

**User input:**
```
/agent --activate graphql-specialist
```

**Step 4.1 - Load Context:**
```json
{
  "consultant_id": "graphql-specialist",
  "name": "GraphQL Schema Specialist",
  "activation_count": 3,
  "memory": {
    "content": "## Recent Interactions\n\n### Activation #3\n**Request:** Add pagination to queries\n**Outcome:** Implemented cursor-based pagination\n\n## Preferences\n- Use DataLoader for N+1 prevention\n- Prefer explicit nullable types\n",
    "exists": true
  },
  "learnings": {
    "content": "- Always add index hints for polymorphic relations\n- Use connection pattern for lists over 20 items\n- Prefer input types over raw arguments\n",
    "exists": true
  }
}
```

**Step 4.4 - Built Prompt:**
```markdown
You are being activated as consultant "GraphQL Schema Specialist" (graphql-specialist).

## Your Accumulated Context

### Previous Work
### Activation #3
**Request:** Add pagination to queries
**Outcome:** Implemented cursor-based pagination

### Learnings
- Always add index hints for polymorphic relations
- Use connection pattern for lists over 20 items
- Prefer input types over raw arguments

### Preferences
- Use DataLoader for N+1 prevention
- Prefer explicit nullable types

## Current Request
Help with subscription schema design for real-time updates

## Instructions
1. Read your agent definition for capabilities and process
2. Apply your accumulated learnings to this task
3. Follow established preferences where applicable
4. Document new learnings discovered during this work

Your agent definition is at: .claude/agents/company/graphql-specialist.md
Your memory file is at: .company/employees/graphql-specialist/memory.md
```

**Step 4.8 - Spawn:**
The subagent receives this full context and can leverage the consultant's accumulated knowledge about GraphQL patterns, preferences for DataLoader usage, and past work on pagination.

---

## Fresh Start Context for New Consultants

When a consultant is newly created (no activation history), provide appropriate first-activation context:

```markdown
You are being activated as consultant "[name]" ([id]).

## Your Accumulated Context

### Previous Work
No previous assignments. This is your first activation.

### Learnings
No learnings recorded yet. Document useful patterns you discover.

### Preferences
No preferences established yet. Develop and record preferences as you work.

## Current Request
[request]

## Instructions
1. Read your agent definition for capabilities and process
2. This is your first activation - establish good patterns
3. Document learnings and preferences for future activations
4. Update your memory with useful context for future work

Your agent definition is at: [agent_def_path]
Your memory file is at: [memory_path]
```

This ensures new consultants start with appropriate expectations and instructions to begin building their knowledge base.
