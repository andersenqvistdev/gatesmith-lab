# /company-health — Deep Management Insights Report

Generate a comprehensive company health report with strategic management insights. Provides executive summary, delivery forecasts, workforce health analysis, risk assessment, historical trends, and AI-generated recommendations.

## Input
$ARGUMENTS

Optional arguments:
- `--delivery` — Focus on delivery forecast and project status
- `--workforce` — Focus on team health and utilization
- `--risks` — Focus on risk assessment and mitigation
- `--trends` — Focus on historical trends and patterns
- `--project=<id>` — Report for specific project (multi-project mode)
- No flags — Generate full comprehensive report

## Step 0: Resolve Company Root and Check Initialization

Use the company_resolver to find the company root and determine operating mode:

```bash
# Find company root (searches upward for .forge-company-root)
uv run .claude/hooks/company/company_resolver.py find

# Check if in multi-project mode
uv run .claude/hooks/company/company_resolver.py mode

# Get current project context (if in multi-project mode)
uv run .claude/hooks/company/company_resolver.py project
```

Store the results:
- `company_root` — Path to company root (multi-project) or current directory (legacy)
- `company_dir` — Path to `.company/` directory
- `is_multi_project` — Boolean indicating multi-project mode
- `current_project` — Current project context (if multi-project)

Check if the resolved `.company/` directory exists:

```bash
ls -la [company_dir]/ 2>/dev/null
```

**If not exists:**
```
## Company Not Initialized

No company directory found at [company_root]/.company/.

To initialize a new company structure, run:
  /company-init

The health report requires an initialized company with:
- Organization structure (departments, teams)
- Work queue with task tracking
- Employee assignments
```

Exit without further processing.

## Step 1: Gather All Data

Run the data collection scripts in parallel to gather comprehensive metrics.

### 1.1: Dashboard Full Aggregation

```bash
uv run .claude/hooks/company/dashboard_aggregator.py full
```

This returns JSON with:
- `health` — Overall health score and factors
- `progress` — Task completion, velocity, forecast
- `workforce` — Agent status, utilization, departments
- `risks` — Identified risks with severity
- `overall_status` — "healthy", "warning", or "critical"

### 1.2: Active Alerts

```bash
uv run .claude/hooks/company/alert_rules.py list
```

This returns:
- Active alerts by severity (critical, warning)
- Alert timestamps and messages

### 1.3: Stalled Work Detection

```bash
uv run .claude/hooks/company/progress_tracker.py stalled --threshold 60
```

This returns:
- Tasks stalled in progress (no updates for 60+ minutes)
- Long-blocked tasks (blocked for 3x threshold)
- Recommendations

### 1.4: Knowledge Metrics (if available)

```bash
uv run .claude/hooks/company/knowledge_capture.py query --type patterns
uv run .claude/hooks/company/knowledge_capture.py query --type decisions
```

Count patterns and decisions for knowledge growth metrics.

### 1.5: Load Organization Data

Read `.company/org.json` for:
- Company name and description
- Department structure
- Employee roster and statuses
- Active work items

## Step 2: Generate Report Based on Flags

Based on the arguments provided, generate the appropriate report section(s).

---

## Full Report (No Flags)

When no flags are provided, generate the complete management report:

