# /company-bootstrap — Intelligent Company Bootstrapping

Bootstrap a complete company structure with a single command. Instead of manually calling `/company-init`, then `/company-hire` multiple times, this command analyzes your project goals and creates an optimized organizational structure automatically.

## Input
$ARGUMENTS

## Command Syntax

```
/company-bootstrap [description]              # Auto-detect domain, create org
/company-bootstrap --template [name]          # Use specific template
/company-bootstrap --discover                 # Research mode for uncertain projects
/company-bootstrap --list-templates           # Show available templates
/company-bootstrap --preview [description]    # Preview structure without creating
```

## Step 0: Check Existing State

First, check if a company already exists:

```bash
# Check for multi-project company
uv run .claude/hooks/company/company_resolver.py find 2>/dev/null || echo "NO_ROOT"

# Check for local company
ls -la .company/org.json 2>/dev/null && echo "LOCAL_COMPANY" || echo "NO_LOCAL"
```

**If company already exists:**
```
## Company Already Exists

A company structure is already initialized at this location.

**Location:** [path]
**Mode:** [single-project / multi-project]

To view current organization:
  /company-status

To hire additional employees:
  /company-hire [role description]

To reinitialize (WARNING: destructive):
  /company-bootstrap --force
```

Exit without changes.

---

## Step 1: Parse Arguments

Parse `$ARGUMENTS` to determine the operation:

| Pattern | Operation | Go to |
|---------|-----------|-------|
| `--list-templates` | LIST_TEMPLATES | Step 2A |
| `--discover` | DISCOVERY | Step 2B |
| `--template [name]` | USE_TEMPLATE | Step 2C |
| `--preview [desc]` | PREVIEW | Step 2D |
| `[description]` | AUTO_DETECT | Step 2E |
| (empty) | INTERACTIVE | Step 2F |

---

## Step 2A: List Templates

Display available organizational templates:

```bash
uv run .claude/hooks/company/org_templates.py list
```

**Display:**
```
## Available Organization Templates

═══════════════════════════════════════════════════════════════
 TEMPLATES                                         [8 available]
═══════════════════════════════════════════════════════════════

| Template | Description | Departments | Core Roles |
|----------|-------------|-------------|------------|
| saas_platform | SaaS product with product + engineering focus | engineering, product, design | 4 |
| ecommerce | E-commerce with payments + marketing | engineering, product, design, marketing | 5 |
| mobile_app | Mobile-first with UX focus | engineering, product, design | 4 |
| content_platform | Content/media with editorial | engineering, product, design, content | 4 |
| api_service | Developer tools with docs focus | engineering, product, developer-relations | 4 |
| data_platform | Data/ML with analytics | engineering, data-science, product | 4 |
| agency | Flexible consulting/agency | engineering, design, project-management | 4 |
| minimal | Basic engineering-only | engineering | 1 |

═══════════════════════════════════════════════════════════════

### Usage

Use a specific template:
  /company-bootstrap --template saas_platform

Preview a template:
  /company-bootstrap --preview "my project" --template ecommerce

Let us detect the best template:
  /company-bootstrap "Building a subscription analytics dashboard"
```

---

## Step 2B: Discovery Mode

For users who don't know what to build yet. Spawn a Research Agent to explore.

**Display:**
```
## Discovery Mode

You've entered discovery mode. I'll help you explore your problem space
and recommend an organizational structure.

Starting research process...
```

**Spawn Research Agent:**
```
Task(subagent_type="general-purpose", description="Research problem space for company bootstrap")
```

**Pass to Research Agent:**

Read `.claude/agents/company/research-agent.md` for instructions.

Your task is to help the user discover what they want to build and recommend
an appropriate organizational structure.

Process:
1. Ask clarifying questions about:
   - What problem are they solving?
   - Who are the target users?
   - What's the business model?
   - What are the technical constraints?
   - What's the timeline/budget?

2. Research similar solutions if helpful

3. Generate a discovery report with:
   - Problem statement summary
   - Key requirements identified
   - Technical stack recommendations
   - Recommended template from org_templates.py
   - Suggested modifications to the template

4. Present recommendations for user confirmation

**After Research Agent completes:**

Parse the recommendations and proceed to Step 3 with the recommended template.

