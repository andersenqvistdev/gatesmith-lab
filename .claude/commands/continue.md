# /continue — Resume Previous Session (from GSD)

Pick up where you left off. Reads session state and continues work.

## Step 1: Load State

Read `.planning/STATE.md` to understand:
- What phase we're in
- What was the last completed task
- What's the next task
- Current branch and commit

## Step 2: Load Context

Read all planning docs for full context:
- `.planning/PROJECT.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP.md`
- `.planning/DISCUSS.md`

## Step 3: Verify Git State

```bash
git status
git log --oneline -5
```

Check that the branch and commit match what STATE.md expects. If they diverge, inform the user.

## Step 4: Report & Continue

```
## Session Resumed

### Last Session
- Phase: [phase]
- Last completed: [task]
- Branch: [branch]

### Remaining Work
[list of incomplete tasks from ROADMAP.md]

### Picking up with:
[next task description]
```

Then immediately begin working on the next task — either directly if trivial, or by running the appropriate command (`/plan`, `/build`, etc.).