```
═══════════════════════════════════════════════════════════════
  COMPANY HEALTH REPORT
  Generated: [YYYY-MM-DD HH:MM] | Company: [company_name]
═══════════════════════════════════════════════════════════════

## EXECUTIVE SUMMARY

[One-line health statement based on overall_status:]
- If healthy: "Company is operating at HEALTHY level with strong delivery and workforce metrics."
- If warning: "Company is operating at WARNING level. [Top concern from risks/factors]."
- If critical: "Company is operating at CRITICAL level. Immediate attention required for [top risk]."

| Metric | Value | Status |
|--------|-------|--------|
| Health Score | [score]/100 | [emoji] [status] |
| Velocity | [velocity] tasks/day | [trend arrow] [percent]% |
| Completion | [completion_percentage]% | [On Track/Behind/Ahead] |
| Autonomy (verified) | [autonomy_audit.verified_autonomy_rate × 100]% | [autonomy_audit.available ? "calibrated [generated_at]" : "not calibrated — run /calibrate"] |
| Trust (phantom rate) | [autonomy_audit.phantom_rate × 100]% | [autonomy_audit.phantom_count] phantoms |
| Risk Level | [risk_level] | [critical_count] active |

### Top 3 Action Items
1. [Most urgent action based on critical risks or blockers]
2. [Second priority based on warnings or stalled work]
3. [Third priority based on workforce or delivery concerns]

───────────────────────────────────────────────────────────────

## DELIVERY STATUS

[For each project in multi-project mode, or single project view:]

### [project_name] ([completion_percentage]% complete)
[Progress bar: filled/empty based on percentage] Est: [estimated_date]
In Progress: [count] | Blocked: [count]

Progress bar format:
- Use 24 characters total
- Filled portion: Unicode block chars based on percentage
- Example 75%: ██████████████████░░░░░░

[If blockers exist:]
### Blockers Impacting Delivery
| Blocker | Tasks Affected | Duration | Impact |
|---------|----------------|----------|--------|
| [blocker description] | [count] tasks | [duration] | [estimated delay] |

───────────────────────────────────────────────────────────────

## WORKFORCE HEALTH

Total: [total_agents] employees | Utilization: [utilization]%

| Department | Active | Idle | Blocked | Workload |
|------------|--------|------|---------|----------|
| [dept_name] | [count] | [count] | [count] | [Heavy/Balanced/Light] |
| ... | ... | ... | ... | ... |

### Workload Assessment
[Based on utilization percentage:]
- 0-40%: "UNDERUTILIZED - Consider assigning more work"
- 40-60%: "LIGHT - Room for additional work"
- 60-80%: "OPTIMAL - Healthy workload balance"
- 80-90%: "HEAVY - Monitor for burnout"
- 90%+: "OVERLOADED - Redistribute work immediately"

### Skill Coverage
[Analyze departments with zero agents or high blocked counts]

| Gap | Severity | Recommendation |
|-----|----------|----------------|
| [No QA coverage] | [WARNING] | [Consider hiring QA or cross-training] |

### Knowledge Accumulation (7 days)
- Patterns captured: [patterns_count]
- Decisions recorded: [decisions_count]
- Total learnings: [total]

───────────────────────────────────────────────────────────────

## AUTONOMY & TRUST

[From `autonomy_audit` (produced by /calibrate). If `autonomy_audit.available` is
false, print: "Not yet calibrated — run /calibrate --write to verify how many
'complete' tasks actually shipped." and skip the rest.]

Build: [autonomy_audit.build_sha] | Calibrated: [autonomy_audit.generated_at]

| Signal | Value | Meaning |
|--------|-------|---------|
| Local proxy | [autonomy_proxy_rate × 100]% | Tasks that reached a PR (upper bound) |
| Verified autonomy | [verified_autonomy_rate × 100]% | Genuinely merged AND addresses the task |
| Trust score | [trust_score × 100]% | Of "complete" tasks, how many truly shipped |
| Phantom rate | [phantom_rate × 100]% | The gap — completions that didn't ship |

[The gap between local proxy and verified autonomy IS the phantom leak — call it
out explicitly. [phantom_count] phantom completions detected.]

───────────────────────────────────────────────────────────────

## RISK ASSESSMENT

[List all risks from dashboard_aggregator, sorted by severity]

| Risk | Severity | Status |
|------|----------|--------|
| [risk_title] | [CRITICAL/WARNING] | [trend: New/Stable/Improving] |
| ... | ... | ... |

### Risk Details

[For each CRITICAL risk:]
**[risk_id]: [risk_title]**
- Description: [description]
- Value: [metric_value] (threshold: [threshold])
- Recommendation: [recommendation]

[For each WARNING risk:]
**[risk_id]: [risk_title]**
- Description: [description]
- Recommendation: [recommendation]

### Risk Trend
[Based on comparing current vs historical if available:]
- IMPROVING: Risk count decreased or severity reduced
- STABLE: No significant change
- DEGRADING: New risks appeared or severity increased

───────────────────────────────────────────────────────────────

## HISTORICAL TRENDS (7 days)

[Generate ASCII sparklines based on available historical data]

Velocity:  [sparkline] ([trend description])
Health:    [sparkline] ([trend description])
Escalations: [sparkline] ([frequency])

Sparkline format using Unicode blocks:
- ▁ (lowest) to █ (highest)
- 7 characters for 7 days
- Example improving: ▁▂▃▅▆▇█

[If no historical data available:]
Historical data tracking begins after first health report.
Run /company-health daily to build trend data.

───────────────────────────────────────────────────────────────

## RECOMMENDATIONS

[Generate AI recommendations based on all gathered metrics]

### Immediate Actions (This Hour)
[Only if CRITICAL risks or severely stalled work exists]
1. [Specific actionable recommendation]
2. [Specific actionable recommendation]

### Short-Term Improvements (This Week)
[Based on WARNING level issues and workforce metrics]
1. [Recommendation with context]
2. [Recommendation with context]

### Strategic Suggestions (This Month)
[Based on patterns, trends, and overall health factors]
1. [Higher-level recommendation]
2. [Higher-level recommendation]

### Example Recommendations Logic:

[If blocked_ratio > 20%:]
- "Prioritize unblocking [count] blocked tasks. Blockers are impacting delivery estimates by approximately [X] days."

[If utilization < 50%:]
- "Workforce underutilized at [X]%. Consider assigning work from backlog or cross-training opportunities."

[If utilization > 85%:]
- "Workforce showing strain at [X]% utilization. Consider hiring or redistributing work to prevent burnout."

[If stalled_count > 0:]
- "Address [count] stalled task(s). Task [task_id] has been stalled for [duration] - check on assigned employee."

[If escalations_active > 2:]
- "Resolve [count] active escalations. Oldest escalation pending for [duration]."

[If patterns_count increased significantly:]
- "Knowledge base growing well with [count] new patterns. Schedule team sync to review and adopt."

[If any project ahead of schedule:]
- "Project [name] is ahead of schedule. Consider reallocating [X] resources to [lagging project]."

[If department overloaded:]
- "[Department] department showing heavy workload. Redistribute work or consider temporary reinforcement."

═══════════════════════════════════════════════════════════════
```

