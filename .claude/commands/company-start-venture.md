# /company-start-venture — Create New Venture

Create a new venture/project under the company. This command enables autonomous project creation based on market opportunities, strategic decisions, or direct requests.

## Input
$ARGUMENTS

**Arguments:**
- `<name>` — Name of the new venture (required)
- `--template=<template>` — Project template to use (default: minimal)
- `--description=<text>` — Venture description
- `--budget=<amount>` — Initial budget allocation
- `--opportunity=<id>` — Link to market opportunity that inspired this venture
- `--assign=<employee-id>` — Assign employees (can repeat)
- `--dry-run` — Preview without creating

**Templates:**
- `minimal` — Basic structure (.planning/, .claude/, CLAUDE.md)
- `python-lib` — Python library (pyproject.toml, src/, tests/)
- `python-cli` — Python CLI tool
- `node-api` — Node.js API service
- `node-frontend` — React frontend

## Step 0: Validate Environment

### 0.1: Check Company Mode

```bash
uv run .claude/hooks/company/company_resolver.py mode
```

**If not company mode:**
```
## Company Mode Required

This command requires company mode. Initialize with:
  /company-init

Or create a multi-project company with:
  /company-create
```

Exit without changes.

### 0.2: Load Context

Read:
- `.company/org.json` — Available employees
- `.company/venture_state.json` — Existing ventures and opportunities

## Step 1: Parse Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| name | (required) | Venture name |
| template | minimal | Project template |
| description | "" | Venture description |
| budget | 0.0 | Initial budget |
| opportunity | null | Opportunity ID |
| assign | [] | Employee IDs to assign |
| dry-run | false | Preview mode |

## Step 2: Validate Inputs

### 2.1: Check Name

- Name must be non-empty
- Generate ID from name (lowercase, hyphens)
- Check ID doesn't conflict with existing ventures

### 2.2: Check Employees

For each `--assign` employee:
- Verify employee exists in org.json
- Verify employee status is available
- Warn if employee has 3+ project assignments

### 2.3: Check Opportunity

If `--opportunity` specified:
- Verify opportunity exists in venture_state.json
- Link venture to opportunity

## Step 2.5: Venture Kickoff (REQUIRED — do not skip)

**Purpose**: Capture the customer's venture scope before engineering starts. This prevents ambiguous / regulated-but-unflagged ventures from silently landing in the work queue.

