# /submit — Submit Work Request to Input Channel

Submit a work request to the company's input channel for decomposition, allocation, and execution. This is the primary human-to-company interface for quick task submission.

## Input
$ARGUMENTS

## Command Syntax

```
/submit "Task description" [--priority N] [--department ID] [--deadline ISO]
/submit                     # Interactive mode
```

## Step 0: Parse Arguments

Parse `$ARGUMENTS` to determine the operation mode:

| Pattern | Mode | Go to |
|---------|------|-------|
| (empty) | INTERACTIVE | Step 1 |
| `"description" [flags]` | INLINE | Step 2 |
| `description [flags]` | INLINE | Step 2 |

**If empty arguments:** Go to Step 1 (Interactive Mode)
**If arguments provided:** Go to Step 2 (Inline Mode)

---

## Step 1: Interactive Mode

Prompt the user for required information:

```
================================================================================
  SUBMIT WORK REQUEST                                           [interactive]
================================================================================

Enter the details for your work request.

### Required
**Title:** [prompt user for title]

### Optional (press Enter to skip)
**Priority (1-4, default=3):** [prompt user]
  1 = Critical (system down, security breach)
  2 = High (blocking work, urgent deadline)
  3 = Normal (standard work)
  4 = Low (nice to have, when time permits)

**Department:** [prompt user]
  e.g., engineering, product, design

**Deadline (ISO format):** [prompt user]
  e.g., 2025-02-15T17:00:00Z

================================================================================
```

Collect responses and proceed to Step 3.

---

## Step 2: Inline Mode

Parse the inline arguments:

### 2.1: Extract Title

The title is the first quoted string or unquoted text before any flags:

```python
import re

# Extract quoted title
quoted_match = re.match(r'^"([^"]+)"', arguments)
if quoted_match:
    title = quoted_match.group(1)
    remaining = arguments[quoted_match.end():].strip()
else:
    # Extract title as text before first flag
    flag_match = re.search(r'\s--\w+', arguments)
    if flag_match:
        title = arguments[:flag_match.start()].strip()
        remaining = arguments[flag_match.start():].strip()
    else:
        title = arguments.strip()
        remaining = ""
```

### 2.2: Extract Optional Flags

Parse remaining arguments for flags:

| Flag | Format | Example |
|------|--------|---------|
| `--priority` | `--priority N` | `--priority 2` |
| `--department` | `--department ID` | `--department engineering` |
| `--deadline` | `--deadline ISO` | `--deadline 2025-02-15T17:00:00Z` |
| `--complexity` | `--complexity LEVEL` | `--complexity complex` |

```python
import re

def parse_flags(text):
    flags = {}

    # Priority
    match = re.search(r'--priority\s+(\d+)', text)
    if match:
        flags['priority'] = int(match.group(1))

    # Department
    match = re.search(r'--department\s+(\S+)', text)
    if match:
        flags['department'] = match.group(1)

    # Deadline
    match = re.search(r'--deadline\s+(\S+)', text)
    if match:
        flags['deadline'] = match.group(1)

    # Complexity
    match = re.search(r'--complexity\s+(\S+)', text)
    if match:
        flags['complexity'] = match.group(1)

    return flags
```

### 2.3: Validate Priority

**If priority provided:**
- Must be between 1 and 4 (inclusive)
- If invalid, show error:

```
## Invalid Priority

Priority must be 1-4:
  1 = Critical (system down, security breach)
  2 = High (blocking work, urgent deadline)
  3 = Normal (standard work)
  4 = Low (nice to have, when time permits)

You provided: [value]
```
Exit without submitting.

---

## Step 3: Submit to Input Channel

Call the input channel script to submit the work request:

```bash
uv run .claude/hooks/company/input_channel.py submit \
  --title "[title]" \
  --description "[the user's full request text, flags removed]" \
  [--priority N] \
  [--department ID] \
  [--deadline ISO] \
  [--complexity LEVEL]
```

