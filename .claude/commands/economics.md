# /economics — G6 Economics Dashboard

Display comprehensive economics dashboard including efficiency, capacity, trends, and optimization recommendations.

## Input
$ARGUMENTS

Optional arguments:
- `--capacity` — Show subscription capacity usage details
- `--trends` — Show efficiency trends over time
- `--recommendations` — Show AI-generated optimization suggestions

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

## Step 1: Gather Economics Data

Fetch all economics-related data:

```bash
# Get efficiency report
uv run .claude/hooks/company/efficiency_tracker.py report

# Get efficiency insights for recommendations
uv run .claude/hooks/company/efficiency_tracker.py insights

# Get memory statistics
uv run .claude/hooks/company/efficiency_tracker.py memory

# Get metrics summary for trends
uv run .claude/hooks/company/metrics_tracker.py summary
```

Also read organization data:
```bash
# Get economics config from org.json
cat [company_dir]/org.json
```

Extract from org.json:
- `company_name` — Company name
- `economics.efficiency` — Company efficiency tracking
- `economics.resource_awareness` — Subscription capacity tracking
- `economics.learning` — Pattern and optimization tracking

---

## Step 2: Render Full Economics Dashboard (Default, no flags)

Generate comprehensive economics dashboard:

```
═══════════════════════════════════════════════════════════════
 G6 ECONOMICS DASHBOARD
 Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
═══════════════════════════════════════════════════════════════

 [EFFICIENCY]                                   [Score: X.XX ↑]
 ───────────────────────────────────────────────────────────────

 Company Efficiency: X.XX (+X.XX vs last week)
 Target: X.XX | Gap: X.XX
 Optimization Lift: +XX% since baseline

 Most Efficient Employees:
   [name]              X.XX  ████████████████████ ([strength])
   [name]              X.XX  ██████████████████░░ ([strength])
   [name]              X.XX  █████████████████░░░ ([strength])

 Patterns Discovered: XX | Optimizations Applied: XX

 [CAPACITY]                                    [XX% used]
 ───────────────────────────────────────────────────────────────

 Subscription: [tier]
 Monthly Capacity: XXXX units
 Current Usage: XXX units [████████░░░░░░░░░░░░] XX%
 Efficiency Multiplier: X.Xx

 Projected Month-End: XXX units (XX% of capacity)
 Status: [On Track / At Risk / Over Budget]

 [LEARNING]
 ───────────────────────────────────────────────────────────────

 Patterns Discovered: XX
 Optimizations Applied: XX
 Last Analysis: YYYY-MM-DD HH:MM

 Top Patterns:
   • [pattern] → [employee] (+XX% efficiency)
   • [pattern] → [employee] (+XX% efficiency)
   • [pattern] → [employee] (+XX% efficiency)

 [INSIGHTS]
 ───────────────────────────────────────────────────────────────

 • [recommendation_1]
 • [recommendation_2]
 • [recommendation_3]

═══════════════════════════════════════════════════════════════
```

---

## Step 3: Capacity View (`--capacity` flag)

If `--capacity` flag is present:

```
═══════════════════════════════════════════════════════════════
 G6 ECONOMICS: CAPACITY ANALYSIS
 Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
═══════════════════════════════════════════════════════════════

 [SUBSCRIPTION]
 ───────────────────────────────────────────────────────────────

 Tier: [pro/enterprise/etc]
 Monthly Capacity Estimate: XXXX work units
 (1 work unit ≈ 1 standard task execution)

 [CURRENT PERIOD]
 ───────────────────────────────────────────────────────────────

 Period: [Month YYYY]
 Days Elapsed: XX of XX (XX%)

 Usage: XXX / XXXX units
 [████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░] XX%

 Daily Average: X.X units/day
 Projected Total: XXX units

 [EFFICIENCY IMPACT]
 ───────────────────────────────────────────────────────────────

 Base Capacity: XXXX units
 Efficiency Multiplier: X.Xx
 Effective Capacity: XXXX units

 Explanation:
   Higher efficiency = more output per unit
   Current efficiency (X.XX) provides X.Xx multiplier

 [CAPACITY BREAKDOWN BY DEPARTMENT]
 ───────────────────────────────────────────────────────────────

 | Department | Tasks | % of Total | Efficiency |
 |------------|-------|------------|------------|
 | Engineering | XX | XX% | X.XX |
 | Product | XX | XX% | X.XX |
 | Design | XX | XX% | X.XX |

 [RECOMMENDATIONS]
 ───────────────────────────────────────────────────────────────

 [If over budget:]
 ⚠ Usage on pace to exceed capacity by XX units
   • Prioritize high-impact tasks
   • Route to most efficient employees
   • Defer low-priority work

 [If under budget:]
 ✓ On track to use XX% of capacity
   • Capacity available for additional work
   • Consider queuing deferred tasks

═══════════════════════════════════════════════════════════════
```

---

## Step 4: Trends View (`--trends` flag)

If `--trends` flag is present:

