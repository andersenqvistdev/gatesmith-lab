# /workshop — Collaborative Multi-Participant Sessions

Run a structured workshop engaging multiple employees or teams. Workshops enable collaborative problem-solving, knowledge sharing, planning, and educational sessions that spread competence across the organization.

**Company-aware:** In company mode, workshops engage actual employees. In non-company mode, Claude facilitates a solo discussion capturing requirements.

## Input
$ARGUMENTS

Supported arguments:
- `<topic>` — Workshop topic or objective (required)
- `--scope=<scope>` — Who participates: `team:<id>`, `department:<id>`, `project:<id>`, or `company` (default: company)
- `--type=<type>` — Workshop type: `discussion`, `planning`, `learning`, `alignment`, `brainstorm`, `decision` (default: discussion)
- `--duration=<minutes>` — Expected duration (default: 60)
- `--facilitator=<employee-id>` — Who leads the workshop (default: most senior available)

Examples:
- `/workshop "API Design Standards"` — Company-wide discussion
- `/workshop "Sprint Planning" --scope=team:core --type=planning`
- `/workshop "GraphQL Best Practices" --scope=department:engineering --type=learning`
- `/workshop "Company Vision Q1" --type=alignment --scope=company`

## Step 0: Check Environment

### 0.1: Detect Company Mode

Check if `.company/org.json` exists:

```bash
ls -la .company/org.json 2>/dev/null
```

**If exists:** Company mode — engage employees
**If not exists:** Non-company mode — Claude facilitates solo

### 0.2: Load Context

Read relevant context:
- `.company/org.json` — Organization structure, employees
- `.company/vision.md` — Company vision (if exists)
- `.planning/ROADMAP.md` — Current roadmap
- `.company/knowledge/workshops.md` — Previous workshops

## Step 1: Parse Arguments & Determine Participants

### 1.1: Parse Arguments

| Argument | Default | Options |
|----------|---------|---------|
| topic | (required) | Free text |
| scope | company | team:<id>, department:<id>, project:<id>, company |
| type | discussion | discussion, planning, learning, alignment, brainstorm, decision |
| duration | 60 | Minutes |
| facilitator | auto-select | Employee ID |

### 1.2: Resolve Participants (Company Mode)

Based on scope, identify workshop participants:

**scope=company:**
- Include all available employees
- Prioritize department heads and team leads
- Include executives (CEO, CTO) for alignment/decision types

**scope=department:<id>:**
- Include all employees in specified department
- Include department head as facilitator (if not specified)

**scope=team:<id>:**
- Include all team members
- Include team lead as facilitator (if not specified)

**scope=project:<id>:**
- Include employees assigned to the project
- Cross-functional representation

### 1.3: Select Facilitator

If facilitator not specified:
- For `alignment` type: CEO or most senior executive
- For `learning` type: Subject matter expert or CTO
- For `planning` type: Project lead or department head
- For `decision` type: Most senior stakeholder
- Default: Most senior available participant

### 1.4: Validate Participants

Ensure minimum viable participation:
- At least 2 participants for company mode
- Warn if key roles missing for workshop type

```
## Workshop Setup

═══════════════════════════════════════════════════════════════════════════════
 WORKSHOP                                                    [type: <type>]
═══════════════════════════════════════════════════════════════════════════════
 Topic: <topic>
 Scope: <scope>
 Duration: <duration> minutes
 Facilitator: <facilitator-name> (<role>)
═══════════════════════════════════════════════════════════════════════════════

### Participants (N)

| Employee | Role | Department | Status |
|----------|------|------------|--------|
| alice-eng | Senior Engineer | Engineering | available |
| bob-design | UX Lead | Design | available |
| carol-pm | Product Manager | Product | available |

### Workshop Agenda

1. Opening & Context (5 min)
2. Main Discussion (40 min)
3. Action Items (10 min)
4. Closing & Next Steps (5 min)

Proceed with workshop? [Y/n]
```

## Step 2: Run Workshop by Type

### Type: discussion

Open-ended discussion to explore a topic.

**Facilitator opens:**
```
[FACILITATOR: <name>]

Welcome to our workshop on "<topic>".

Today's goal: Gather diverse perspectives and reach shared understanding.

Let's go around and share initial thoughts. <first-participant>, what's your perspective?
```

**Simulate participant contributions:**
For each participant, generate a response based on their role, expertise, and perspective:

```
[<participant-name> (<role>)]

<Contribution based on their domain expertise and role perspective>
```

**Capture key points and areas of agreement/disagreement.**

### Type: planning

Collaborative planning session.

**Structure:**
1. Review current state (roadmap, progress)
2. Identify gaps and blockers
3. Propose solutions/priorities
4. Assign ownership
5. Define timeline

**Output:** Updated ROADMAP.md or action items

### Type: learning

Educational session to spread knowledge.

**Structure:**
1. Expert presents topic (based on source material)
2. Q&A with participants
3. Hands-on examples or exercises
4. Knowledge check
5. Document learnings

**Source material:** Check `.company/knowledge/` for relevant patterns/decisions

**Output:** Updated employee learnings, new patterns documented

### Type: alignment

Vision and roadmap alignment check.

**Structure:**
1. Review company vision (from `.company/vision.md`)
2. Review current roadmap
3. Each participant shares understanding of:
   - Company direction
   - Their role in achieving goals
   - Current priorities
4. Identify misalignments
5. Clarify and correct

**Output:** Alignment report, knowledge gaps identified, follow-up actions

### Type: brainstorm

Creative ideation session.

