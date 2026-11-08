# /pending-approvals — Show Documents Awaiting Approval

Display all documents requiring approval based on YAML frontmatter metadata.

## Input
$ARGUMENTS

## Command Syntax

```
/pending-approvals                  # Show all pending approvals
/pending-approvals --tier c_level   # Filter by approval tier
/pending-approvals --tier board     # Show only board-level approvals
/pending-approvals --scan           # Run scan before showing pending
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine mode:

| Pattern | Mode | Action |
|---------|------|--------|
| (empty) | SHOW | Show pending approvals |
| `--tier <tier>` | FILTER | Filter by tier (team_lead, c_level, board) |
| `--scan` | SCAN | Run document scan then show pending |

---

## Step 1: Run Document Scan (if requested)

If `--scan` flag is present, run the document approval scanner:

```bash
uv run .claude/hooks/company/document_approvals.py scan --json
```

Parse the JSON response:

```json
{
  "success": true,
  "discovered": [...],
  "count": 2
}
```

Report scan results before showing pending.

---

## Step 2: Get Pending Approvals

Get documents pending approval:

```bash
uv run .claude/hooks/company/document_approvals.py pending --json
```

Parse the JSON response:

```json
{
  "pending": [
    {
      "doc_id": "doc-q2-goals-abc123",
      "file_path": ".planning/Q2-GOALS.md",
      "title": "Q2 2026 Goals",
      "doc_type": "goals",
      "status": "proposed",
      "approval_tier": "c_level",
      "approval_required": ["forge-ceo", "forge-cto"],
      "author": "technical-writer",
      "proposed_at": "2026-02-13",
      "votes": [],
      "created_at": "2026-02-14T...",
      "updated_at": "2026-02-14T..."
    }
  ],
  "count": 1
}
```

---

## Step 3: Apply Filters

### 3.1: Filter by Tier

If `--tier` flag provided, filter documents:

| Tier | Documents Shown |
|------|-----------------|
| `team_lead` | Single-approver documents |
| `c_level` | Executive-level documents requiring all approvers |
| `board` | Board-level documents requiring quorum |

---

## Step 4: Render Output

### 4.1: Header

```
================================================================================
  DOCUMENTS AWAITING APPROVAL                              [document approvals]
================================================================================

Generated: YYYY-MM-DD HH:MM UTC
```

### 4.2: Summary Section

```
### Summary

| Tier | Count | Logic |
|------|-------|-------|
| C-Level | [count] | All listed must approve |
| Board | [count] | Majority quorum |
| Team Lead | [count] | Single approver |

Total pending: [total_count]
```

### 4.3: Document Details

For each pending document:

```
--------------------------------------------------------------------------------
[doc_id]: [title]
--------------------------------------------------------------------------------

**Type:** [doc_type]
**Status:** [status]
**Approval Tier:** [approval_tier]
**Author:** [author]
**Proposed:** [proposed_at]
**File:** [file_path]

### Approval Status

**Required Approvers:**
- [ ] forge-ceo
- [ ] forge-cto

**Logic:** [routing_logic based on tier]

**Votes Cast:** [votes_cast] / [votes_required]

### Actions

To approve:
  uv run .claude/hooks/company/document_approvals.py vote \
    --doc-id [doc_id] --approver [your-id] --decision approve

To reject:
  uv run .claude/hooks/company/document_approvals.py vote \
    --doc-id [doc_id] --approver [your-id] --decision reject --comments "reason"

To view full document:
  Read [file_path]

--------------------------------------------------------------------------------
```

### 4.4: Tier Logic Reference

```
================================================================================
  APPROVAL TIER REFERENCE
================================================================================

### team_lead
Single approver signs off. First vote determines outcome.

### c_level
ALL listed executives must approve. Any rejection fails the document.

### board
Majority quorum required (>50%). Board of 3 needs 2 approvals.

================================================================================
```

---

## Step 5: Empty State

If no documents pending:

```
================================================================================
  NO DOCUMENTS AWAITING APPROVAL                               [all clear]
================================================================================

No documents currently require approval.

### What This Means

- No documents with `status: proposed` found
- All discovered documents have been approved or rejected

### To Scan for New Documents

/pending-approvals --scan    # Run document scan

### To Create Documents Needing Approval

Add YAML frontmatter to your markdown files:

---
title: Document Title
type: goals
status: proposed
approval_tier: c_level
approval_required:
  - forge-ceo
  - forge-cto
author: your-id
proposed_at: 2026-02-14
---

