# Continue the build — {{PR_REF}} (round {{ROUND}} of {{MAX_ROUNDS}})

You are continuing an unfinished pull request for a product goal. A challenger
reviewed the branch and found it incomplete; its report is below. Finish the
work on this branch: the same pull request, no new one.

You are working in a checkout of the pull request's branch:
`{{WORKTREE}}` (branch `{{BRANCH}}`, head `{{HEAD_SHA}}`, base `origin/main`).

## The original task

{{TASK_DESCRIPTION}}

## The goal

{{GOAL_ROW}}

The spec is `{{SPEC_PATH}}`; read the sections the goal cites.

## The goal's acceptance entries

Entry ids: {{ENTRY_IDS}}

Run them the ledger way with:

```
{{RUN_ENTRIES_COMMAND}}
```

## The challenger's report

{{REPORT}}

## Rules

1. Build every missing surface and fix every open finding the report names,
   each with a test. Keep to the goal: no unrelated changes.
2. Never edit `acceptance/` (the oracle), `.claude/`, `.github/` or any
   human-protected file.
3. Run the acceptance entries above, the unit tests, lint and type checks.
4. Do NOT run git add, commit or push — the system commits your changes to
   this branch when you finish, and a challenger reviews them again.
5. This is a one-shot run: there is no later turn. If time runs short, stop
   long-running work and leave the code in its best working state.
