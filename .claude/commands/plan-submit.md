# /plan-submit — Submit Plan for CEO Review

Submit a planning document for CEO review and approval. Plans requiring board review will automatically schedule a board session.

## Input
<args/>

Usage:
- `/plan-submit "P21: Feature X" --type roadmap_phase`
- `/plan-submit --file .planning/P21-PROPOSAL.md`
- `/plan-submit "Vision Update Q2" --type vision_change --size large`

## Step 1: Parse Arguments

Parse `<args/>` to extract:
- `title`: Plan title (quoted string or --title flag)
- `--type`: Plan type (roadmap_phase, goal, initiative, vision_change, reorg, bug_fix, documentation)
- `--size`: Plan size (small, medium, large, epic) - default: medium
- `--file`: Path to proposal document
- `--description`: Plan description
- `--alignment`: Strategic goals this aligns with (comma-separated, e.g., "G1,G3")

**If no title provided:**
```
## Usage

/plan-submit "<title>" [options]

Options:
  --type TYPE        Plan type (default: roadmap_phase)
                     Types: roadmap_phase, goal, initiative, vision_change, reorg, bug_fix, documentation
  --size SIZE        Plan size for approval routing (default: medium)
                     Sizes: small, medium, large, epic
  --file PATH        Path to proposal document
  --description TEXT Plan description
  --alignment GOALS  Strategic alignment (e.g., "G1,G3")

Examples:
  /plan-submit "P21: New Feature" --type roadmap_phase --size medium
  /plan-submit "Vision Q2 Update" --type vision_change --size large
  /plan-submit --file .planning/P21-PROPOSAL.md --type roadmap_phase
```
Exit.

## Step 2: Determine Submitter

Get current user context:
```bash
uv run .claude/hooks/company/company_resolver.py whoami 2>/dev/null || echo "unknown"
```

Default to "human" if unknown.

## Step 3: Submit Plan

```bash
uv run .claude/hooks/company/planning_authority.py submit \
  --title "<title>" \
  --type <type> \
  --proposed-by <submitter> \
  --size <size> \
  --description "<description>"
```

## Step 4: Display Result

```
════════════════════════════════════════════════════════════════════════════════
 PLAN SUBMITTED FOR CEO REVIEW                                      [success]
════════════════════════════════════════════════════════════════════════════════

### Plan Details

| Field | Value |
|-------|-------|
| Plan ID | <plan_id> |
| Title | <title> |
| Type | <type> |
| Size | <size> |
| Status | ceo_review |
| Proposed By | <submitter> |
| Proposed At | <timestamp> |

### Approval Routing

Based on plan size and type:
- [x] CEO Review Required
- [ ] Board Review Required (for large/epic or vision_change/reorg)

### Next Steps

1. Run `/plan-review` to have the CEO review this plan — no automated loop will pick it up (the executive loop is dormant since 2026-04-06, WS-121; `forge_daemon.py` logs `[DISCOVERY] Thread DISABLED` at startup)
2. Use `/plan-review <plan-id> --approve|--revise|--reject` to decide
3. If approved, plan moves to implementation

════════════════════════════════════════════════════════════════════════════════
```

## Rules

1. **Always validate required fields.** Title and type are required.
2. **Auto-detect size from type.** vision_change and reorg default to "large".
3. **Record strategic alignment.** Link plans to goals when specified.
4. **Notify CEO.** Plan appears in CEO's pending review queue.
