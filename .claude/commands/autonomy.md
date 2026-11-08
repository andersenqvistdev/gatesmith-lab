# /autonomy — The Auto-Merge Ladder

View the auto-merge level and get the exact edit to move one level up or down.

Three settings in `autonomy.autoMerge` decide whether a program may merge to main:
`enabled`, `allowMergeToMain` and `requireApproval`. The merge gate
(`merge_gate.may_merge`) reads them, and every merge path asks it. They give three levels:

| Level | `enabled` | `allowMergeToMain` | `requireApproval` | What happens |
|---|---|---|---|---|
| **0 — Manual** | `false` | any | any | No program merges. The gate refuses with `enabled=false`. |
| **1 — Gated, you merge** | `true` | `true` | `true` | Every check runs, and a human merges. The gate refuses with `requireApproval=true (a human merges)`. |
| **2 — Judge-gated auto-merge** | `true` | `true` | `false` | A pull request that passes every check of the gate merges without a human. |

Any other combination also refuses every merge: show it as "refuses every merge" and name the
setting that refuses first, in the order `enabled`, `allowMergeToMain`, `requireApproval`.

> **INTERACTIVE USE ONLY.** This command must never run inside a daemon worker. If the
> environment variable `FORGE_WORKER_ID` is set, refuse immediately and print:
> `ERROR: /autonomy cannot be run inside a daemon worker. Run this in your interactive session.`
>
> **This command never edits the config.** Both config files are human-protected: the
> Write/Edit tools are blocked on them in every session, by design. The command shows the
> human the one change to make, waits, and then checks it.

## Input

$ARGUMENTS

Supported usage:
- `/autonomy` — Show the current level
- `/autonomy on` — Level 1 → 2 (set `requireApproval` to `false`)
- `/autonomy off` — Level 2 → 1 (set `requireApproval` to `true`)
- `/autonomy stop` — any level → 0 (set `enabled` to `false`): the emergency stop

## Step 1: Load Config

Read the block from **the governing config**: the root `gatesmith.json` when it declares
`autonomy.autoMerge`, otherwise the install template `.claude/gatesmith.json` (a fresh install
has only the template). Every merge lever uses the same rule:

```bash
uv run .claude/hooks/company/governing_config.py get autonomy.autoMerge \
  | jq '{source, error, enabled: .value.enabled, allowMergeToMain: .value.allowMergeToMain, requireApproval: .value.requireApproval}'
```

Store `source` (the file that governs) and the three values. If `error` is set, show it and
stop: every merge lever refuses until the file is fixed. If `source` is null, neither file
declares the block, which is Level 0; say so and suggest `/gatesmith-start`.

## Step 2: Parse Arguments

- empty → Step 3 (show)
- `on` → Step 4
- `off` → Step 5
- `stop` → Step 6
- anything else → print the usage above and exit.

## Step 3: Display Current State

```
═══════════════════════════════════════════════════════════════
 AUTONOMY                                     [<source>]
═══════════════════════════════════════════════════════════════
 enabled:            <value>
 allowMergeToMain:   <value>
 requireApproval:    <value>
═══════════════════════════════════════════════════════════════
 Level <N> — <name from the table>
═══════════════════════════════════════════════════════════════
```

Then one line on how to move: `/autonomy on` (1 → 2), `/autonomy off` (2 → 1),
`/autonomy stop` (→ 0). Level 0 → 1 is a policy decision with more than one setting; point to
`enabled` and `allowMergeToMain` (both `true`) and `requireApproval` (`true`) in `<source>`.

## Step 4: Level 1 → 2 (`/autonomy on`)

### 4.0 CI Liveness Gate

Before allowing auto-merge, verify that CI is active and branch protection requires it.
Allowing auto-merge on a repo with no CI gate lets the daemon merge pull requests that break
the codebase.

```bash
bin/gatesmith-protect-main --ensure-ci
```

This command (in order):
1. Looks for workflow files under `.github/workflows/`. If none exist, writes a minimal
   `ci.yml` and exits with `[ACTION REQUIRED]`: commit and push it, then run it again.
2. Checks GitHub for a recent successful Actions run; if none exists, triggers one and waits
   up to 10 minutes.
3. Applies branch protection to main with the CI checks required.