**Build the command:**
- `--title` is required: a short summary (first sentence of the request, at most ~80 characters)
- `--description` is the user's full request text with the flags removed. Always pass it: the worker's prompt is built from the description, not the title
- Include `--priority` only if provided (defaults to 3)
- Include `--department` only if provided
- Include `--deadline` only if provided
- Include `--complexity` only if provided

**Example commands:**

```bash
# Minimal
uv run .claude/hooks/company/input_channel.py submit --title "Fix login bug"

# With priority
uv run .claude/hooks/company/input_channel.py submit --title "Fix login bug" --priority 2

# Full
uv run .claude/hooks/company/input_channel.py submit \
  --title "Add OAuth support" \
  --priority 2 \
  --department engineering \
  --deadline 2025-02-15T17:00:00Z \
  --complexity complex
```

---

## Step 4: Parse Response

The input channel returns JSON. Parse the response:

**On success (`success: true`):**
```json
{
  "success": true,
  "task_id": "task-abc123",
  "task": { ... },
  "requested_by": "human",
  "submitted_at": "2025-02-09T..."
}
```

**On error (`success: false`):**
```json
{
  "success": false,
  "error": "Error message here"
}
```

---

## Step 5: Display Result

### 5.1: Success Output

```
================================================================================
  WORK REQUEST SUBMITTED                                          [success]
================================================================================

+------------------------------------------------------------------------------+
|  REQUEST DETAILS                                                             |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:     [task_id]                                                      |
|  Title:       [title]                                                        |
|  Priority:    [priority] ([priority_label])                                  |
|  Department:  [department or "auto-assigned"]                                |
|  Deadline:    [deadline or "none"]                                           |
|  Complexity:  [complexity or "standard"]                                     |
|  Submitted:   [submitted_at]                                                 |
|                                                                              |
+------------------------------------------------------------------------------+

### Queue Position

Your request has been added to the work queue.

### Next Steps
- `/company-status` - Check overall queue status
- `/employee-status` - See who is working on what
- `/dashboard` - Quick operational snapshot

The work allocator will assign this task based on priority and availability.

================================================================================
```

**Priority labels:**
| Priority | Label |
|----------|-------|
| 1 | Critical |
| 2 | High |
| 3 | Normal |
| 4 | Low |

### 5.2: Error Output

```
================================================================================
  SUBMISSION FAILED                                                 [error]
================================================================================

**Error:** [error message]

### Common Issues

| Error | Solution |
|-------|----------|
| "Title is required" | Provide a task title |
| "Priority must be 1-4" | Use --priority with value 1, 2, 3, or 4 |
| "Invalid deadline format" | Use ISO 8601 format (e.g., 2025-02-15T17:00:00Z) |
| "Invalid complexity" | Use trivial, standard, complex, or epic |

### Try Again

/submit "Your task description" --priority 3

================================================================================
```

---

## Step 6: Validate Company Context (Optional Enhancement)

Before submitting, optionally check if the company is initialized:

```bash
ls .company/org.json 2>/dev/null && echo "COMPANY_EXISTS" || echo "NO_COMPANY"
```

**If no company exists:**
```
================================================================================
  COMPANY NOT INITIALIZED                                           [warning]
================================================================================

The company structure has not been initialized.

The input channel will still queue your request, but work allocation
requires an initialized company with employees.

### Initialize Company

/company-init              # Single-project company
/company-bootstrap         # Intelligent setup with templates
/company-create            # Multi-project company root

### Submit Anyway

The request will be queued but not allocated until the company is set up.

Proceed? (yes/no)
================================================================================
```

---

## Examples

### Example 1: Simple Submission

```
/submit "Fix the login button not working on mobile"
```