---

## Step 2C: Use Specific Template

User specified `--template [name]`.

Validate template exists:

```bash
uv run .claude/hooks/company/org_templates.py get [name]
```

**If not found:**
```
## Template Not Found

The template "[name]" does not exist.

Available templates:
[list from org_templates.py list]

Did you mean one of these?
- [fuzzy match suggestions]
```

**If found:**
Store template and proceed to Step 3.

---

## Step 2D: Preview Mode

User wants to see what would be created without actually creating it.

1. Parse description (if provided)
2. Detect or use specified template
3. Generate preview of structure
4. Display without creating

**Display:**
```
## Bootstrap Preview

═══════════════════════════════════════════════════════════════
 PREVIEW                                          [not created]
═══════════════════════════════════════════════════════════════

### Detected Domain
- **Description:** [user description]
- **Matched Template:** [template_name]
- **Confidence:** [high/medium/low based on keyword matches]

### Proposed Structure

**Departments:**
[table of departments and teams]

**Core Roles (would be hired):**
[table of roles]

**Config Settings:**
[key config values]

═══════════════════════════════════════════════════════════════

To create this structure:
  /company-bootstrap --template [template_name]

To modify:
  /company-bootstrap --template [template_name] --departments eng,product
```

---

## Step 2E: Auto-Detect Domain

User provided a description. Detect the best template.

```bash
uv run .claude/hooks/company/org_templates.py detect "[description]"
```

Parse the result to get template name.

**Display for confirmation:**
```
## Domain Detected

Based on your description:
> "[description]"

**Recommended Template:** [template_name] - [template description]

### Proposed Structure

**Departments:** [list]

**Core Roles:**
| Role | Department | Skills |
|------|------------|--------|
| [role name] | [dept] | [skills] |
...

═══════════════════════════════════════════════════════════════

Does this look right?

1. **Yes, create this structure** (proceed)
2. **Use a different template** → /company-bootstrap --list-templates
3. **I need help deciding** → /company-bootstrap --discover
```

If user confirms, proceed to Step 3.

---

## Step 2F: Interactive Mode (No Arguments)

No arguments provided. Guide the user.

```
## Company Bootstrap

═══════════════════════════════════════════════════════════════
 INTELLIGENT COMPANY SETUP
═══════════════════════════════════════════════════════════════

Tell me about your project, and I'll create the perfect
organizational structure for it.

### Quick Start Options

1. **Describe your project:**
   /company-bootstrap "Building a SaaS analytics dashboard"

2. **Use a specific template:**
   /company-bootstrap --template saas_platform

3. **I don't know what to build yet:**
   /company-bootstrap --discover

4. **See all templates:**
   /company-bootstrap --list-templates

### Common Templates

| If you're building... | Use... |
|-----------------------|--------|
| SaaS/subscription product | `--template saas_platform` |
| Online store | `--template ecommerce` |
| Mobile app | `--template mobile_app` |
| API/developer tools | `--template api_service` |
| Data/ML platform | `--template data_platform` |
| Something simple | `--template minimal` |

═══════════════════════════════════════════════════════════════
```

---

## Step 3: Create Company Structure

Now create the actual company with the selected template.

### Step 3.1: Determine Mode

Ask user about single-project vs multi-project:

```
### Company Mode

How will you use this company?

1. **Single Project** (recommended for most cases)
   - Company exists within this project
   - Simpler setup
   - Use: /company-init

2. **Multi-Project Company**
   - Company manages multiple projects
   - Shared employees across projects
   - Use: /company-create

[For most users, single-project is the right choice]
```

If unclear, default to single-project.

### Step 3.2: Select Claude Subscription Tier (WS-040)

Gatesmith uses the Claude CLI for all agent execution. Ask the user's subscription to set the optimal model profile.

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

**Map choice to profile:**
| Choice | Profile | Executive | Standard | Complex | Epic |
|--------|---------|-----------|----------|---------|------|
| 1 | `max-200` | Opus | Sonnet | Sonnet | Opus |
| 2 | `max-100` | Sonnet | Haiku | Sonnet | Sonnet |
| 3 | `pro` | Sonnet | Haiku | Sonnet | Sonnet |