---

## Focused Reports (With Flags)

### --delivery Flag

```
═══════════════════════════════════════════════════════════════
  DELIVERY FORECAST REPORT
  Generated: [YYYY-MM-DD HH:MM] | Company: [company_name]
═══════════════════════════════════════════════════════════════

## DELIVERY SUMMARY

| Metric | Value |
|--------|-------|
| Total Tasks | [total] |
| Completed | [completed] ([percentage]%) |
| In Progress | [in_progress] |
| Blocked | [blocked] |
| Pending | [pending] |

## PROJECT STATUS

[For each project:]

### [project_name]
██████████████████░░░░░░ [percentage]% Complete

| Status | Count | Est. Hours |
|--------|-------|------------|
| Completed | [n] | [hours] |
| In Progress | [n] | [hours] |
| Blocked | [n] | [hours] |
| Pending | [n] | [hours] |

**Estimated Completion:** [date]
**Confidence:** [High/Medium/Low] (based on velocity stability)
**Risk Factors:** [list any blockers or risks affecting this project]

## DELIVERY RISKS

| Risk | Impact | Mitigation |
|------|--------|------------|
| [risk] | [delay estimate] | [action] |

## VELOCITY ANALYSIS

Daily Velocity: [velocity] tasks/day
Velocity Trend: [arrow] [percentage]% vs last week

[If velocity is declining:]
ALERT: Velocity trending downward. At current rate, delivery may slip by [X] days.

═══════════════════════════════════════════════════════════════
```

### --workforce Flag

