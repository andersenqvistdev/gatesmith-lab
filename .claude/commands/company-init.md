# /company-init — Initialize Company Directory Structure

Initialize the `.company/` directory with organizational configuration, knowledge base, and agent memory structure.

This command creates a **single-project company** (v1.1 behavior). For multi-project setups where employees work across multiple projects, use `/company-create` instead.

## Input
$ARGUMENTS

Optional arguments:
- `--departments=eng,product,design` — Custom department selection (default: engineering, product, design)
- `--force` — Reinitialize even if already exists (WARNING: overwrites existing config)
- `--upgrade` — Migrate existing single-project company to multi-project structure

## Step 0: Check Multi-Project Company Context

**First**, check if already inside a multi-project company by searching upward for `.forge-company-root`:

```bash
# Using company_resolver.py to find company root
uv run .claude/hooks/company/company_resolver.py find 2>/dev/null || echo "NO_ROOT_FOUND"
```

**If inside a multi-project company:**
```
## Already Inside Multi-Project Company

Found company root at: [path to root]
Company name: [company name from marker]
Mode: multi-project

This directory is already part of a multi-project company structure.

**To register this directory as a project:**
  /company-add-project .

**To view company status:**
  /company-status

**Note:** Single-project initialization (`/company-init`) is not available
inside a multi-project company. Projects join the company via
`/company-add-project` and share company-level employees and resources.
```

Exit without changes.

## Step 0.1: Check Existing Local State

Check if `.company/` already exists locally:

```bash
ls -la .company/ 2>/dev/null
```

**If exists and no `--force` flag:**
```
## Company Already Initialized

The .company/ directory already exists at this location.

| File | Status |
|------|--------|
| manifest.json | [exists/missing] |
| org.json | [exists/missing] |
| config.json | [exists/missing] |
| knowledge/ | [exists/missing] |
| agents/ | [exists/missing] |

To reinitialize (WARNING: may overwrite config), run:
  /company-init --force

To view current configuration:
  /company-status

To upgrade to multi-project company:
  /company-init --upgrade
```

Exit without changes.

## Step 1: Parse Arguments

Parse `$ARGUMENTS` for options:
- Extract `--departments` list or use default: `["engineering", "product", "design"]`
- Check for `--force` flag
- Check for `--upgrade` flag

## Step 1.1: Handle --upgrade Flag

**If `--upgrade` flag is present:**

Check if `.company/` exists locally with v1.1 structure:

```bash
ls -la .company/config.json 2>/dev/null
```

**If no existing company:**
```
## No Company to Upgrade

The --upgrade flag requires an existing v1.1 single-project company.

No .company/ directory found at this location.

**To create a new multi-project company:**
  /company-create

**To create a single-project company first:**
  /company-init
```

Exit without changes.

**If existing company:**

1. Read existing config to verify it's v1.1 (single-project):

```bash
cat .company/config.json 2>/dev/null
```

If `mode` field is already "multi-project", inform user:
```
## Already Multi-Project

This company is already in multi-project mode.

**To view company status:**
  /company-status
```

Exit without changes.

2. If v1.1 single-project structure detected, proceed with upgrade:
```
## Upgrading to Multi-Project Company

Current mode: single-project (v1.1)
Target mode: multi-project (v1.2)

This will:
1. Create .forge-company-root marker file at current location
2. Rename .company/agents/ to .company/employees/
3. Create .company/assignments/ directory
4. Update config.json with mode: "multi-project"
5. Update org.json schema for multi-project

**Backup location:** .company/.backup-v1.1/

Proceed with upgrade? (This action is reversible via backup)
```

If confirmed, delegate to the migration script:
```bash
uv run .claude/tools/migrate_company_v1.2.py --backup-dir .company/.backup-v1.1
```