**Store the selected profile** — it will be written to `gatesmith.json` in Step 3.5.

If user skips or is unsure, default to `max-100` (safe middle ground).

### Step 3.2b: Get Template Details

```bash
uv run .claude/hooks/company/org_templates.py get [template_name]
```

Parse the template to extract:
- `departments`: List for --departments flag
- `teams`: For org.json setup
- `core_roles`: For batch hiring
- `recommended_config`: For config.json

### Step 3.3: Create Company Directory

**For single-project:**
```bash
# Simulate /company-init with custom departments
mkdir -p .company/{knowledge,employees}
```

Then create the core files manually following `/company-init` structure but with template-specific departments.

**For multi-project:**
```bash
# Simulate /company-create with custom departments
mkdir -p .company/{knowledge,employees,assignments}
```

Create `.forge-company-root` marker and files following `/company-create` structure.

### Step 3.4: Create org.json with Template Structure

Build org.json with:
- Template departments and teams
- Empty employees array (will be filled by batch hire)

> **CRITICAL — employees[] schema.** Leave `"employees": []` empty here and let
> Step 3.6 (`batch_hire.py`) populate it. If you ever write employees directly,
> **each entry MUST be a full JSON object** with at least
> `{"id", "name", "status", "capabilities"}` — **never a bare ID string**.
> Bare-string entries (`"employees": ["cli-developer"]`) crash ~34 downstream
> consumers that read `emp.get(...)` with
> `"'str' object has no attribute 'get'"` (ProjectK K2). Step 3.7 normalizes as
> a safety net, but do not rely on it — write objects.

### Step 3.5: Apply Recommended Config & Model Profile

Merge template's `recommended_config` into config.json.

**Also write the model profile** selected in Step 3.2 to `gatesmith.json`:

```json
{
  "modelProfile": "[selected-profile]",
  "modelProfiles": {
    "profiles": {
      "max-200": {
        "executive": "claude-opus-4-8",
        "trivial": "claude-haiku-4-5-20251001",
        "standard": "claude-sonnet-5",
        "complex": "claude-sonnet-5",
        "epic": "claude-opus-4-8"
      },
      "max-100": {
        "executive": "claude-sonnet-5",
        "trivial": "claude-haiku-4-5-20251001",
        "standard": "claude-haiku-4-5-20251001",
        "complex": "claude-sonnet-5",
        "epic": "claude-sonnet-5"
      },
      "pro": {
        "executive": "claude-sonnet-5",
        "trivial": "claude-haiku-4-5-20251001",
        "standard": "claude-haiku-4-5-20251001",
        "complex": "claude-sonnet-5",
        "epic": "claude-sonnet-5"
      }
    }
  }
}
```

If `gatesmith.json` already exists, merge the `modelProfile` and `modelProfiles` keys. If it doesn't exist, create it with these keys plus the template's `recommended_config`.

Also remove any `employeeActivation.model` or `employeeActivation.modelByComplexity` keys that would override the profile.

### Step 3.6: Batch Hire Core Roles

Use batch_hire utility to create all core roles at once:

```bash
echo '[template.core_roles as JSON]' | uv run .claude/hooks/company/batch_hire.py hire -
```

Parse result to get hired employees.

### Step 3.7: Normalize org.json (deterministic safety net)

Run the canonical normalizer to guarantee every `employees[]` entry is a full
dict record before the daemon ever reads org.json. This coerces any
bare-string entries a direct write may have introduced (ProjectK K2
root-cause fix) and is a no-op when everything is already an object:

```bash
uv run .claude/hooks/company/company_resolver.py normalize-org
```

Expected output is either `Normalized N bare-string employee(s) …` or
`org.json already normalized (N employees)`. This must succeed before the
daemon is started.

---

## Step 4: Display Success Summary

