# /calibrate — Verdict-Calibration Autonomy Audit

Measure Gatesmith's *real* autonomy by verifying that tasks marked "complete" actually shipped a merged PR — exposing the phantom-completion leak and producing a trustworthy autonomy number.

## Usage

```bash
# Full audit on the current build, persist the snapshot
/calibrate --write

# Quick proxy only (no gh / no agents)
/calibrate --proxy-only

# Limit the verification sample (default: all completed-with-PR tasks)
/calibrate --sample 25

# Skip the semantic Tier-2 judgment (gh merged-check only)
/calibrate --no-tier2
```

## Arguments

- `--write` — persist the snapshot to `.company/state/autonomy_audit.json` (surfaces on `/dashboard` + `/company-health`)
- `--proxy-only` — print only the deduped local proxy; no network, no agents
- `--sample <N>` — verify at most N completed-with-PR tasks (default: all)
- `--no-tier2` — run only deterministic Tier-1 gh merged-check, skip the semantic judge pass

## Instructions

<command name="calibrate">
Run a two-tier verdict-calibration audit. The heavy lifting lives in
`.claude/hooks/company/autonomy_metrics.py` (deterministic) and
`.claude/workflows/calibrate.js` (Tier-2 fan-out). Do NOT reimplement the math
inline — call those.

**Procedure:**

1. **Local proxy (always):**
   ```bash
   uv run .claude/hooks/company/autonomy_metrics.py proxy
   ```
   This is the deduped `distinct_completed_with_pr / distinct_tasks_queued`. It is
   an UPPER BOUND — a `pr_url` proves a PR was opened, not merged. If `--proxy-only`,
   print this and stop.

2. **Tier-1 ground truth (deterministic gh merged-check):**
   ```bash
   uv run .claude/hooks/company/autonomy_metrics.py tier1
   ```
   This returns `{results, survivors, build_sha}`. Each result is merged/phantom
   per `gh pr view <n> --json state,mergedAt,additions,deletions` (MERGED + non-null
   mergedAt + additions+deletions>0). It FAILS CLOSED: any gh error / unmerged /
   empty diff is a phantom. Apply `--sample N` by truncating the candidate list
   first (`autonomy_metrics.py candidates`) if requested. If gh is unauthenticated
   (`gh auth status` fails), STOP and tell the user — do not report phantoms that
   are really auth failures.

3. **Tier-2 semantic judgment (unless `--no-tier2`):** run the workflow over the
   Tier-1 `survivors` to catch rubber-stamp / wrong-PR / trivial-diff phantoms a
   merged-check can't:
   ```
   Workflow({ scriptPath: ".claude/workflows/calibrate.js", args: <survivors list> })
   ```
   It returns `{judgments: [{task_id, addresses_task, confidence, reason}]}`.

4. **Summarize:** combine Tier-1 + Tier-2 with
   `autonomy_metrics.summarize_ground_truth(tier1, tier2, distinct_tasks_queued=<proxy value>,
   window_days=30, distinct_tasks_queued_windowed=<compute_autonomy(window_days=30).distinct_tasks_queued>)`
   to get `trust_score`, `phantom_rate`, the lifetime `verified_autonomy_rate`, AND
   the TREND-honest `verified_autonomy_rate_windowed` (recent cohort only — Phase 0).
   Build the `phantoms[]` list from Tier-1 non-merged results plus Tier-2
   `addresses_task=false` judgments (include `task_id`, `pr_url`, `reason`).
   The windowed number is the one to LEAD with — the lifetime rate is dragged down
   by a fixed historical graveyard (see `.planning/autonomy/00-MASTER.md`).

5. **Persist (if `--write`):** call
   `autonomy_metrics.build_snapshot(proxy, ground_truth, phantoms, build_sha=<sha>)`
   then `autonomy_metrics.append_autonomy_audit(Path(".company"), snapshot)`.

6. **Feed the learning loop (Phase 3):** record the *semantic* (Tier-2
   `addresses_task=false`, NON-errored) phantoms as learned anti-patterns so the
   proactive/initiative generator and the admission gate stop re-proposing that
   class. Build a JSON list of `{task_id, title, reason, pr}` from those judgments
   joined with their survivor titles (EXCLUDE any judgment flagged `errored` — those
   are transient failures, not phantoms), write it to a temp file, then:
   ```bash
   uv run .claude/hooks/company/learned_antipatterns.py record-batch --path /tmp/calibrate_phantoms.json --source calibrate
   ```
   Skip Tier-1 not-merged phantoms here — they are merge failures, not task-quality
   classes. This step is read-only against PRs/queue; it only appends to
   `.company/knowledge/anti_patterns.json`.

7. **Report** an ASCII summary:

```
═══════════════════════════════════════════════════════════════
  AUTONOMY CALIBRATION  ·  build <sha>  ·  <timestamp>
═══════════════════════════════════════════════════════════════
  Verified (30d) .......... XX.X%   ← LEAD: current capability (recent cohort)
  Verified (all-time) ..... XX.X%   (lifetime; dragged by historical graveyard)
  Local proxy ............. XX.X%   (79 / 159 distinct tasks reached a PR)
  Trust score ............. XX.X%   (of "complete" tasks that truly shipped)
  Phantom rate ............ XX.X%   ← the leak
───────────────────────────────────────────────────────────────
  Phantoms (N):
    task-… → PR #…  (not merged | empty diff | diff doesn't address task)
═══════════════════════════════════════════════════════════════
```

**Guardrails:**
- Read-only against the repo and GitHub. Never modify tasks, PRs, or the queue.
- Fail closed on any gh error — never count an unverifiable task as shipped.
- The gap between *local proxy* and *verified autonomy* IS the phantom leak; lead
  with it. This is the Phase-2 baseline — record the `build_sha`.
</command>

## When to Use

| Scenario | Use /calibrate? |
|----------|-----------------|
| Establish the honest autonomy baseline before an architecture change | Yes |
| After landing daemon/executor fixes, to confirm they helped | Yes |
| Need a quick deduped completion proxy, no network | Yes — `--proxy-only` |
| GitHub / `gh` is unauthenticated | No — authenticate first |

## Related Commands

- `/dashboard` — shows the latest calibrated autonomy + trust line
- `/company-health` — full report including the autonomy & trust section
- `/morning-report` — overnight merged-PR activity