**Output:**
```
================================================================================
  WORK REQUEST SUBMITTED                                          [success]
================================================================================

+------------------------------------------------------------------------------+
|  REQUEST DETAILS                                                             |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:     task-1707494400-abc                                            |
|  Title:       Fix the login button not working on mobile                     |
|  Priority:    3 (Normal)                                                     |
|  Department:  auto-assigned                                                  |
|  Deadline:    none                                                           |
|  Complexity:  standard                                                       |
|  Submitted:   2025-02-09T15:00:00Z                                           |
|                                                                              |
+------------------------------------------------------------------------------+

### Queue Position

Your request has been added to the work queue.

### Next Steps
- `/company-status` - Check overall queue status
- `/employee-status` - See who is working on what
- `/dashboard` - Quick operational snapshot

The work allocator will assign this task based on priority and availability.

================================================================================
```

### Example 2: High Priority with Department

```
/submit "Database connection pool exhaustion causing timeouts" --priority 1 --department engineering
```

**Output:**
```
================================================================================
  WORK REQUEST SUBMITTED                                          [success]
================================================================================

+------------------------------------------------------------------------------+
|  REQUEST DETAILS                                                             |
+------------------------------------------------------------------------------+
|                                                                              |
|  Task ID:     task-1707494401-xyz                                            |
|  Title:       Database connection pool exhaustion causing timeouts           |
|  Priority:    1 (Critical)                                                   |
|  Department:  engineering                                                    |
|  Deadline:    none                                                           |
|  Complexity:  standard                                                       |
|  Submitted:   2025-02-09T15:00:01Z                                           |
|                                                                              |
+------------------------------------------------------------------------------+

### Queue Position

Your request has been added to the work queue.
CRITICAL priority - will be processed immediately.

### Next Steps
- `/company-status` - Check overall queue status
- `/employee-status` - See who is working on what
- `/dashboard` - Quick operational snapshot

The work allocator will assign this task based on priority and availability.

================================================================================
```

### Example 3: With Deadline

```
/submit "Prepare Q1 analytics report" --priority 2 --deadline 2025-02-15T17:00:00Z
```

### Example 4: Interactive Mode

```
/submit

================================================================================
  SUBMIT WORK REQUEST                                           [interactive]
================================================================================

Enter the details for your work request.

### Required
**Title:** Add dark mode support to the dashboard

### Optional (press Enter to skip)
**Priority (1-4, default=3):** 3
**Department:** design
**Deadline (ISO format):**

================================================================================

Submitting...

================================================================================
  WORK REQUEST SUBMITTED                                          [success]
================================================================================
...
```

---

## Rules

1. **Title is required.** Never submit without a title. Prompt in interactive mode.

2. **Validate priority range.** Priority must be 1-4. Reject invalid values with helpful error.

3. **Use ISO 8601 for deadlines.** If deadline is invalid format, show error with example.

4. **Show task_id prominently.** The task_id is how users track and reference the request.

5. **Suggest next steps.** Always show relevant follow-up commands.

6. **Handle errors gracefully.** Parse error responses and show actionable guidance.

7. **Default priority is 3 (Normal).** Don't require priority for every submission.

8. **Support both quoted and unquoted titles.** Parse flexibly for good UX.

---

## Error Handling

### Missing Title

```
## Missing Title

A title is required to submit a work request.

**Usage:**
  /submit "Your task description"
  /submit                          # Interactive mode

**Examples:**
  /submit "Fix login bug"
  /submit "Add user authentication" --priority 2
```

### Invalid Priority

```
## Invalid Priority

Priority must be 1-4:
  1 = Critical
  2 = High
  3 = Normal
  4 = Low

You provided: [value]

**Example:**
  /submit "Fix bug" --priority 2
```

### Invalid Deadline Format

```
## Invalid Deadline Format

Deadline must be in ISO 8601 format.

**Examples:**
  2025-02-15T17:00:00Z
  2025-02-15T17:00:00+00:00
  2025-02-15

You provided: [value]
```

### Input Channel Not Found

```
## Input Channel Not Found

The input channel script was not found at:
  .claude/hooks/company/input_channel.py

This may indicate:
1. Gatesmith is not properly installed
2. The hooks directory is missing

**To fix:**
1. Verify Gatesmith installation
2. Check that .claude/hooks/company/ exists
3. Run `/prime` to reload project context
```