```
═══════════════════════════════════════════════════════════════
  WORKFORCE HEALTH REPORT
  Generated: [YYYY-MM-DD HH:MM] | Company: [company_name]
═══════════════════════════════════════════════════════════════

## WORKFORCE SUMMARY

| Metric | Value | Status |
|--------|-------|--------|
| Total Employees | [count] | - |
| Active | [count] | [percentage]% |
| Idle | [count] | [percentage]% |
| Blocked | [count] | [percentage]% |
| Utilization | [percentage]% | [status] |

## DEPARTMENT BREAKDOWN

| Department | Total | Active | Idle | Blocked | Utilization |
|------------|-------|--------|------|---------|-------------|
| [dept] | [n] | [n] | [n] | [n] | [%] |
| ... | ... | ... | ... | ... | ... |

## WORKLOAD DISTRIBUTION

### Overloaded (>2 concurrent tasks)
| Employee | Department | Tasks | Hours In Progress |
|----------|------------|-------|-------------------|
| [id] | [dept] | [n] | [hours] |

### Idle (No active tasks)
| Employee | Department | Last Active |
|----------|------------|-------------|
| [id] | [dept] | [timestamp] |

### Blocked (Waiting on dependencies)
| Employee | Department | Blocked By | Duration |
|----------|------------|------------|----------|
| [id] | [dept] | [dependency] | [hours] |

## SKILL COVERAGE ANALYSIS

| Skill Area | Coverage | Gap Severity |
|------------|----------|--------------|
| Engineering | [n] employees | OK |
| QA/Testing | [n] employees | [WARNING if low] |
| Design | [n] employees | [status] |
| DevOps | [n] employees | [status] |

## KNOWLEDGE GROWTH

| Metric | This Week | Total |
|--------|-----------|-------|
| Patterns Captured | [n] | [total] |
| Decisions Recorded | [n] | [total] |
| Employee Learnings | [n] files | [total] |

## WORKFORCE RECOMMENDATIONS

[Based on analysis above]

1. [Specific recommendation about staffing/workload]
2. [Specific recommendation about skill gaps]
3. [Specific recommendation about blocked employees]

═══════════════════════════════════════════════════════════════
```

### --risks Flag

```
═══════════════════════════════════════════════════════════════
  RISK ASSESSMENT REPORT
  Generated: [YYYY-MM-DD HH:MM] | Company: [company_name]
═══════════════════════════════════════════════════════════════

## RISK SUMMARY

| Severity | Count | Trend |
|----------|-------|-------|
| CRITICAL | [n] | [arrow] |
| WARNING | [n] | [arrow] |
| Total Active | [n] | [status] |

## CRITICAL RISKS

[For each critical risk:]

### [risk_id]: [risk_title]

| Attribute | Value |
|-----------|-------|
| Severity | CRITICAL |
| Category | [category] |
| Detected | [timestamp] |
| Metric | [metric_name]: [value] (threshold: [threshold]) |

**Description:**
[Full description]

**Impact:**
[Potential impact if not addressed]

**Recommended Action:**
[recommendation]

**Owner:** [Suggested owner based on category]
**Due:** Immediate

---

## WARNING RISKS

[For each warning risk:]

### [risk_id]: [risk_title]

| Attribute | Value |
|-----------|-------|
| Severity | WARNING |
| Category | [category] |
| Detected | [timestamp] |

**Description:**
[description]

**Recommended Action:**
[recommendation]

---

## STALLED WORK (Risk Factor)

| Task | Assignee | Stalled Duration | Last Activity |
|------|----------|------------------|---------------|
| [id] | [employee] | [minutes] min | [timestamp] |

## LONG-BLOCKED ITEMS (Risk Factor)

| Task | Blocked By | Duration | Impact |
|------|------------|----------|--------|
| [id] | [dependency] | [hours] hrs | [tasks affected] |

## RISK MITIGATION PRIORITIES

| Priority | Risk | Action | Owner |
|----------|------|--------|-------|
| 1 | [highest severity risk] | [action] | [owner] |
| 2 | [next risk] | [action] | [owner] |
| 3 | [next risk] | [action] | [owner] |

## RISK TREND ANALYSIS

[Compare to previous data if available]

Overall Risk Trend: [IMPROVING/STABLE/DEGRADING]

[Explanation of trend]

═══════════════════════════════════════════════════════════════
```

### --trends Flag

