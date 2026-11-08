# /review — Code Review

Spawn a Reviewer agent to analyze the current state of the code.

## Input
$ARGUMENTS

If arguments specify files or a PR, review those. Otherwise, review all uncommitted changes.

## Step 1: Determine Scope

If no specific files given, run:
```bash
git diff --name-only          # unstaged changes
git diff --cached --name-only # staged changes
```

## Step 2: Launch Reviewer

```
Task(subagent_type="general-purpose", description="Code review")
```

Pass the Reviewer:
- The list of files to review
- Instruction to read `.claude/agents/reviewer.md` for full rules
- If reviewing a PR: the PR description and commit messages for context

## Step 3: Present Results

Show the Reviewer's full structured output to the user.

If the review finds CRITICAL issues, highlight them prominently and ask:
"There are X critical issues. Want me to fix them with /build?"