Display upgrade summary and next steps:
```
## Upgrade Complete

Company upgraded from v1.1 (single-project) to v1.2 (multi-project).

### Changes Made
| Item | Action |
|------|--------|
| .forge-company-root | Created (company root marker) |
| .company/agents/ | Renamed to .company/employees/ |
| .company/assignments/ | Created |
| .company/config.json | Updated (mode: "multi-project") |
| .company/org.json | Updated (v1.2 schema) |
| .company/.backup-v1.1/ | Created (backup of v1.1 state) |

### Next Steps

1. **Add projects to the company:**
   Projects in other directories can now join this company:
   ```bash
   cd /path/to/other/project
   /company-add-project .
   ```

2. **View upgraded company:**
   ```bash
   /company-status
   ```

3. **If issues occur, rollback:**
   ```bash
   /company-upgrade --rollback
   ```
```

Exit after upgrade.

## Step 2: Create Directory Structure

Create the full directory tree:

```
.company/
├── manifest.json          # Extension manifest
├── org.json               # Organization structure
├── config.json            # Runtime configuration
├── knowledge/             # Shared knowledge base
│   ├── README.md          # Knowledge base guide
│   ├── decisions.md       # Architecture Decision Records
│   └── patterns.md        # Implementation patterns
└── agents/                # Agent memory directories
    └── TEMPLATE/          # Template for new agent memory
        ├── memory.md      # Working memory template
        └── learnings.md   # Long-term learnings template
```

## Step 2.5: Select Claude Subscription Tier (WS-040)

Before creating config files, ask the user's Claude subscription to set the optimal model profile for the daemon.

**Display:**
```
### Claude Subscription

Gatesmith uses different AI models based on task complexity.
Your subscription determines which models are available.

What's your Claude subscription?

  1. Max $200/month  → Best quality (Opus for executives + epic tasks)
  2. Max $100/month  → Great quality (Sonnet for complex work)
  3. Pro $20/month   → Good quality (same models as Max $100)
```

**Use AskUserQuestion** to get the user's choice.

**Map choice to profile name:**
- 1 → `max-200`
- 2 → `max-100`
- 3 → `pro`
- Default (if skipped) → `max-100`

Store the selected profile for use in Step 3 when writing gatesmith.json.

After creating the company config files in Step 3, also write the model profile to `gatesmith.json` at the project root:

```json
{
  "modelProfile": "[selected-profile]",
  "modelProfiles": {
    "profiles": {
      "max-200": { "executive": "claude-opus-4-8", "trivial": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "complex": "claude-sonnet-5", "epic": "claude-opus-4-8" },
      "max-100": { "executive": "claude-sonnet-5", "trivial": "claude-haiku-4-5-20251001", "standard": "claude-haiku-4-5-20251001", "complex": "claude-sonnet-5", "epic": "claude-sonnet-5" },
      "pro": { "executive": "claude-sonnet-5", "trivial": "claude-haiku-4-5-20251001", "standard": "claude-haiku-4-5-20251001", "complex": "claude-sonnet-5", "epic": "claude-sonnet-5" }
    }
  }
}
```

If `gatesmith.json` already exists, merge the model profile keys without overwriting other config.

## Step 3: Create Core Configuration Files

### manifest.json

```json
{
  "name": "company",
  "version": "0.1.0",
  "description": "Company-specific extension for Gatesmith providing organizational patterns, team configurations, and enterprise customizations.",
  "forgeVersion": ">=1.0.0",
  "features": [
    "team-configs",
    "org-templates",
    "custom-hooks",
    "enterprise-auth"
  ]
}
```

### config.json

```json
{
  "enabledDepartments": ["engineering", "product", "design"],
  "workAllocationMode": "pull",
  "escalation": {
    "tier1Timeout": 15,
    "tier2Timeout": 30,
    "tier3Timeout": 60,
    "tier4Timeout": 120
  },
  "agents": {
    "maxConcurrentTasks": 2,
    "maxConcurrentAgents": 10,
    "autoArchiveConsultants": true,
    "consultantIdleTimeout": 24
  },
  "memory": {
    "maxLinesPerFile": 1000,
    "archiveRetentionDays": 30
  },
  "metrics": {
    "rollingWindowDays": 7,
    "enabled": true
  }
}
```

Update `enabledDepartments` if custom departments specified via `--departments`.

### org.json

