# /efficiency — G6 Efficiency Report

Display efficiency metrics and optimization insights for the company.

## Input
$ARGUMENTS

Optional arguments:
- `--employee [id]` — Show individual employee efficiency analysis
- `--insights` — Show discovered patterns and recommendations
- `--optimize` — Run optimization analysis and apply learned patterns
- `--research` — Run comprehensive efficiency analysis session
- `--intervals` — Show adaptive intervals analysis and recommendations (P18)

---

## Step 0: Detect Company Mode

Use the company_resolver to find the company root:

```bash
# Find company root
uv run .claude/hooks/company/company_resolver.py find

# Check if in company mode
uv run .claude/hooks/company/company_resolver.py mode
```

Store the results:
- `company_root` — Path to company root
- `company_dir` — Path to `.company/` directory
- `is_company_mode` — Boolean indicating if company exists

### Check Company Exists

```bash
ls [company_dir]/org.json 2>/dev/null
```

**If not exists:**
```
No company initialized. Run /company-init or /company-bootstrap first.
```
Exit without further processing.

---

## Step 1: Gather Efficiency Data

Fetch efficiency data from the tracker:

```bash
# Get company efficiency report
uv run .claude/hooks/company/efficiency_tracker.py report

# Get memory hit rate statistics
uv run .claude/hooks/company/efficiency_tracker.py memory
```

Parse the JSON output and extract:
- `company_efficiency` — Overall company score and target
- `employee_breakdown` — Per-employee efficiency scores
- `patterns` — Discovered routing patterns
- `memory_stats` — Memory sharing effectiveness
- `learning` — Patterns discovered and optimizations applied
- `recommendations` — Improvement suggestions

Also read company metadata:
```bash
# Get company name from org.json
cat [company_dir]/org.json
```

Extract `company_name` from the organization config.

---

## Step 2: Render Efficiency Dashboard (Default, no flags)

Generate the ASCII dashboard with efficiency metrics:

```
===============================================================
  GATESMITH EFFICIENCY REPORT
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[EFFICIENCY] Score: X.XX / Target: X.XX [efficiency_bar] ([status])

  Company Efficiency: X.XX (+/-X.XX vs target)
  Capacity Usage: XXX/XXXX units [capacity_bar] XX%
  Optimization Lift: +XX% since baseline

[TOP PERFORMERS]
| Rank | Employee | Score | Tasks | Trend |
|------|----------|-------|-------|-------|
| 1 | [name] | X.XX | XX | [trend_arrow] |
| 2 | [name] | X.XX | XX | [trend_arrow] |
| 3 | [name] | X.XX | XX | [trend_arrow] |

[PATTERNS] X discovered | X optimizations applied

[MEMORY]
  Hit Rate: XX% [hit_rate_bar]
  Estimated Savings: ~XX tasks worth of context

[INSIGHTS]
  [bullet_points_from_recommendations]

===============================================================
```

### Trend Arrows

| Trend | Arrow |
|-------|-------|
| improving | ↑ |
| stable | → |
| declining | ↓ |

### Efficiency Bar (20 characters)

Use filled and empty block characters:
- Score >= target: Full green (implied)
- Score < target: Partial fill showing gap

```
Score: 0.85 / Target: 0.90 [▓▓▓▓▓▓▓▓▓░░░░░░░░░░░] (Below Target)
Score: 0.95 / Target: 0.90 [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓] (On Target)
```

### Status Mapping

| Score vs Target | Status |
|-----------------|--------|
| score >= target | On Target |
| score >= target * 0.9 | Near Target |
| score >= target * 0.8 | Below Target |
| score < target * 0.8 | Critical |

---

## Step 3: Employee Analysis (`--employee [id]` flag)

If `--employee` flag is present with an employee ID:

```bash
# Get individual employee efficiency
uv run .claude/hooks/company/efficiency_tracker.py employee --employee-id [employee_id]
```

Show detailed employee view:

