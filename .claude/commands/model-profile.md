# /model-profile — Switch Model Profile

Show the model profiles and switch the one the daemon uses for each task.

> **This command never edits the config.** Config files are human-protected: the Write/Edit
> tools are blocked on them in every session, by design. The switch is done by
> `bin/gatesmith-model-profile`, which the human runs. It changes only the `modelProfile` value
> (and `adaptiveScheduler.maxParallelWorkers` with `--sync-workers`) in the file's own text,
> and refuses without writing anything if it cannot do exactly that.

## Input

$ARGUMENTS

Supported usage:
- `/model-profile` or `/model-profile --list` — Show the active profile and all profiles
- `/model-profile <name>` — Give the command that switches to `<name>`
- `/model-profile <name> --sync-workers` — The same, and align `maxParallelWorkers`

## Step 1: Show the profiles

First decide which project is meant: the repository the user is working on, which can differ
from the directory this command file came from (a session can have several repositories).
Each repository has its own config, and its daemon reads only that one. If it is unclear, ask.
Then run the tool **in that repository**:

```bash
cd <project> && bin/gatesmith-model-profile --list
```

It reads the file that governs `modelProfile` there: the root `gatesmith.json` when it
declares the setting, otherwise the install template `.claude/gatesmith.json` (the same rule
as every other setting, see `governing_config.py`). Show its table as printed, and always name
the file it read in the heading (the tool prints it as `Config:`), then:

```
To switch: /model-profile <name>
```

If there are no arguments, or only `--list`, stop here.

## Step 2: Validate the name

The profile must appear in the table from Step 1. If it does not, print:

```
Profile "<name>" not found.

Available profiles: <comma-separated list>
```

and stop. If it is already the active profile, print `[NO CHANGE]` and stop.

If the profile has a `workers` value that differs from `adaptiveScheduler.maxParallelWorkers`
and `--sync-workers` was not given, say so in one line and mention `--sync-workers`.

## Step 3: Give the human the command

```
═══════════════════════════════════════════════════════════════
 MODEL PROFILE — <old> → <new>
═══════════════════════════════════════════════════════════════
 Run this yourself (the config is human-protected):

   !cd <project> && bin/gatesmith-model-profile <name> [--sync-workers]

 It changes only modelProfile (and maxParallelWorkers with
 --sync-workers) and prints the before/after table.
 The model takes effect on the next task; a new worker
 limit (--sync-workers) needs a daemon restart.
═══════════════════════════════════════════════════════════════
```

Do not run the command yourself, through Bash or any other way: a human-protected file is
changed by a human.

## Step 4: Verify

When the human says it ran, run `cd <project> && bin/gatesmith-model-profile --list` again and confirm the active
profile is `<name>` (and the worker count, with `--sync-workers`). If the tool printed
`Nothing was written`, show its reason; the human then edits the file by hand.

## Rules

- **Name the project and the file.** Never assume the project from where this command came from.
- **Never edit or rewrite a config file.** Show the command; the human runs it.
- **Validate before suggesting.** Never suggest an unknown profile name.
- **Warn on a worker mismatch;** `--sync-workers` is the fix.
- **Short model names** (Haiku, Sonnet, Opus, Fable) in tables, as the tool prints them.