**Structure:**
1. Define problem/opportunity
2. Diverge: Generate ideas (no criticism)
3. Group and categorize ideas
4. Converge: Prioritize top ideas
5. Define next steps

**Output:** Idea list, prioritized recommendations

### Type: decision

Decision-making session.

**Structure:**
1. Present decision context
2. Review options
3. Each participant states position with rationale
4. Discuss trade-offs
5. Reach decision (consensus or authority)
6. Document decision as ADR

**Output:** ADR entry in `.company/knowledge/decisions.md`

## Step 3: Facilitate Discussion Rounds

For each round of the workshop:

### 3.1: Gather Perspectives

Simulate each participant contributing based on:
- Their role and expertise
- Previous workshop contributions (from memory)
- Known opinions/patterns from their employee file

```
### Round 1: Initial Perspectives

[alice-eng (Senior Engineer)]
From a technical standpoint, I think we should consider...

[bob-design (UX Lead)]
From the user experience perspective, my concern is...

[carol-pm (Product Manager)]
Looking at our roadmap and priorities, I'd suggest...
```

### 3.2: Identify Themes

Synthesize contributions into themes:

```
### Emerging Themes

| Theme | Supporters | Key Points |
|-------|------------|------------|
| API Consistency | alice-eng, carol-pm | Need standard patterns |
| User Experience | bob-design | Current flow is confusing |
| Timeline Concerns | carol-pm | Q2 deadline tight |
```

### 3.3: Resolve Conflicts

If disagreements exist:

```
### Points of Discussion

**Topic:** <point of disagreement>

| Position | Advocates | Rationale |
|----------|-----------|-----------|
| Option A | alice-eng | Technical simplicity |
| Option B | bob-design | Better UX |

**Resolution:** [Facilitator guides to consensus or escalates]
```

## Step 4: Generate Outputs

### 4.1: Workshop Summary

```
## Workshop Summary

═══════════════════════════════════════════════════════════════════════════════
 WORKSHOP COMPLETE                                           [<date>]
═══════════════════════════════════════════════════════════════════════════════
 Topic: <topic>
 Type: <type>
 Duration: <actual-duration> minutes
 Participants: <count>
 Facilitator: <name>
═══════════════════════════════════════════════════════════════════════════════

### Key Outcomes

1. <outcome-1>
2. <outcome-2>
3. <outcome-3>

### Decisions Made

| Decision | Rationale | Owner |
|----------|-----------|-------|
| <decision> | <why> | <who> |

### Action Items

| ID | Action | Owner | Due | Priority |
|----|--------|-------|-----|----------|
| WS-001 | <action> | <owner> | <date> | High |
| WS-002 | <action> | <owner> | <date> | Medium |

### Knowledge Captured

| Type | Title | Location |
|------|-------|----------|
| Pattern | <name> | .company/knowledge/patterns.md |
| ADR | <title> | .company/knowledge/decisions.md |

### Follow-up

- Next workshop: <date> (if recurring)
- Review date: <date>
- Escalations: <any items needing executive attention>

═══════════════════════════════════════════════════════════════════════════════
```

### 4.2: Update Knowledge Base

**Append to `.company/knowledge/workshops.md`:**

```markdown
---

## WS-<id>: <topic>

**Date:** YYYY-MM-DD
**Type:** <type>
**Scope:** <scope>
**Duration:** <minutes> minutes
**Facilitator:** <name>
**Participants:** <count> (<list>)

### Summary
<1-2 paragraph summary>

### Key Decisions
- <decision-1>
- <decision-2>

### Action Items
- [ ] WS-<id>-001: <action> (@<owner>)
- [ ] WS-<id>-002: <action> (@<owner>)

### Learnings
<What the organization learned from this workshop>

### Related
- Previous: WS-<prev-id>
- Follow-up: WS-<next-id> (scheduled)
```

### 4.3: Update Employee Learnings (for learning type)

For each participant, append to their learnings file:

```markdown
## Workshop Learning: YYYY-MM-DD

**Workshop:** <topic>
**Role:** Participant | Facilitator | Presenter

### Key Takeaways
- <learning-1>
- <learning-2>

### Action Items Assigned
- WS-<id>-<n>: <action>
```

## Step 5: Non-Company Mode

If no company structure exists, Claude facilitates a solo discussion:

```
## Workshop Mode: Solo Facilitation

No company structure detected. Claude will facilitate a structured discussion
to explore this topic with you.

═══════════════════════════════════════════════════════════════════════════════
 TOPIC: <topic>
 TYPE: <type>
═══════════════════════════════════════════════════════════════════════════════

### Opening Questions

Based on your topic, let's explore:

1. What problem are you trying to solve?
2. What constraints or requirements exist?
3. What have you already considered?

[Proceed with AskUserQuestion for structured discussion]
```

Use `AskUserQuestion` to gather input, then synthesize and document outcomes.

## Rules

- **Simulate realistic participation.** Each employee contributes based on their documented expertise and role.
- **Capture everything.** All decisions, action items, and learnings must be documented.
- **Respect scope.** Only include participants within the specified scope.
- **Generate unique IDs.** Workshop IDs follow WS-NNNN format.
- **Update employee records.** Workshop participation affects employee learnings.
- **Enable follow-up.** Action items must have owners and due dates.
- **Support recurring workshops.** Track workshop series for ongoing topics.
- **Integrate with knowledge base.** New patterns/decisions flow into knowledge base.
- **Handle conflicts gracefully.** Document disagreements and resolution process.
- **Non-company fallback.** Always provide value even without company structure.
