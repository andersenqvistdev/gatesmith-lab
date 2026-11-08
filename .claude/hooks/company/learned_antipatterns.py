"""Learned anti-patterns — close the autonomy learning loop (Phase 3).

Phantom detections used to be diagnostic only: ``/calibrate`` and the Phase-2
pre-merge deliverable gate would *flag* a phantom, but nothing fed that back so the
proactive/initiative engine kept generating the same CLASS of dead task. This module
is the feedback channel.

When a phantom is confirmed (a merged PR whose diff does not address its task, or a
Tier-1 / Tier-2 audit phantom) we record a structured ANTI-PATTERN: the offending
task title, a normalized token *signature*, the reason, and an example. The
task-admission gate (``task_admission.admit_task``) and the proactive generator
(``initiative_engine.scan_all_opportunities``) then consult these anti-patterns and
drop newly-proposed work that matches one — reinforcing Phase 1's admission gate at
generation time.

Design rules (mirroring task_admission):
  * ``match_task`` is a PURE function (no IO) so it is unit-testable. The store is a
    small JSON file loaded once and passed in.
  * Matching is CONSERVATIVE — high Jaccard similarity on a normalized title
    signature, and only when both signatures are non-trivial. A too-aggressive
    matcher that drops real work is worse than a few phantoms (same philosophy as
    the admission gate: fail OPEN when uncertain).
  * Recording is best-effort and NEVER raises into a caller's hot path.

Storage: ``.company/knowledge/anti_patterns.json`` — a DEDICATED file, deliberately
separate from ``knowledge/patterns.json`` (the success-pattern store). patterns.json
has a rigid success schema consumed/rewritten by many modules (pattern_extractor,
pattern_propagator, knowledge_capture, …); mixing a different record shape in there
would risk those readers and race their non-locked rewrites. A separate file is
isolated and safe.

Public API:
    load_antipatterns(repo_root) -> list[dict]
    record_antipattern(repo_root, *, kind, title, reason, source, ...) -> dict
    match_task(task, antipatterns, *, threshold=0.85) -> reason|None
    signature(title) -> list[str]
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ANTIPATTERNS_REL = Path("knowledge") / "anti_patterns.json"

# Anti-pattern kinds (free-form; these are the taxonomy from the autonomy plan).
KIND_PHANTOM_MERGE = "phantom-merge"  # merged PR whose diff doesn't address the task
KIND_TESTS_ALREADY_SHIPPED = "tests-for-already-shipped"
KIND_DOC_REWORD = "doc-reword"  # re-document an already-documented thing
KIND_WRONG_TARGET = "wrong-target"  # references a symbol/file that doesn't exist
KIND_DUPLICATE_TITLE = "duplicate-title"

# Default similarity at/above which a new task is considered the same class as a
# recorded anti-pattern. Tuned so a re-proposal that merely adds a qualifier word
# ("… feature" → "… feature path") still matches, while genuinely different work
# (which shares far fewer tokens) does not. The store only ever contains CONFIRMED
# phantoms and every consult fails OPEN, so the blast radius is small.
DEFAULT_THRESHOLD = 0.7
# Signatures shorter than this are too small for Jaccard to be reliable — skip them
# (both when recording-as-matchable and when matching) to avoid false positives.
MIN_SIGNATURE_TOKENS = 3

# Tracker / artifact tokens stripped before tokenizing a title so e.g.
# "[WS-124-002]", "#1063", "P85", "task-20260613-abc" don't dominate the signature.
_TRACKER_RE = re.compile(
    r"\[[^\]]*\]"  # bracketed tags [WS-…], [Employee Idea]
    r"|#\d+"  # PR/issue numbers
    r"|\bws-?\d[\w-]*"  # WS-124-002
    r"|\bp\d{1,3}\b"  # P85 phase tokens
    r"|\btask-\d[\w-]*",  # task ids
    re.IGNORECASE,
)
# Split on non-word runs but KEEP underscores so snake_case identifiers
# (``build_demo_data``) stay one discriminating token instead of fragmenting.
_NONWORD_RE = re.compile(r"[^a-z0-9_]+")

# Generic words that carry no discriminating signal for "what class of task is this".
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "for",
        "to",
        "of",
        "in",
        "on",
        "and",
        "or",
        "with",
        "add",
        "added",
        "adding",
        "fix",
        "fixed",
        "update",
        "updated",
        "improve",
        "create",
        "created",
        "make",
        "ensure",
        "implement",
        "support",
        "new",
        "this",
        "that",
        "is",
        "are",
        "be",
        "use",
        "using",
        "via",
        "from",
        "into",
        "document",
        "docs",
        "documentation",
        "test",
        "tests",
        "testing",
        "coverage",
    }
)


def signature(title: str) -> list[str]:
    """Normalize a task title to a sorted list of discriminating tokens.

    Lowercases, strips tracker/artifact tokens, drops stopwords and 1-2 char tokens,
    and returns the unique remaining tokens sorted (stable, JSON-friendly).
    """
    if not title:
        return []
    text = _TRACKER_RE.sub(" ", title.lower())
    tokens = [t for t in _NONWORD_RE.split(text) if t]
    keep = {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}
    return sorted(keep)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def match_task(
    task: dict,
    antipatterns: list[dict],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> str | None:
    """Return a reject reason if ``task`` matches a recorded anti-pattern, else None.

    Pure: no IO. Compares the task's title signature against every anti-pattern's
    stored signature by Jaccard similarity; the first match at/above ``threshold``
    (with both signatures non-trivial) wins.
    """
    if not antipatterns:
        return None
    sig = set(signature(task.get("title") or ""))
    if len(sig) < MIN_SIGNATURE_TOKENS:
        return None
    for ap in antipatterns:
        ap_sig = set(ap.get("signature") or [])
        if len(ap_sig) < MIN_SIGNATURE_TOKENS:
            continue
        if _jaccard(sig, ap_sig) >= threshold:
            kind = ap.get("kind", "anti-pattern")
            reason = ap.get("reason") or "matches a known phantom class"
            return f"learned anti-pattern [{kind}]: {reason}"
    return None


# --------------------------------------------------------------------------- #
# Store IO
# --------------------------------------------------------------------------- #


def _default_repo_root() -> Path:
    try:
        try:
            from . import company_resolver as cr  # type: ignore[attr-defined]
        except ImportError:
            import company_resolver as cr  # type: ignore[no-redef]
        return cr.get_company_dir().parent
    except Exception:
        return Path.cwd()


def _store_path(repo_root: Path | str | None) -> Path:
    if repo_root is None:
        repo_root = _default_repo_root()
    return Path(repo_root) / ".company" / ANTIPATTERNS_REL


def load_antipatterns(repo_root: Path | str | None = None) -> list[dict]:
    """Load the anti-pattern list. Returns [] on any error (never raises)."""
    path = _store_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        items = data.get("anti_patterns", [])
        return items if isinstance(items, list) else []
    if isinstance(data, list):
        return data
    return []


def _save_antipatterns(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "anti_patterns": items,
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ap_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def record_antipattern(
    repo_root: Path | str | None = None,
    *,
    kind: str,
    title: str,
    reason: str,
    source: str,
    example_pr: str | int | None = None,
    example_task_id: str | None = None,
) -> dict | None:
    """Record (or reinforce) an anti-pattern from a confirmed phantom.

    Dedups by title signature: an identical-signature entry is reinforced
    (``seen_count`` bumped, ``last_seen`` refreshed, example appended) rather than
    duplicated. Returns the stored entry, or None if the title is too thin to form a
    reliable signature (we never record an un-matchable anti-pattern). Best-effort:
    swallows IO errors and returns the in-memory entry so callers in a hot path are
    never broken by a write failure.
    """
    sig = signature(title)
    if len(sig) < MIN_SIGNATURE_TOKENS:
        # Too few discriminating tokens — a signature this thin would over-match.
        return None

    path = _store_path(repo_root)
    items = load_antipatterns(repo_root)
    now = datetime.now(timezone.utc).isoformat()
    example = {}
    if example_task_id:
        example["task_id"] = example_task_id
    if example_pr is not None:
        example["pr"] = str(example_pr)

    sig_set = set(sig)
    for ap in items:
        if set(ap.get("signature") or []) == sig_set:
            ap["seen_count"] = int(ap.get("seen_count", 1)) + 1
            ap["last_seen"] = now
            # Keep the most informative kind/reason; refresh reason to the latest.
            ap["reason"] = reason or ap.get("reason", "")
            if example:
                examples = ap.setdefault("examples", [])
                if example not in examples:
                    examples.append(example)
            try:
                _save_antipatterns(path, items)
            except Exception:
                pass
            return ap

    entry = {
        "kind": kind,
        "title": title,
        "signature": sig,
        "reason": reason,
        "source": source,
        "examples": [example] if example else [],
        "seen_count": 1,
        "first_seen": now,
        "last_seen": now,
    }
    items.append(entry)
    try:
        _save_antipatterns(path, items)
    except Exception:
        pass
    return entry


# --------------------------------------------------------------------------- #
# CLI — manual recording + ingesting calibrate / gate phantoms
# --------------------------------------------------------------------------- #


def _classify_kind(title: str, reason: str) -> str:
    """Best-effort kind from the phantom's reason/title (defaults to phantom-merge)."""
    blob = f"{title} {reason}".lower()
    if "not found" in blob or "does not exist" in blob or "wrong" in blob:
        return KIND_WRONG_TARGET
    if "already" in blob and ("test" in blob):
        return KIND_TESTS_ALREADY_SHIPPED
    if "already" in blob and ("doc" in blob or "document" in blob):
        return KIND_DOC_REWORD
    return KIND_PHANTOM_MERGE