```
===============================================================
  GATESMITH EFFICIENCY: [employee_name]
  Generated: YYYY-MM-DD HH:MM UTC
===============================================================

[EFFICIENCY SCORE] X.XX [status]
  - Tasks Completed: XX (last 30 days)
  - First-Pass Success: XX%
  - Memory Hit Rate: XX%
  - Trend: [trend] [trend_arrow]

[TASKS BY COMPLEXITY]
| Complexity | Count | % of Total |
|------------|-------|------------|
| Trivial | XX | XX% |
| Standard | XX | XX% |
| Complex | XX | XX% |
| Epic | XX | XX% |

[STRENGTHS]
  [bullet_list_of_strength_tags]

[IMPROVEMENT AREAS]
  [bullet_list_of_improvement_areas]

[DETAILS]
  - Value Delivered: X.XX (complexity-weighted)
  - Resources Used: XX min (including retry penalty)
  - Quality Factor: X.XX
  - Successful Tasks: XX
  - Escalated Tasks: XX

===============================================================
```

---

## Step 4: Insights View (`--insights` flag)

If `--insights` flag is present:

```bash
# Get efficiency insights
uv run .claude/hooks/company/efficiency_tracker.py insights
```

Show insights dashboard:

```
===============================================================
  GATESMITH EFFICIENCY INSIGHTS
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[DISCOVERED PATTERNS] X patterns
| Pattern | Optimal Employee | Efficiency | Samples |
|---------|------------------|------------|---------|
| [tag] | [employee_id] | X.XX | XX |
| [tag] | [employee_id] | X.XX | XX |

[RECOMMENDATIONS] X suggestions
| Priority | Type | Description |
|----------|------|-------------|
| High | Routing | Route [pattern] tasks to [employee] |
| Medium | Memory | Increase context reuse (XX% -> 70%) |
| High | Quality | Reduce escalation rate (XX% -> <10%) |

[TOP PERFORMERS] X employees
| Employee | Score | Tasks | Strengths |
|----------|-------|-------|-----------|
| [name] | X.XX | XX | [tags] |

[IMPROVEMENT OPPORTUNITIES]
  [For declining employees:]
  - [employee_name]: Showing declining efficiency - Review recent tasks

  [For underutilized employees:]
  - [employee_name]: High efficiency but low volume - Route more tasks

===============================================================
```

---

## Step 5: Optimization (`--optimize` flag)

If `--optimize` flag is present:

```bash
# Run optimization analysis
uv run .claude/hooks/company/efficiency_tracker.py optimize
```

Show optimization results:

```
===============================================================
  GATESMITH EFFICIENCY OPTIMIZATION
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[ANALYSIS COMPLETE]
  Analyzed: XX task executions
  Time Window: Last 30 days

[PATTERNS DISCOVERED] X new patterns
| Pattern | Optimal Employee | Improvement |
|---------|------------------|-------------|
| [tag] | [employee_id] | +XX% efficiency |

[OPTIMIZATIONS APPLIED]
  - Routing patterns saved for future task allocation
  - Memory patterns indexed for context sharing

[NEXT STEPS]
  1. New tasks matching discovered patterns will be auto-routed
  2. Run /efficiency --insights to see current recommendations
  3. Monitor /efficiency regularly to track improvements

===============================================================
```

---

## Step 6: Research Mode (`--research` flag)

If `--research` flag is present, run comprehensive analysis:

```bash
# Run full optimization
uv run .claude/hooks/company/efficiency_tracker.py optimize

# Get insights
uv run .claude/hooks/company/efficiency_tracker.py insights

# Get memory stats
uv run .claude/hooks/company/efficiency_tracker.py memory

# Get company report
uv run .claude/hooks/company/efficiency_tracker.py report
```

Show research dashboard:

```
===============================================================
  GATESMITH EFFICIENCY RESEARCH SESSION
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[EXECUTIVE SUMMARY]
  Company Efficiency: X.XX (Target: X.XX)
  Data Points: XX task executions over XX days
  Patterns Discovered: XX
  Optimization Potential: +XX% (estimated)

[KEY FINDINGS]

1. TASK ROUTING
   - Best performer for [pattern]: [employee] (X.XX efficiency)
   - Current routing: Random/capability-based
   - Recommendation: Implement pattern-based routing

2. MEMORY EFFICIENCY
   - Current hit rate: XX%
   - Target hit rate: 70%
   - Estimated savings: XX tasks worth of context

3. QUALITY METRICS
   - First-pass success: XX%
   - Escalation rate: XX%
   - Areas of concern: [list]

4. EMPLOYEE ANALYSIS
   - Top performer: [name] (X.XX score)
   - Needs attention: [name] (declining trend)
   - Underutilized: [name] (high score, low volume)

[RECOMMENDATIONS BY PRIORITY]

HIGH:
  [high_priority_recommendations]

MEDIUM:
  [medium_priority_recommendations]

LOW:
  [low_priority_recommendations]

[ACTION ITEMS]
  1. Apply routing optimizations: /efficiency --optimize
  2. Review declining employees: /employee-status [id]
  3. Increase task allocation for high performers
  4. Monitor progress: /efficiency (weekly recommended)

===============================================================
```

