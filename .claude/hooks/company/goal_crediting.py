"""Automated goal crediting: a merged goal-minted task -> a verified goal.

Why this exists
---------------
``assess_generic_success_metric`` reads a product goal as COMPLETE only from a
record in ``state/goal_verification_state.json``, and before this module the
only writer of that record was the manual ``goal_tracker.py verify`` CLI. A met
product goal therefore kept reading not_started and autofill re-minted it
forever (mdlint sandbox: G5 203x, G2 126x, G6 125x).

What it does
------------
For each completed ``[QUEUE-FILL] Gn:`` task (``source == "gap_analysis"``)
with a PR, :func:`credit_goals_from_merged_tasks` credits goal ``Gn`` only if
ALL of these hold, cheapest check first:

1. the goal is in vision.md, is NOT Gatesmith's own goal (its real assessor stays
   the only source of truth) and is not already verified;
2. the PR is MERGED with a diff, merged within :data:`CREDIT_WINDOW_DAYS`;
3. the PRE-MERGE gate verdict for the merged head sha is passing, and no
   negative verdict exists for that sha anywhere (a later nightly calibration
   re-judge never turns a gate REJECT into a pass) -- OR, when that verdict is
   missing (e.g. an operator committed fix rounds on top of the daemon's head
   after the gate last ran), the goal's own acceptance-ledger entries read
   CONSISTENT in an ELIGIBLE ledger run (see
   ``goal_tracker._ledger_credit_check``): a run on "main", not flagged
   environment_error / environment_suspect, whose commit contains the PR's
   merge commit and is itself an ancestor of origin/main, fetched just before
   the check (never the possibly-stale local branch). A run behind main's head
   still counts. The ledger is an oracle on main itself, not on this one PR's
   sha, so it can certify a goal the gate never saw the final state of.
   "Not yet" is never final: a failed fetch, no eligible run containing the
   merge, or eligible runs on fewer than
   ``goal_tracker.LEDGER_CREDIT_FINAL_AFTER_RUNS`` (3) distinct commits reading
   the entries failing is a ``retry``, re-checked with
   back-off from :data:`LEDGER_RECHECK_SECONDS` up to
   :data:`LEDGER_RECHECK_MAX_SECONDS` without spending a transport attempt.
   Only failing runs on that many commits ledger a final ``no_passing_verdict``.
   A ledger confirmation is still refused when origin/main carries a revert of
   the merge commit (a run that predates the revert saw code main no longer has);
4. the merge commit is still on the default branch and was not reverted;
5. an ISOLATED goal-level judge agrees the merged diff satisfies the goal's
   success metric as written now. The task verdict answered a narrower
   question ("does the diff address this brief"), so it gates the attempt but
   never certifies the goal alone -- a goal built across several PRs is not
   credited off whichever piece merged first; autofill's mint brake asks the
   operator instead.

The goal judge here writes nothing the task gate owns: not the sha-keyed
verdict cache, not ``deliverable_gate.jsonl``, not a learned anti-pattern. Its
own audit trail is ``state/goal_credit_judgments.jsonl``.

How it runs
-----------
Reconcile calls :func:`start_credit_sweep`, which starts ONE background thread
and returns at once (an in-flight sweep makes it a no-op), so gh and a judge
call of up to ``timeoutSeconds`` never block the daemon loop. The judge runs in
its own session and its whole process group is killed at its timeout and at
interpreter exit (atexit), the LEDGER-5c acceptance-step pattern. A
cross-process flock keeps a CLI reconcile and the daemon from sweeping at once.

Every outcome lands in ``state/goal_credit_ledger.json``. Definitive outcomes
are permanent. Transport failures (gh, git, judge timeout / 529 / unparseable)
are ``retry`` entries with exponential backoff; they never credit.

Re-queueing a settled record
----------------------------
``python goal_crediting.py requeue --task <task_id> [--task ...]`` or
``--outcome no_passing_verdict`` sets matching records back to ``retry`` (due
now), so the next sweep re-evaluates them under the current rules. It exists
for the final ``no_passing_verdict`` records the pre-2026-10-03 ledger fallback
wrote from a run that predated the merge. ``--company-dir`` picks the
``.company`` directory; ``--dry-run`` only lists the matches.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LEDGER_FILE = "state/goal_credit_ledger.json"
LOCK_FILE = "state/goal_credit.lock"
JUDGMENT_LOG_FILE = "state/goal_credit_judgments.jsonl"
GATE_LOG_FILE = "state/deliverable_gate.jsonl"
VERDICT_CACHE_FILE = "state/deliverable_verdicts.json"

# Candidates that may cost a gh call in one sweep (gh failures count too).
MAX_CANDIDATES_PER_SWEEP = 3
# Goal-judge calls per sweep. One: a judge call is minutes, not seconds.
MAX_JUDGE_CALLS_PER_SWEEP = 1
# Only merges this recent are credited. Older ones have had the longest time to
# be regressed or overwritten by later work, and nothing here can see that; the
# mint brake escalates those goals to a human instead. Matches the daemon's
# autonomy reconcile window.
CREDIT_WINDOW_DAYS = 30
RETRY_BASE_SECONDS = 3600
RETRY_MAX_SECONDS = 86400
# After this many transport failures on one task it is ledgered "gave_up".
MAX_RETRIES = 8
# A task marked completed whose PR is still OPEN is re-checked this often.
OPEN_PR_RECHECK_SECONDS = 3600
# A ledger-metric goal whose last ledger run predates main's current tip is
# re-checked this often (see _sweep); the ledger runs when main moves.
LEDGER_RECHECK_SECONDS = 1800
LEDGER_RECHECK_MAX_SECONDS = 14400
# Fetching origin/main before a ledger-credit check.
FETCH_TIMEOUT_SECONDS = 60

_QUEUE_FILL_TITLE_RE = re.compile(r"^\[QUEUE-FILL\]\s+(G\d+):")
_GATE_LOG_MAX_LINES = 20000

_worker: threading.Thread | None = None
_worker_guard = threading.Lock()
_current_proc: subprocess.Popen | None = None
_atexit_registered = False
# Resolved at call time so tests can substitute a double.
_popen = subprocess.Popen


def _import(name: str):
    """Sibling module, whether this file was imported as a package or not."""
    import importlib

    if __package__:
        try:
            return importlib.import_module(f"{__package__}.{name}")
        except ImportError:
            pass
    return importlib.import_module(name)


# =============================================================================
# Background worker
# =============================================================================


def kill_judge(grace: float = 2.0) -> None:
    """Kill the running goal-judge process group. Used on timeout and at exit."""
    proc = _current_proc
    if proc is None:
        return
    # The group can outlive its leader: uv may have exited while claude still
    # runs in the same group. So the group is signalled even when the leader
    # is already gone; a group with no members left raises ProcessLookupError.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(min(grace, 0.2))
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def start_credit_sweep(company_dir: Path, queue: dict, **kwargs: Any) -> dict:
    """Launch one credit sweep in a background thread and return immediately.

    A sweep already in flight makes this a no-op. Never raises.
    """
    global _worker, _atexit_registered
    try:
        with _worker_guard:
            if _worker is not None and _worker.is_alive():
                return {"started": False, "reason": "in_flight"}
            if not _atexit_registered:
                atexit.register(kill_judge)
                _atexit_registered = True
            snapshot = json.loads(json.dumps(queue or {}))
            thread = threading.Thread(
                target=_sweep_quietly,
                args=(Path(company_dir), snapshot),
                kwargs=kwargs,
                name="goal-credit",
                daemon=True,
            )
            thread.start()
            _worker = thread
        return {"started": True}
    except Exception as exc:
        return {"started": False, "reason": f"{type(exc).__name__}: {exc}"}


def join_credit_sweep(timeout: float | None = None) -> None:
    """Wait for the in-flight sweep (CLI use; the daemon never waits)."""
    worker = _worker
    if worker is not None:
        worker.join(timeout)


def _sweep_quietly(company_dir: Path, queue: dict, **kwargs: Any) -> None:
    try:
        credit_goals_from_merged_tasks(company_dir, queue, **kwargs)
    except Exception:
        pass
    # Then the goals whose PR merged outside the daemon's path (a human merge
    # on top of fix rounds: no gate verdict for the merged sha) -- the step an
    # operator otherwise runs by hand with ``goal_tracker.py verify``. Only on
    # the production call (no test doubles injected above).
    if kwargs:
        return
    try:
        _import("goal_verify_sweep").verify_goals_from_merged_prs(company_dir, queue)
    except Exception:
        pass


# =============================================================================
# Ledger + lock
# =============================================================================


def _load_ledger(company_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads((company_dir / LEDGER_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(company_dir: Path, ledger: dict[str, dict]) -> None:
    path = company_dir / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _try_lock(company_dir: Path) -> int | None:
    import fcntl

    path = company_dir / LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pr_number_from_url(pr_url: str) -> str | None:
    match = re.search(r"/pull/(\d+)", pr_url or "")
    return match.group(1) if match else None


# =============================================================================
# Evidence: PR state, git state, task verdict
# =============================================================================


def check_pr(pr_url: str, *, cwd: Path | None = None, timeout: int = 30) -> dict:
    """MERGED-with-diff check that also returns the head sha and merge commit.

    ``unverified=True`` means gh reached no verdict (retry later).
    """
    out: dict[str, Any] = {"merged": False, "unverified": False, "reason": ""}
    number = _pr_number_from_url(pr_url)
    if not number:
        out["reason"] = "no PR number in url"
        return out
    out["pr_number"] = number
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                number,
                "--json",
                "state,mergedAt,additions,deletions,headRefOid,mergeCommit",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        data = json.loads(res.stdout) if res.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        out.update(unverified=True, reason="gh error / non-json (fail closed)")
        return out
    diff = int(data.get("additions") or 0) + int(data.get("deletions") or 0)
    merge_commit = (data.get("mergeCommit") or {}).get("oid")
    out.update(
        state=data.get("state"),
        merged_at=data.get("mergedAt"),
        head_sha=data.get("headRefOid"),
        merge_commit=merge_commit,
    )
    if data.get("state") == "MERGED" and data.get("mergedAt") and diff > 0:
        out.update(merged=True, reason="merged with diff")
    else:
        out["reason"] = f"not shipped (state={data.get('state')}, diff={diff})"
    return out


def check_merge_commit(merge_commit: str | None, project_root: Path) -> str:
    """ "ok" | "not_on_main" | "reverted" | "unknown" for a merge commit.

    "not_on_main" is retryable: a stale local ref reads the same as a merge that
    was dropped, and only a later sweep can tell them apart.
    """
    if not merge_commit:
        return "unknown"

    def _git(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(project_root), *args],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

    ref = None
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "HEAD"):
        res = _git("rev-parse", "--verify", "--quiet", candidate)
        if res is not None and res.returncode == 0 and res.stdout.strip():
            ref = candidate
            break
    if ref is None:
        return "unknown"
    res = _git("merge-base", "--is-ancestor", merge_commit, ref)
    if res is None or res.returncode not in (0, 1):
        return "unknown"
    if res.returncode == 1:
        return "not_on_main"
    res = _git(
        "log",
        ref,
        "--format=%H",
        "-n",
        "1",
        "--fixed-strings",
        f"--grep=This reverts commit {merge_commit}",
    )
    if res is None or res.returncode != 0:
        return "unknown"
    return "reverted" if res.stdout.strip() else "ok"


def resolve_origin_main(
    project_root: Path, branch: str = "main", timeout: int = FETCH_TIMEOUT_SECONDS
) -> tuple[str | None, str]:
    """Fetch ``origin <branch>``; return ``(refs/remotes/origin/<branch>, why)``.

    The local branch can lag the remote by seconds to hours (the daemon pulls
    on its own schedule), so crediting reads the remote-tracking ref right
    after a fetch. A failed fetch returns ``(None, reason)``: an offline
    machine must retry, never judge against a ref it could not refresh.
    """

    # The sweep runs headless (daemon thread, launchd): a credential prompt
    # must fail the fetch at once, never wait on a terminal until the timeout.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

    def _git(*args: str, limit: int) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(project_root), *args],
                capture_output=True,
                text=True,
                timeout=limit,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

    res = _git("fetch", "--quiet", "origin", branch, limit=timeout)
    if res is None or res.returncode != 0:
        detail = (res.stderr or res.stdout) if res is not None else "timeout"
        return None, f"fetch of origin {branch} failed: {detail.strip()[:200]}"
    res = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/origin/{branch}^{{commit}}",
        limit=20,
    )
    if res is None or res.returncode != 0 or not res.stdout.strip():
        return None, f"refs/remotes/origin/{branch} missing after the fetch"
    return res.stdout.strip(), "fetched"


def _definitive(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and not record.get("skipped_reason")
        and record.get("error") is None
        and record.get("addresses_task") in (True, False)
    )


def _passing(record: dict) -> bool:
    return (
        record.get("addresses_task") is True
        and not record.get("blocked")
        and not record.get("needs_manual_review")
    )


def pre_merge_verdict_passes(
    company_dir: Path, task_id: str, head_sha: str | None, merged_at: str | None
) -> bool:
    """Did the task gate pass THIS merged head sha before the merge?

    * Gate log: the latest definitive line for (task_id, head_sha) dated at or
      before ``merged_at`` must pass. Lines after the merge are re-judges
      (nightly calibration), never the gate that let the PR through.
    * Only when the gate log has no line at all for the task (the pre-#652
      workflow-executor path) does ``deliverable_verdicts.json[head_sha]``
      decide.
    * Any definitive NEGATIVE for (task_id, head_sha), in either file, at any
      time, blocks. Fail closed.
    """
    if not head_sha:
        return False
    merged = _parse_ts(merged_at)
    task_lines: list[dict] = []
    try:
        with open(company_dir / GATE_LOG_FILE, encoding="utf-8") as handle:
            lines = handle.readlines()[-_GATE_LOG_MAX_LINES:]
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("task_id") == task_id:
            task_lines.append(record)

    cache_record = None
    try:
        cache = json.loads(
            (company_dir / VERDICT_CACHE_FILE).read_text(encoding="utf-8")
        )
        candidate = cache.get(head_sha) if isinstance(cache, dict) else None
        if isinstance(candidate, dict) and candidate.get("task_id") == task_id:
            cache_record = candidate
    except (OSError, json.JSONDecodeError):
        pass

    task_verdicts = [r for r in task_lines if _definitive(r)]
    for_sha = [r for r in task_verdicts if r.get("sha") == head_sha]
    if any(not _passing(r) for r in for_sha):
        return False
    if _definitive(cache_record) and not _passing(cache_record):
        return False

    if task_verdicts:
        pre_merge = [
            r
            for r in for_sha
            if merged is not None
            and (_parse_ts(r.get("timestamp")) or merged) <= merged
        ]
        return bool(pre_merge) and _passing(pre_merge[-1])
    return _definitive(cache_record) and _passing(cache_record)


def _has_explicit_reject_for_sha(
    company_dir: Path, task_id: str, head_sha: str | None
) -> bool:
    """A definitive NEGATIVE verdict recorded for this exact (task_id, head_sha)
    -- in the gate log or the verdict cache, at any time.

    Used to distinguish a hard gate REJECT (never overridable -- the gate
    looked at this exact diff and refused it) from a merely MISSING verdict
    (no line exists for this sha at all, e.g. because an operator committed
    fix rounds on top of the daemon's head after the gate last ran). Only the
    missing case is eligible for the acceptance-ledger fallback in
    :func:`_sweep`; fail closed (False) on any read error, same as
    ``pre_merge_verdict_passes``.
    """
    if not head_sha:
        return False
    try:
        with open(company_dir / GATE_LOG_FILE, encoding="utf-8") as handle:
            lines = handle.readlines()[-_GATE_LOG_MAX_LINES:]
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and record.get("task_id") == task_id
            and record.get("sha") == head_sha
            and _definitive(record)
            and not _passing(record)
        ):
            return True
    try:
        cache = json.loads(
            (company_dir / VERDICT_CACHE_FILE).read_text(encoding="utf-8")
        )
        candidate = cache.get(head_sha) if isinstance(cache, dict) else None
        if (
            isinstance(candidate, dict)
            and candidate.get("task_id") == task_id
            and _definitive(candidate)
            and not _passing(candidate)
        ):
            return True
    except (OSError, json.JSONDecodeError):
        pass
    return False


# =============================================================================
# Isolated goal judge
# =============================================================================


def _judge_command(
    prompt: str, project_root: Path
) -> tuple[list[str], str | None, dict]:
    """(argv, stdin, env) for the configured judge provider, like the task gate."""
    ap = _import("agent_providers")
    dj = _import("deliverable_judge")
    root_config: dict = {}
    for path in (
        project_root / "gatesmith.json",
        project_root / ".claude" / "gatesmith.json",
    ):
        try:
            root_config = json.loads(path.read_text(encoding="utf-8"))
            break
        except (OSError, json.JSONDecodeError):
            continue
    provider = ap.resolve_provider(root_config, "trivial")
    env = ap.prepare_environment(provider)
    if provider.provider_type == "codex":
        cmd = ["codex"]
        if provider.reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{provider.reasoning_effort}"'])
        cmd.extend(
            [
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--cd",
                str(project_root),
            ]
        )
        if provider.model and provider.model != "default":
            cmd.extend(["--model", provider.model])
        cmd.append("-")
        return cmd, prompt, env
    prefix = dj._claude_cmd_prefix()
    if not prefix:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = list(prefix)
    gate = dj.load_gate_config()
    model = gate.get("model")
    if model:
        cmd.extend(["--model", str(model)])
    effort = gate.get("effort")
    if effort:
        cmd.extend(["--effort", str(effort)])
    cmd.extend(
        [
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ]
    )
    return cmd, None, env


def judge_goal_isolated(goal: Any, pr_number: str, project_root: Path) -> dict:
    """Ask the judge whether a merged PR's diff satisfies ``goal``'s metric.

    Returns ``{"addresses": bool|None, "confidence", "reason", "error"}``; an
    ``error`` is a transport/model failure (retryable, never credits). Writes
    nothing: no verdict cache, no gate log, no anti-pattern.
    """
    global _current_proc
    dj = _import("deliverable_judge")
    spawn_guard = _import("spawn_guard")
    config = dj.load_gate_config()
    result: dict[str, Any] = {"addresses": None, "confidence": None, "reason": ""}

    ctx = dj.fetch_pr_context(pr_number, int(config.get("maxDiffChars", 60000)))
    if ctx is None:
        result["error"] = "could not fetch PR diff"
        return result
    meta_json, diff_text = ctx
    if not diff_text.strip():
        result.update(addresses=False, reason="empty diff")
        return result
    identity = dj.load_product_identity()
    tripwire = dj._check_identity_tripwire(
        diff_text,
        identity["product_name"],
        is_framework_repo=dj._is_framework_repo(),
    )
    if tripwire:
        result.update(addresses=False, confidence=1.0, reason=tripwire)
        return result

    prompt = dj.build_judge_prompt(
        f"goal-{goal.id}",
        goal.name,
        goal.description,
        pr_number,
        meta_json,
        diff_text,
        product_name=identity["product_name"],
        product_description=identity["product_description"],
        goal_metric=goal.success_metric,
    )
    try:
        cmd, stdin_text, env = _judge_command(prompt, project_root)
    except Exception as exc:
        result["error"] = f"judge unavailable: {exc}"
        return result

    popen = _popen
    spawn_guard.assert_spawn_allowed("goal_crediting.judge_goal_isolated", popen)
    try:
        proc = popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        result["error"] = f"judge failed to start: {exc}"
        return result
    _current_proc = proc
    try:
        try:
            stdout, stderr = proc.communicate(
                input=stdin_text, timeout=int(config.get("timeoutSeconds", 240))
            )
        except subprocess.TimeoutExpired:
            kill_judge(grace=0)
            try:
                proc.communicate(timeout=5)  # reap it; no zombie
            except Exception:
                pass
            result["error"] = "judge timed out"
            return result
    finally:
        _current_proc = None

    parsed = dj.parse_verdict_json(stdout or "")
    if parsed is None or not isinstance(parsed.get("addresses_task"), bool):
        detail = (stderr or stdout or "").strip()[:200]
        result["error"] = f"no parseable verdict (rc={proc.returncode}): {detail}"
        return result
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    threshold = float(config.get("confidenceThreshold", 0.7))
    result.update(
        addresses=bool(parsed["addresses_task"])
        and confidence is not None
        and confidence >= threshold,
        confidence=confidence,
        reason=str(parsed.get("reason") or ""),
    )
    return result


def _log_judgment(company_dir: Path, entry: dict) -> None:
    try:
        path = company_dir / JUDGMENT_LOG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# =============================================================================
# The sweep
# =============================================================================


def credit_goals_from_merged_tasks(
    company_dir: Path,
    queue: dict,
    *,
    pr_checker: Callable[[str], dict] | None = None,
    git_checker: Callable[[str | None], str] | None = None,
    judge: Callable[[Any, str], dict] | None = None,
    main_resolver: Callable[[], tuple[str | None, str]] | None = None,
    now: datetime | None = None,
    max_candidates: int = MAX_CANDIDATES_PER_SWEEP,
    max_judge_calls: int = MAX_JUDGE_CALLS_PER_SWEEP,
    window_days: int | None = None,
) -> dict:
    """One synchronous sweep (see the module docstring). Never raises.

    Returns ``{"credited": [goal_ids], "outcomes": {task_id: outcome}}``;
    ``skipped`` is set when another process holds the sweep lock.
    """
    result: dict[str, Any] = {"credited": [], "outcomes": {}}
    company_dir = Path(company_dir)
    try:
        gt = _import("goal_tracker")
        goals = {g.id: g for g in gt.parse_goals_from_vision(company_dir / "vision.md")}
        candidates = []
        for task in (queue or {}).get("completed", []) or []:
            if not isinstance(task, dict) or task.get("status") != "completed":
                continue
            if not task.get("pr_url") or task.get("source") != "gap_analysis":
                continue
            match = _QUEUE_FILL_TITLE_RE.match(str(task.get("title") or ""))
            if match and task.get("task_id"):
                candidates.append((match.group(1), task))
        if not goals or not candidates:
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    fd = _try_lock(company_dir)
    if fd is None:
        result["skipped"] = "locked"
        return result
    try:
        _sweep(
            company_dir,
            gt,
            goals,
            candidates,
            result,
            pr_checker=pr_checker
            or (lambda url: check_pr(url, cwd=company_dir.parent)),
            git_checker=git_checker
            or (lambda sha: check_merge_commit(sha, company_dir.parent)),
            judge=judge
            or (lambda goal, pr: judge_goal_isolated(goal, pr, company_dir.parent)),
            main_resolver=main_resolver,
            now=now or datetime.now(timezone.utc),
            max_candidates=max_candidates,
            max_judge_calls=max_judge_calls,
            # Read at call time, not bound as a default at import: a default
            # froze the value, so monkeypatching CREDIT_WINDOW_DAYS did
            # nothing and a test merged on 2026-09-20 started failing on its own
            # once the wall clock passed 30 days later.
            window_days=CREDIT_WINDOW_DAYS if window_days is None else window_days,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _unlock(fd)
    return result


def _sweep(
    company_dir: Path,
    gt: Any,
    goals: dict,
    candidates: list,
    result: dict,
    *,
    pr_checker: Callable,
    git_checker: Callable,
    judge: Callable,
    now: datetime,
    max_candidates: int,
    max_judge_calls: int,
    window_days: int,
    main_resolver: Callable[[], tuple[str | None, str]] | None = None,
) -> None:
    ledger = _load_ledger(company_dir)
    stamp = now.isoformat()
    resolved_main: list[tuple[str | None, str]] = []

    def _origin_main() -> tuple[str | None, str]:
        # At most one fetch per sweep, and only when a ledger-form goal asks.
        if not resolved_main:
            resolver = main_resolver or (
                lambda: resolve_origin_main(company_dir.parent)
            )
            resolved_main.append(resolver())
        return resolved_main[0]

    def _record(task: dict, goal_id: str, outcome: str, **extra: Any) -> None:
        ledger[task["task_id"]] = {
            "goal_id": goal_id,
            "outcome": outcome,
            "pr_url": task.get("pr_url"),
            "at": stamp,
            **extra,
        }
        result["outcomes"][task["task_id"]] = outcome
        _save_ledger(company_dir, ledger)  # per outcome: a killed sweep loses nothing

    def _retry(task: dict, goal_id: str, why: str) -> None:
        attempts = int((ledger.get(task["task_id"]) or {}).get("attempts", 0)) + 1
        if attempts >= MAX_RETRIES:
            _record(task, goal_id, "gave_up", attempts=attempts, last_error=why)
            return
        delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
        _record(
            task,
            goal_id,
            "retry",
            attempts=attempts,
            last_error=why,
            next_attempt_at=(now + timedelta(seconds=delay)).isoformat(),
        )

    # Newest first: the latest merged PR is the likeliest to finish a goal.
    candidates = sorted(
        candidates, key=lambda c: str(c[1].get("completed_at") or ""), reverse=True
    )
    checked = judged = 0
    for goal_id, task in candidates:
        entry = ledger.get(task["task_id"])
        if entry and entry.get("outcome") != "retry":
            continue
        if entry and (_parse_ts(entry.get("next_attempt_at")) or now) > now:
            continue
        goal = goals.get(goal_id)
        if goal is None:
            _record(task, goal_id, "goal_not_in_vision")
            continue
        if gt._is_framework_dogfood_goal(goal):
            _record(task, goal_id, "framework_goal")
            continue
        if gt._verified_stamp_for_goal(goal, company_dir) is not None:
            _record(task, goal_id, "already_verified")
            continue
        if checked >= max_candidates:
            break
        checked += 1
        pr = pr_checker(task["pr_url"])
        if pr.get("unverified"):
            _retry(task, goal_id, f"gh: {pr.get('reason')}")
            continue
        if not pr.get("merged"):
            if pr.get("state") == "OPEN":
                # Not a verdict: the PR can still merge. Re-check hourly, and
                # without spending a transport-failure attempt, so a PR that
                # sits open for days is not given up on.
                attempts = int((entry or {}).get("attempts", 0))
                _record(
                    task,
                    goal_id,
                    "retry",
                    attempts=attempts,
                    last_error="PR still open",
                    next_attempt_at=(
                        now + timedelta(seconds=OPEN_PR_RECHECK_SECONDS)
                    ).isoformat(),
                )
                continue
            _record(task, goal_id, "not_merged", reason=pr.get("reason"))
            continue
        merged_at = _parse_ts(pr.get("merged_at"))
        if merged_at is None or now - merged_at > timedelta(days=window_days):
            _record(task, goal_id, "outside_window", merged_at=pr.get("merged_at"))
            continue
        if not pre_merge_verdict_passes(
            company_dir, task["task_id"], pr.get("head_sha"), pr.get("merged_at")
        ):
            # No passing task-gate verdict for this exact merged sha. Two
            # different situations land here and only one is eligible for the
            # ledger fallback below: a MISSING verdict (often because the
            # operator committed fix rounds on top of the daemon's head, so
            # the gate that ran never saw the final diff) can still be
            # certified by the acceptance ledger's live read of the CURRENT
            # state of main -- but a hard gate REJECT for this exact sha is a
            # verdict the gate DID reach, and the ledger (which judges
            # something else -- main's current state, not this diff) must
            # never be allowed to override it.
            ledger_check = (
                None
                if _has_explicit_reject_for_sha(
                    company_dir, task["task_id"], pr.get("head_sha")
                )
                else gt._ledger_credit_check(
                    goal,
                    company_dir,
                    merge_commit=pr.get("merge_commit"),
                    main_resolver=_origin_main,
                )
            )
            if ledger_check is not None and ledger_check.confirmed:
                # The run proves main once held the merged code; a revert of
                # the merge since then means main no longer does. (Other
                # git states are moot: ancestry was proven on origin/main.)
                if git_checker(pr.get("merge_commit")) == "reverted":
                    _record(
                        task,
                        goal_id,
                        "reverted",
                        merge_commit=pr.get("merge_commit"),
                        ledger_run_commit=ledger_check.run_commit,
                    )
                    continue
                pr_number = str(
                    pr.get("pr_number") or _pr_number_from_url(task["pr_url"])
                )
                gt._stamp_ledger_confirmation(
                    goal,
                    company_dir,
                    ledger_check,
                    pr_number=pr_number,
                    pr_url=task.get("pr_url"),
                )
                _record(task, goal_id, "verified_via_ledger", pr_number=pr_number)
                result["credited"].append(goal_id)
                continue
            if (
                ledger_check is not None
                and ledger_check.state in gt.LEDGER_NOT_YET_STATES
            ):
                # "Not yet", not a verdict: origin/main could not be fetched,
                # no eligible run contains the merge (the sweep often runs
                # minutes after the merge, before the ledger run on it), or
                # too few eligible runs have read the entries failing.
                # Re-check with back-off and without spending a transport
                # attempt; CREDIT_WINDOW_DAYS bounds it.
                attempts = int((entry or {}).get("attempts", 0))
                waits = int((entry or {}).get("ledger_waits", 0))
                delay = min(
                    LEDGER_RECHECK_SECONDS * 2**waits, LEDGER_RECHECK_MAX_SECONDS
                )
                _record(
                    task,
                    goal_id,
                    "retry",
                    attempts=attempts,
                    ledger_waits=waits + 1,
                    ledger_state=ledger_check.state,
                    last_error=f"ledger: {ledger_check.reason}",
                    next_attempt_at=(now + timedelta(seconds=delay)).isoformat(),
                )
                continue
            extra: dict[str, Any] = {}
            if ledger_check is not None:
                extra = {
                    "merge_commit": pr.get("merge_commit"),
                    "ledger_reason": ledger_check.reason,
                    "ledger_failing_runs": list(ledger_check.failing_runs),
                }
            _record(
                task,
                goal_id,
                "no_passing_verdict",
                head_sha=pr.get("head_sha"),
                **extra,
            )
            continue
        git_state = git_checker(pr.get("merge_commit"))
        if git_state == "reverted":
            _record(task, goal_id, "reverted", merge_commit=pr.get("merge_commit"))
            continue
        if git_state != "ok":
            _retry(task, goal_id, f"git: {git_state}")
            continue
        if judged >= max_judge_calls:
            break
        judged += 1
        pr_number = str(pr.get("pr_number") or _pr_number_from_url(task["pr_url"]))
        verdict = judge(goal, pr_number)
        _log_judgment(
            company_dir,
            {
                "timestamp": now.isoformat(),
                "goal_id": goal_id,
                "task_id": task["task_id"],
                "pr_number": pr_number,
                **{k: verdict.get(k) for k in ("addresses", "confidence", "reason")},
                "error": verdict.get("error"),
            },
        )
        if verdict.get("error"):
            _retry(task, goal_id, f"judge: {verdict['error']}")
            continue
        if verdict.get("addresses") is not True:
            _record(task, goal_id, "not_complete", reason=verdict.get("reason"))
            continue
        gt._stamp_goal_verified(
            goal,
            company_dir,
            pr_number=pr_number,
            pr_url=task.get("pr_url"),
            evidence=verdict.get("reason"),
            method="pr_judge_auto",
        )
        _record(task, goal_id, "verified", pr_number=pr_number)
        result["credited"].append(goal_id)


# =============================================================================
# CLI: re-queue settled records
# =============================================================================


# Credited outcomes: requeueing one cannot re-credit (the goal is stamped) and
# would overwrite the record of which task credited the goal.
REQUEUE_REFUSED_OUTCOMES = frozenset({"verified", "verified_via_ledger"})


def requeue_records(
    company_dir: Path,
    *,
    task_ids: list[str] | None = None,
    outcome: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Set matching ledger records back to ``retry`` with no back-off (due at
    the next sweep).

    A record matches when its task id is in ``task_ids`` or its outcome equals
    ``outcome``; ``retry`` records are left alone, and credited records
    (:data:`REQUEUE_REFUSED_OUTCOMES`) are refused: the goal is stamped, so a
    re-check could only overwrite them with ``already_verified`` and lose which
    task credited the goal. The previous outcome is kept
    in ``requeued_from``; attempts and ledger waits restart at 0. Takes the
    sweep lock, so it never races a sweep. Returns ``{"requeued", "refused",
    "missing", "dry_run"}`` or ``{"error"}``.
    """
    company_dir = Path(company_dir)
    wanted = set(task_ids or [])
    if not wanted and not outcome:
        return {"error": "give --task or --outcome"}
    fd = _try_lock(company_dir)
    if fd is None:
        return {"error": "a credit sweep holds the lock; try again shortly"}
    try:
        ledger = _load_ledger(company_dir)
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        requeued: list[str] = []
        refused: list[str] = []
        for task_id, record in ledger.items():
            if not isinstance(record, dict) or record.get("outcome") == "retry":
                continue
            if task_id not in wanted and record.get("outcome") != outcome:
                continue
            if record.get("outcome") in REQUEUE_REFUSED_OUTCOMES:
                refused.append(task_id)
                continue
            requeued.append(task_id)
            if dry_run:
                continue
            ledger[task_id] = {
                "goal_id": record.get("goal_id"),
                "outcome": "retry",
                "pr_url": record.get("pr_url"),
                "at": stamp,
                "attempts": 0,
                "ledger_waits": 0,
                "requeued_from": record.get("outcome"),
                "requeued_at": stamp,
                "last_error": f"requeued from {record.get('outcome')!r} by operator",
            }
        if requeued and not dry_run:
            _save_ledger(company_dir, ledger)
        missing = sorted(t for t in wanted if t not in ledger)
        return {
            "requeued": requeued,
            "refused": refused,
            "missing": missing,
            "dry_run": dry_run,
        }
    finally:
        _unlock(fd)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Goal-credit ledger maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    rq = sub.add_parser("requeue", help="set settled goal-credit records to retry")
    rq.add_argument("--task", action="append", default=[], help="task id (repeat)")
    rq.add_argument("--outcome", help="requeue every record with this outcome")
    rq.add_argument("--company-dir", help=".company directory (default: resolved)")
    rq.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.company_dir:
        company_dir = Path(args.company_dir).expanduser()
    else:
        company_dir = Path(_import("company_resolver").get_company_dir())
    if not (company_dir / LEDGER_FILE).is_file():
        print(f"no goal-credit ledger at {company_dir / LEDGER_FILE}")
        return 1
    out = requeue_records(
        company_dir, task_ids=args.task, outcome=args.outcome, dry_run=args.dry_run
    )
    if out.get("error"):
        print(f"error: {out['error']}")
        return 1
    verb = "would requeue" if args.dry_run else "requeued"
    print(f"{verb} {len(out['requeued'])}: {', '.join(out['requeued']) or '-'}")
    if out["refused"]:
        print(f"refused (already credited): {', '.join(out['refused'])}")
    if out["missing"]:
        print(f"not in the ledger: {', '.join(out['missing'])}")
        return 1
    if out["refused"] and not out["requeued"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