Generate based on enabled departments. Each department gets:
- Unique ID (lowercase, hyphenated)
- Display name
- Default teams (varies by department type)
- Empty head/lead/members (to be filled by hiring)

Standard department templates:

**engineering:**
- Teams: core, integrations, devops

**product:**
- Teams: product-strategy, user-research

**design:**
- Teams: ux, visual

**Custom departments:** Create with single "general" team.

## Step 4: Create Knowledge Base

### knowledge/README.md

Create the knowledge base guide explaining:
- Purpose of the knowledge base
- How agents contribute
- How to query existing knowledge
- File structure reference

### knowledge/decisions.md

Initialize with ADR template and ADR-0001 (Use ADR Format).

### knowledge/patterns.md

Initialize with pattern template and core patterns:
- Builder-Validator Loop
- Atomic Commits

## Step 5: Create Agent Memory Templates

### agents/TEMPLATE/memory.md

Working memory template with sections:
- Current Context
- Active Assignments
- Recent Interactions
- Preferences
- Scratchpad

### agents/TEMPLATE/learnings.md

Long-term learnings template with sections:
- Mistakes & Lessons
- Successful Patterns
- Domain Expertise
- Collaboration Notes
- Meta-Learnings
- Knowledge Gaps

## Step 6: Create Department Agent Directories

For each enabled department, create memory directories for department agents:

```bash
mkdir -p .company/agents/{department-id}
```

Copy TEMPLATE files to create initial memory:
- `.company/agents/{department-id}/memory.md`
- `.company/agents/{department-id}/learnings.md`

## Step 7: Display Summary

```
## Company Initialized

═══════════════════════════════════════════════════════════════
 SINGLE-PROJECT COMPANY                              [created]
═══════════════════════════════════════════════════════════════
 Mode: single-project (v1.1)
═══════════════════════════════════════════════════════════════

### Configuration Files
| File | Status | Description |
|------|--------|-------------|
| manifest.json | created | Extension manifest |
| org.json | created | Organization structure |
| config.json | created | Runtime configuration |

### Knowledge Base
| File | Status | Description |
|------|--------|-------------|
| knowledge/README.md | created | Knowledge base guide |
| knowledge/decisions.md | created | Architecture decisions |
| knowledge/patterns.md | created | Implementation patterns |

### Enabled Departments
| Department | Teams | Agent Memory |
|------------|-------|--------------|
| Engineering | core, integrations, devops | .company/agents/engineering/ |
| Product | product-strategy, user-research | .company/agents/product/ |
| Design | ux, visual | .company/agents/design/ |

### Agent Templates
| Template | Purpose |
|----------|---------|
| agents/TEMPLATE/memory.md | Working memory for agents |
| agents/TEMPLATE/learnings.md | Long-term learnings storage |

═══════════════════════════════════════════════════════════════

### Next Steps

1. **Customize configuration:**
   Edit `.company/config.json` to adjust timeouts, limits, and features.

2. **Review organization:**
   Edit `.company/org.json` to customize departments and teams.

3. **Initialize agents:**
   Use `/company-hire` to create agents for each role.

4. **Start work:**
   Use `/company-assign` to delegate work to the organization.

### Available Commands
- `/company-status` — View current organization state
- `/company-hire` — Create new agents
- `/company-assign` — Assign work to agents
- `/company-standup` — Run organization standup

### Multi-Project Upgrade

This is a single-project company. If you need agents to work across
multiple projects with shared knowledge, you can upgrade:

  /company-init --upgrade

Or create a new multi-project company root in a parent directory:

  cd ..
  /company-create
```

## Rules

- **Never overwrite without --force.** Existing config represents customization that should be preserved.
- **Validate department names.** Must be lowercase, alphanumeric with hyphens only.
- **Create all directories atomically.** Either all succeed or none (rollback on failure).
- **Use existing templates as source of truth.** If `.company/` already exists partially, use existing files as templates for missing ones.
- **Never init inside multi-project company.** If `.forge-company-root` found upward, refuse and suggest `/company-add-project` instead.
- **Preserve v1.1 behavior by default.** Users without multi-project needs should see no change.