---

## Step 7: Handle Empty States

### No Task Executions
```
[EFFICIENCY] No data available
  No task executions recorded yet.
  Execute tasks to start tracking efficiency.
```

### No Employees
```
[EFFICIENCY] No employees
  No employees hired yet.
  Use /company-hire to add employees.
```

### Insufficient Data
```
[EFFICIENCY] Insufficient data
  Only XX task executions recorded.
  Need at least 5 executions for meaningful analysis.
```

### Employee Not Found
```
Employee '[id]' not found.
Available employees: [list of employee IDs]
```

---

## Step 8: Adaptive Intervals (`--intervals` flag)

If `--intervals` flag is present, show executive loop interval analysis:

```bash
# Get interval recommendation
uv run .claude/hooks/company/interval_learner.py recommend --current-interval 4.0

# Detect patterns from session history
uv run .claude/hooks/company/interval_learner.py patterns
```

Show adaptive intervals dashboard:

```
===============================================================
  GATESMITH ADAPTIVE INTERVALS
  Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
===============================================================

[CURRENT SETTINGS]
  Interval: X.X hours
  Sessions Analyzed: XX
  Adaptive Mode: [Enabled/Disabled]

[RECOMMENDATION]
  Suggested Interval: X.X hours
  Confidence: XX% [confidence_bar]
  Expected Savings: XX%
  Auto-Apply: [Yes/No]

  Reasoning: [recommendation_reasoning]

[DETECTED PATTERNS] X patterns

| Pattern | Description | Confidence |
|---------|-------------|------------|
| queue_state | Empty queue sessions avg X.XX value | XX% |
| time_of_day | Peak hour: XX:00 (avg X.XX) | XX% |

[SESSION VALUE DISTRIBUTION]
  High value (>0.7): XX sessions
  Medium (0.3-0.7): XX sessions
  Low value (<0.3): XX sessions

[HOW TO APPLY]
  To apply the recommended interval:
  1. Update gatesmith.json:
     "executiveLoop": {
       "intervalHours": X.X
     }
  2. Restart daemon: /daemon restart

  To enable auto-apply:
  1. Set in gatesmith.json:
     "executiveLoop": {
       "adaptiveIntervals": {
         "enabled": true,
         "autoApplyThreshold": 0.8
       }
     }

===============================================================
```

### Interval Scoring

The `--intervals --score-session [id]` subcommand shows scoring for a specific session:

```bash
# Score a specific session
uv run .claude/hooks/company/interval_learner.py score --session-id "2026-02-27T10:00:00Z"

# Score raw session data
uv run .claude/hooks/company/interval_learner.py score-raw --session '{"work_submitted": 2, "decisions_count": 1, "queue_pending_at_start": 0}'
```

Output:
```
[SESSION SCORE]
  Session ID: 2026-02-27T10:00:00Z
  Value Score: X.XX / 1.00

  [FACTORS]
  | Factor | Value | Score |
  |--------|-------|-------|
  | Work Submitted | X | 0.50 |
  | Decisions Made | X | 0.30 |
  | Queue Empty | [Yes/No] | 0.20 |
  | High Queue Penalty | [Yes/No] | -0.20 |

  [FORMULA]
  value = 0.5*(work>0) + 0.3*(decisions>0) + 0.2*(queue_empty)
  penalty = -0.2 if queue > 5
```

---

## Rules

- **Always fetch fresh data.** Don't cache between invocations.
- **Parse JSON carefully.** Handle missing fields gracefully with defaults.
- **Respect flags.** If a specific flag is passed, show ONLY that section with more detail.
- **Use ASCII art consistently.** Box-drawing characters for borders, block characters for bars.
- **Handle errors gracefully.** If a data source fails, show "Data unavailable" for that section.
- **Include generation timestamp.** Always show when the report was generated.
- **Quick operation.** This should complete in under 2 seconds for typical use.
- **Focus on actionable insights.** Highlight what can be improved, not just metrics.
- **Show trends.** Use arrows and status to indicate direction of change.