def _record_batch(repo_root: Path | None, phantoms: list[dict], source: str) -> int:
    """Record a list of phantom dicts ({task_id,title,reason,pr?,kind?}). Returns count."""
    recorded = 0
    for p in phantoms:
        title = p.get("title") or ""
        reason = p.get("reason") or "merged PR diff did not address the task"
        kind = p.get("kind") or _classify_kind(title, reason)
        entry = record_antipattern(
            repo_root,
            kind=kind,
            title=title,
            reason=reason,
            source=p.get("source") or source,
            example_pr=p.get("pr") or p.get("pr_number") or p.get("pr_url"),
            example_task_id=p.get("task_id"),
        )
        if entry is not None:
            recorded += 1
    return recorded


def main() -> int:
    import sys

    argv = sys.argv[1:]
    cmd = argv[0] if argv else "list"

    def _flag(name: str) -> str | None:
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    repo_root = None

    if cmd == "list":
        items = load_antipatterns(repo_root)
        print(json.dumps({"count": len(items), "anti_patterns": items}, indent=2))
        return 0

    if cmd == "match":
        title = _flag("--title") or ""
        reason = match_task({"title": title}, load_antipatterns(repo_root))
        print(
            json.dumps(
                {"title": title, "matched": reason is not None, "reason": reason}
            )
        )
        return 0

    if cmd == "record":
        title = _flag("--title")
        if not title:
            print("Error: --title required")
            return 1
        reason = _flag("--reason") or "merged PR diff did not address the task"
        entry = record_antipattern(
            repo_root,
            kind=_flag("--kind") or _classify_kind(title, reason),
            title=title,
            reason=reason,
            source=_flag("--source") or "manual",
            example_pr=_flag("--pr"),
            example_task_id=_flag("--task-id"),
        )
        print(json.dumps({"recorded": entry is not None, "entry": entry}, indent=2))
        return 0

    if cmd == "record-batch":
        # --path FILE : JSON list of {task_id,title,reason,pr?,kind?} phantoms.
        # Typically the /calibrate phantoms[] list (Tier-2 addresses_task=false joined
        # with survivor titles), or the deliverable_gate jsonl filtered to blocks.
        path = _flag("--path")
        if not path:
            print("Error: --path required")
            return 1
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error reading {path}: {e}")
            return 1
        phantoms = raw if isinstance(raw, list) else raw.get("phantoms", [])
        n = _record_batch(repo_root, phantoms, _flag("--source") or "calibrate")
        print(json.dumps({"input": len(phantoms), "recorded": n}, indent=2))
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