```
═══════════════════════════════════════════════════════════════
 G6 ECONOMICS: EFFICIENCY TRENDS
 Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
═══════════════════════════════════════════════════════════════

 [COMPANY EFFICIENCY OVER TIME]
 ───────────────────────────────────────────────────────────────

 Current: X.XX | Week Ago: X.XX | Month Ago: X.XX
 Change: +X.XX (+XX%)

 Last 7 Days:
 Day 1 | ████████████████░░░░ | X.XX
 Day 2 | █████████████████░░░ | X.XX
 Day 3 | ██████████████████░░ | X.XX
 Day 4 | ███████████████████░ | X.XX
 Day 5 | ████████████████████ | X.XX
 Day 6 | ████████████████████ | X.XX
 Day 7 | ████████████████████ | X.XX

 Trend: [Improving / Stable / Declining]

 [EMPLOYEE TRENDS]
 ───────────────────────────────────────────────────────────────

 Improving:
   [name]: X.XX → X.XX (+XX%)
   [name]: X.XX → X.XX (+XX%)

 Declining:
   [name]: X.XX → X.XX (-XX%) ⚠
   [name]: X.XX → X.XX (-XX%) ⚠

 Stable:
   [name]: X.XX (no significant change)

 [PATTERN LEARNING]
 ───────────────────────────────────────────────────────────────

 Patterns Discovered Over Time:
   Week 1: X patterns
   Week 2: X patterns (+X new)
   Week 3: X patterns (+X new)
   Week 4: X patterns (+X new)

 Total Optimizations Applied: XX
 Estimated Efficiency Gain: +XX%

 [MEMORY EFFICIENCY]
 ───────────────────────────────────────────────────────────────

 Context Reuse Rate:
   Week 1: XX%
   Week 2: XX%
   Week 3: XX%
   Week 4: XX%

 Target: 70% | Current: XX% | Gap: XX%

═══════════════════════════════════════════════════════════════
```

---

## Step 5: Recommendations View (`--recommendations` flag)

If `--recommendations` flag is present:

```
═══════════════════════════════════════════════════════════════
 G6 ECONOMICS: AI RECOMMENDATIONS
 Generated: YYYY-MM-DD HH:MM UTC | Company: [company_name]
═══════════════════════════════════════════════════════════════

 [EXECUTIVE SUMMARY]
 ───────────────────────────────────────────────────────────────

 Current Efficiency: X.XX (Target: X.XX)
 Potential Improvement: +XX% with optimizations
 Top Priority: [routing/memory/quality]

 [HIGH PRIORITY]
 ───────────────────────────────────────────────────────────────

 1. ROUTING OPTIMIZATION
    Impact: High | Confidence: [high/medium]

    Description: Route [pattern] tasks to [employee]
    Expected Improvement: +XX% efficiency

    Reasoning: [employee] shows X.XX avg efficiency for this
    task type vs X.XX company average.

    Action: Apply pattern-based routing via /efficiency --optimize

 2. QUALITY IMPROVEMENT
    Impact: High | Confidence: Medium

    Description: Reduce escalation rate from XX% to <10%
    Expected Improvement: +XX% first-pass success

    Reasoning: Escalations waste resources on retry and
    human intervention.

    Action: Review escalated tasks for common patterns

 [MEDIUM PRIORITY]
 ───────────────────────────────────────────────────────────────

 3. MEMORY OPTIMIZATION
    Impact: Medium | Confidence: High

    Description: Increase context reuse rate to 70%
    Current Rate: XX%
    Expected Improvement: ~XX% token savings

    Action: Pre-load relevant context for task patterns

 4. CAPACITY UTILIZATION
    Impact: Medium | Confidence: Medium

    Description: Increase workload for [employee]
    Current Utilization: Low (X tasks)
    Efficiency Score: X.XX (above average)

    Action: Route more matching tasks to this employee

 [LOW PRIORITY]
 ───────────────────────────────────────────────────────────────

 5. TREND MONITORING
    Description: Monitor [employee] for declining efficiency
    Current Trend: Declining

    Action: Review recent tasks for blockers or training needs

 [IMPLEMENTATION ROADMAP]
 ───────────────────────────────────────────────────────────────

 Phase 1 (This Week):
   ☐ Apply routing optimizations
   ☐ Review escalated tasks

 Phase 2 (Next Week):
   ☐ Increase context pre-loading
   ☐ Rebalance task allocation

 Phase 3 (Ongoing):
   ☐ Monitor trends
   ☐ Refine patterns

 [EXPECTED OUTCOMES]
 ───────────────────────────────────────────────────────────────

 If all recommendations applied:
   • Efficiency: X.XX → X.XX (+XX%)
   • First-pass rate: XX% → XX%
   • Memory savings: +XX tasks worth of context
   • Capacity multiplier: X.Xx → X.Xx

═══════════════════════════════════════════════════════════════
```

---

## Step 6: Handle Empty States

### No Economics Data
```
[ECONOMICS] No data available
  No task executions recorded yet.
  Execute tasks to start tracking economics.
```

### No Patterns Discovered
```
[LEARNING] Insufficient data
  Need at least 5 task executions to discover patterns.
  Current: XX executions
```

### No Recommendations
```
[RECOMMENDATIONS] All systems optimal
  Company efficiency is on target.
  No urgent optimizations needed.
```

---

## Rules

- **Always fetch fresh data.** Don't cache between invocations.
- **Parse JSON carefully.** Handle missing fields gracefully with defaults.
- **Respect flags.** If a specific flag is passed, show ONLY that section with more detail.
- **Use ASCII art consistently.** Box-drawing characters for borders, block characters for bars.
- **Handle errors gracefully.** If a data source fails, show "Data unavailable" for that section.
- **Include generation timestamp.** Always show when the dashboard was generated.
- **Focus on actionable insights.** Every recommendation should have a clear action.
- **Show confidence levels.** Indicate how reliable each recommendation is.
- **Provide context.** Explain why recommendations are being made.
- **Track improvement over time.** Show before/after expectations.