```
═══════════════════════════════════════════════════════════════
  HISTORICAL TRENDS REPORT
  Generated: [YYYY-MM-DD HH:MM] | Company: [company_name]
═══════════════════════════════════════════════════════════════

## 7-DAY TREND SUMMARY

### Velocity Trend
[ASCII sparkline: ▁▂▃▅▆▇█]

| Day | Tasks Completed | Velocity | Change |
|-----|-----------------|----------|--------|
| Day 1 | [n] | [v] | - |
| Day 2 | [n] | [v] | [+/-X%] |
| ... | ... | ... | ... |
| Today | [n] | [v] | [+/-X%] |

Trend Analysis: [IMPROVING/STABLE/DECLINING]
[Explanation]

### Health Score Trend
[ASCII sparkline]

| Day | Score | Status | Notable Events |
|-----|-------|--------|----------------|
| Day 1 | [n] | [status] | [any significant events] |
| ... | ... | ... | ... |

Trend Analysis: [assessment]

### Escalation Trend
[ASCII sparkline]

| Day | New | Resolved | Active |
|-----|-----|----------|--------|
| Day 1 | [n] | [n] | [n] |
| ... | ... | ... | ... |

Trend Analysis: [assessment]

## PATTERN ANALYSIS

### Recurring Issues
| Issue | Frequency | Last Occurrence | Recommendation |
|-------|-----------|-----------------|----------------|
| [issue] | [n] times | [date] | [action] |

### Positive Patterns
| Pattern | Consistency | Impact |
|---------|-------------|--------|
| [pattern] | [frequency] | [description] |

## PROJECTIONS

Based on current trends:

| Metric | Current | Projected (7 days) | Confidence |
|--------|---------|-------------------|------------|
| Velocity | [current] | [projected] | [High/Med/Low] |
| Completion | [%] | [%] | [confidence] |
| Risk Level | [level] | [level] | [confidence] |

## TREND-BASED RECOMMENDATIONS

1. [Recommendation based on velocity trend]
2. [Recommendation based on health trend]
3. [Recommendation based on patterns]

═══════════════════════════════════════════════════════════════
```

---

## Step 3: Format Output

### Progress Bar Generation

Generate visual progress bars for delivery status:

```
Percentage to bar conversion (24 chars):
- Calculate filled chars: floor(percentage / 100 * 24)
- Use Unicode: filled = ████, empty = ░░░░

Examples:
0%:   ░░░░░░░░░░░░░░░░░░░░░░░░
25%:  ██████░░░░░░░░░░░░░░░░░░
50%:  ████████████░░░░░░░░░░░░
75%:  ██████████████████░░░░░░
100%: ████████████████████████
```

### Sparkline Generation

Generate ASCII sparklines for trends:

```
Value to block conversion:
- Normalize values to 0-7 range
- Map to: ▁▂▃▄▅▆▇█

Example (7 days of velocity: 3, 4, 5, 7, 8, 9, 10):
Normalized: 0, 1, 2, 4, 5, 6, 7
Sparkline: ▁▂▃▅▆▇█
```

### Status Indicators

| Score Range | Status | Indicator |
|-------------|--------|-----------|
| 80-100 | Healthy | (green checkmark or OK) |
| 60-79 | Warning | (yellow warning) |
| 0-59 | Critical | (red alert) |

### Trend Arrows

| Change | Arrow |
|--------|-------|
| > +5% | (up arrow) +X% |
| -5% to +5% | (dash) stable |
| < -5% | (down arrow) -X% |

## Rules

- **Always resolve company root first.** Use company_resolver for multi-project support.
- **Handle missing data gracefully.** If metrics are unavailable, show "N/A" or skip section.
- **Generate actionable recommendations.** Every recommendation should be specific and assignable.
- **Respect flag filters.** If a specific flag is provided, focus output on that section.
- **Use consistent formatting.** Follow the visual templates exactly for readability.
- **Calculate relative times.** Show durations as "X hours ago" or "X minutes" for clarity.
- **Prioritize by severity.** Always show CRITICAL issues before WARNING issues.
- **Include owner suggestions.** When recommending actions, suggest who should own them.
- **Support project filtering.** In multi-project mode, `--project=<id>` limits report to one project.
- **Generate insights, not just data.** The report should interpret metrics and provide strategic guidance.

## Multi-Project Mode Behavior

When in multi-project mode:
- Full report aggregates across all projects
- Per-project breakdown shown in delivery section
- Workforce shown at company level (employees can work across projects)
- Risks shown with project attribution where applicable
- `--project=<id>` filters to single project view

## Data Freshness

All metrics are gathered at report generation time. For trending:
- If historical data available in `.company/metrics/`, use it for sparklines
- If no historical data, show current snapshot only
- Recommend running `/company-health` regularly to build trend data

## Integration Points

This command aggregates data from:
- `dashboard_aggregator.py` — Core metrics aggregation
- `alert_rules.py` — Active alert management
- `progress_tracker.py` — Task progress and stalled work detection
- `knowledge_capture.py` — Knowledge base metrics
- `org.json` — Organization structure and work items
- `work_queue.json` — Task queue status
