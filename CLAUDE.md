# Project instructions

This project uses [Gatesmith](https://github.com/gatesmithdev/gatesmith): AI-assisted
development with deterministic safety controls. Edit this file freely; Gatesmith only writes it
when a project has no `CLAUDE.md`, and never overwrites it afterwards.

## What the guardrails do

Hooks in `.claude/hooks/` run on every action and cannot be talked out of a decision.

| Tier | Examples | What happens |
|------|----------|--------------|
| Free | Read, Glob, Grep, `git status` | Runs automatically |
| Guarded | Write, Edit, `git commit` | Runs automatically, checked by hooks |
| Gated | `rm`, `docker`, network requests to unlisted hosts | Asks you first (blocked outright when the session cannot show a prompt) |
| Forbidden | `rm -rf`, `sudo rm`, push or force-push to `main`/`master` | Blocked |

`git push` is not a prompt. A push or force-push to `main`, `master`, `production` or
`release` is blocked by `git_guardian.py`; a push to any other branch, a force-push included,
runs without asking.
The human step is reviewing and merging the pull request (enforce it with branch protection
on GitHub). `rm` asks first, `rm -rf` is denied, and `block_dangerous.py` blocks recursive
deletes aimed at `/`, `~`, `$HOME`, system directories, the current directory or an
unexpanded `$VAR`. See [SECURITY.md](https://github.com/gatesmithdev/gatesmith/blob/main/SECURITY.md)
for the full tier model.

Hooks also block API keys and tokens in code and in commits. If a hook blocks something,
read its message; do not look for a way around it.

## Git workflow

Never push to `main`. Work on a branch and open a pull request:

```bash
git checkout -b feat/my-change
git add <files> && git commit -m "feat: describe the change"
git push -u origin feat/my-change
gh pr create
```

## Commands

| Need | Command |
|------|---------|
| Plan a change | `/plan` |
| Build it | `/build` |
| Review it | `/review` |
| Check it is complete | `/verify` |
| Load project context | `/prime` |
| Hand work to the background worker | `/submit` |

More commands are installed but unsupported and may change; they are listed in
`EXPERIMENTAL.md`. Start with the ones above.

## Work queue

Submitted work waits in a queue that the background worker (the daemon) drains.

```bash
./bin/gatesmith-queue          # show the queue
./bin/gatesmith-queue watch    # follow it live
/daemon start              # start the worker
/daemon stop               # stop it
```

## Pausing the worker while you edit its own code

If you change Gatesmith's engine files in this project, pause the worker first so it does not
race your edits, and resume afterwards:

```bash
./bin/gatesmith-maintenance on "reason"   # blocks the worker from starting
./bin/gatesmith-maintenance off           # always run this when you are done
```

## Project notes

Add what Claude should know about this project below: stack, how to run tests, conventions.
