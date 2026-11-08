# /reviews — View and Complete Task Reviews

View completed tasks awaiting quality review and provide feedback.

## Usage

```bash
# List tasks awaiting review
/reviews

# Complete a review with feedback
/reviews complete <task-id>

# Complete with quality score
/reviews complete <task-id> --quality 0.85

# Add feedback without completing
/reviews feedback <task-id> --message "Great work, consider adding tests"

# Get review statistics
/reviews stats
```

## Arguments

- `complete <task-id>` - Complete review and move task to completed
- `feedback <task-id>` - Add feedback to a task in review
- `stats` - Show review statistics
- `--quality <0.0-1.0>` - Quality score (0.9+ excellent, 0.7+ good, 0.5+ acceptable)
- `--message <text>` - Feedback message

## Instructions

<command name="reviews">
Execute the /reviews command to view and complete quality reviews.

**For listing reviews:**
1. Load the work queue and extract tasks from the "review" queue
2. Display tasks in a formatted table showing:
   - Task ID
   - Title
   - Assigned to (employee who completed the work)
   - Complexity
   - Review requested date
   - Time in review

**For complete:**
1. Verify you have reviewer permissions
2. Optionally get quality score (default 0.75)
3. Auto-generate feedback if not provided
4. Call work_allocator.complete_review()
5. Store feedback for employee learning
6. Report success

**For stats:**
1. Call manager_review.get_review_stats()
2. Display:
   - Pending reviews count
   - Tasks reviewed this week
   - Average quality score
   - Approval rate for proposals

**Example output for /reviews:**
```
Tasks Awaiting Review (2)
─────────────────────────────────────────────────────────
ID                       Title                          Employee              Complexity   Waiting
task-20260214-abc123     Implement user validation      senior-python-dev     standard     2h
task-20260214-def456     Add API documentation          technical-writer      trivial      30m

Commands:
  /reviews complete <id>                    - Complete review
  /reviews complete <id> --quality 0.9      - Complete with quality score
  /reviews feedback <id> --message "..."    - Add feedback
```

**Example output for /reviews complete task-20260214-abc123:**
```
Review Completed

Task: Implement user validation
ID: task-20260214-abc123
Reviewed by: forge-architect
Quality Score: 0.85 (Good)
Status: completed

Feedback sent to: senior-python-dev
"Good work on 'Implement user validation'. The implementation meets quality
standards for standard complexity. Keep up the consistent delivery."
```

**Quality Score Guide:**
- 0.9+ : Excellent - Exceeds expectations
- 0.7-0.89 : Good - Meets standards
- 0.5-0.69 : Acceptable - Needs improvement
- <0.5 : Needs work - Consider pairing
</command>

## Related Commands

- `/proposals` - View and approve proposals
- `/efficiency` - View efficiency metrics
- `/employee-status` - View employee details
