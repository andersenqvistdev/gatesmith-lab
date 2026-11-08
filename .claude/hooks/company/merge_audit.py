"""
Merge audit chain — one JSONL line per daemon merge.

Each line captures the complete provenance of a merge:
    ts, task_id, pr, deliverable_verdict, ci_state, merge_lever

Missing fields are explicit JSON null values (never absent keys).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_AUDIT_REL = "state/merge_audit.jsonl"
_GATE_LOG_REL = "state/deliverable_gate.jsonl"
_VERDICT_CACHE_REL = "state/deliverable_verdicts.json"


def _hermetic(company_dir: Path) -> Path:
    """The hermetic test redirect (company_resolver.hermetic_redirect).

    forge_daemon hands this module ``Path(__file__).parents[3] / ".company"``
    — the checkout's real .company — without going through the resolver, and
    fixture rows from test runs landed in the real merge_audit.jsonl that way
    (HERMETIC-WORKTREE-1). Outside a hermetic test run this returns
    *company_dir* unchanged.
    """
    try:
        import sys

        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        from company_resolver import hermetic_redirect
    except Exception:
        return company_dir
    redirected = hermetic_redirect(company_dir)
    return company_dir if redirected is company_dir else Path(redirected)


def _audit_path(company_dir: Path) -> Path:
    return _hermetic(company_dir) / _AUDIT_REL


def lookup_deliverable_verdict(company_dir: Path, task_id: str | None) -> str | None:
    """Return the most-recent deliverable verdict for *task_id*.

    Returns 'PASS', 'BLOCK', 'ERROR', 'SKIP', or None when no entry is found.
    """
    if not task_id:
        return None
    gate_log = company_dir / _GATE_LOG_REL
    if not gate_log.exists():
        return None

    last: dict | None = None
    try:
        with open(gate_log, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") == task_id:
                    last = rec
    except OSError:
        return None

    if last is None:
        return None
    return verdict_label(last)


def verdict_label(rec: dict | None) -> str | None:
    """Map a verdict record (cache entry or gate-log row) to an audit label.

    'PASS' only for addresses_task=True that the gate did not block (a
    below-threshold confidence is blocked and reads 'BLOCK'); 'SKIP' for a
    skip row; 'ERROR' when the judge produced no verdict; None for no record.
    """
    if not rec:
        return None
    if rec.get("skipped_reason"):
        return "SKIP"
    addresses = rec.get("addresses_task")
    if addresses is True:
        return "BLOCK" if rec.get("blocked") else "PASS"
    if addresses is False:
        return "BLOCK"
    return "ERROR"  # addresses_task is None → judge error


def lookup_verdict_record_for_sha(company_dir: Path, sha: str | None) -> dict | None:
    """Return the deliverable verdict recorded for commit *sha*, or None.

    The judge files its verdict under the PR head commit it judged, in the
    verdict cache and as a gate-log row. The cache is read first; the gate log
    is the fallback, because the cache is one JSON file rewritten whole by
    every judge run and a concurrent write can drop an entry, while the log is
    append-only. Skip rows are not verdicts and are ignored. A verdict for any
    OTHER commit — including an earlier head of the same PR — never answers
    for *sha*.
    """
    if not sha:
        return None
    cache_path = company_dir / _VERDICT_CACHE_REL
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
        rec = cache.get(sha) if isinstance(cache, dict) else None
        if isinstance(rec, dict):
            return rec
    except (OSError, json.JSONDecodeError):
        pass

    gate_log = company_dir / _GATE_LOG_REL
    last: dict | None = None
    try:
        with open(gate_log, encoding="utf-8") as fh:
            for raw in fh:
                if sha not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("sha") == sha and not rec.get("skipped_reason"):
                    last = rec
    except OSError:
        return None
    return last


def lookup_deliverable_verdict_for_merge(
    company_dir: Path, task_id: str | None, head_sha: str | None
) -> str | None:
    """The verdict to audit for a merge: the merged commit's own, else the task's.

    The workflow executor's verdict lives in the sha-keyed cache and never
    reached the task-keyed gate log, so a task-only lookup audited judged
    merges as null.
    """
    label = verdict_label(lookup_verdict_record_for_sha(company_dir, head_sha))
    if label is not None:
        return label
    return lookup_deliverable_verdict(company_dir, task_id)


def ci_state_from_gates(gate_results: dict | None) -> str | None:
    """Extract a CI state string from security-gate results.

    Returns 'pass', 'fail', 'pending', or None when not available.
    """
    if not gate_results:
        return None
    if gate_results.get("verdict") == "PENDING":
        return "pending"
    ci = gate_results.get("gates", {}).get("ci_checks")
    if ci is None:
        return None
    return "pass" if ci.get("passed") else "fail"


def append_merge_chain(
    company_dir: Path,
    *,
    task_id: str | None,
    pr: int | None,
    deliverable_verdict: str | None,
    ci_state: str | None,
    merge_lever: str | None,
) -> None:
    """Append one audit line to merge_audit.jsonl.

    All fields are always present; missing values are explicit null.
    Best-effort — never raises.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "pr": pr,
        "deliverable_verdict": deliverable_verdict,
        "ci_state": ci_state,
        "merge_lever": merge_lever,
    }
    path = _audit_path(company_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def load_audit_for_pr(company_dir: Path, pr_number: int) -> list[dict]:
    """Return all audit entries for *pr_number* in file order (oldest first)."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("pr") == pr_number:
                    results.append(rec)
    except OSError:
        pass
    return results


def load_audit_for_task(company_dir: Path, task_id: str) -> list[dict]:
    """Return all audit entries for *task_id* in file order (oldest first)."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("task_id") == task_id:
                    results.append(rec)
    except OSError:
        pass
    return results


def load_all_audit(company_dir: Path, *, newest_first: bool = True) -> list[dict]:
    """Return all audit entries, sorted newest-first by default."""
    path = _audit_path(company_dir)
    if not path.exists():
        return []
    results: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                results.append(rec)
    except OSError:
        pass
    if newest_first:
        results.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return results