If it exits non-zero, show its output and stop. Do not suggest the edit.

### 4.1 Preconditions

- Not at Level 1 (Step 3) → show the state and stop. `on` only moves 1 → 2.
- Already at Level 2 → print `[NO CHANGE]` and stop.
- List pull requests that could merge as soon as the setting changes, and include them in
  the panel below:

```bash
gh pr list --label auto-merge-ready --state open --json number,title,headRefName
```

### 4.2 Show the edit

```
═══════════════════════════════════════════════════════════════
 AUTONOMY — LEVEL 1 → 2                              [WARNING]
═══════════════════════════════════════════════════════════════
 File:    <source>   (human-protected: you make this edit)
 Change:  "autonomy" → "autoMerge" → "requireApproval": true  →  false
          Change nothing else.

 What this means:
   A pull request that passes every check of the merge gate
   (policy, CI, the deliverable judge, blocked paths, size)
   merges without you. Everything else still waits for you.

 Before you edit, confirm:
   □ CI is green on main right now
   □ blockedPaths covers every file you never want auto-merged
   □ You will run the canary pair right after (Step 7)
═══════════════════════════════════════════════════════════════
```

Edit `<source>`, the file that governs now. Do not create a root `gatesmith.json` for
this: a new root file would start to govern and hide the settings you already have.
`install.sh --upgrade` keeps an existing `.claude/gatesmith.json`.

Wait until the human says the edit is done.

### 4.3 Verify

Run Step 1 again. Confirm:
- `source` is the same file as before;
- `requireApproval` is `false`, and `enabled` and `allowMergeToMain` did not change;

If any check fails, say which one and stop. Then show Step 7.

## Step 5: Level 2 → 1 (`/autonomy off`)

Not at Level 2 → print `[NO CHANGE]` and stop. Otherwise show the edit as in 4.2 with
`"requireApproval": false  →  true`, wait, and verify as in 4.3 (`requireApproval` is `true`;
the gate refuses with `requireApproval=true (a human merges)`).

Then check what the config does not stop: GitHub's own auto-merge, once armed on a pull
request, merges when its checks pass, whatever this file says.

```bash
gh pr list --state open --json number,title,autoMergeRequest \
  | jq '.[] | select(.autoMergeRequest != null)'
```

Disarm any that should not land: `gh pr merge <number> --disable-auto`.

## Step 6: Emergency stop (`/autonomy stop`)

Show the edit `"enabled": true → false` in `<source>`, then stop the daemon so no worker is
left running:

```bash
./bin/gatesmith-maintenance on "autonomy stop"
uv run .claude/hooks/company/forge_daemon.py stop
```

`stop` can take up to about five minutes while a worker finishes. Wait for it; do not kill
the process. Verify as in 4.3 (`enabled` is `false`, the gate refuses with `enabled=false`),
then disarm GitHub auto-merge as in Step 5. Turn maintenance off only when you mean to start
again: `./bin/gatesmith-maintenance off`.

## Step 7: Canary after every change

After `on` or `off`, prove the new level with two tasks before you trust it:

1. Queue one real task (a small, clear change) and one bait task whose easy answer is an
   empty or test-only diff.
2. Start the daemon (`/daemon start`) and watch: `./bin/gatesmith-queue watch`.
3. At Level 2: the real pull request merges; the bait is held by the deliverable judge and
   does not merge. At Level 1: both stay open for you.
4. Read each verdict where the gate reads it: the pull request's labels
   (`gh pr view <number> --json labels`) and `.company/state/deliverable_gate.jsonl`.
5. Run `bin/gatesmith-canary --mode preconditions`: its `config-governing` row names the file
   that governs and the three values.

If anything merged that should not have: `/autonomy stop`, then investigate.

## Rules

- **Refuse if FORGE_WORKER_ID is set.**
- **Never edit either config file.** Show the edit; the human makes it; then verify.
- **Edit the governing file.** Never create a root config to change one setting.
- **One setting per step.** `on`/`off` change only `requireApproval`; `stop` only `enabled`.
- **Run the CI liveness gate before every `on`.**
- **Read fields with jq or the resolver.** Never paste the whole config.
- **Detect no-op early.** If the level already matches, exit without asking for an edit.
- **Canary after every change.**