================================================================================
```

---

## Step 6: Error Handling

### 6.1: Invalid Tier Filter

If `--tier` value is invalid:

```
## Invalid Tier Filter

The --tier flag accepts: team_lead, c_level, board

**Usage:**
  /pending-approvals --tier c_level   # C-level approvals
  /pending-approvals --tier board     # Board approvals
  /pending-approvals --tier team_lead # Team lead approvals

You provided: --tier [value]
```

### 6.2: Document Approvals Module Not Found

```
## Document Approvals Module Not Found

The document approvals script was not found at:
  .claude/hooks/company/document_approvals.py

This may indicate:
1. Gatesmith is not properly installed
2. P23 (Universal Document Approval System) is not implemented

**To fix:**
1. Verify Gatesmith installation
2. Check that .claude/hooks/company/ exists
3. Run `/prime` to reload project context
```

### 6.3: Company Not Initialized

```
## Company Not Initialized

The company structure has not been initialized.

Document approvals require:
- .company/ directory
- .company/document_approvals.json state file

**To initialize:**
/company-init       # Single-project company
/company-bootstrap  # Intelligent setup with templates
```

---

## Examples

### Example 1: Show All Pending Approvals

```
/pending-approvals

================================================================================
  DOCUMENTS AWAITING APPROVAL                              [document approvals]
================================================================================

Generated: 2026-02-14 15:30 UTC

### Summary

| Tier | Count | Logic |
|------|-------|-------|
| C-Level | 1 | All listed must approve |
| Board | 0 | Majority quorum |
| Team Lead | 0 | Single approver |

Total pending: 1

--------------------------------------------------------------------------------
[doc-q2-goals-abc123]: Q2 2026 Goals
--------------------------------------------------------------------------------

**Type:** goals
**Status:** proposed
**Approval Tier:** c_level
**Author:** technical-writer
**Proposed:** 2026-02-13
**File:** .planning/Q2-GOALS.md

### Approval Status

**Required Approvers:**
- [ ] forge-ceo
- [ ] forge-cto

**Logic:** ALL listed executives must approve

**Votes Cast:** 0 / 2

### Actions

To approve:
  uv run .claude/hooks/company/document_approvals.py vote \
    --doc-id doc-q2-goals-abc123 --approver forge-ceo --decision approve

To reject:
  uv run .claude/hooks/company/document_approvals.py vote \
    --doc-id doc-q2-goals-abc123 --approver forge-ceo --decision reject \
    --comments "Reason for rejection"

--------------------------------------------------------------------------------

================================================================================
  APPROVAL TIER REFERENCE
================================================================================

### team_lead
Single approver signs off. First vote determines outcome.

### c_level
ALL listed executives must approve. Any rejection fails the document.

### board
Majority quorum required (>50%). Board of 3 needs 2 approvals.

================================================================================
```

### Example 2: Filter by Tier

```
/pending-approvals --tier board

================================================================================
  DOCUMENTS AWAITING APPROVAL                              [document approvals]
================================================================================

Generated: 2026-02-14 15:30 UTC
Filter: board tier only

### Summary

| Tier | Count | Logic |
|------|-------|-------|
| Board | 0 | Majority quorum |

Total pending: 0

================================================================================
  NO BOARD-LEVEL DOCUMENTS AWAITING APPROVAL
================================================================================

No documents requiring board approval are currently pending.

================================================================================
```

### Example 3: Scan and Show

```
/pending-approvals --scan

Scanning for documents with approval metadata...

Found 1 document(s):

  [doc-q2-goals-abc123]
    Title: Q2 2026 Goals
    Path: .planning/Q2-GOALS.md
    Tier: c_level
    Required: forge-ceo, forge-cto

================================================================================
  DOCUMENTS AWAITING APPROVAL                              [document approvals]
================================================================================

[... rest of pending approvals output ...]
```

---

## Rules

1. **Always show tier logic.** Users need to understand how approval works for each document type.

2. **Include actionable commands.** Every pending document should have copy-paste vote commands.

3. **Show file paths.** Users need to know where to find the actual document.

4. **Display vote progress.** Show how many votes cast vs required.

5. **Respect filters.** When `--tier` is specified, only show that tier.

6. **Handle scan failures gracefully.** If scan fails, still try to show existing pending.

7. **Clear empty states.** When nothing is pending, explain what that means and how to create documents.

8. **Group by tier.** In summary, group documents by approval tier.

9. **Show proposed date.** Include when document was proposed for context.

10. **Link to reference.** Always include the tier logic reference at the end.