**Behavior**: Ask the customer (or the human operator running this command on a customer's behalf) the five questions below. If **any** answer is missing, REFUSE to proceed — venture creation is blocked until scope is captured.

### 2.5.1: Ask five scoping questions

1. **Vertical & regulatory status** — What industry/domain does this venture serve?
   - Options: `trading`, `betting`, `fintech`, `healthcare`, `consumer-finance`, `ecommerce`, `content`, `saas-general`, `other`
   - Is this a regulated domain? If yes, list the frameworks: SEC/CFTC/FINRA/FCA/BitLicense/MiCA (finance); state gaming commissions/UKGC/MGA (betting); HIPAA (healthcare); GDPR/CCPA (data); etc.

2. **Tech stack preference** — Required languages (Python/Node/Go/Rust), frameworks, hosting target, database?

3. **Timeline & budget** — When does the customer expect v1 shippable? Budget envelope?

4. **Integration needs** — Third-party services required: payment (Stripe/Adyen), KYC (Onfido/Persona), market data (Polygon/IEX), SMS (Twilio), auth (Auth0), etc.?

5. **Acceptance criteria** — What specifically does "done" mean for v1? (concrete features, user flows, or metrics — not "make it good")

### 2.5.2: Persist scope

Write answers to `<venture-root>/.company/venture-scope.json`:

```json
{
  "vertical": "trading|betting|fintech|healthcare|consumer-finance|ecommerce|content|saas-general|other",
  "regulated": true,
  "regulatory_frameworks": ["SEC", "KYC/AML"],
  "tech_stack": {
    "language": "python",
    "framework": "fastapi",
    "hosting": "aws",
    "database": "postgres"
  },
  "timeline_weeks": 8,
  "budget_usd": 25000,
  "integrations": ["stripe", "onfido"],
  "acceptance_criteria": "User can register → pass KYC → fund account → execute spot trade on supported pairs",
  "captured_at": "2026-04-19T10:00:00Z",
  "captured_by": "<human-operator-or-customer-license>"
}
```

### 2.5.3: Route regulated ventures to Legal & Compliance Officer

If `regulated: true`:
- Flag the venture with `requires_compliance_review: true` in `venture_state.json`
- Route the kickoff scope to the `legal-compliance-officer` agent for an initial compliance-gate report BEFORE engineering starts
- Block PR merges on ventures tagged `requires_compliance_review: true` until a compliance report exists

### 2.5.4: Failure mode

If the customer cannot answer a question (e.g. "not sure what framework yet"), that is a valid answer — record it verbatim. But REFUSE to proceed if they provide no answer at all. Ambiguity is the caller's choice to make explicit; silence is not.

## Step 3: Preview (if --dry-run)

```
## Venture Preview (Dry Run)

═══════════════════════════════════════════════════════════════
 NEW VENTURE                                     [dry-run]
═══════════════════════════════════════════════════════════════

### Configuration

| Setting | Value |
|---------|-------|
| Name | <name> |
| ID | <generated-id> |
| Template | <template> |
| Description | <description or "none"> |
| Budget | $<budget> |
| Opportunity | <opportunity-id or "none"> |

### Path

Will create: <company-root>/projects/<venture-id>/

### Structure

<venture-id>/
├── .planning/
│   └── PROJECT.md
├── .claude/
└── CLAUDE.md

### Employee Assignments

| Employee | Current Projects | Status |
|----------|------------------|--------|
| <employee-id> | <count> | ready |

### Actions

No changes made (dry-run). Run without --dry-run to create.

═══════════════════════════════════════════════════════════════
```

Exit without changes.

## Step 4: Create Venture

### 4.1: Run Creation

```bash
uv run .claude/hooks/company/p17_features.py create-venture \
  --name "<name>" \
  --description "<description>" \
  --template "<template>" \
  --budget <budget>
```

### 4.2: Check Result

If creation failed, show error and exit.

### 4.3: Allocate Resources (if employees specified)

```bash
uv run .claude/hooks/company/p17_features.py allocate-resources \
  --venture "<venture-id>" \
  --employees "<employee-id-1>,<employee-id-2>" \
  --budget <budget>
```

### 4.4: Link Opportunity (if specified)

Update opportunity status in venture_state.json from "open" to "pursued".

## Step 5: Display Summary

```
## Venture Created

═══════════════════════════════════════════════════════════════
 NEW VENTURE                                     [created]
═══════════════════════════════════════════════════════════════

### <name>

| Setting | Value |
|---------|-------|
| ID | <venture-id> |
| Status | proposed |
| Path | <path> |
| Template | <template> |
| Budget | $<budget> |

### Structure Created

<path>/
├── .planning/
│   └── PROJECT.md
├── .claude/
└── CLAUDE.md

### Employees Assigned (<count>)

| Employee | Role | Status |
|----------|------|--------|
| <employee-id> | contributor | assigned |

### Next Steps

1. Navigate to the venture:
   cd <path>

2. Initialize planning:
   /new-project

3. Start development:
   /build

4. Monitor health:
   /company-health --project <venture-id>

### Related Commands

- `/company-projects` — List all projects
- `/company-route` — Route tasks to this project
- `/company-assign` — Assign more employees

═══════════════════════════════════════════════════════════════
```

## Rules

- **Company mode required.** Must be in company mode.
- **Unique IDs.** Venture ID must not conflict with existing ventures.
- **Template validation.** Template must exist in .company/templates/projects/.
- **Atomic creation.** Venture record and directory created atomically.
- **Opportunity linkage.** If opportunity specified, update its status.
- **Employee limits.** Warn if employee exceeds 3 project assignments.
- **Dry-run safety.** --dry-run never creates files or modifies state.
- **Integration with P17.** Uses p17_features.py for actual creation.