```
## Company Bootstrapped Successfully

═══════════════════════════════════════════════════════════════
 [TEMPLATE_NAME] COMPANY                              [created]
═══════════════════════════════════════════════════════════════
 Mode: [single-project / multi-project]
 Template: [template_name]
═══════════════════════════════════════════════════════════════

### Organization Structure

**Departments Created:**
| Department | Teams |
|------------|-------|
| engineering | core, integrations, infrastructure |
| product | product-strategy, analytics |
| design | ux, visual |

### Employees Hired

| ID | Name | Department | Team | Skills |
|----|------|------------|------|--------|
| platform-architect | Platform Architect | engineering | core | architecture, scalability |
| full-stack-developer | Full-Stack Developer | engineering | core | frontend, backend |
| product-manager | Product Manager | product | product-strategy | product, strategy |
| ux-designer | UX Designer | design | ux | ux, wireframe |

**Total: [N] employees hired**

### Configuration

| Setting | Value |
|---------|-------|
| workAllocationMode | pull |
| maxConcurrentAgents | 10 |
| modelProfile | [selected-profile] |
| executives model | [executive model from profile] |
| employee models | [trivial/standard/complex/epic from profile] |

═══════════════════════════════════════════════════════════════

### Next Steps

1. **View your organization:**
   /company-status

2. **Start working:**
   /company-request "Build the authentication system"

3. **Add optional roles:**
[List optional roles from template with /company-hire commands]

4. **Create specialist agents on-demand:**
   /agent "A caching specialist for Redis optimization"

5. **Set up your product vision** (enables employee ideation):
   Create `.company/vision.md` with a goals table. Format:

   ```markdown
   ### Period: Q1 2026 [status: active]

   | G1: Goal Name | Goal description | Success metric | owner |
   | G2: Another Goal | What it means | How you measure it | owner |
   ```

   Without a parseable goal table under a `### Period: X [status: active]` header,
   ideation runs in degraded mode (generates 0 ideas) and logs a warning to stderr.

═══════════════════════════════════════════════════════════════
```

---

## Error Handling

### Invalid Template Name

```
## Invalid Template

The template "[name]" was not found.

**Available templates:**
[list]

**Suggestions:**
- Check spelling
- Use /company-bootstrap --list-templates to see all options
- Describe your project for auto-detection
```

### Batch Hire Failed

```
## Hiring Failed

Some employees could not be created.

**Errors:**
[list of errors from batch_hire]

**Partial state:**
Company structure was created but hiring failed.

**Recovery:**
1. Fix the issues above
2. Run: /company-hire [role] for each failed role
```

### File System Errors

```
## Creation Failed

Could not create company structure.

**Error:** [error details]

**Possible causes:**
- Insufficient permissions
- Disk full
- Path does not exist

**Try:**
- Check permissions on current directory
- Ensure parent directories exist
```

---

## Rules

1. **Always check for existing company first.** Don't overwrite without explicit --force.

2. **Prefer auto-detection over asking.** If user provides description, detect and confirm rather than asking them to choose.

3. **Atomic operations.** Either create everything successfully or roll back completely.

4. **Default to single-project.** Most users don't need multi-project complexity.

5. **Show what was created.** Users should see exactly what employees were hired with what skills.

6. **Suggest next steps.** Always end with actionable commands for the user.

7. **Include optional roles.** Show users what else they could add from the template.

8. **Support iteration.** If user doesn't like the result, make it easy to try again.

---

## Examples

### Example 1: Quick SaaS Bootstrap

```
User: /company-bootstrap "Building a B2B analytics SaaS"

Claude:
## Domain Detected

Based on your description:
> "Building a B2B analytics SaaS"

**Recommended Template:** saas_platform - SaaS product with product + engineering focus

### Proposed Structure

**Departments:** engineering, product, design

**Core Roles:**
| Role | Department | Skills |
|------|------------|--------|
| Platform Architect | engineering | architecture, scalability, api |
| Full-Stack Developer | engineering | frontend, backend, database |
| Product Manager | product | product, strategy, roadmap |
| UX Designer | design | ux, wireframe, prototype |

Proceeding with creation...

[Creates company and displays summary]
```

### Example 2: Template-Based Bootstrap

```
User: /company-bootstrap --template ecommerce

Claude:
## Using E-Commerce Template

Creating e-commerce company structure with:
- Departments: engineering, product, design, marketing
- Core roles: 5 employees

[Creates and displays summary]
```

### Example 3: Discovery Mode

```
User: /company-bootstrap --discover

Claude:
## Discovery Mode

Let me help you figure out what to build...

[Spawns research agent, gathers info, recommends template]
```
