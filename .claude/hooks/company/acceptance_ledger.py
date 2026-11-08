#!/usr/bin/env python3
"""Acceptance Ledger — a deterministic runner for externally-authored acceptance entries.

GAP-7 (`.planning/GAP7-ACCEPTANCE-LEDGER-2026-08-30.md`, increment 1): every
acceptance signal Gatesmith has — tests, coverage, the deliverable judge, CI — is
produced by, or reads only, the change under test. A self-consistent
misunderstanding passes all four. This module runs *metamorphic relations*
(N inputs that one spec-reading puts in the same class; the artifact must
treat them alike) and frozen *cases* (one input, one spec-derived expected
observation) against a product's black-box CLI, with no known-correct answer
required for most relation kinds and no model in the judgement path.

Dependency-free by design, matching ``auto_merge_paths.py``: increment 2 will
have CI ``git show`` this file from ``origin/main`` and import it standalone,
so a PR cannot weaken the gate that judges that same PR. Keep it stdlib-only.

Increment 1 scope: every entry is advisory. The runner always exits 0 and
reports divergence counts — it blocks nothing. See the design doc for the
promotion/quarantine mechanism that makes an entry merge-blocking later.

Second-attempt repair (PR 518 closed after review): a relation compares
observations, and an empty set trivially equals another empty set. Ten of
the twelve kinds returned CONSISTENT on zero real observations, and the
"honesty" accounting meant to catch that was itself dishonest — two kinds
hard-coded an observation count of two and a third counted input files
instead of anything actually observed. Every kind below now reports a
BARREN verdict, distinct from CONSISTENT, when it cannot point to a real
observation backing its answer. Silence is never evidence of health.

Barren accounting is POST-filter: a relation ``filter``ed on one rule that
only ever sees a *different* rule fire is exactly as uninformed as one that
sees nothing at all. Counting unrelated noise as "observed something" would
let a misconfigured or permanently-silent filter read as CONSISTENT
forever — the same failure class this module exists to catch, one layer in.

Third-attempt repair (PR 529, review executed against ``true``): the
``exit_code`` observer laundered silence — a clean, silent exit 0 and a
timeout each became an observation, so six kinds read CONSISTENT against a
tool that never looked at its input. And ``equivalence_class`` could never
pass on a correct tool: every member of a well-formed class yields nothing,
so its verdict was always BARREN or INCONSISTENT. The optional ``control``
member — one input the spec puts OUTSIDE the class, which must fire — is
what turns agreement-on-silence into evidence.

Increment 2b adds the promotion mechanism referenced above: every ``run``
appends one line to ``runs.jsonl`` recording which entries passed, and
``promote_entry`` moves an entry's ``tier`` from advisory to required only
when the last recorded run was on ``main``, that entry passed on it, AND
increment 2c's mutant canary (``.company/state/acceptance/canary.json``)
reports it caught its mutant. No condition is inferred — a missing,
malformed or non-main input refuses by name rather than defaulting either
way, because every one of them can be forged by the same person who wants
the promotion. This increment does not make a required-tier entry block
anything yet; that is wired up separately. What it does do is make
``required`` a value that has to be earned, which is why ``load_entries``
now refuses to read a tier it does not recognize and sniffs the tier out of
an entry too corrupt to parse: promotion is the only path in, so an edit
must not be a way around it.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import fnmatch
import hashlib
import importlib
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from xml.etree import ElementTree as ET

DEFAULT_TIMEOUT_SECONDS = 30
# The daemon's default ``acceptance.stepTimeoutSeconds``
# (``DaemonConfig.acceptance_step_timeout_seconds``); a test holds the two equal.
DEFAULT_STEP_TIMEOUT_SECONDS = 900.0
# Confirmation re-runs are budgeted from the time LEFT after the main pass,
# not from a share of the step timeout counted from process start
# (DIVERGENCE-UNCONFIRMED-NOT-OPENED: webapp's main pass took 1106-1458 s, past
# a half-timeout budget, so no re-run ever started and every new regression
# went out unconfirmed). The deadline is the step timeout less a reserve for
# writing the run's records, which a killed step never writes; a re-run starts
# only when the entry's own first-run time still fits before that deadline.
CONFIRM_RESERVE_SECONDS = 30.0
CONFIRM_RESERVE_SHARE = 0.05


def confirm_reserve_seconds(step_timeout_seconds: float) -> float:
    """What the confirmation deadline leaves of the step timeout for the records."""
    return max(CONFIRM_RESERVE_SECONDS, step_timeout_seconds * CONFIRM_RESERVE_SHARE)


def _monotonic() -> float:
    """The clock the confirmation budget reads; a seam, so a test can move it
    without moving the clock ``subprocess`` times its invocations with."""
    return time.monotonic()


# The ceiling on the manifest's own ``timeoutSeconds`` (the per-entry default).
# An entry may run longer only by declaring it or by its history
# (LEDGER-ADAPTIVE-ENTRY-TIMEOUT, ``entry_timeout_seconds``), up to
# ``acceptance.maxEntryTimeoutSeconds``.
MAX_TIMEOUT_SECONDS = 120

# LEDGER-ADAPTIVE-ENTRY-TIMEOUT. On 2026-10-04 a webapp entry that must wait for
# a real one-minute meeting to end took ~2.5 min, past the fixed 120 s cap, so it
# would fail every run. Each entry's timeout is now
#   clamp(max(manifest default, the entry's declared ``timeoutSeconds``,
#             LEARNED_TIMEOUT_FACTOR x p95 of its last LEARNED_TIMEOUT_WINDOW
#             passing durations), <= acceptance.maxEntryTimeoutSeconds).
DEFAULT_MAX_ENTRY_TIMEOUT_SECONDS = 300.0
LEARNED_TIMEOUT_WINDOW = 20
LEARNED_TIMEOUT_FACTOR = 1.5
LEARNED_TIMEOUT_PERCENTILE = 0.95
# Guardrails, so a learned limit never silently absorbs a slowdown:
# a duration above the lower of the rolling median and the entry's frozen
# baseline (``_baseline_reference``) by both PERF_REGRESSION_RATIO and
# PERF_REGRESSION_MIN_SECONDS is a perf_regression finding (with at least
# PERF_REGRESSION_MIN_SAMPLES passing durations behind the median); a duration
# above NEAR_LIMIT_SHARE of the entry's timeout is a near-limit warning; a sum
# of the entries' learned timeouts above STEP_BUDGET_WARN_SHARE of the step
# timeout is a budget warning.
#
# LEDGER-PERF-LANE-NOISE (webapp, 2026-10-11): the backlog lane this feeds
# (``backlog_generator.generate_acceptance_perf_regressions``) minted 5 PRs in
# one day, each only a proposal doc, for two entries, plus 5 more blocked
# tasks for one of them. Two of the three root causes live here:
#   1. PERF_REGRESSION_MIN_SECONDS was 10.0 -- raised to 20.0 so one noisy run
#      on a loaded self-hosted runner needs a bigger absolute jump to count.
#   2. the per-run comparison flagged a SINGLE duration; ``_timing_findings``
#      now also judges the MEDIAN of the last up to 3 consecutive passing
#      durations (this one plus its last two) against the same thresholds,
#      and only THAT (the "reportable" finding, ``report.perf_regressions``)
#      feeds ``perf_regressions.json`` / the backlog signal. The single-run
#      comparison (``perf_regression`` called with the raw duration) still
#      decides what is excluded from the learned timeout -- unchanged, so one
#      smoothed-away blip never gets silently absorbed into the ceiling.
# The third (baseline identity not tracking a case/check edit) is
# ``_entry_content_hash`` / LEDGER-PERF-BASELINE-IDENTITY below.
PERF_REGRESSION_RATIO = 1.5
PERF_REGRESSION_MIN_SECONDS = 20.0
PERF_REGRESSION_MIN_SAMPLES = 5
# How many of the most recent consecutive passing durations (this run plus
# its predecessors) the reportable finding's median is taken over.
PERF_REGRESSION_WINDOW = 3
NEAR_LIMIT_SHARE = 0.8
STEP_BUDGET_WARN_SHARE = 0.8
# The environment variable every invocation gets with its own timeout, in whole
# seconds. A product adapter that runs its own test runner under a timeout of its
# own should honour it (never stop sooner); one that ignores it is killed at it.
TIMEOUT_ENV = "FORGE_ACCEPTANCE_TIMEOUT_SECONDS"
PERF_FINDINGS_FILENAME = "perf_regressions.json"
DEFAULT_ALLOWED_INVOKE_BINARIES: tuple[str, ...] = (
    ".venv/bin/*",
    "python",
    "python3",
    "node",
)

# Promotion sets ``TIER_REQUIRED`` on an entry's own JSON file; every entry
# that has not been promoted reads as ``TIER_ADVISORY``. These two are the
# ONLY accepted values — see ``load_entries``.
TIER_REQUIRED = "required"
TIER_ADVISORY = "advisory"

_TIER_REQUIRED_RE = re.compile(r'"tier"\s*:\s*"required"')


class LedgerError(Exception):
    """Raised for malformed ledgers, manifests, or disallowed invocations."""


@dataclass(frozen=True)
class LedgerConfig:
    """Anchors every path off ``base_dir`` — no cwd-relative defaults.

    Test isolation is a single ``base_dir=tmp_path`` override (per CLAUDE.md's
    test-isolation rule for path-defaulting configs).
    """

    base_dir: Path
    ledger_dir: Path
    state_dir: Path
    allowed_invoke_binaries: tuple[str, ...] = DEFAULT_ALLOWED_INVOKE_BINARIES
    tmp_root: Path | None = None
    step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS
    max_entry_timeout_seconds: float = DEFAULT_MAX_ENTRY_TIMEOUT_SECONDS
    # Set per entry by ``run_ledger``: the timeout every invocation of that
    # entry gets (None: the manifest default), and a list each invocation's
    # wall time is appended to (None: not recorded).
    entry_timeout_seconds: int | None = None
    invoke_seconds: list[float] | None = dataclasses.field(default=None, compare=False)

    @classmethod
    def for_project(cls, base_dir: Path) -> "LedgerConfig":
        base_dir = base_dir.resolve()
        return cls(
            base_dir=base_dir,
            ledger_dir=base_dir / "acceptance",
            state_dir=base_dir / ".company" / "state" / "acceptance",
            allowed_invoke_binaries=_load_allowed_binaries(base_dir),
            step_timeout_seconds=_load_step_timeout(base_dir),
            max_entry_timeout_seconds=_load_max_entry_timeout(base_dir),
        )


_ROOT_CONFIG = "gatesmith.json"
_TEMPLATE_CONFIG = ".claude/gatesmith.json"
_UNDECLARED = object()


def _governing_setting(base_dir: Path, dotted: str) -> Any:
    """The value of ``dotted`` (e.g. ``acceptance.runner``) by the one rule
    every config reader follows (``governing_config.py``): the root file
    governs a path it declares, else the template, else ``_UNDECLARED``.

    A private copy of that rule, not an import: this module is stdlib-only
    and CI imports it standalone from origin/main. A file that exists but is
    not a readable JSON object, or holds a non-object where the path needs
    one, raises ``LedgerError`` -- an unreadable root is an error even when
    the template declares the path, because the root may be what the operator
    meant to say.
    """
    keys = dotted.split(".")
    for rel in (_ROOT_CONFIG, _TEMPLATE_CONFIG):
        path = base_dir / rel
        if not (path.is_symlink() or path.exists()):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LedgerError(f"{rel} cannot be read as JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LedgerError(f"{rel} is not a JSON object")
        node: Any = data
        for i, key in enumerate(keys):
            if not isinstance(node, dict):
                where = ".".join(keys[:i])
                raise LedgerError(f"{rel}: {where} is not an object")
            if key not in node:
                break
            node = node[key]
        else:
            return node
    return _UNDECLARED


def _load_step_timeout(base_dir: Path) -> float:
    """``acceptance.stepTimeoutSeconds``, as the daemon reads it.

    The daemon kills the ``run`` sub-step at this many seconds; the ledger
    budgets its confirmation re-runs against it. A missing or non-positive
    value is the daemon's own default; an unreadable config file raises
    ``LedgerError``.
    """
    value = _governing_setting(base_dir, "acceptance.stepTimeoutSeconds")
    seconds = _positive_seconds(None if value is _UNDECLARED else value)
    return DEFAULT_STEP_TIMEOUT_SECONDS if seconds is None else seconds


def _positive_seconds(value: Any) -> float | None:
    """``value`` as a finite positive number of seconds, or None."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not value > 0
        or value == float("inf")
    ):
        return None
    return float(value)


def _load_max_entry_timeout(base_dir: Path) -> float:
    """``acceptance.maxEntryTimeoutSeconds``; 300 when missing or non-positive.

    The hard ceiling on any one entry's timeout, declared or learned. An
    unreadable config file raises ``LedgerError``.
    """
    value = _governing_setting(base_dir, "acceptance.maxEntryTimeoutSeconds")
    seconds = _positive_seconds(None if value is _UNDECLARED else value)
    return DEFAULT_MAX_ENTRY_TIMEOUT_SECONDS if seconds is None else seconds


def _load_allowed_binaries(base_dir: Path) -> tuple[str, ...]:
    allowed = _governing_setting(base_dir, "acceptance.allowedInvokeBinaries")
    if (
        isinstance(allowed, list)
        and allowed
        and all(isinstance(item, str) for item in allowed)
    ):
        return tuple(allowed)
    return DEFAULT_ALLOWED_INVOKE_BINARIES


# --------------------------------------------------------------------------
# Loading — one bad file must not take down the whole ledger.
# --------------------------------------------------------------------------


def load_manifest(ledger_dir: Path) -> dict[str, Any]:
    manifest_path = ledger_dir / "manifest.json"
    if not manifest_path.is_file():
        raise LedgerError(f"no manifest.json under {ledger_dir}")
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("invoke"):
        raise LedgerError(f"{manifest_path}: 'invoke' is required")
    if not isinstance(manifest.get("observers"), dict):
        raise LedgerError(f"{manifest_path}: 'observers' must be an object")
    try:
        manifest_port_env(manifest)
    except LedgerError as exc:
        raise LedgerError(f"{manifest_path}: {exc}") from exc
    return manifest


def _sniff_tier(raw: str | None) -> str:
    """Read a tier out of text that failed to parse as JSON.

    Used only on the load-error path, where the entry's own structure is
    unavailable. Conservative by construction: anything that still looks
    like a required declaration is read as required.
    """
    if raw and _TIER_REQUIRED_RE.search(raw):
        return TIER_REQUIRED
    return TIER_ADVISORY


def load_entries(ledger_dir: Path, subdir: str) -> list[dict[str, Any]]:
    """Load every ``*.json`` file under ``subdir``.

    A file that fails to parse is not skipped silently and does not abort
    the rest of the load — it is carried forward as an entry marked
    ``_load_error`` so ``run_ledger`` can report it as one failed entry
    among many that ran fine (a later increment blocks merges on this
    output; a load error must never fail closed over the whole run).

    Two tier rules, ported from the increment-2 recovery branch, because
    increment 2b is what first makes ``required`` writable:

    * a load error carries the tier **sniffed out of the raw text**, not a
      hardcoded advisory. Otherwise corrupting a required entry — or just
      deleting its tier line — quietly demotes it out of the blocking set,
      which is the single edit that disarms a promoted entry with no
      diagnostic at all. Unparseable JSON is exactly when the entry cannot
      be trusted to speak for itself, so the conservative reading wins.
    * an unrecognized tier (typo, wrong case, stray whitespace) becomes a
      load error rather than reading as advisory: a merge-blocking
      intention must never go unenforced silently.

    The read is byte-level and decoded permissively, not ``read_text()``:
    invalid UTF-8 raises ``UnicodeDecodeError``, a ``ValueError`` that is
    neither ``OSError`` nor ``JSONDecodeError`` and so used to escape this
    function and take the whole run down — the exact "one bad file" failure
    this function exists to isolate. ``ValueError`` covers that and
    ``JSONDecodeError`` (a subclass) in one clause, and still leaves
    ``_sniff_tier`` some text to search.
    """
    entries: list[dict[str, Any]] = []
    directory = ledger_dir / subdir
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("*.json")):
        raw: str | None = None
        try:
            raw = path.read_bytes().decode("utf-8", errors="replace")
            entry = json.loads(raw)
        except (OSError, ValueError) as exc:
            entries.append(
                {
                    "id": path.stem,
                    "kind": "unknown",
                    "tier": _sniff_tier(raw),
                    "_source": str(path),
                    "_load_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        tier = entry.get("tier", TIER_ADVISORY)
        if tier not in (TIER_ADVISORY, TIER_REQUIRED):
            entries.append(
                {
                    "id": entry.get("id", path.stem),
                    "kind": entry.get("kind", "unknown"),
                    "tier": TIER_ADVISORY,
                    "_source": str(path),
                    "_load_error": f"unrecognized tier {tier!r} (expected "
                    f"{TIER_ADVISORY!r} or {TIER_REQUIRED!r})",
                }
            )
            continue
        entry.setdefault("id", path.stem)
        entry["_source"] = str(path)
        entries.append(entry)
    if entries:
        _resolve_tiers(entries, ledger_dir)
    return entries


# --------------------------------------------------------------------------
# Operator-declared required tier (non-Python artifacts)
#
# ``promote_entry`` earns "required" with Gatesmith's mutant canary, and that
# canary mutates PYTHON source. An artifact it cannot mutate (a TypeScript
# product behind a node adapter) could never reach "required", so its
# divergences never became repair work. Such a product proves "this entry
# can fail" with its own canaries; an entry may say so in a ``promotion``
# block. See docs/acceptance-ledger.md § "Operator-declared required tier".
# --------------------------------------------------------------------------

PROMOTION_BY_OPERATOR = "operator"
# Provenance of a required entry that carries no promotion block: ``canary``
# when promotions.json holds a ``promote`` record for its id, ``unrecorded``
# when it does not (written by hand, or the state file is absent — as in CI).
# The tier is honoured either way; only the label differs.
PROMOTION_BY_CANARY = "canary"
PROMOTION_UNRECORDED = "unrecorded"


def _promoted_ids(ledger_dir: Path) -> set[str]:
    """Entry ids ``promote_entry`` recorded in promotions.json; empty if unreadable."""
    path = ledger_dir.parent / ".company" / "state" / "acceptance" / "promotions.json"
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return set()
    records = data.get("promotions") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return set()
    return {
        r["entry_id"]
        for r in records
        if isinstance(r, dict) and isinstance(r.get("entry_id"), str)
    }


# The placeholder acceptance_canary.ARTIFACT_TOKEN reads; a literal here so
# this module stays import-free.
_CANARY_ARTIFACT_TOKEN = "{ARTIFACT}"
_PYTHON_INTERPRETER_RE = re.compile(r"python(\d+(\.\d+)*)?(\.exe)?")


# Larger files are not parsed: a 16 MB data file named in invoke cost ~5 s per
# load_entries. No real adapter or tool source comes near this size.
PYTHON_PARSE_MAX_BYTES = 1024 * 1024


def _parses_as_python(path: Path) -> bool:
    try:
        if path.stat().st_size > PYTHON_PARSE_MAX_BYTES:
            return False
        ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
        # ValueError covers UnicodeDecodeError and null bytes.
        return False
    return True


def canary_mutable_sources(manifest: dict[str, Any], base_dir: Path) -> list[str]:
    """The ``invoke`` elements that make this ledger's artifact Python.

    Empty means Gatesmith's mutant canary cannot mutate the artifact, which is the
    one condition under which an operator-declared required tier is honoured.
    An element counts when it is the canary's ``{ARTIFACT}`` placeholder,
    names a Python interpreter (``python``, ``python3``, ``python3.12``), or
    — after ``{PROJECT}`` is filled in — resolves (absolute, or relative to
    ``base_dir``) to a file that parses as Python. That is deliberately
    wider than what ``acceptance_canary.resolve_artifact_marker`` can reach
    (it ignores ``{PROJECT}/tool.py`` and ``python -m tool``): a Python
    artifact the canary cannot address gets its manifest fixed, not an
    operator declaration.
    """
    invoke = manifest.get("invoke")
    if isinstance(invoke, str):
        invoke = [invoke]
    if not isinstance(invoke, list):
        return []
    sources: list[str] = []
    for position, arg in enumerate(invoke):
        if not isinstance(arg, str) or not arg:
            continue
        if _CANARY_ARTIFACT_TOKEN in arg:
            sources.append(arg)
            continue
        if position == 0 and _PYTHON_INTERPRETER_RE.fullmatch(Path(arg).name):
            sources.append(arg)
            continue
        text = arg.replace("{PROJECT}", str(base_dir))
        if "{" in text:
            # {INPUT} / {FIXTURES}: a per-invocation temp file, never the artifact.
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            if candidate.is_file() and _parses_as_python(candidate):
                sources.append(arg)
        except OSError:
            continue
    return sources


def _manifest_canary_sources(ledger_dir: Path) -> tuple[list[str] | None, str]:
    """``canary_mutable_sources`` for ``ledger_dir``'s manifest, or ``(None, why)``.

    None means the manifest could not be read, so nothing can be said about
    the artifact and no operator declaration can be honoured.
    """
    manifest_path = ledger_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{manifest_path} could not be read ({type(exc).__name__})"
    if not isinstance(manifest, dict):
        return None, f"{manifest_path} is not a JSON object"
    return canary_mutable_sources(manifest, ledger_dir.parent), ""


def operator_promotion_problem(
    promotion: Any, base_dir: Path, ledger_dir: Path | None = None
) -> str | None:
    """Why ``promotion`` is not a well-formed operator declaration, or None.

    Shape: ``{"by": "operator", "reason": "<non-empty>", "canary": [paths]}``
    where ``canary`` (a string or a non-empty list of strings) names the
    artifact's own canary evidence: project-relative paths to non-empty FILES
    inside the project — not a directory (the root included), and not the
    ledger's own ``manifest.json`` or entry files. An optional
    ``canary_command`` string says how to run it.
    """
    if not isinstance(promotion, dict):
        return f"'promotion' is a {type(promotion).__name__}, expected an object"
    if promotion.get("by") != PROMOTION_BY_OPERATOR:
        return (
            f"'promotion.by' is {promotion.get('by')!r}; only "
            f"{PROMOTION_BY_OPERATOR!r} can be declared in an entry file "
            "(the canary path is 'gatesmith-acceptance promote')"
        )
    reason = promotion.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "'promotion.reason' is missing or empty"
    canary = promotion.get("canary")
    if isinstance(canary, str):
        canary = [canary]
    if (
        not isinstance(canary, list)
        or not canary
        or not all(isinstance(item, str) and item.strip() for item in canary)
    ):
        return (
            "'promotion.canary' must name the artifact's own canary evidence "
            "(a project-relative path, or a list of them)"
        )
    command = promotion.get("canary_command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        return "'promotion.canary_command' is present but not a non-empty string"
    root = base_dir.resolve()
    ledger_root = (ledger_dir or base_dir / "acceptance").resolve()
    own_dirs = (ledger_root / "cases", ledger_root / "relations")
    for item in canary:
        relative = Path(item)
        if relative.is_absolute():
            return f"'promotion.canary' path {item!r} is absolute; name it relative to the project"
        try:
            resolved = (root / relative).resolve()
        except (OSError, RuntimeError):
            return f"'promotion.canary' path {item!r} cannot be resolved"
        if resolved != root and root not in resolved.parents:
            return f"'promotion.canary' path {item!r} is outside the project"
        if not resolved.exists():
            return f"'promotion.canary' path {item!r} does not exist in {root}"
        if not resolved.is_file():
            return (
                f"'promotion.canary' path {item!r} is not a file in {root} "
                "(name the canary test file itself, not a directory)"
            )
        if resolved == ledger_root / "manifest.json" or any(
            own in resolved.parents for own in own_dirs
        ):
            return (
                f"'promotion.canary' path {item!r} is the ledger's own manifest "
                "or entries, not the artifact's canary"
            )
        try:
            empty = resolved.stat().st_size == 0
        except OSError:
            empty = True
        if empty:
            return f"'promotion.canary' path {item!r} is an empty file"
    return None


def _resolve_tiers(entries: list[dict[str, Any]], ledger_dir: Path) -> None:
    """Settle each loaded entry's effective tier, in place.

    Sets ``_promotion`` (``"operator"``, ``"canary"`` or ``"unrecorded"``) on an entry that ends
    up required, and ``_tier_warning`` wherever a declaration was not honoured.
    A load-error entry keeps its sniffed tier: it cannot speak for itself.

    * No ``promotion`` block: unchanged from before, EXCEPT that a required
      entry over an artifact Gatesmith's canary cannot mutate is read as advisory
      — ``promote`` cannot have granted it, so it was written by hand.
    * A ``promotion`` block on a required entry is honoured only when it is a
      well-formed operator declaration AND the manifest's artifact is one
      Gatesmith's canary cannot mutate (``canary_mutable_sources`` is empty).
      Otherwise the entry reads as advisory.
    """
    base_dir = ledger_dir.parent
    sources: list[str] | None = None
    sources_why = ""
    sources_read = False
    promoted: set[str] | None = None

    def canary_sources() -> tuple[list[str] | None, str]:
        nonlocal sources, sources_why, sources_read
        if not sources_read:
            sources, sources_why = _manifest_canary_sources(ledger_dir)
            sources_read = True
        return sources, sources_why

    for entry in entries:
        if "_load_error" in entry:
            continue
        declared = entry.get("tier", TIER_ADVISORY)
        has_block = "promotion" in entry
        if declared != TIER_REQUIRED:
            if has_block:
                entry["_tier_warning"] = (
                    "a 'promotion' block on an advisory entry has no effect; "
                    f"set tier {TIER_REQUIRED!r} to declare it"
                )
            continue
        mutable, why = canary_sources()
        if not has_block:
            if mutable == []:
                entry["tier"] = TIER_ADVISORY
                entry["_tier_warning"] = (
                    f"declares tier {TIER_REQUIRED!r} with no 'promotion' block, "
                    "and Gatesmith's mutant canary cannot mutate this ledger's "
                    "artifact, so 'promote' cannot have granted it; read as "
                    f"{TIER_ADVISORY!r}. Add an operator promotion block naming "
                    "the artifact's own canary (docs/acceptance-ledger.md)"
                )
            else:
                if promoted is None:
                    promoted = _promoted_ids(ledger_dir)
                entry["_promotion"] = (
                    PROMOTION_BY_CANARY
                    if entry.get("id") in promoted
                    else PROMOTION_UNRECORDED
                )
            continue
        problem = operator_promotion_problem(
            entry["promotion"], base_dir, ledger_dir=ledger_dir
        )
        if problem is None and mutable is None:
            problem = (
                f"{why}, so it cannot be shown that Gatesmith's mutant canary "
                "cannot mutate the artifact"
            )
        if problem is None and mutable:
            problem = (
                "Gatesmith's mutant canary can mutate this ledger's artifact "
                f"(invoke names Python: {', '.join(mutable)}); earn "
                f"{TIER_REQUIRED!r} with 'gatesmith-acceptance promote', which runs "
                "it — an operator declaration is only for artifacts it cannot "
                "mutate"
            )
        if problem is not None:
            entry["tier"] = TIER_ADVISORY
            entry["_tier_warning"] = (
                f"operator promotion refused, read as {TIER_ADVISORY!r}: {problem}"
            )
            continue
        entry["_promotion"] = PROMOTION_BY_OPERATOR


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


@dataclass
class InvokeResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def check_invoke_allowed(argv: list[str], allowed_binaries: tuple[str, ...]) -> None:
    if not argv:
        raise LedgerError("invoke argv is empty")
    binary = argv[0]
    if not any(fnmatch.fnmatch(binary, pattern) for pattern in allowed_binaries):
        raise LedgerError(
            f"invoke binary {binary!r} is not in allowedInvokeBinaries {allowed_binaries!r}"
        )


def _substitute_argv(
    argv_template: list[str],
    input_path: Path,
    fixtures_dir: Path,
    project_dir: Path | None = None,
) -> list[str]:
    """Fill ``{INPUT}``, ``{FIXTURES}`` and, when given, ``{PROJECT}``.

    ``{PROJECT}`` is the project root the run was pointed at. The invocation's
    ``cwd`` is a throwaway workdir, so a file the ledger tracks (an adapter
    script) cannot be named by a relative path; only ``argv[0]`` is resolved
    against the project root.
    """
    substituted = []
    for arg in argv_template:
        arg = arg.replace("{INPUT}", str(input_path)).replace(
            "{FIXTURES}", str(fixtures_dir)
        )
        if project_dir is not None:
            arg = arg.replace("{PROJECT}", str(project_dir))
        substituted.append(arg)
    return substituted


def resolve_invoke_binary(binary: str, base_dir: Path) -> str:
    """Resolve a relative, path-shaped invoke binary against the project root.

    A relative path like ``.venv/bin/mycli`` cannot resolve against the
    throwaway per-invocation temp directory ``run_invoke`` uses as ``cwd`` —
    it is never there, so the shipped ``.venv/bin/*`` default could never
    fire. Bare command names (``python3``, ``node``) have no ``/`` and are
    left alone so normal ``PATH`` lookup still applies.

    ``check_invoke_allowed`` matches this string with ``fnmatch`` BEFORE
    resolution, and ``fnmatch``'s ``*`` matches ``/`` — so
    ``.venv/bin/../../../../usr/bin/whatever`` passes a ``.venv/bin/*``
    allowlist entry. Containment must be re-checked AFTER resolution, using
    the same rejection ``_resolve_within`` already applies to fixtures
    (covers a real ``..`` escape and a symlink planted inside ``base_dir``
    that points outside it — ``Path.resolve()`` follows symlinks, so a
    string-only ``..`` check would miss the second case entirely).
    """
    if "/" in binary and not Path(binary).is_absolute():
        return str(_resolve_within(base_dir, binary))
    return binary


def _resolve_within(anchor: Path, name: str) -> Path:
    """Resolve ``name`` under ``anchor``, rejecting any escape.

    Shared by two callers, both handling untrusted, ledger-JSON-authored
    strings: fixture/input paths against the throwaway workdir, and a
    relative invoke binary against the project root. An absolute path, a
    ``..`` segment, or a symlink planted inside ``anchor`` that points
    outside it must never resolve to something outside ``anchor`` —
    ``Path.resolve()`` follows symlinks, so checking containment on the
    resolved path (not the literal string) catches all three the same way.
    """
    target = (anchor / name).resolve()
    try:
        target.relative_to(anchor.resolve())
    except ValueError:
        raise LedgerError(f"path {name!r} escapes {anchor}") from None
    return target


def materialize_workdir(
    tmp_root: Path, fixtures: dict[str, str] | None, input_filename: str, content: str
) -> tuple[Path, Path]:
    workdir = Path(tempfile.mkdtemp(dir=tmp_root))
    for name, body in (fixtures or {}).items():
        target = _resolve_within(workdir, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    input_path = _resolve_within(workdir, input_filename)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(content)
    return workdir, input_path


_INVOKE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")

# Set in every invocation's environment, whatever the caller's shell holds.
# ``CI=1`` is what a product's test runner reads to decide whether it may attach
# to a server that is already listening (playwright's
# ``reuseExistingServer: !process.env.CI``). Without it, a ledger run started
# while a daemon worker had the product's dev servers up on the well-known ports
# ATTACHED to the worker's servers and read their state as the product's (webapp,
# 2026-09-28: 12 browser entries failed that passed on the same code). With it,
# the runner starts its own server, and a port someone else holds fails the
# start instead of being shared (with ``portEnv``: ``_SERVER_START_FAILURE``).
_INVOKE_ENV_FORCED = {"CI": "1"}

# A product whose entries start a server (a browser suite's relay and app) names
# the environment variables that carry its listening ports in the manifest's
# ``portEnv``. The suite's well-known defaults are shared with every other run
# on the machine, a daemon worker's test run included; a collision reads as a
# regression in the product that is really the ledger colliding with a neighbour.
PORT_ENV_MAX_NAMES = 8
_PORT_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

# Ports handed out through ``portEnv`` are drawn from this range: below the
# ephemeral ranges macOS (49152-65535) and Linux (32768-60999) give outgoing
# connections, so the kernel does not hand one of them to an outbound socket
# opened between the probe and the tool's own bind.
PORT_RANGE = (20000, 30000)
_PORT_PICK_ATTEMPTS = 200


def manifest_port_env(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """The validated ``portEnv`` names of a manifest; empty when it declares none.

    The names come from ledger JSON, so a name that could redirect how the
    invoked tool starts (``PATH``, ``NODE_OPTIONS``, ``LD_PRELOAD``) or that
    the ledger sets itself (``CI``) is refused rather than overwritten with a
    number.
    """
    declared = manifest.get("portEnv")
    if declared is None:
        return ()
    if (
        not isinstance(declared, list)
        or len(declared) > PORT_ENV_MAX_NAMES
        or not all(isinstance(name, str) for name in declared)
    ):
        raise LedgerError(
            f"'portEnv' must be a list of at most {PORT_ENV_MAX_NAMES} "
            "environment variable names"
        )
    if len(set(declared)) != len(declared):
        raise LedgerError("'portEnv' names the same variable twice")
    for name in declared:
        if not _PORT_ENV_NAME.fullmatch(name):
            raise LedgerError(f"'portEnv' name {name!r} is not an UPPER_SNAKE name")
        if (
            name in _INVOKE_ENV_PASSTHROUGH
            or name in _INVOKE_ENV_FORCED
            or name == TIMEOUT_ENV
            or name.startswith(("LD_", "DYLD_", "NODE_", "PYTHON"))
        ):
            raise LedgerError(f"'portEnv' name {name!r} is reserved")
    return tuple(declared)


def _free_ports(count: int) -> list[int]:
    """``count`` distinct loopback ports in ``PORT_RANGE`` that are free right now.

    Each candidate is picked at random and bound to check it is free; the probe
    sockets are held open together, so the ports differ from each other, then
    released so the invoked tool can bind them. A bound port cannot be handed
    from this process to another, so a window remains between the release and
    the tool's own bind in which another listener could take the port. A
    collision in that window fails the runner's server start, which the ledger
    reads as an ERROR, not a divergence (``check_server_started``).
    """
    low, high = PORT_RANGE
    probes: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(_PORT_PICK_ATTEMPTS):
            if len(ports) == count:
                break
            port = random.randrange(low, high)
            if port in ports:
                continue
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                probe.close()
                continue
            probes.append(probe)
            ports.append(port)
    finally:
        for probe in probes:
            probe.close()
    if len(ports) < count:
        raise LedgerError(
            f"could not find {count} free port(s) in {low}-{high - 1} "
            f"after {_PORT_PICK_ATTEMPTS} tries"
        )
    return ports


def _invoke_env(
    port_env: Sequence[str] = (), timeout_seconds: float | None = None
) -> dict[str, str]:
    """A minimal environment for the invoked CLI — not the caller's full one.

    The tool under test is often a third-party black box; running it with
    every environment variable the ledger's own process has (API keys,
    tokens, credentials picked up from the operator's or daemon's shell)
    hands it secrets it has no reason to see.

    ``CI`` is always ``1`` (``_INVOKE_ENV_FORCED``). ``port_env`` names get a
    free port each, drawn anew for every invocation; the caller's own value
    for such a name is not passed on. ``timeout_seconds``, when given, is
    passed as ``TIMEOUT_ENV`` (whole seconds) so a product adapter can size its
    own runner's timeout to the one the ledger kills it at.
    """
    env = {
        name: os.environ[name] for name in _INVOKE_ENV_PASSTHROUGH if name in os.environ
    }
    env.update(_INVOKE_ENV_FORCED)
    if port_env:
        env.update(
            (name, str(port))
            for name, port in zip(port_env, _free_ports(len(port_env)))
        )
    if timeout_seconds is not None:
        env[TIMEOUT_ENV] = str(int(timeout_seconds))
    return env


# The test runner's OWN report that it could not start the server the product's
# config names, because the port was taken. Two lines, both anchored at a line
# start so a product's assertion or log text quoting the same words does not
# match:
#   playwright, CI set:  "Error: http://localhost:5173 is already used, make sure
#                         that your server is not running or set
#                         reuseExistingServer:true in config.webServer."
#   playwright prefixes the webServer command's own output with "[WebServer] ",
#   so a server that died binding its port prints "[WebServer] ... EADDRINUSE".
# A server that crashed on boot, a webServer timeout and anything the product
# prints are not in this list: they are the product failing, and stay a verdict.
_SERVER_START_FAILURE = re.compile(
    r"^\s*Error: https?://\S+ is already used, make sure that your server is "
    r"not running"
    r"|^\s*\[WebServer\][^\n]*\bEADDRINUSE\b",
    re.MULTILINE,
)


class ServerStartError(LedgerError):
    """The test runner could not start its server on a taken port; nothing was judged."""


def check_server_started(result: "InvokeResult", port_env: Sequence[str]) -> None:
    """Raise ``ServerStartError`` when the runner reports its server's port was taken.

    Only for a manifest that declares ``portEnv``: the ledger picked those
    ports, so a collision on one is the ledger's, not the product's. Only on a
    non-zero exit, and only on the runner's own line (``_SERVER_START_FAILURE``).
    Every other failure, a server that crashes on boot or never comes up
    included, stays the verdict the entry's kind gives it.
    """
    if not port_env or result.timed_out or result.exit_code in (0, None):
        return
    for stream in (result.stderr, result.stdout):
        match = _SERVER_START_FAILURE.search(stream)
        if match:
            end = stream.find("\n", match.start())
            line = stream[match.start() : end if end != -1 else len(stream)].strip()
            raise ServerStartError(
                f"the test runner could not start its server on a port the ledger "
                f"handed out (exit {result.exit_code}), so this invocation judged "
                f"nothing: {line[:300]}"
            )


def run_invoke(
    argv_template: list[str],
    workdir: Path,
    input_path: Path,
    fixtures_dir: Path,
    timeout_seconds: int,
    allowed_binaries: tuple[str, ...],
    base_dir: Path,
    extra_args: list[str] | None = None,
    port_env: Sequence[str] = (),
) -> InvokeResult:
    argv = _substitute_argv(argv_template, input_path, fixtures_dir, base_dir)
    if extra_args:
        argv = [*argv, *extra_args]
    check_invoke_allowed(argv, allowed_binaries)
    resolved_argv = [resolve_invoke_binary(argv[0], base_dir), *argv[1:]]
    try:
        completed = subprocess.run(
            resolved_argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_invoke_env(port_env, timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        return InvokeResult(
            argv=resolved_argv,
            exit_code=None,
            stdout="",
            stderr=f"timed out after {timeout_seconds}s",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise LedgerError(
            f"invoke binary not found: {resolved_argv[0]!r} ({exc})"
        ) from exc
    return InvokeResult(
        argv=resolved_argv,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        timed_out=False,
    )


def invoke_timeout_seconds(manifest: dict[str, Any]) -> int:
    """How long one invocation may run before it is killed."""
    return min(
        manifest.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS
    )


# --------------------------------------------------------------------------
# Per-entry timeouts, learned from history (LEDGER-ADAPTIVE-ENTRY-TIMEOUT)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryHistory:
    """An entry's recent PASSING durations from ``runs.jsonl``, oldest first.

    ``passing``: up to ``LEARNED_TIMEOUT_WINDOW`` passing durations, the
    rolling median's sample. ``learnable``: the same, less every duration that
    run flagged ``perf_regression`` — what the learned timeout is computed
    from, so a slowdown is reported rather than absorbed into the limit.
    """

    passing: tuple[float, ...] = ()
    learnable: tuple[float, ...] = ()
    # The ``timestamp`` of the run each ``passing`` duration came from (0.0
    # when the record had none), so a re-baselined entry's rolling median can
    # leave out the durations from before its new declared timeout.
    passing_at: tuple[float, ...] = ()

    def since(self, started: float) -> "EntryHistory":
        """The passing durations recorded after ``started`` (learnable kept)."""
        kept = [
            (seconds, at)
            for seconds, at in zip(self.passing, self.passing_at)
            if at > started
        ]
        return EntryHistory(
            passing=tuple(seconds for seconds, _ in kept),
            learnable=self.learnable,
            passing_at=tuple(at for _, at in kept),
        )


@dataclass(frozen=True)
class EntryTimeout:
    """The timeout one entry runs under, and where it came from."""

    seconds: int
    # "default" (the manifest's), "declared" (the entry's own
    # ``timeoutSeconds``), "learned" (its history) or "ceiling" (the largest of
    # those was above ``acceptance.maxEntryTimeoutSeconds``).
    source: str
    declared: float | None = None
    learned: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"seconds": self.seconds, "source": self.source}
        if self.declared is not None:
            out["declared"] = self.declared
        if self.learned is not None:
            out["learned"] = round(self.learned, 3)
        return out


def _percentile(values: Sequence[float], share: float) -> float:
    """Nearest-rank percentile of a non-empty sample."""
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return ordered[rank - 1]


def _str_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def read_entry_history(
    runs_path: Path, window: int = LEARNED_TIMEOUT_WINDOW
) -> dict[str, EntryHistory]:
    """Each entry's last ``window`` passing durations, read from ``runs.jsonl``.

    A duration counts when its run recorded it (``entry_seconds``), the entry
    passed on that run (``passing_ids``) and was not a flake there
    (``flaky_ids``: its recorded duration is the failed first run's). A run
    flagged by ``environment_flag`` is skipped, as is a torn line. Empty when
    the file is missing or unreadable: every entry then runs under its default
    or declared timeout.
    """
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    passing: dict[str, list[float]] = {}
    passing_at: dict[str, list[float]] = {}
    learnable: dict[str, list[float]] = {}
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or environment_flag(record):
            continue
        seconds = record.get("entry_seconds")
        if not isinstance(seconds, dict):
            continue
        passed = _str_set(record.get("passing_ids")) - _str_set(record.get("flaky_ids"))
        flagged = _str_set(record.get("perf_regression_ids"))
        stamp = _positive_seconds(record.get("timestamp")) or 0.0
        for entry_id, value in seconds.items():
            duration = _positive_seconds(value)
            if entry_id not in passed or duration is None:
                continue
            sample = passing.setdefault(entry_id, [])
            if len(sample) < window:
                sample.append(duration)
                passing_at.setdefault(entry_id, []).append(stamp)
            if entry_id not in flagged:
                learned = learnable.setdefault(entry_id, [])
                if len(learned) < window:
                    learned.append(duration)
    return {
        entry_id: EntryHistory(
            passing=tuple(reversed(sample)),
            learnable=tuple(reversed(learnable.get(entry_id, []))),
            passing_at=tuple(reversed(passing_at.get(entry_id, []))),
        )
        for entry_id, sample in passing.items()
    }


def entry_timeout_seconds(
    manifest: Mapping[str, Any],
    entry: Mapping[str, Any],
    history: EntryHistory | None,
    ceiling: float = DEFAULT_MAX_ENTRY_TIMEOUT_SECONDS,
) -> EntryTimeout:
    """The timeout each invocation of ``entry`` runs under.

    ``max(manifest default, the entry's declared timeoutSeconds,
    LEARNED_TIMEOUT_FACTOR x p95 of its learnable passing durations)``, never
    above ``ceiling`` (``acceptance.maxEntryTimeoutSeconds``). A declared value
    that is not a positive number is ignored. Whole seconds, rounded up.
    """
    default = float(invoke_timeout_seconds(dict(manifest)))
    declared = _positive_seconds(entry.get("timeoutSeconds"))
    learned = (
        LEARNED_TIMEOUT_FACTOR
        * _percentile(history.learnable, LEARNED_TIMEOUT_PERCENTILE)
        if history is not None and history.learnable
        else None
    )
    source, wanted = "default", default
    for name, value in (("declared", declared), ("learned", learned)):
        if value is not None and value > wanted:
            source, wanted = name, value
    if wanted > ceiling:
        source, wanted = "ceiling", ceiling
    return EntryTimeout(
        seconds=max(1, math.ceil(wanted)),
        source=source,
        declared=declared,
        learned=learned,
    )


def perf_regression(
    seconds: float,
    history: EntryHistory | None,
    baseline: float | None = None,
    rolling: bool = True,
) -> dict[str, Any] | None:
    """A ``perf_regression`` finding for one passing duration, or None.

    Raised when the duration exceeds its reference by more than
    ``PERF_REGRESSION_RATIO`` AND by more than ``PERF_REGRESSION_MIN_SECONDS``
    (a 2 s entry taking 4 s is noise). The reference is the LOWER of the
    rolling median of the entry's last passing durations (needs
    ``PERF_REGRESSION_MIN_SAMPLES`` of them; skipped when ``rolling`` is
    False) and ``baseline``, the entry's frozen median (``perf_baselines``).

    The frozen baseline is the absolute guard: the rolling median moves with
    the entry, so a lasting slowdown stops being flagged once it fills half
    the window and a slow creep (+1 s per run) is never flagged at all, while
    its learned timeout climbs to the ceiling. Against the baseline it stays
    flagged — and out of the learned timeout — until the entry declares a
    new ``timeoutSeconds`` (a reviewed change), which re-baselines it.
    """
    references: list[tuple[float, str]] = []
    if (
        rolling
        and history is not None
        and len(history.passing) >= PERF_REGRESSION_MIN_SAMPLES
    ):
        references.append((float(statistics.median(history.passing)), "rolling"))
    if baseline is not None:
        references.append((float(baseline), "baseline"))
    if not references:
        return None
    reference, kind = min(references)
    if seconds > reference * PERF_REGRESSION_RATIO and seconds > (
        reference + PERF_REGRESSION_MIN_SECONDS
    ):
        finding: dict[str, Any] = {
            "seconds": round(seconds, 3),
            "median": round(reference, 3),
            "reference": kind,
            "samples": len(history.passing) if history is not None else 0,
        }
        if baseline is not None:
            finding["baseline"] = round(float(baseline), 3)
        return finding
    return None


def _entry_content_hash(
    entry: Mapping[str, Any], manifest: Mapping[str, Any], base_dir: Path
) -> str | None:
    """sha256 of the entry's own case file plus the check (adapter) that runs it.

    LEDGER-PERF-BASELINE-IDENTITY (webapp, #703 fix round): ERASE-OTHERS-RECORDS
    went from ~2.5 min to 13 s when its case/check changed (a clock shim), but
    the baseline was keyed only on the entry's declared ``timeoutSeconds`` --
    unaffected by that edit -- so the frozen median kept comparing the new,
    fast behavior to the old, slow one. The identity now also covers the
    entry's own JSON bytes (the case) and the ``invoke`` adapter's Python
    source, resolved the same way the mutant canary does
    (``canary_mutable_sources``, ``{PROJECT}``-substituted). Either edit
    changes the hash; ``_baseline_reference``/``_baseline_update`` treat a
    hash change exactly like a declared-timeout change, so the baseline resets
    and no perf signal is raised until a fresh median re-freezes
    (``PERF_REGRESSION_MIN_SAMPLES`` new runs under the new content).

    Best-effort: a file that cannot be read contributes nothing to the hash
    rather than raising -- the worst case is a missed reset, never a crash.
    None only when there is nothing at all to hash (no readable case, no
    resolvable adapter), so a caller can tell "unknown" from a real digest.
    """
    digest = hashlib.sha256()
    hashed = False
    source = entry.get("_source")
    if isinstance(source, str):
        try:
            digest.update(Path(source).read_bytes())
            hashed = True
        except OSError:
            pass
    if not hashed:
        try:
            digest.update(
                json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
            )
            hashed = True
        except (TypeError, ValueError):
            pass
    try:
        sources = canary_mutable_sources(dict(manifest), base_dir)
    except Exception:  # noqa: BLE001 - a malformed manifest hashes just the case
        sources = []
    for arg in sources:
        if not isinstance(arg, str):
            continue
        text = arg.replace("{PROJECT}", str(base_dir))
        if "{" in text:
            # {INPUT}/{FIXTURES} (per-invocation) or {ARTIFACT} (hashed
            # separately when it names a real file elsewhere in the list).
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            if candidate.is_file():
                digest.update(candidate.read_bytes())
                hashed = True
        except OSError:
            continue
    return digest.hexdigest() if hashed else None


def _baseline_reference(
    record: Mapping[str, Any] | None,
    declared: float | None,
    content_hash: str | None = None,
) -> tuple[float | None, bool]:
    """(frozen baseline, rolling rule on?) for one entry from its record.

    A baseline record (``perf_regressions.json`` ``baselines``) is
    ``{"declared", "content_hash", "samples", "median", "set_at", "reset"}``:
    it collects the entry's first ``PERF_REGRESSION_MIN_SAMPLES`` unflagged
    passing durations, then freezes their median. No record: the rolling rule
    alone. A record for another declared ``timeoutSeconds`` OR another
    ``content_hash`` (LEDGER-PERF-BASELINE-IDENTITY: the entry's case file or
    its check changed) -- or one collecting after such a change, ``reset`` --
    gets neither rule until it freezes again: a reviewed new limit, or a new
    version of the entry, is judged from its own level, never the old median.
    """
    if not isinstance(record, Mapping):
        return None, True
    if record.get("declared") != declared or record.get("content_hash") != content_hash:
        return None, False
    median = _positive_seconds(record.get("median"))
    if median is not None:
        return median, True
    return None, not bool(record.get("reset"))


def _baseline_update(
    record: Mapping[str, Any] | None,
    declared: float | None,
    content_hash: str | None,
    seconds: float | None,
    now: float,
) -> dict[str, Any] | None:
    """The entry's baseline record after this run, or None when unchanged.

    ``seconds``: the run's duration when the entry passed unflagged (None:
    nothing to collect). A missing record, or one for another declared
    ``timeoutSeconds`` or ``content_hash``, starts a new one (``reset`` when it
    replaces one; its ``epoch_start`` then limits the rolling median to later
    runs).
    """
    if (
        not isinstance(record, Mapping)
        or record.get("declared") != declared
        or record.get("content_hash") != content_hash
    ):
        reset = isinstance(record, Mapping)
        return {
            "declared": declared,
            "content_hash": content_hash,
            "samples": [] if seconds is None else [round(seconds, 3)],
            "median": None,
            "set_at": None,
            "reset": reset,
            # After a reset the rolling median counts only runs from here on.
            "epoch_start": now if reset else None,
        }
    if _positive_seconds(record.get("median")) is not None or seconds is None:
        return None
    raw = record.get("samples")
    samples = [
        float(s) for s in (raw if isinstance(raw, list) else []) if _positive_seconds(s)
    ]
    samples.append(round(seconds, 3))
    updated: dict[str, Any] = {**record, "samples": samples}
    if len(samples) >= PERF_REGRESSION_MIN_SAMPLES:
        updated["median"] = round(float(statistics.median(samples)), 3)
        updated["set_at"] = now
    return updated


def step_budget_warning(
    timeouts: Mapping[str, EntryTimeout], step_timeout_seconds: float
) -> str | None:
    """A warning when the entries' learned timeouts approach the step timeout.

    Sums, per entry, what its history says it may need (its learned timeout;
    for an entry with no history, its declared ``timeoutSeconds``; neither:
    nothing), each at most the ceiling it runs under. The daemon kills the whole
    ``run`` step at ``stepTimeoutSeconds`` before any record is written, so a
    learned increase on a few long entries must not quietly starve the rest.
    """
    total = 0.0
    counted = 0
    for timeout in timeouts.values():
        bound = timeout.learned if timeout.learned is not None else timeout.declared
        if bound is None:
            continue
        total += min(bound, float(timeout.seconds))
        counted += 1
    if total < STEP_BUDGET_WARN_SHARE * step_timeout_seconds:
        return None
    return (
        f"the learned timeouts of {counted} entries add up to {total:.0f}s, "
        f"{total / step_timeout_seconds:.0%} of the step timeout "
        f"({step_timeout_seconds:.0f}s); a slow run can be killed before it "
        "records anything — raise acceptance.stepTimeoutSeconds or split the ledger"
    )


def _invoke_content(
    manifest: dict[str, Any],
    config: LedgerConfig,
    content: str,
    fixtures: dict[str, str] | None = None,
    input_filename: str = "input.md",
    extra_args: list[str] | None = None,
) -> InvokeResult:
    assert config.tmp_root is not None, (
        "LedgerConfig.tmp_root must be set before invoking"
    )
    workdir, input_path = materialize_workdir(
        config.tmp_root, fixtures, input_filename, content
    )
    timeout_seconds = (
        config.entry_timeout_seconds
        if config.entry_timeout_seconds is not None
        else invoke_timeout_seconds(manifest)
    )
    port_env = manifest_port_env(manifest)
    invoke_started = time.monotonic()
    result = run_invoke(
        manifest["invoke"],
        workdir,
        input_path,
        workdir,
        timeout_seconds,
        config.allowed_invoke_binaries,
        config.base_dir,
        extra_args,
        port_env,
    )
    if config.invoke_seconds is not None:
        config.invoke_seconds.append(time.monotonic() - invoke_started)
    check_server_started(result, port_env)
    return result


# --------------------------------------------------------------------------
# Observers
# --------------------------------------------------------------------------


def apply_observer(
    observer: dict[str, Any], result: InvokeResult
) -> list[dict[str, Any]]:
    obs_type = observer.get("type")
    if obs_type == "regex_lines":
        pattern = re.compile(observer["pattern"], re.MULTILINE)
        return [match.groupdict() for match in pattern.finditer(result.stdout)]
    if obs_type == "exit_code":
        # An exit code is an observation only when the process actually said
        # something. A timed-out run has no exit code — ``None == None`` let
        # an always-hanging tool pass equivalence_class — and a clean, silent
        # exit 0 is exactly what ``true`` produces: six kinds read CONSISTENT
        # on ``[{"exit_code": 0}] == [{"exit_code": 0}]`` against a tool that
        # never looked at its input. Both are silence, not evidence.
        if result.timed_out:
            return []
        if result.exit_code == 0 and not (
            result.stdout.strip() or result.stderr.strip()
        ):
            return []
        return [{"exit_code": result.exit_code}]
    raise LedgerError(f"unknown observer type {obs_type!r}")


def filter_and_project(
    manifest: dict[str, Any], entry: dict[str, Any], result: InvokeResult
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    """Project an invocation's observations for comparison.

    Returns ``(projected, raw)``, where ``raw`` is POST-filter. Barren
    checks must count post-filter evidence, not pre-filter: a relation
    ``filter``ed on one rule that only ever sees a *different* rule fire is
    exactly as uninformed as one that sees nothing at all — counting the
    unrelated noise as "observed something" would let a misconfigured or
    permanently-silent filter read as CONSISTENT forever, which is the same
    failure class this module exists to catch.
    """
    observer_name = entry.get("observe")
    if observer_name is None:
        raise LedgerError(f"entry {entry.get('id')!r} is missing 'observe'")
    observers = manifest.get("observers", {})
    if observer_name not in observers:
        raise LedgerError(
            f"entry {entry.get('id')!r} observe={observer_name!r} is not defined in manifest observers"
        )
    observer = observers[observer_name]
    unfiltered = apply_observer(observer, result)
    key_fields = observer.get("key") or sorted(
        {k for obs in unfiltered for k in obs.keys()}
    )
    filter_value = entry.get("filter")
    raw = unfiltered
    if filter_value is not None:
        if len(key_fields) != 1:
            raise LedgerError(
                f"entry {entry.get('id')!r}: 'filter' requires a single-field observer key"
            )
        raw = [obs for obs in unfiltered if obs.get(key_fields[0]) == filter_value]
    projected = sorted(tuple(obs.get(field) for field in key_fields) for obs in raw)
    return projected, raw


def _text_observed_count(text: str) -> int:
    """Non-empty output is one observation; empty output is silence, not evidence.

    Used by the kinds that compare raw stdout text directly (idempotence,
    round_trip) or that check process-level signals (no_crash) rather than
    going through ``filter_and_project`` — they need the same honesty rule
    without an ``observe`` entry to lean on.
    """
    return 1 if text.strip() else 0


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    entry_id: str
    kind: str
    tier: str
    verdict: str  # "CONSISTENT" | "INCONSISTENT" | "OBSERVED" | "BARREN" | "ERROR"
    detail: str


def _consistent(entry: dict[str, Any], detail: str) -> Outcome:
    return Outcome(
        entry["id"], entry["kind"], entry.get("tier", "advisory"), "CONSISTENT", detail
    )


def _inconsistent(entry: dict[str, Any], detail: str) -> Outcome:
    return Outcome(
        entry["id"],
        entry["kind"],
        entry.get("tier", "advisory"),
        "INCONSISTENT",
        detail,
    )


def _barren(entry: dict[str, Any], detail: str) -> Outcome:
    return Outcome(
        entry["id"], entry["kind"], entry.get("tier", "advisory"), "BARREN", detail
    )


# --------------------------------------------------------------------------
# The twelve relation kinds — every one must earn a CONSISTENT verdict with
# a real observation. Zero observations is BARREN, never CONSISTENT.
# --------------------------------------------------------------------------


def _kind_equivalence_class(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    """N same-class inputs must yield identical filtered observations.

    A correct tool emits *nothing* for every member of a well-formed class
    (no MDL02 on any non-empty link text), so agreement-on-silence is the
    expected shape of a pass — and also the exact shape of a tool that never
    ran. The optional ``control`` member breaks that tie the way
    ``masked_region_exemption``'s ``control_content`` does: it is one input
    the spec puts OUTSIDE the class (an empty link text) and must yield at
    least one filtered observation, proving the observer fires before its
    silence on the members is trusted. Without a control the old rule
    stands: zero observations is BARREN. Divergence among the members is
    evidence in its own right and is reported whether or not a control
    would have fired — the control only licenses the CONSISTENT verdict.
    """
    template = entry["template"]
    members = entry["members"]
    if len(members) < 2:
        return _barren(entry, "fewer than two members — nothing to compare"), 0
    control = entry.get("control")
    if control is not None and not isinstance(control, dict):
        raise LedgerError(
            f"entry {entry.get('id')!r}: 'control' must be a member object, "
            f"got {type(control).__name__}"
        )
    fixtures = entry.get("fixtures")
    total_obs = 0
    projections: list[tuple[dict[str, Any], list[tuple[Any, ...]]]] = []
    for member in members:
        content = template.format(**member)
        result = _invoke_content(manifest, config, content, fixtures=fixtures)
        projected, raw = filter_and_project(manifest, entry, result)
        total_obs += len(raw)
        projections.append((member, projected))
    baseline = projections[0][1]
    for member, projected in projections[1:]:
        if projected != baseline:
            return _inconsistent(
                entry,
                f"member {member!r} -> {projected!r} diverges from baseline {baseline!r}",
            ), total_obs
    if control is None:
        if total_obs == 0:
            return (
                _barren(
                    entry,
                    "no member produced any observation — cannot assert equivalence",
                ),
                0,
            )
        return _consistent(entry, f"all {len(members)} members agree"), total_obs
    control_result = _invoke_content(
        manifest, config, template.format(**control), fixtures=fixtures
    )
    control_projected, control_raw = filter_and_project(manifest, entry, control_result)
    total_obs += len(control_raw)
    if not control_projected:
        return (
            _barren(
                entry,
                "control member produced no matching observation — cannot confirm "
                "the observer fires at all, so the members' agreement is silence",
            ),
            total_obs,
        )
    return (
        _consistent(
            entry,
            f"all {len(members)} members agree; control member {control!r} "
            f"confirms the observer fires",
        ),
        total_obs,
    )


def _kind_permutation_invariance(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    contents = entry["contents"]
    if len(contents) < 2:
        return _barren(entry, "fewer than two contents — nothing to compare"), 0
    fixtures = entry.get("fixtures")
    total_obs = 0
    projections: list[list[tuple[Any, ...]]] = []
    for content in contents:
        result = _invoke_content(manifest, config, content, fixtures=fixtures)
        projected, raw = filter_and_project(manifest, entry, result)
        total_obs += len(raw)
        projections.append(projected)
    if total_obs == 0:
        return (
            _barren(
                entry, "no ordering produced any observation — cannot assert invariance"
            ),
            0,
        )
    baseline = projections[0]
    for projected in projections[1:]:
        if projected != baseline:
            return _inconsistent(
                entry, f"reordering changed observations: {projected!r} vs {baseline!r}"
            ), total_obs
    return _consistent(entry, "order-independent"), total_obs


def _kind_monotonicity(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    base_result = _invoke_content(
        manifest, config, entry["base_content"], fixtures=fixtures
    )
    aug_result = _invoke_content(
        manifest, config, entry["augmented_content"], fixtures=fixtures
    )
    base_projected, base_raw = filter_and_project(manifest, entry, base_result)
    aug_projected, aug_raw = filter_and_project(manifest, entry, aug_result)
    total_obs = len(base_raw) + len(aug_raw)
    if total_obs == 0:
        return (
            _barren(
                entry, "neither content produced any observation — nothing to check"
            ),
            0,
        )
    if base_projected != aug_projected:
        return _inconsistent(
            entry,
            f"neutral addition changed observations: {base_projected!r} -> {aug_projected!r}",
        ), total_obs
    return _consistent(entry, "neutral addition had no effect"), total_obs


def _kind_null_effect(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    content = entry["content"]
    base_result = _invoke_content(manifest, config, content, fixtures=fixtures)
    flagged_result = _invoke_content(
        manifest,
        config,
        content,
        fixtures=fixtures,
        extra_args=entry.get("noop_extra_args"),
    )
    base_projected, base_raw = filter_and_project(manifest, entry, base_result)
    flagged_projected, flagged_raw = filter_and_project(manifest, entry, flagged_result)
    total_obs = len(base_raw) + len(flagged_raw)
    if total_obs == 0:
        return (
            _barren(
                entry, "neither invocation produced any observation — nothing to check"
            ),
            0,
        )
    if base_projected != flagged_projected:
        return _inconsistent(
            entry,
            f"declared no-op flag changed observations: {base_projected!r} -> {flagged_projected!r}",
        ), total_obs
    return _consistent(entry, "no-op flag had no effect"), total_obs


def _kind_masked_region_exemption(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    """A masked (e.g. fenced) region should be exempt from the observer's filter.

    Exempt content is *supposed* to yield zero matching observations, so a
    silent, broken tool would look identical to a correctly-masking one.
    The fix is a control: ``control_content`` is the same offending content
    WITHOUT the mask, and must itself trigger the filtered observation —
    proving the check actually fires before trusting its absence inside the
    mask. This is the mutant-canary idea applied to a single entry.
    """
    fixtures = entry.get("fixtures")
    control_content = entry.get("control_content")
    if control_content is None:
        raise LedgerError(f"entry {entry.get('id')!r} is missing 'control_content'")
    control_result = _invoke_content(
        manifest, config, control_content, fixtures=fixtures
    )
    control_projected, control_raw = filter_and_project(manifest, entry, control_result)
    if not control_projected:
        return (
            _barren(
                entry,
                "control content (same issue, unmasked) produced no matching "
                "observation — cannot confirm the check fires at all",
            ),
            len(control_raw),
        )
    result = _invoke_content(manifest, config, entry["content"], fixtures=fixtures)
    projected, raw = filter_and_project(manifest, entry, result)
    total_obs = len(control_raw) + len(raw)
    if projected:
        return _inconsistent(
            entry, f"masked region produced observations: {projected!r}"
        ), total_obs
    return (
        _consistent(
            entry, "masked region exempt; control confirms the check fires outside it"
        ),
        total_obs,
    )


def _kind_subset_consistency(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    full_result = _invoke_content(
        manifest, config, entry["full_content"], fixtures=fixtures
    )
    subset_result = _invoke_content(
        manifest, config, entry["subset_content"], fixtures=fixtures
    )
    full_projected, full_raw = filter_and_project(manifest, entry, full_result)
    subset_projected, subset_raw = filter_and_project(manifest, entry, subset_result)
    total_obs = len(full_raw) + len(subset_raw)
    if total_obs == 0:
        return (
            _barren(
                entry, "neither the full nor the subset run produced any observation"
            ),
            0,
        )
    extra = set(subset_projected) - set(full_projected)
    if extra:
        return _inconsistent(
            entry,
            f"subset produced observations absent from the full run: {sorted(extra)!r}",
        ), total_obs
    return _consistent(
        entry, "subset observations contained in the full run"
    ), total_obs


def _kind_duplicate_invariance(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    factor = entry.get("factor", 2)
    base_result = _invoke_content(
        manifest, config, entry["base_content"], fixtures=fixtures
    )
    dup_result = _invoke_content(
        manifest, config, entry["duplicated_content"], fixtures=fixtures
    )
    _base_projected, base_raw = filter_and_project(manifest, entry, base_result)
    _dup_projected, dup_raw = filter_and_project(manifest, entry, dup_result)
    total_obs = len(base_raw) + len(dup_raw)
    if len(base_raw) == 0:
        # Zero equals twice zero: a duplication factor can't be confirmed
        # against a base run that observed nothing to duplicate.
        return (
            _barren(
                entry,
                "base content produced no observations — cannot confirm duplication scaled anything",
            ),
            total_obs,
        )
    expected = factor * len(base_raw)
    if len(dup_raw) != expected:
        return _inconsistent(
            entry, f"expected {expected} observations ({factor}x), got {len(dup_raw)}"
        ), total_obs
    return _consistent(
        entry, f"observation count scaled by {factor}x as declared"
    ), total_obs


def _kind_determinism(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    content = entry["content"]
    result1 = _invoke_content(manifest, config, content, fixtures=fixtures)
    result2 = _invoke_content(manifest, config, content, fixtures=fixtures)
    projected1, raw1 = filter_and_project(manifest, entry, result1)
    projected2, raw2 = filter_and_project(manifest, entry, result2)
    total_obs = len(raw1) + len(raw2)
    if total_obs == 0:
        return (
            _barren(
                entry,
                "neither run produced any observation — determinism of nothing is not a claim",
            ),
            0,
        )
    if projected1 != projected2:
        return _inconsistent(
            entry,
            f"identical rerun produced different observations: {projected1!r} vs {projected2!r}",
        ), total_obs
    return _consistent(entry, "rerun matched"), total_obs


def _kind_idempotence(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    result1 = _invoke_content(manifest, config, entry["content"], fixtures=fixtures)
    text1 = result1.stdout
    obs1 = _text_observed_count(text1)
    if obs1 == 0:
        return (
            _barren(
                entry,
                "f(x) produced no output — no fixed point to confirm, only silence",
            ),
            0,
        )
    result2 = _invoke_content(manifest, config, text1, fixtures=fixtures)
    text2 = result2.stdout
    total_obs = obs1 + _text_observed_count(text2)
    if text1.strip() != text2.strip():
        return _inconsistent(entry, "f(x) != f(f(x))"), total_obs
    return _consistent(entry, "fixed point reached after one application"), total_obs


def _kind_round_trip(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    content = entry["content"]
    encoded = _invoke_content(
        manifest,
        config,
        content,
        fixtures=fixtures,
        extra_args=entry.get("encode_extra_args"),
    ).stdout
    obs_encoded = _text_observed_count(encoded)
    if obs_encoded == 0:
        return (
            _barren(entry, "encode(x) produced no output — no round trip to confirm"),
            0,
        )
    decoded = _invoke_content(
        manifest,
        config,
        encoded,
        fixtures=fixtures,
        extra_args=entry.get("decode_extra_args"),
    ).stdout
    total_obs = obs_encoded + _text_observed_count(decoded)
    if decoded.strip() != content.strip():
        return _inconsistent(entry, "decode(encode(x)) != x"), total_obs
    return _consistent(entry, "round trip preserved content"), total_obs


def _kind_no_crash(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    allowed = set(entry.get("allowed_exit_codes", [0, 1]))
    contents = entry["contents"]
    total_obs = 0
    for content in contents:
        result = _invoke_content(manifest, config, content, fixtures=fixtures)
        if result.timed_out:
            return _inconsistent(
                entry, f"invocation timed out on content {content[:40]!r}"
            ), total_obs
        if result.exit_code not in allowed:
            return _inconsistent(
                entry,
                f"exit code {result.exit_code} not in allowed {sorted(allowed)!r} for {content[:40]!r}",
            ), total_obs
        if "Traceback" in result.stderr:
            return _inconsistent(
                entry, f"traceback on stderr for content {content[:40]!r}"
            ), total_obs
        total_obs += _text_observed_count(result.stdout)
    if total_obs == 0:
        return (
            _barren(
                entry,
                f"all {len(contents)} inputs exited cleanly but produced no output — "
                "cannot tell processing from a silent no-op",
            ),
            0,
        )
    return _consistent(entry, f"all {len(contents)} inputs exited cleanly"), total_obs


def _kind_contract_shape(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    fixtures = entry.get("fixtures")
    required_fields = entry["required_fields"]
    result = _invoke_content(manifest, config, entry["content"], fixtures=fixtures)
    _projected, raw = filter_and_project(manifest, entry, result)
    if not raw:
        return _barren(entry, "no observations to check for required fields"), 0
    for observation in raw:
        # Membership in (None, ""), not falsiness: an exit_code observer's 0
        # is a present, well-formed value — ``not 0`` read it as missing, so
        # ``required_fields: ["exit_code"]`` was INCONSISTENT on every clean exit.
        missing = [
            field for field in required_fields if observation.get(field) in (None, "")
        ]
        if missing:
            return _inconsistent(
                entry,
                f"observation missing required fields {missing!r}: {observation!r}",
            ), len(raw)
    return _consistent(entry, f"{len(raw)} observations well-formed"), len(raw)


_KIND_HANDLERS: dict[
    str, Callable[[dict[str, Any], dict[str, Any], LedgerConfig], tuple[Outcome, int]]
] = {
    "equivalence_class": _kind_equivalence_class,
    "permutation_invariance": _kind_permutation_invariance,
    "monotonicity": _kind_monotonicity,
    "null_effect": _kind_null_effect,
    "masked_region_exemption": _kind_masked_region_exemption,
    "subset_consistency": _kind_subset_consistency,
    "duplicate_invariance": _kind_duplicate_invariance,
    "determinism": _kind_determinism,
    "idempotence": _kind_idempotence,
    "round_trip": _kind_round_trip,
    "no_crash": _kind_no_crash,
    "contract_shape": _kind_contract_shape,
}


def run_relation_entry(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    kind = entry.get("kind")
    handler = _KIND_HANDLERS.get(kind)
    if handler is None:
        raise LedgerError(f"entry {entry.get('id')!r} has unknown kind {kind!r}")
    return handler(manifest, entry, config)


def run_case_entry(
    manifest: dict[str, Any], entry: dict[str, Any], config: LedgerConfig
) -> tuple[Outcome, int]:
    """A frozen, spec-derived case with a known-correct expectation.

    Unlike the relation kinds, a case's author committed to a specific
    expected observation set — including, sometimes, an explicitly empty
    one. That is a real, falsifiable claim about one input, not two
    invocations trivially agreeing because neither produced anything, so
    cases are not subject to the BARREN rule.
    """
    fixtures = entry.get("fixtures")
    result = _invoke_content(manifest, config, entry["content"], fixtures=fixtures)
    projected, raw = filter_and_project(manifest, entry, result)
    expected = entry.get("expected")
    if expected is None:
        return Outcome(
            entry["id"],
            "case",
            entry.get("tier", "advisory"),
            "OBSERVED",
            f"observed {projected!r}",
        ), len(raw)
    expected_projected = sorted(tuple(row) for row in expected)
    if projected != expected_projected:
        return _inconsistent(
            entry, f"expected {expected_projected!r}, observed {projected!r}"
        ), len(raw)
    return _consistent(entry, "matched expected observations"), len(raw)


def _entry_display_id(entry: dict[str, Any]) -> str:
    return entry.get("id") or entry.get("_source", "?")


def _load_error_outcome(entry: dict[str, Any]) -> Outcome:
    return Outcome(
        _entry_display_id(entry),
        entry.get("kind", "unknown"),
        "advisory",
        "ERROR",
        f"failed to load: {entry['_load_error']}",
    )


def _run_entry_isolated(
    run_fn: Callable[
        [dict[str, Any], dict[str, Any], LedgerConfig], tuple[Outcome, int]
    ],
    manifest: dict[str, Any],
    entry: dict[str, Any],
    config: LedgerConfig,
) -> tuple[Outcome, int]:
    """Run one entry, converting any failure into an ERROR outcome for just that entry.

    A later increment puts this runner on the merge path. An entry that
    raises — a typo'd required field, an unknown kind, a broken invocation —
    must not abort every other entry in the same run; that is the same
    fail-closed trap that hit the deliverable judge.
    """
    try:
        return run_fn(manifest, entry, config)
    except Exception as exc:  # noqa: BLE001 - isolate externally-authored entries
        return (
            Outcome(
                _entry_display_id(entry),
                entry.get("kind", "unknown"),
                entry.get("tier", "advisory"),
                "ERROR",
                f"{type(exc).__name__}: {exc}",
            ),
            0,
        )


# --------------------------------------------------------------------------
# Run + report
# --------------------------------------------------------------------------


@dataclass
class RunReport:
    outcomes: list[Outcome]
    barren: bool
    started_at: float
    finished_at: float
    # Entry id -> how a required entry got there ("operator", "canary" or "unrecorded").
    promotions: dict[str, str] = dataclasses.field(default_factory=dict)
    # Entry id -> why its declared tier was not honoured.
    tier_warnings: dict[str, str] = dataclasses.field(default_factory=dict)
    # New regressions (passed on the previous recorded run, failed on this one)
    # that were re-run once in isolation. ``flakes``: entry id -> the first
    # run's account, for an entry whose re-run passed (its outcome is the
    # re-run's). ``confirmed``: ids whose re-run failed too.
    flakes: dict[str, str] = dataclasses.field(default_factory=dict)
    confirmed: list[str] = dataclasses.field(default_factory=list)
    # New regressions not re-run because the confirmation budget was spent;
    # their first INCONSISTENT verdict stands.
    unconfirmed: list[str] = dataclasses.field(default_factory=list)
    # Ids of the entries a browser test runner judges (``BROWSER_RUNNERS``).
    browser_ids: frozenset[str] = frozenset()
    # Set when the run says nothing about the product: the dependency install
    # failed (``environment_error``, no entry ran) or every browser entry that
    # passed last time failed at once while the rest still pass
    # (``environment_suspect``). Either one: no divergence, no goal credit.
    environment_error: str | None = None
    environment_suspect: str | None = None
    # Set instead of ``environment_suspect`` when the uniform browser failure
    # has now held for more than ``SUSPECT_MAX_CONSECUTIVE_RUNS`` runs in a
    # row: it is treated as product evidence again, so a real regression that
    # breaks every page opens its divergences instead of hiding forever.
    environment_suspect_expired: str | None = None
    # What ``ensure_dependencies`` did before the run (``InstallResult.as_dict``).
    install: dict[str, Any] | None = None
    # LEDGER-ADAPTIVE-ENTRY-TIMEOUT. Entry id -> its longest invocation's wall
    # time on the main pass (a flake's is left out: it is the failed first
    # run's); the timeout each entry ran under; passing entries whose
    # SMOOTHED duration (median of this run and its last two, see
    # ``_timing_findings``) broke from their rolling median (``perf_regression``;
    # empty on a run flagged environment_suspect) -- what feeds
    # ``perf_regressions.json`` / the backlog signal; entries that used more
    # than NEAR_LIMIT_SHARE of their timeout; and ``step_budget_warning``.
    entry_seconds: dict[str, float] = dataclasses.field(default_factory=dict)
    entry_timeouts: dict[str, EntryTimeout] = dataclasses.field(default_factory=dict)
    perf_regressions: dict[str, dict[str, Any]] = dataclasses.field(
        default_factory=dict
    )
    # Entry ids this run's OWN (unsmoothed) duration excluded from the
    # learned timeout -- always a superset of (or equal to) perf_regressions'
    # keys, since a run can be individually anomalous without yet moving a
    # 3-run median past the threshold. ``record_run`` writes this, not
    # ``perf_regressions``, as ``perf_regression_ids`` (read back by
    # ``read_entry_history`` to keep it out of ``learnable``).
    perf_excluded: list[str] = dataclasses.field(default_factory=list)
    near_limit: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    step_budget_warning: str | None = None
    # The per-entry frozen-median records this run changed (``_baseline_update``);
    # ``update_perf_findings`` merges them into ``perf_regressions.json``.
    perf_baselines: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    # LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES. A run that reached its step
    # budget (``timed_out`` "deadline") or was interrupted by the daemon's
    # SIGINT at the step timeout ("interrupted") still records what it
    # judged. ``unfinished``: the entries it never judged -- no verdict, so
    # they are in no id list and are never "failed"; ``stale_passing``: those
    # of them that passed on the previous trusted run, whose pass is carried
    # forward as STALE (``previous_passing_ids``), never as fresh evidence.
    unfinished: list[str] = dataclasses.field(default_factory=list)
    stale_passing: list[str] = dataclasses.field(default_factory=list)
    timed_out: str | None = None
    step_budget_seconds: float | None = None
    # LEDGER-AS-SHARDED-RUNNER-JOB: "local" (one serial run), "shards" (a
    # ``merge`` of shard results) or "ci:<run id>" (shard results fetched
    # from the product's CI run); and how many shards the run was split into.
    source: str = "local"
    shards: int | None = None

    @property
    def partial(self) -> bool:
        return bool(self.unfinished)

    @property
    def inconsistent(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.verdict == "INCONSISTENT"]

    @property
    def consistent(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.verdict == "CONSISTENT"]

    @property
    def barren_entries(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.verdict == "BARREN"]

    @property
    def errored(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.verdict == "ERROR"]


def _confirm_new_regressions(
    outcomes: list[Outcome],
    ran: list[tuple[Callable[..., tuple[Outcome, int]], dict[str, Any]] | None],
    manifest: dict[str, Any],
    config: LedgerConfig,
    previously_passing: frozenset[str],
    confirm_until: float | None = None,
    durations: Sequence[float] | None = None,
    timeouts: Sequence[int | None] | None = None,
    recorded_rerun: Callable[[str], Outcome | None] | None = None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Re-run each new regression once, alone, and keep what the re-run says.

    A new regression is an entry that passed on the previous recorded run and
    is INCONSISTENT on this one. A busy machine or a neighbour's server fails
    such an entry on code it passes minutes later, so the failure is checked
    before anything downstream opens a divergence for it: the entry runs again,
    one entry per invocation, in a fresh work directory with fresh ports, on the
    same checkout. Each is re-run at most once per run.

    * The re-run passes: the entry is a flake. Its outcome becomes the re-run's
      and its first account is returned in the flakes map.
    * The re-run fails: the regression is confirmed; the re-run's outcome stands.
    * The re-run cannot judge (ERROR or BARREN): the first failure is not
      confirmed, so the entry reads ERROR with both accounts, which opens no
      divergence and repairs none.

    An entry that did not pass on the previous run (never built, operator-
    promoted ahead of its code, or already failing) is not re-run.

    ``confirm_until`` is a ``time.monotonic()`` deadline: a re-run starts only
    when what it may need still fits before it. That is the entry's first-run
    time (``durations[index]``, 0 when unknown) but never less than one
    invocation's timeout — the entry's own (``timeouts[index]``,
    ``entry_timeout_seconds``), else the manifest's: a first run that failed
    fast says nothing about a re-run that hangs, and a re-run still going at
    the step timeout is killed with the whole step, before any record is
    written. An entry that does not fit is returned in the third list with its
    first INCONSISTENT verdict standing, as if never re-run; a shorter entry
    after it may still fit. The re-run runs under the same entry timeout.

    ``recorded_rerun`` (LEDGER-AS-SHARDED-RUNNER-JOB): ``merge`` replays the
    re-runs a shard already made instead of running anything. It maps an
    entry id to that re-run's outcome, or None when the shard did not re-run
    it (its own history did not call it a new regression, or its budget was
    spent) -- the entry is then unconfirmed, exactly as a re-run that did not
    fit the budget.
    """
    flakes: dict[str, str] = {}
    confirmed: list[str] = []
    unconfirmed: list[str] = []
    candidates = [
        index
        for index, outcome in enumerate(outcomes)
        if outcome.verdict == "INCONSISTENT"
        and outcome.entry_id in previously_passing
        and ran[index] is not None
    ]
    for index in candidates:
        first = outcomes[index]
        entry_timeout = timeouts[index] if timeouts is not None else None
        needs = max(
            durations[index] if durations is not None else 0.0,
            entry_timeout
            if entry_timeout is not None
            else invoke_timeout_seconds(manifest),
        )
        if confirm_until is not None and _monotonic() + needs >= confirm_until:
            unconfirmed.append(first.entry_id)
            continue
        if recorded_rerun is not None:
            replayed = recorded_rerun(first.entry_id)
            if replayed is None:
                unconfirmed.append(first.entry_id)
                continue
            rerun = replayed
        else:
            run_fn, entry = ran[index]  # type: ignore[misc]
            with tempfile.TemporaryDirectory(prefix="acceptance-confirm-") as tmp_root:
                rerun, _obs = _run_entry_isolated(
                    run_fn,
                    manifest,
                    entry,
                    dataclasses.replace(
                        config,
                        tmp_root=Path(tmp_root),
                        entry_timeout_seconds=entry_timeout,
                        invoke_seconds=None,
                    ),
                )
        if rerun.verdict == "CONSISTENT":
            flakes[first.entry_id] = first.detail
            outcomes[index] = rerun
        elif rerun.verdict == "INCONSISTENT":
            confirmed.append(first.entry_id)
            outcomes[index] = rerun
        else:
            outcomes[index] = Outcome(
                first.entry_id,
                first.kind,
                first.tier,
                "ERROR",
                f"failed ({first.detail}); the isolated re-run could not judge "
                f"({rerun.verdict}: {rerun.detail}), so the failure is unconfirmed",
            )
    return flakes, confirmed, unconfirmed


RunItem = tuple[Callable[..., tuple[Outcome, int]], dict[str, Any]]


@dataclass
class EntryRun:
    """One entry's main-pass result (LEDGER-AS-SHARDED-RUNNER-JOB).

    The unit a serial run, a shard and a ``merge`` share: ``_assemble_report``
    turns a list of these into the same ``RunReport`` whichever produced them.
    ``ordinal`` is the entry's place in the canonical order (relations, then
    cases, each in ``load_entries`` order), so a merged run lists its entries
    exactly as a serial run does. ``outcome`` is the MAIN-PASS verdict (before
    any confirmation re-run); ``rerun`` is a shard's isolated re-run, replayed
    by ``merge``. ``loaded`` is False for an entry that failed to load.
    """

    ordinal: int
    entry_id: str
    outcome: Outcome
    loaded: bool = True
    duration: float = 0.0
    timeout: EntryTimeout | None = None
    longest: float | None = None
    obs: int = 0
    content_hash: str | None = None
    browser: bool = False
    promotion: str | None = None
    tier_warning: str | None = None
    rerun: Outcome | None = None


def _ledger_items(
    config: LedgerConfig,
) -> tuple[dict[str, Any], list[tuple[int, RunItem]]]:
    """The manifest and every entry with its runner, in canonical order."""
    manifest = load_manifest(config.ledger_dir)
    relations = load_entries(config.ledger_dir, "relations")
    cases = load_entries(config.ledger_dir, "cases")
    if not relations and not cases:
        raise LedgerError(f"no relations or cases found under {config.ledger_dir}")
    items: list[tuple[int, RunItem]] = []
    for run_fn, entries in ((run_relation_entry, relations), (run_case_entry, cases)):
        for entry in entries:
            items.append((len(items), (run_fn, entry)))
    return manifest, items


def ledger_entry_ids(config: LedgerConfig) -> list[str]:
    """Every entry's id, in canonical order (a load-errored entry by its file)."""
    _manifest, items = _ledger_items(config)
    return [_entry_display_id(entry) for _, (_fn, entry) in items]


def _main_pass_monotonic() -> float:
    """The clock the main pass's deadline reads; a seam separate from
    ``_monotonic`` so a test can script one budget without consuming the other."""
    return time.monotonic()


def _entry_needs(timeout: EntryTimeout | None, manifest: Mapping[str, Any]) -> float:
    """What one entry may need of the budget at worst: its own timeout, else
    the manifest default (the confirmation re-run's fit check)."""
    if timeout is not None:
        return float(timeout.seconds)
    return float(invoke_timeout_seconds(dict(manifest)))


def _entry_expected_seconds(history: EntryHistory | None) -> float:
    """What one entry is expected to take: the p95 of its recorded passing
    durations, 0 when it has none (an unknown entry is tried; the step's
    SIGINT backstop records the run if it overruns)."""
    if history is None or not history.passing:
        return 0.0
    return _percentile(history.passing, LEARNED_TIMEOUT_PERCENTILE)


def _main_pass(
    config: LedgerConfig,
    manifest: dict[str, Any],
    items: Sequence[tuple[int, RunItem]],
    history: Mapping[str, EntryHistory],
    *,
    deadline: float | None = None,
    run_first: frozenset[str] = frozenset(),
) -> tuple[list[EntryRun], list[str], str | None]:
    """Run each item once. Returns (results, unfinished ids, timed_out).

    LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES: ``deadline`` is a
    ``_main_pass_monotonic`` instant. An entry whose expected duration (the p95
    of its passing history, ``_entry_expected_seconds``) no longer fits before
    it is not started and is returned as unfinished (a shorter one after it may
    still fit); ``timed_out`` is then "deadline". The first entry always
    starts, so even a budget too small for anything judges one entry per run
    and the rotation below still covers the ledger. A KeyboardInterrupt
    (the daemon's SIGINT at its step timeout, see ``cmd_run``) ends the pass:
    the entry it interrupted and every one not yet started are unfinished and
    ``timed_out`` is "interrupted". Either way the caller still records what
    was judged. Entries in ``run_first`` (the previous run's unfinished ones)
    run before the rest, so a ledger that keeps outgrowing its budget rotates
    instead of starving the same tail every time; results are returned in
    canonical order whatever order they ran in.
    """
    ordered = sorted(
        items,
        key=lambda item: (_entry_display_id(item[1][1]) not in run_first, item[0]),
    )
    results: list[EntryRun] = []
    unfinished: list[str] = []
    timed_out: str | None = None
    started_any = False
    with tempfile.TemporaryDirectory(prefix="acceptance-ledger-") as tmp_root:
        run_config = dataclasses.replace(config, tmp_root=Path(tmp_root))
        for position, (ordinal, (run_fn, entry)) in enumerate(ordered):
            entry_id = _entry_display_id(entry)
            if "_load_error" in entry:
                results.append(
                    EntryRun(
                        ordinal, entry_id, _load_error_outcome(entry), loaded=False
                    )
                )
                continue
            try:
                timeout: EntryTimeout | None = entry_timeout_seconds(
                    manifest,
                    entry,
                    history.get(entry_id),
                    config.max_entry_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - the entry's own run reports it
                timeout = None
            if (
                deadline is not None
                and started_any
                and _main_pass_monotonic()
                + _entry_expected_seconds(history.get(entry_id))
                >= deadline
            ):
                unfinished.append(entry_id)
                timed_out = "deadline"
                continue
            started_any = True
            invokes: list[float] = []
            entry_config = dataclasses.replace(
                run_config,
                entry_timeout_seconds=timeout.seconds if timeout else None,
                invoke_seconds=invokes,
            )
            entry_started = time.monotonic()
            try:
                outcome, obs_count = _run_entry_isolated(
                    run_fn, manifest, entry, entry_config
                )
            except KeyboardInterrupt:
                timed_out = "interrupted"
                for rest_ordinal, (_fn, rest) in ordered[position:]:
                    if "_load_error" in rest:
                        results.append(
                            EntryRun(
                                rest_ordinal,
                                _entry_display_id(rest),
                                _load_error_outcome(rest),
                                loaded=False,
                            )
                        )
                    else:
                        unfinished.append(_entry_display_id(rest))
                break
            results.append(
                EntryRun(
                    ordinal,
                    entry_id,
                    outcome,
                    duration=time.monotonic() - entry_started,
                    timeout=timeout,
                    longest=max(invokes) if invokes else None,
                    obs=obs_count,
                )
            )
    # An entry left unfinished may have a load-errored twin under the same id;
    # never report one id as both judged and unfinished.
    judged = {r.entry_id for r in results}
    unfinished = [i for i in dict.fromkeys(unfinished) if i not in judged]
    results.sort(key=lambda r: r.ordinal)
    return results, unfinished, timed_out


def _annotate_entry_runs(
    results: Sequence[EntryRun],
    items: Sequence[tuple[int, RunItem]],
    manifest: Mapping[str, Any],
    base_dir: Path,
) -> None:
    """Fill each result's content hash, browser flag, promotion and tier warning."""
    by_ordinal = {ordinal: entry for ordinal, (_fn, entry) in items}
    for result in results:
        entry = by_ordinal.get(result.ordinal, {})
        result.browser = _is_browser_entry(entry)
        if isinstance(entry.get("_promotion"), str):
            result.promotion = entry["_promotion"]
        if isinstance(entry.get("_tier_warning"), str):
            result.tier_warning = entry["_tier_warning"]
        if result.loaded:
            result.content_hash = _entry_content_hash(entry, manifest, base_dir)


def _assemble_report(
    results: Sequence[EntryRun],
    *,
    manifest: Mapping[str, Any],
    config: LedgerConfig,
    previously_passing: frozenset[str] | None,
    suspect_streak: int,
    confirm_until: float | None,
    history: Mapping[str, EntryHistory],
    baselines: Mapping[str, Mapping[str, Any]],
    started_at: float,
    unfinished: Sequence[str] = (),
    timed_out: str | None = None,
    all_browser_ids: frozenset[str] | None = None,
    rerun_items: Sequence[RunItem | None] | None = None,
    replay: bool = False,
) -> RunReport:
    """Everything after the main pass, shared by a serial run and ``merge``.

    Suspect classification, the confirmation re-runs (run for real when
    ``rerun_items`` is given, replayed from each result's ``rerun`` when
    ``replay``), the timing guardrails and the report. A merge of shard
    results therefore reads its outcomes exactly as a serial run over the same
    entries and the same history would.
    """
    outcomes = [r.outcome for r in results]
    ran: list[Any] = (
        list(rerun_items)
        if rerun_items is not None
        else [True if r.loaded else None for r in results]
    )
    browser_ids = (
        all_browser_ids
        if all_browser_ids is not None
        else frozenset(r.entry_id for r in results if r.browser)
    )
    suspect = classify_environment_suspect(
        outcomes, browser_ids, previously_passing or frozenset()
    )
    expired: str | None = None
    if suspect and suspect_streak >= SUSPECT_MAX_CONSECUTIVE_RUNS:
        # The same shape has now held for SUSPECT_MAX_CONSECUTIVE_RUNS runs
        # before this one (each of which reinstalled on a lockfile change): a
        # persistent failure is evidence, product or environment, and must
        # reach the register rather than be suppressed indefinitely.
        expired = (
            f"{suspect} — but {suspect_streak} runs in a row before this one "
            "were flagged the same way, so this run is product evidence again"
        )
        suspect = None
    reruns = {r.entry_id: r.rerun for r in results if r.rerun is not None}
    # A suspect run's browser failures are the environment's, not regressions:
    # re-running each one alone would spend the confirmation budget on the
    # same broken environment.
    flakes, confirmed, unconfirmed = _confirm_new_regressions(
        outcomes,
        ran,
        dict(manifest),
        config,
        frozenset() if suspect else (previously_passing or frozenset()),
        confirm_until,
        [r.duration for r in results],
        [r.timeout.seconds if r.timeout else None for r in results],
        recorded_rerun=reruns.get if replay else None,
    )
    timing = _timing_findings(
        outcomes,
        [r.timeout for r in results],
        [r.longest for r in results],
        history,
        flakes,
        config.step_timeout_seconds,
        suspect is not None,
        baselines,
        {r.entry_id: r.content_hash for r in results if r.loaded},
    )
    promotions = {r.entry_id: r.promotion for r in results if r.promotion}
    tier_warnings = {r.entry_id: r.tier_warning for r in results if r.tier_warning}
    stale = sorted(set(unfinished) & set(previously_passing or ()))
    return RunReport(
        outcomes=outcomes,
        barren=sum(r.obs for r in results) == 0,
        started_at=started_at,
        finished_at=time.time(),
        promotions=promotions,
        tier_warnings=tier_warnings,
        flakes=flakes,
        confirmed=confirmed,
        unconfirmed=unconfirmed,
        browser_ids=browser_ids,
        environment_suspect=suspect,
        environment_suspect_expired=expired,
        unfinished=list(unfinished),
        stale_passing=stale,
        timed_out=timed_out,
        **timing,
    )


def run_ledger(
    config: LedgerConfig,
    previously_passing: frozenset[str] | None = None,
    confirm_until: float | None = None,
    suspect_streak: int = 0,
    *,
    deadline: float | None = None,
    run_first: frozenset[str] = frozenset(),
) -> RunReport:
    """Run every entry once; then re-run the new regressions once each.

    ``previously_passing`` is the passing ids of the previous recorded run
    (``cmd_run`` reads it from ``runs.jsonl``). None or empty re-runs nothing.
    ``confirm_until`` is the ``time.monotonic()`` deadline after which no
    re-run starts (``_confirm_new_regressions``); None is no deadline.
    ``suspect_streak`` is how many runs in a row before this one were flagged
    ``environment_suspect`` (``environment_suspect_streak``); at
    ``SUSPECT_MAX_CONSECUTIVE_RUNS`` the flag expires for this run.
    ``deadline`` and ``run_first``: see ``_main_pass`` (a partial run).
    """
    manifest, items = _ledger_items(config)
    started_at = time.time()
    history = read_entry_history(config.state_dir / "runs.jsonl")
    baselines = read_perf_baselines(config.state_dir / PERF_FINDINGS_FILENAME)
    results, unfinished, timed_out = _main_pass(
        config, manifest, items, history, deadline=deadline, run_first=run_first
    )
    _annotate_entry_runs(results, items, manifest, config.base_dir)
    by_ordinal = dict(items)
    all_browser = frozenset(
        _entry_display_id(entry)
        for _, (_fn, entry) in items
        if _is_browser_entry(entry)
    )
    return _assemble_report(
        results,
        manifest=manifest,
        config=config,
        previously_passing=previously_passing,
        suspect_streak=suspect_streak,
        # An interrupted run re-runs nothing: the daemon is about to kill it.
        confirm_until=(
            _monotonic() - 1 if timed_out == "interrupted" else confirm_until
        ),
        history=history,
        baselines=baselines,
        started_at=started_at,
        unfinished=unfinished,
        timed_out=timed_out,
        all_browser_ids=all_browser,
        rerun_items=[by_ordinal[r.ordinal] if r.loaded else None for r in results],
    )


def _timing_findings(
    outcomes: Sequence[Outcome],
    timeouts: Sequence[EntryTimeout | None],
    longest: Sequence[float | None],
    history: Mapping[str, EntryHistory],
    flakes: Mapping[str, str],
    step_timeout_seconds: float,
    suspect: bool,
    baselines: Mapping[str, Mapping[str, Any]] | None = None,
    content_hashes: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The run's per-entry durations, timeouts and the three timing guardrails.

    A perf regression is judged only on an entry that passed on the main pass
    (a flake's first run failed) and never on a run flagged environment_suspect,
    whose durations are the environment's. ``baselines`` are the frozen
    per-entry medians (``read_perf_baselines``); ``perf_baselines`` in the
    result holds only the records this run changed (none on a suspect run).
    ``content_hashes`` are each entry's ``_entry_content_hash`` (None: no
    content-based reset, only a declared-timeout change can reset).

    Two distinct judgements run on every passing entry, against the same
    reference and never absorbing a flagged duration into the baseline's own
    sample collection:

    * ``single`` -- this run's raw duration alone. It alone decides what is
      excluded from the learned timeout (``_baseline_update``'s ``seconds``
      argument): a real step change must never be silently learned away, not
      even for the one run it takes a smoothed median to confirm.
    * ``reportable`` -- the median of this run's duration and up to its last
      two (``PERF_REGRESSION_WINDOW``), from the SAME passing-duration history
      the learned timeout reads. This is what becomes ``perf_regressions``
      (and so ``perf_regressions.json`` / the backlog signal): a single noisy
      run on a loaded runner moves the median by at most a third, so it takes
      at least two slow runs out of three to open or refresh a finding.
    """
    entry_seconds: dict[str, float] = {}
    entry_timeouts: dict[str, EntryTimeout] = {}
    perf: dict[str, dict[str, Any]] = {}
    excluded: set[str] = set()
    near: dict[str, dict[str, Any]] = {}
    changed: dict[str, dict[str, Any]] = {}
    baselines = baselines or {}
    content_hashes = content_hashes or {}
    now = time.time()
    for outcome, timeout, seconds in zip(outcomes, timeouts, longest):
        entry_id = outcome.entry_id
        if timeout is not None:
            entry_timeouts[entry_id] = timeout
        if seconds is None or entry_id in flakes:
            continue
        entry_seconds[entry_id] = round(seconds, 3)
        if timeout is not None and seconds > NEAR_LIMIT_SHARE * timeout.seconds:
            near[entry_id] = {
                "seconds": round(seconds, 3),
                "timeout": timeout.seconds,
                "source": timeout.source,
            }
        if outcome.verdict == "CONSISTENT" and not suspect:
            declared = timeout.declared if timeout is not None else None
            content_hash = content_hashes.get(entry_id)
            record = baselines.get(entry_id)
            baseline, rolling = _baseline_reference(record, declared, content_hash)
            entry_history = history.get(entry_id)
            epoch = record.get("epoch_start") if isinstance(record, Mapping) else None
            if entry_history is not None and _positive_seconds(epoch) is not None:
                entry_history = entry_history.since(float(epoch))
            single = perf_regression(seconds, entry_history, baseline, rolling)
            recent = (
                (*entry_history.passing[-(PERF_REGRESSION_WINDOW - 1) :], seconds)
                if entry_history is not None
                else (seconds,)
            )
            smoothed_seconds = float(statistics.median(recent))
            reportable = perf_regression(
                smoothed_seconds, entry_history, baseline, rolling
            )
            if reportable is not None:
                reportable = {
                    **reportable,
                    "seconds": round(smoothed_seconds, 3),
                    "latest_seconds": round(seconds, 3),
                    "window": len(recent),
                }
                if timeout is not None:
                    reportable["timeout"] = timeout.seconds
                perf[entry_id] = reportable
            if single is not None:
                excluded.add(entry_id)
            if timeout is not None:
                updated = _baseline_update(
                    record, declared, content_hash, None if single else seconds, now
                )
                if updated is not None:
                    changed[entry_id] = updated
    return {
        "perf_baselines": changed,
        "entry_seconds": entry_seconds,
        "entry_timeouts": entry_timeouts,
        "perf_regressions": perf,
        "perf_excluded": sorted(excluded),
        "near_limit": near,
        "step_budget_warning": step_budget_warning(
            entry_timeouts, step_timeout_seconds
        ),
    }


# --------------------------------------------------------------------------
# Environment guard (LEDGER-REINSTALLS-ON-LOCKFILE-CHANGE) — a run is evidence
# about the product only when the product's dependencies are the ones its
# lockfile names. On 2026-10-03 webapp's lockfile gained two packages, the
# ledger kept running against a node_modules from a week earlier, Vite could
# not resolve the new imports, and 73 browser entries timed out: a broken
# checkout read as 73 product regressions, opened divergences and decided a
# goal. Two guards: reinstall when the lockfile changed (``ensure_dependencies``),
# and flag a run whose browser entries all fell over at once
# (``classify_environment_suspect``).
# --------------------------------------------------------------------------

INSTALL_STATE_FILENAME = "install_state.json"
INSTALL_LOCK_FILENAME = "install.lock"
# ``run``'s exit code when the run is flagged ``environment_error`` or
# ``environment_suspect``: the entries stay advisory, but a run that judged
# nothing must not read as a clean one in the daemon's log.
ENVIRONMENT_EXIT_CODE = 3
# npm ci on a cold cache for a mid-sized web app is one to three minutes; the
# install counts against the daemon's step timeout like the run itself.
INSTALL_TIMEOUT_SECONDS = 300.0
# First match wins; a project with none of these has nothing to install.
LOCKFILE_INSTALLERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("package-lock.json", ("npm", "ci")),
    ("pnpm-lock.yaml", ("pnpm", "install", "--frozen-lockfile")),
    ("yarn.lock", ("yarn", "install", "--frozen-lockfile")),
)
# Bundler caches under node_modules that outlive an install and can serve
# pre-bundled dependencies from the old lockfile.
BUNDLER_CACHE_DIRS: tuple[str, ...] = (".vite",)
_INSTALL_OUTPUT_TAIL_CHARS = 2000

# An entry is a browser entry when its ``runner`` is one of these.
BROWSER_RUNNERS = frozenset(
    {"playwright", "cypress", "puppeteer", "webdriverio", "selenium"}
)
# The uniform-failure guard fires only when at least this many browser entries
# passed on the previous trusted run, ...
SUSPECT_MIN_BROWSER_ENTRIES = 5
# ... at least this share of them fail now, ...
SUSPECT_BROWSER_FAIL_SHARE = 0.9
# ... and no more than this share of the previously passing non-browser
# entries fail now (at least one such entry must exist and still pass).
SUSPECT_MAX_OTHER_FAIL_SHARE = 0.1
# A suspect flag holds for at most this many runs in a row. The next run with
# the same shape is NOT flagged (``environment_suspect_expired``): a genuine
# regression that breaks every page (an app crash on main) is exactly that
# shape, and it must open its divergences eventually rather than never.
SUSPECT_MAX_CONSECUTIVE_RUNS = 2


def _is_browser_entry(entry: Mapping[str, Any]) -> bool:
    runner = entry.get("runner")
    return isinstance(runner, str) and runner.strip().lower() in BROWSER_RUNNERS


def classify_environment_suspect(
    outcomes: Sequence[Outcome],
    browser_ids: frozenset[str],
    previously_passing: frozenset[str],
) -> str | None:
    """Why this run looks like a broken environment, or None.

    Conservative on purpose — a product change CAN break every page. The run
    is suspect only when ALL of these hold:

    * at least ``SUSPECT_MIN_BROWSER_ENTRIES`` browser entries passed on the
      previous trusted run (``previously_passing``);
    * at least ``SUSPECT_BROWSER_FAIL_SHARE`` of those do not pass now;
    * at least one previously passing non-browser entry ran, and at most
      ``SUSPECT_MAX_OTHER_FAIL_SHARE`` of those do not pass now.

    The per-entry account cannot carry an error class (a browser entry reports
    ``expected PASS, observed FAIL`` whatever went wrong), so the shape of the
    failure is the evidence: the browser layer fell over as a block while the
    code under it still passes. The 2026-10-03 webapp run that motivated this
    (41 of 41 browser entries failing, 50 of 50 vitest entries passing) fires;
    the partial run before it (36 of 76) does not — the lockfile reinstall is
    what covers that one.
    """
    by_id = {o.entry_id: o.verdict for o in outcomes}
    was_browser = [i for i in browser_ids if i in previously_passing and i in by_id]
    if len(was_browser) < SUSPECT_MIN_BROWSER_ENTRIES:
        return None
    browser_failed = [i for i in was_browser if by_id[i] != "CONSISTENT"]
    if len(browser_failed) < SUSPECT_BROWSER_FAIL_SHARE * len(was_browser):
        return None
    was_other = [i for i in previously_passing if i not in browser_ids and i in by_id]
    if not was_other:
        return None
    other_failed = [i for i in was_other if by_id[i] != "CONSISTENT"]
    if len(other_failed) > SUSPECT_MAX_OTHER_FAIL_SHARE * len(was_other):
        return None
    return (
        f"{len(browser_failed)} of {len(was_browser)} browser entries that passed "
        f"on the previous run fail now, while {len(was_other) - len(other_failed)} "
        f"of {len(was_other)} other entries still pass: the browser environment "
        "(dependencies, bundler, server start) failed, not the product"
    )


@dataclass(frozen=True)
class InstallResult:
    """What ``ensure_dependencies`` did.

    ``action``: ``"none"`` (no lockfile), ``"current"`` (installed for this
    lockfile already), ``"installed"`` or ``"failed"`` (``error`` says why).
    """

    action: str
    lockfile: str | None = None
    reason: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _find_lockfile(base_dir: Path) -> tuple[Path, tuple[str, ...]] | None:
    for name, argv in LOCKFILE_INSTALLERS:
        path = base_dir / name
        if path.is_file():
            return path, argv
    return None


def _read_install_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_bundler_caches(base_dir: Path) -> list[str]:
    """Remove ``BUNDLER_CACHE_DIRS`` under ``base_dir/node_modules``; the removed paths.

    Removes only a real directory whose resolved parent IS the project's own
    ``node_modules`` (itself inside ``base_dir``): a symlinked cache or a
    symlinked ``node_modules`` pointing elsewhere is left alone.
    """
    removed: list[str] = []
    node_modules = base_dir / "node_modules"
    if node_modules.is_symlink() or not node_modules.is_dir():
        return removed
    root = base_dir.resolve()
    modules = node_modules.resolve()
    if modules.parent != root:
        return removed
    for name in BUNDLER_CACHE_DIRS:
        cache = node_modules / name
        if cache.is_symlink() or not cache.is_dir():
            continue
        if cache.resolve().parent != modules:
            continue
        shutil.rmtree(cache, ignore_errors=True)
        removed.append(str(cache))
    return removed


def _run_install(argv: list[str], cwd: Path, timeout: float) -> str | None:
    """Run the install in its own process group; None on success, else why not.

    The environment is ``_invoke_env()``, not the ledger's own: a clean
    install runs the lifecycle scripts of every package the lockfile names,
    and a lockfile change is exactly when a new, unvetted package arrives. It
    gets the same minimal environment as the entries themselves (``PATH``,
    ``HOME`` for the package manager's cache and config, locale, ``CI=1``),
    never the daemon's or operator's API keys and tokens.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=_invoke_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return f"{' '.join(argv)} could not start: {exc}"
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return f"{' '.join(argv)} timed out after {timeout:.0f}s"
    if proc.returncode != 0:
        tail = (output or "")[-_INSTALL_OUTPUT_TAIL_CHARS:].strip()
        return (
            f"{' '.join(argv)} exited {proc.returncode}: {tail}"
            + _install_error_lines(output or "", tail)
        )
    return None


_INSTALL_ERROR_LINE_RE = re.compile(
    r"(?i)(npm (err!|error)|ERR_PNPM|^error |\berror\b.*(sync|lock|resolve))"
)
_INSTALL_ERROR_LINES_MAX = 8
_INSTALL_ERROR_CHARS = 800


def _install_error_lines(output: str, tail: str) -> str:
    """The package manager's error lines that the tail cut off, appended LAST.

    ``npm ci`` prints why it failed ("package.json and package-lock.json are
    not in sync") and then pages of usage text, so the tail alone often holds
    only the help. They go at the end so that a caller keeping a tail of this
    message keeps them too.
    """
    lines = [
        ln.strip()
        for ln in output.splitlines()
        if _INSTALL_ERROR_LINE_RE.search(ln) and ln.strip() not in tail
    ][:_INSTALL_ERROR_LINES_MAX]
    if not lines:
        return ""
    return "\n--- error lines ---\n" + "\n".join(lines)[:_INSTALL_ERROR_CHARS]


def ensure_dependencies(
    base_dir: Path,
    state_dir: Path,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> InstallResult:
    """Reinstall the project's dependencies when its lockfile changed.

    Installs with the lockfile's own clean-install command (``npm ci``,
    ``pnpm install --frozen-lockfile``, ``yarn install --frozen-lockfile``) in
    ``base_dir`` when the lockfile's sha256 differs from the one recorded in
    ``state_dir/install_state.json`` after the last successful install, or
    when ``node_modules`` is missing. Then clears the bundler caches and
    records the hash. A failed install records nothing, so the next run
    tries again. No lockfile: nothing to do.

    Two ledger runs on one project (the daemon's scheduled step and an
    operator's ``run``) must not run two clean installs into one
    ``node_modules`` at once: the check-install-record sequence holds an
    exclusive ``flock`` on ``state_dir/install.lock``, and a second caller
    waits, then finds the hash current.
    """
    found = _find_lockfile(base_dir)
    if found is None:
        return InstallResult("none")
    import fcntl

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / INSTALL_LOCK_FILENAME, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _ensure_dependencies_locked(base_dir, state_dir, timeout)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# Manifests whose change must force a clean install even when the lockfile
# did not change: ``npm ci`` refuses a package.json that no longer matches its
# lockfile, and a node_modules installed for the old manifest would hide that.
INSTALL_MANIFESTS: tuple[str, ...] = ("package.json",)


def _install_digest(base_dir: Path, lockfile: Path) -> str:
    """sha256 over the lockfile and the manifests (``INSTALL_MANIFESTS``).

    Keyed on both, so a manifest-only change (a dependency added to
    package.json without a lockfile update) is never "current".
    """
    h = hashlib.sha256()
    h.update(lockfile.name.encode() + b"\0" + lockfile.read_bytes())
    for name in INSTALL_MANIFESTS:
        path = base_dir / name
        h.update(b"\0" + name.encode() + b"\0")
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


def _ensure_dependencies_locked(
    base_dir: Path, state_dir: Path, timeout: float
) -> InstallResult:
    found = _find_lockfile(base_dir)
    if found is None:
        return InstallResult("none")
    lockfile, argv = found
    digest = _install_digest(base_dir, lockfile)
    state_path = state_dir / INSTALL_STATE_FILENAME
    state = _read_install_state(state_path)
    has_modules = (base_dir / "node_modules").is_dir()
    if (
        has_modules
        and state.get("lockfile") == lockfile.name
        and state.get("lockfile_sha256") == digest
    ):
        return InstallResult("current", lockfile.name)
    if not has_modules:
        reason = "node_modules is missing"
    elif state.get("lockfile_sha256"):
        reason = f"{lockfile.name} or package.json changed since the last install"
    else:
        reason = f"no install recorded for {lockfile.name}"

    binary = shutil.which(argv[0])
    if binary is None:
        return InstallResult(
            "failed", lockfile.name, reason, f"{argv[0]} not found on PATH"
        )
    error = _run_install([binary, *argv[1:]], base_dir, timeout)
    if error is not None:
        return InstallResult("failed", lockfile.name, reason, error)
    clear_bundler_caches(base_dir)
    _write_json_atomic(
        state_path,
        {
            "lockfile": lockfile.name,
            "lockfile_sha256": digest,
            "command": " ".join(argv),
            "installed_at": time.time(),
        },
    )
    return InstallResult("installed", lockfile.name, reason)


def environment_flag(record: Mapping[str, Any] | None) -> str | None:
    """The reason a recorded run is not evidence about the product, or None.

    Reads ``environment_error`` then ``environment_suspect`` from a
    ``last_run.json`` payload or a ``runs.jsonl`` line. Every consumer that
    opens divergences or credits goals from a run asks this first.
    """
    if not isinstance(record, Mapping):
        return None
    for key in ("environment_error", "environment_suspect"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def write_junit_xml(report: RunReport, path: Path) -> None:
    testsuite = ET.Element(
        "testsuite",
        {
            "name": "acceptance_ledger",
            "tests": str(len(report.outcomes)),
            "failures": str(len(report.inconsistent) + len(report.barren_entries)),
            "errors": str(len(report.errored)),
            "time": f"{report.finished_at - report.started_at:.3f}",
        },
    )
    for outcome in report.outcomes:
        testcase = ET.SubElement(
            testsuite,
            "testcase",
            {"classname": f"acceptance.{outcome.kind}", "name": outcome.entry_id},
        )
        if outcome.verdict == "ERROR":
            error = ET.SubElement(testcase, "error", {"message": outcome.detail})
            error.text = outcome.detail
        elif outcome.verdict in ("INCONSISTENT", "BARREN"):
            failure = ET.SubElement(
                testcase,
                "failure",
                {"message": f"{outcome.verdict}: {outcome.detail}"},
            )
            failure.text = outcome.detail
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(testsuite).write(path, encoding="utf-8", xml_declaration=True)


def write_last_run_json(report: RunReport, path: Path, base_dir: Path) -> None:
    payload = {
        "generated_at": time.time(),
        "project_dir": str(base_dir),
        "entry_count": len(report.outcomes),
        "consistent_count": len(report.consistent),
        "inconsistent_count": len(report.inconsistent),
        "barren_count": len(report.barren_entries),
        "error_count": len(report.errored),
        "barren": report.barren,
        "inconsistent_ids": [o.entry_id for o in report.inconsistent],
        "barren_ids": [o.entry_id for o in report.barren_entries],
        "error_ids": [o.entry_id for o in report.errored],
        "duration_seconds": report.finished_at - report.started_at,
        # Required entries and how each got there: "operator" (declared in the
        # entry, see docs/acceptance-ledger.md), "canary" ('promote') or "unrecorded".
        "promotions": dict(sorted(report.promotions.items())),
        "tier_warnings": dict(sorted(report.tier_warnings.items())),
        # New regressions re-run once in isolation: "flaky_ids" passed on the
        # re-run (their verdict above is the re-run's), "flakes" holds the first
        # run's account of each, "confirmed_ids" failed on the re-run too.
        "flaky_ids": sorted(report.flakes),
        "flakes": dict(sorted(report.flakes.items())),
        "confirmed_ids": sorted(report.confirmed),
        # Not re-run: the confirmation budget was spent (see cmd_run).
        "unconfirmed_ids": sorted(report.unconfirmed),
        # Entry id -> why it errored, so the register can say why an entry
        # that passed before stopped judging.
        "errors": {o.entry_id: o.detail for o in report.errored},
        # Not evidence about the product (see ``environment_flag``): the
        # divergence fold and goal crediting skip a run with either set.
        "environment_error": report.environment_error,
        "environment_suspect": report.environment_suspect,
        "environment_suspect_expired": report.environment_suspect_expired,
        "install": report.install,
        # LEDGER-ADAPTIVE-ENTRY-TIMEOUT (see RunReport).
        "entry_seconds": dict(sorted(report.entry_seconds.items())),
        "entry_timeouts": {
            entry_id: timeout.as_dict()
            for entry_id, timeout in sorted(report.entry_timeouts.items())
        },
        "perf_regressions": dict(sorted(report.perf_regressions.items())),
        "near_limit": dict(sorted(report.near_limit.items())),
        "step_budget_warning": report.step_budget_warning,
        # LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES (see RunReport): a partial
        # run lists the entries it never judged; "stale_ids" are those of them
        # whose previous pass is carried forward, unconfirmed by this run.
        "partial": report.partial,
        "timed_out": report.timed_out,
        "unfinished_ids": list(report.unfinished),
        "stale_ids": list(report.stale_passing),
        "step_budget_seconds": report.step_budget_seconds,
        # LEDGER-AS-SHARDED-RUNNER-JOB: where the verdicts came from.
        "source": report.source,
        "shards": report.shards,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


# --------------------------------------------------------------------------
# Run history + promotion (increment 2b) — an entry earns "required" only
# by proving it both passed and can fail: a pass on the last recorded run
# (this module, appended once per run below) and a canary catch recorded
# by increment 2c's mutant canary (.company/state/acceptance/canary.json,
# unrelated to the pre-existing forge_canary.py auto-merge smoke test).
# Advisory is the only tier this module sets on its own; promotion is the
# sole path to "required" and refuses outright when either input is
# missing rather than guessing. Neither this function nor any caller here
# makes a required-tier entry block a run — that is wired up separately.
# --------------------------------------------------------------------------


MAIN_BRANCH = "main"

# acceptance_health.json's hard-fail statuses. Mirrors the worst two entries
# of acceptance_canary.HEALTH_PRECEDENCE, kept as a literal here so this
# module stays import-free (CI runs it standalone from origin/main).
HEALTH_FAILING_STATUSES = ("error", "dark")
UNKNOWN_BRANCH = "unknown"


def _git_out(base_dir: Path, *args: str) -> str | None:
    """Best-effort ``git`` stdout for the run record. None outside a checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(base_dir), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_commit(base_dir: Path) -> str | None:
    """Best-effort HEAD sha for the run record. None outside a git checkout."""
    return _git_out(base_dir, "rev-parse", "HEAD")


def _git_branch(base_dir: Path) -> str:
    """Best-effort branch name for the run record.

    Fails SAFE to ``"unknown"``, never to ``MAIN_BRANCH``: promotion requires
    a run on main, so a branch that could not be read must refuse promotion
    rather than pass the check by accident. A detached HEAD reads as the
    literal ``"HEAD"`` git prints, which is also not main — CI checks out a
    detached sha, so a CI run can never promote either.
    """
    return _git_out(base_dir, "rev-parse", "--abbrev-ref", "HEAD") or UNKNOWN_BRANCH


def record_run(
    report: RunReport,
    base_dir: Path,
    runs_path: Path,
    *,
    commit: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Append one line to runs.jsonl describing this run.

    Every invocation of ``run`` appends — this file is the only place
    ``promote_entry`` can later ask "did this entry ever pass?" without
    re-running anything. Best-effort: a write failure here (e.g. a full
    disk) must not turn an otherwise-successful ledger run red over a
    history file, matching ``merge_audit.append_merge_chain``'s convention.

    ``commit``/``branch`` override the checkout's own: a ``merge`` records
    the commit the shards ran on, not whatever this checkout is at.
    """
    passing_ids = sorted(o.entry_id for o in report.consistent)
    record = {
        "timestamp": time.time(),
        "commit": commit if commit is not None else _git_commit(base_dir),
        "branch": branch if branch is not None else _git_branch(base_dir),
        "entry_count": len(report.outcomes),
        "pass_count": len(passing_ids),
        "fail_count": len(report.outcomes) - len(passing_ids),
        "duration_seconds": report.finished_at - report.started_at,
        "passing_ids": passing_ids,
        "environment_error": report.environment_error,
        "environment_suspect": report.environment_suspect,
        "environment_suspect_expired": report.environment_suspect_expired,
        # What ``read_entry_history`` learns entry timeouts from: each entry's
        # longest invocation, the flakes (whose duration is a failed run's) and
        # the individually-anomalous runs (kept out of the learned limit --
        # ``perf_excluded``, this run's OWN duration, not the smoothed,
        # reportable ``perf_regressions``; see RunReport).
        "entry_seconds": dict(sorted(report.entry_seconds.items())),
        "flaky_ids": sorted(report.flakes),
        "perf_regression_ids": sorted(report.perf_excluded),
        # LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES. ``passing_ids`` holds only
        # what THIS run judged CONSISTENT; an unfinished entry is in no list
        # here (never failed), and goal crediting skips a run that did not
        # judge an entry it needs. ``stale_passing_ids`` carries the previous
        # pass of an unfinished entry forward for ``previous_passing_ids``.
        "partial": report.partial,
        "timed_out": report.timed_out,
        "unfinished_ids": list(report.unfinished),
        "stale_passing_ids": list(report.stale_passing),
        "source": report.source,
        "shards": report.shards,
    }
    try:
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        with runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return record


def _perf_reference_text(finding: Mapping[str, Any]) -> str:
    """'its frozen baseline of 30s' or 'a rolling median of 30s over 10 passing runs'."""
    median = finding.get("median")
    value = f"{median:.0f}s" if isinstance(median, (int, float)) else "?"
    if finding.get("reference") == "baseline":
        return (
            f"its frozen baseline of {value} (re-baselined only when the entry "
            "declares a new timeoutSeconds)"
        )
    return (
        f"a rolling median of {value} over {finding.get('samples', '?')} passing runs"
    )


def read_perf_findings(path: Path) -> dict[str, dict[str, Any]]:
    """The open ``perf_regression`` findings by entry id; empty when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, dict):
        return {}
    return {
        entry_id: record
        for entry_id, record in findings.items()
        if isinstance(entry_id, str) and isinstance(record, dict)
    }


def read_perf_baselines(path: Path) -> dict[str, dict[str, Any]]:
    """The per-entry frozen-median records (``_baseline_reference``); empty when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    baselines = data.get("baselines") if isinstance(data, dict) else None
    if not isinstance(baselines, dict):
        return {}
    return {
        entry_id: record
        for entry_id, record in baselines.items()
        if isinstance(entry_id, str) and isinstance(record, dict)
    }


def update_perf_findings(
    report: RunReport, path: Path, commit: str | None = None
) -> dict[str, dict[str, Any]]:
    """Fold one run's ``perf_regressions`` into ``perf_regressions.json``.

    Its own signal, not a divergence: the entry still passes. A finding opens
    (or is refreshed) for each entry the run flagged, and closes when the entry
    passes again on a later run without being flagged. A failing or unrun entry
    leaves its finding as it was. A run flagged by ``environment_flag`` changes
    nothing. ``backlog_generator`` mints from the open findings on required
    entries. Best-effort, like ``record_run``: a write failure is ignored.
    """
    findings = read_perf_findings(path)
    if report.environment_error or report.environment_suspect:
        return findings
    baselines = {**read_perf_baselines(path), **report.perf_baselines}
    now = time.time()
    passed = {o.entry_id for o in report.consistent} - set(report.flakes)
    for entry_id in sorted(passed):
        if entry_id in findings and entry_id not in report.perf_regressions:
            if entry_id in report.entry_seconds:
                del findings[entry_id]
    for entry_id, finding in sorted(report.perf_regressions.items()):
        previous = findings.get(entry_id, {})
        findings[entry_id] = {
            **finding,
            "id": entry_id,
            "status": "open",
            "first_seen": previous.get("first_seen", now),
            "last_seen": now,
            "count": int(previous.get("count", 0) or 0) + 1,
            "commit": commit,
        }
    try:
        _write_json_atomic(
            path, {"updated_at": now, "findings": findings, "baselines": baselines}
        )
    except OSError:
        pass
    return findings


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
    """Read a JSON object from *path*, or raise ``LedgerError`` naming the file.

    Every promotion input is written by something else — ``canary.json`` by
    increment 2c, which does not exist yet. A malformed one must refuse by
    name like every other refusal on this path, not surface a JSONDecodeError
    traceback that names nothing an operator can act on.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what} at {path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise LedgerError(f"{what} at {path} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(
            f"{what} at {path} is a {type(data).__name__}, expected a JSON object"
        )
    return data


def _last_run_record(runs_path: Path) -> dict[str, Any] | None:
    """The most recent line of runs.jsonl, or None if there is no run yet.

    A last line that is not a JSON object refuses by name rather than
    raising: runs.jsonl is appended to on every run, so a torn write leaves
    exactly this shape and the operator needs to be told which file to look at.
    """
    if not runs_path.is_file():
        return None
    last_line = None
    for line in runs_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            last_line = line
    if last_line is None:
        return None
    try:
        record = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"the last line of {runs_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise LedgerError(
            f"the last line of {runs_path} is a {type(record).__name__}, "
            "expected a JSON object"
        )
    return record


def previous_passing_ids(runs_path: Path) -> frozenset[str]:
    """The ``passing_ids`` of the most recent TRUSTED recorded run; empty when unknown.

    A run flagged by ``environment_flag`` is skipped: its failures are the
    environment's, and comparing against its passing ids would hide the next
    broken run from ``classify_environment_suspect`` and from the confirmation
    re-runs. Empty (nothing is re-run, every failure is reported as it came
    out) when there is no run yet, the last line is torn, or it carries no id
    list.
    """
    try:
        record = _last_run_record(runs_path)
    except (LedgerError, OSError, ValueError):
        return frozenset()
    if record is not None and environment_flag(record):
        record = _last_trusted_run_record(runs_path)
    passing = record.get("passing_ids") if record else None
    if not isinstance(passing, list):
        return frozenset()
    # A partial run's unfinished entries keep the verdict they had before it
    # (LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES): their earlier pass still
    # makes a failure on the next run a new regression worth re-running.
    stale = record.get("stale_passing_ids") if record else None
    carried = stale if isinstance(stale, list) else []
    return frozenset(
        entry_id for entry_id in (*passing, *carried) if isinstance(entry_id, str)
    )


def _last_trusted_run_record(runs_path: Path) -> dict[str, Any] | None:
    """The latest well-formed runs.jsonl line not flagged by ``environment_flag``."""
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and not environment_flag(record):
            return record
    return None


def environment_suspect_streak(runs_path: Path) -> int:
    """How many of the most recent recorded runs, in a row, were ``environment_suspect``.

    An ``environment_error`` line (the install failed, no entry ran) neither
    counts nor breaks the streak: it said nothing about the browser layer. Any
    other line, and a torn one, ends it.
    """
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    streak = 0
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            break
        if not isinstance(record, dict):
            break
        if isinstance(record.get("environment_error"), str) and record.get(
            "environment_error"
        ):
            continue
        suspect = record.get("environment_suspect")
        if not (isinstance(suspect, str) and suspect):
            break
        streak += 1
    return streak


def last_recorded_commit(runs_path: Path) -> str | None:
    """The ``commit`` field of the most recently recorded run, or None.

    None whenever there is nothing to compare against: no run yet, a torn
    last line, or a run recorded outside a git checkout. Used by the daemon
    to decide whether the default branch has moved since the last run —
    a malformed history must fall back to interval-based scheduling, not
    raise into the caller's loop tick.
    """
    try:
        record = _last_run_record(runs_path)
    except LedgerError:
        return None
    if record is None:
        return None
    commit = record.get("commit")
    return commit if isinstance(commit, str) else None


# The daemon's head trigger (LEDGER-RUN-ON-MERGE). Its own file, written only
# here: the daemon's scheduled_run.json is rewritten whole on every launch.
HEAD_TRIGGER_FILENAME = "head_trigger.json"
HEAD_TRIGGER_MIN_SPACING_SECONDS = 600


def default_branch_ref(base_dir: Path) -> str | None:
    """The ref naming *base_dir*'s default branch, preferring origin's copy.

    ``origin/HEAD`` names the default branch when the clone recorded it; else
    ``main`` then ``master`` (also when ``origin/HEAD`` names a ref that does
    not resolve). For each name the remote-tracking ref (``origin/<name>``)
    wins over the local branch: the daemon fetches origin every git-update
    tick even when a dirty tree makes it skip the pull, so the
    remote-tracking ref shows a merge the local branch may not have yet.
    Without a fetch it is as stale as the last one. None when nothing resolves.
    """
    names = [MAIN_BRANCH, "master"]
    symbolic = _git_out(base_dir, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if symbolic and symbolic.startswith(prefix) and len(symbolic) > len(prefix):
        names = [symbolic[len(prefix) :], *names]
    for name in names:
        for ref in (f"origin/{name}", name):
            if _git_out(
                base_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
            ):
                return ref
    return None


def default_branch_head(base_dir: Path, branch: str | None = None) -> str | None:
    """Best-effort commit sha at the tip of *branch* in *base_dir*.

    With no *branch*, the default branch as ``default_branch_ref`` resolves it.
    """
    ref = branch if branch is not None else default_branch_ref(base_dir)
    if ref is None:
        return None
    return _git_out(base_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def head_moved_since_last_run(
    base_dir: Path, runs_path: Path, branch: str | None = None
) -> bool:
    """True when the default branch's head differs from the commit the last
    recorded run (``runs.jsonl``) used.

    False whenever either commit cannot be determined (no run recorded yet,
    a torn record, a non-git *base_dir*, or any other git failure) — this
    function only ever ADDS a reason to run sooner; it must never itself
    suppress the existing interval-based cadence.
    """
    last_commit = last_recorded_commit(runs_path)
    if last_commit is None:
        return False
    current = default_branch_head(base_dir, branch)
    if current is None:
        return False
    return current != last_commit


def _read_head_trigger(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim_head_trigger(
    base_dir: Path,
    state_dir: Path,
    *,
    now: float | None = None,
    min_spacing_seconds: float = HEAD_TRIGGER_MIN_SPACING_SECONDS,
) -> bool:
    """Claim a head-triggered run for the default branch's current sha.

    True (after recording the sha in ``head_trigger.json``) when a run is
    due because the default branch moved past the last recorded run's commit;
    False otherwise. At most TWO claims per sha: the first, and one retry when
    the first run recorded nothing to runs.jsonl (killed at the step timeout,
    or failed). A failing or timed-out ledger run would otherwise re-trigger
    on every daemon tick. Claims
    are also at least *min_spacing_seconds* apart, and only made while the
    checkout's HEAD is the default branch's head sha — a feature-branch
    checkout, or a main the pull has not caught up yet, claims nothing. If the claim cannot be
    written, no run is claimed: an unrecordable claim cannot be limited.
    """
    last_commit = last_recorded_commit(state_dir / "runs.jsonl")
    if last_commit is None:
        return False
    current = default_branch_head(base_dir)
    if current is None:
        return False
    if current == last_commit:
        return False
    # The run executes the checkout, not the ref: until the pull brings HEAD
    # to the merge (a dirty tree skips it; the acceptance check also runs
    # before the git-update block in the daemon loop), claiming would spend
    # this sha's one run on the old commit. Not claimed, so a later cycle
    # retries.
    if _git_commit(base_dir) != current:
        return False
    path = state_dir / HEAD_TRIGGER_FILENAME
    previous = _read_head_trigger(path)
    # This sha was claimed and its run recorded nothing (the recorded commit is
    # still not ``current``): the step was killed at its timeout, or failed,
    # before writing runs.jsonl. The claim is honoured once more, so a run
    # killed before recording does not cost the commit its run; a second
    # attempt that records nothing either is not retried again.
    retry = previous.get("sha") == current
    if retry and previous.get("retried") is True:
        return False
    now = time.time() if now is None else now
    last_at = previous.get("triggered_at")
    if isinstance(last_at, (int, float)) and 0 <= now - last_at < min_spacing_seconds:
        return False
    claim: dict[str, Any] = {"sha": current, "triggered_at": now}
    if retry:
        claim["retried"] = True
    tmp = state_dir / f".{HEAD_TRIGGER_FILENAME}.{os.getpid()}.tmp"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(claim), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _find_promotable_entry(entry_id: str, config: LedgerConfig) -> dict[str, Any]:
    for subdir in ("relations", "cases"):
        for entry in load_entries(config.ledger_dir, subdir):
            if entry.get("id") == entry_id and "_load_error" not in entry:
                return entry
    raise LedgerError(
        f"no loadable entry with id {entry_id!r} found under {config.ledger_dir}"
    )


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Read-modify-write for a JSON-object state file without a torn write.

    Matches ``goal_scheduler._save_tracking_data``'s mkstemp+os.replace
    idiom — the pattern this codebase already uses for exactly this shape
    of file (a single JSON object read, mutated, and written back).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=path.stem + "_", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def promote_entry(entry_id: str, config: LedgerConfig) -> dict[str, Any]:
    """Set ``entry_id``'s tier to "required" — refuses unless all three hold:

    1. the last run recorded in runs.jsonl was on ``MAIN_BRANCH``;
    2. the entry passed (verdict CONSISTENT) on that run;
    3. .company/state/acceptance/canary.json (increment 2c) reports this
       entry caught its mutant.

    Any input missing, malformed or unfavourable raises ``LedgerError``
    naming exactly which file and which condition — there is no silent
    partial-credit path, and no read on this function may surface a raw
    JSONDecodeError to the caller.
    """
    entry = _find_promotable_entry(entry_id, config)

    runs_path = config.state_dir / "runs.jsonl"
    last_run = _last_run_record(runs_path)
    if last_run is None:
        raise LedgerError(
            f"no recorded run at {runs_path} — run 'gatesmith-acceptance run' "
            "before promoting"
        )
    # Checked BEFORE the pass, so a green run on a feature branch is refused
    # for the reason it is actually refused for. Records written before this
    # check existed carry no branch and read as unknown, i.e. not main.
    branch = last_run.get("branch", UNKNOWN_BRANCH)
    if branch != MAIN_BRANCH:
        raise LedgerError(
            f"the last recorded run ({runs_path}) was on branch {branch!r}, "
            f"not {MAIN_BRANCH!r} — promotion requires a passing run on "
            f"{MAIN_BRANCH}, because an entry can be made to pass on a branch "
            "by changing the artifact it judges"
        )
    if entry_id not in last_run.get("passing_ids", []):
        raise LedgerError(
            f"entry {entry_id!r} did not pass on the last recorded run "
            f"({runs_path}) — promotion requires a passing run on "
            f"{MAIN_BRANCH} first"
        )

    canary_path = config.state_dir / "canary.json"
    if not canary_path.is_file():
        raise LedgerError(
            f"canary file missing: {canary_path} — run the canary "
            "(increment 2c) before promoting"
        )
    canary = _read_json_object(canary_path, "canary file")
    entries = canary.get("entries")
    if not isinstance(entries, dict):
        raise LedgerError(
            f"canary ({canary_path}) has no 'entries' object "
            f"(found {type(entries).__name__}) — it cannot report a catch"
        )
    canary_entry = entries.get(entry_id)
    if not isinstance(canary_entry, dict) or not canary_entry.get("caught"):
        raise LedgerError(
            f"canary ({canary_path}) does not report entry {entry_id!r} as "
            "having caught its mutant"
        )

    # Read BOTH files before writing either. Reading promotions.json after
    # the tier write left a half-applied promotion on a malformed audit file:
    # the entry was required on disk with nothing recording that it earned
    # the tier — the silent partial credit this function claims not to have.
    source_path = Path(entry["_source"])
    on_disk = _read_json_object(source_path, f"entry {entry_id!r}")
    promotions_path = config.state_dir / "promotions.json"
    if promotions_path.is_file():
        promotions = _read_json_object(promotions_path, "promotions file")
    else:
        promotions = {"promotions": []}
    if not isinstance(promotions.get("promotions"), list):
        promotions["promotions"] = []

    on_disk["tier"] = TIER_REQUIRED
    _write_json_atomic(source_path, on_disk)

    promotion_record = {
        "entry_id": entry_id,
        "promoted_at": time.time(),
        "commit": last_run.get("commit"),
        "branch": branch,
        "canary_catch_rate": canary_entry.get("catch_rate"),
    }
    promotions.setdefault("promotions", []).append(promotion_record)
    _write_json_atomic(promotions_path, promotions)

    return promotion_record


# --------------------------------------------------------------------------
# The step budget, partial runs and the attempt record
# (LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES)
#
# webapp, 2026-10-12/13: the full ledger grew to ~2370 s (183 entries) against
# a fixed acceptance.stepTimeoutSeconds of 2400; at 189 entries every run was
# killed (rc=-2, eight times in 16 h) before it wrote anything. No record
# meant no goal credit, the queue sat empty and nothing paged. Three changes:
#   1. ``scheduled_step_budget``: the budget the daemon gives the ledger step
#      is max(stepTimeoutSeconds, STEP_BUDGET_SAFETY_FACTOR x the sum of the
#      entries' own adaptive timeouts), so it grows with the ledger;
#   2. a run that reaches its budget (or the daemon's SIGINT) still records
#      what it judged -- a partial run (``_main_pass``);
#   3. ``ledger_attempts.json`` counts consecutive timed-out attempts, killed
#      ones included, for the silence sentinel's ``ledger_stalled``.
# --------------------------------------------------------------------------

STEP_BUDGET_SAFETY_FACTOR = 1.5
ATTEMPTS_FILENAME = "ledger_attempts.json"
# ``run``'s exit code for a partial run: the daemon logs a non-zero sub-step
# as a WARNING with the tail of its output.
PARTIAL_EXIT_CODE = 4


def scheduled_step_budget(base_dir: Path, configured: float | None = None) -> int:
    """The seconds the scheduled ledger step may take, in whole seconds.

    ``max(configured, STEP_BUDGET_SAFETY_FACTOR x sum of every loadable
    entry's timeout)``, each entry's timeout being exactly what it will run
    under (``entry_timeout_seconds``: default, declared or learned, at most
    ``acceptance.maxEntryTimeoutSeconds``). ``configured`` defaults to
    ``acceptance.stepTimeoutSeconds``; it is the floor, never lowered. Never
    raises: a ledger that cannot be read gets the floor (its run will say why).

    The daemon calls this to set the ledger sub-step's timeout and passes the
    same number as ``run --step-timeout``, so the run plans its own deadline
    against the budget it will actually be killed at.
    """
    try:
        config = LedgerConfig.for_project(Path(base_dir))
        floor = _positive_seconds(configured) or config.step_timeout_seconds
    except Exception:  # noqa: BLE001 - never raises into the daemon
        return int(
            math.ceil(_positive_seconds(configured) or DEFAULT_STEP_TIMEOUT_SECONDS)
        )
    try:
        manifest, items = _ledger_items(config)
        history = read_entry_history(config.state_dir / "runs.jsonl")
        total = 0.0
        for _ordinal, (_fn, entry) in items:
            if "_load_error" in entry:
                continue
            try:
                total += entry_timeout_seconds(
                    manifest,
                    entry,
                    history.get(_entry_display_id(entry)),
                    config.max_entry_timeout_seconds,
                ).seconds
            except Exception:  # noqa: BLE001 - the entry's run reports it
                total += invoke_timeout_seconds(manifest)
    except Exception:  # noqa: BLE001
        return int(math.ceil(floor))
    return int(math.ceil(max(floor, total * STEP_BUDGET_SAFETY_FACTOR)))


def _read_attempts(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def begin_attempt(state_dir: Path, budget_seconds: float | None) -> dict[str, Any]:
    """Mark a run in flight. An earlier one still marked was killed before it
    could record anything: it counts as a timed-out attempt. Best-effort."""
    path = state_dir / ATTEMPTS_FILENAME
    data = _read_attempts(path)
    now = time.time()
    if isinstance(data.get("in_flight"), dict):
        data["consecutive_timeouts"] = _count(data.get("consecutive_timeouts")) + 1
        data["killed"] = _count(data.get("killed")) + 1
        data["last_outcome"] = "killed"
        data["last_finished_at"] = data["in_flight"].get("started_at")
    data["in_flight"] = {
        "started_at": now,
        "budget_seconds": budget_seconds,
        "pid": os.getpid(),
    }
    try:
        _write_json_atomic(path, data)
    except OSError:
        pass
    return data


def end_attempt(state_dir: Path, outcome: str) -> dict[str, Any]:
    """Record how the run in flight ended. ``outcome``: "complete" resets the
    timeout streak; "partial" (deadline or interrupt) extends it; anything else
    ("environment_error", "error") leaves it. Best-effort."""
    path = state_dir / ATTEMPTS_FILENAME
    data = _read_attempts(path)
    now = time.time()
    data["in_flight"] = None
    data["last_outcome"] = outcome
    data["last_finished_at"] = now
    if outcome == "complete":
        data["consecutive_timeouts"] = 0
        data["last_complete_at"] = now
    elif outcome == "partial":
        data["consecutive_timeouts"] = _count(data.get("consecutive_timeouts")) + 1
        data["last_partial_at"] = now
    try:
        _write_json_atomic(path, data)
    except OSError:
        pass
    return data


def previous_unfinished_ids(runs_path: Path) -> frozenset[str]:
    """The last recorded run's ``unfinished_ids`` (run first next time)."""
    try:
        record = _last_run_record(runs_path)
    except (LedgerError, OSError, ValueError):
        return frozenset()
    ids = record.get("unfinished_ids") if isinstance(record, dict) else None
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(i for i in ids if isinstance(i, str))


class _InterruptGuard:
    """SIGINT/SIGTERM -> KeyboardInterrupt while entries run, ignored after.

    The daemon ends a step that passed its timeout with SIGINT, then SIGKILL
    10 s later. The first signal ends the main pass (the run records what it
    judged); every later one, and one that lands while the records are being
    written, is ignored so the writes are not torn. Outside the main thread
    (a test calling ``cmd_run`` from a worker thread) it installs nothing.
    """

    def __init__(self) -> None:
        self.writing = False
        self.fired = False
        self._previous: dict[int, Any] = {}

    def _handler(self, signum: int, _frame: Any) -> None:
        if self.fired or self.writing:
            self.fired = True
            return
        self.fired = True
        raise KeyboardInterrupt(f"signal {signum}")

    def __enter__(self) -> "_InterruptGuard":
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[signum] = signal.signal(signum, self._handler)
            except (ValueError, OSError):  # not the main thread
                pass
        return self

    def __exit__(self, *exc_info: object) -> None:
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                pass


# --------------------------------------------------------------------------
# Shards and merge (LEDGER-AS-SHARDED-RUNNER-JOB)
#
# Operator decision 2026-10-13: the scheduled ledger moves off the daemon into
# a scheduled CI workflow on the product's self-hosted runners, split into N
# shards that run in parallel (templates/ci/acceptance-ledger.yml).
#   run --shard i/N   judges one deterministic slice (split by entry id,
#                     balanced by recorded per-entry duration) and writes a
#                     shard result file -- never last_run.json/runs.jsonl;
#   merge FILES...    folds the shard results into last_run.json/runs.jsonl
#                     through the same ``_assemble_report`` a serial run uses,
#                     replaying each shard's confirmation re-runs.
# Every shard and the merge run main's checks: the workflow runs only on the
# default branch, and the daemon ingests only such runs (acceptance_ci.py).
# --------------------------------------------------------------------------

SHARD_FORMAT = "gatesmith-acceptance-shard/1"


def parse_shard_spec(text: str) -> tuple[int, int]:
    """``"i/N"`` (1-based, 1 <= i <= N) -> ``(i, N)``; LedgerError otherwise."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", text or "")
    if not match:
        raise LedgerError(f"--shard must look like i/N (e.g. 1/3), not {text!r}")
    index, count = int(match.group(1)), int(match.group(2))
    if count < 1 or not 1 <= index <= count:
        raise LedgerError(f"--shard {text!r}: need 1 <= i <= N")
    return index, count


def recorded_entry_seconds(runs_path: Path) -> dict[str, float]:
    """Each entry's most recently recorded duration (``entry_seconds``) in
    ``runs.jsonl``, whatever its verdict; environment-flagged runs skipped."""
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    found: dict[str, float] = {}
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or environment_flag(record):
            continue
        seconds = record.get("entry_seconds")
        if not isinstance(seconds, dict):
            continue
        for entry_id, value in seconds.items():
            duration = _positive_seconds(value)
            if isinstance(entry_id, str) and duration and entry_id not in found:
                found[entry_id] = duration
    return found


def load_durations_file(path: Path) -> dict[str, float]:
    """Per-entry durations from a JSON file: a ``last_run.json`` (its
    ``entry_seconds``) or a plain ``{id: seconds}`` object. LedgerError when
    unreadable, so every shard of a run fails alike rather than splitting
    differently."""
    data = _read_json_object(path, "durations file")
    source = (
        data.get("entry_seconds")
        if isinstance(data.get("entry_seconds"), dict)
        else data
    )
    return {
        key: seconds
        for key, value in source.items()
        if isinstance(key, str) and (seconds := _positive_seconds(value)) is not None
    }


def shard_assignment(
    entry_ids: Sequence[str], weights: Mapping[str, float], count: int
) -> list[list[str]]:
    """Split ``entry_ids`` into ``count`` slices of about equal recorded time.

    Deterministic: a pure function of the id set, the weights and ``count``.
    Longest-first onto the least-loaded slice (ties: heavier first, then id;
    lowest slice index), an id with no recorded duration weighing the median
    of those that have one (1 s when none has). Each slice is returned sorted.
    Every shard of one CI run computes this from the same inputs (the plan
    job's durations file), so the slices never overlap or leave a gap; the
    merge checks that too.
    """
    if count < 1:
        raise LedgerError("a ledger cannot be split into fewer than 1 shard")
    ids = sorted(set(entry_ids))
    known = [w for i in ids if (w := _positive_seconds(weights.get(i))) is not None]
    default = float(statistics.median(known)) if known else 1.0

    def weight(entry_id: str) -> float:
        return _positive_seconds(weights.get(entry_id)) or default

    loads = [0.0] * count
    slices: list[list[str]] = [[] for _ in range(count)]
    for entry_id in sorted(ids, key=lambda i: (-weight(i), i)):
        target = min(range(count), key=lambda k: (loads[k], k))
        slices[target].append(entry_id)
        loads[target] += weight(entry_id)
    return [sorted(s) for s in slices]


def _outcome_dict(outcome: Outcome) -> dict[str, Any]:
    return dataclasses.asdict(outcome)


def _outcome_from(data: Any, what: str) -> Outcome:
    if not isinstance(data, dict):
        raise LedgerError(f"{what} is not an outcome object")
    try:
        return Outcome(
            str(data["entry_id"]),
            str(data["kind"]),
            str(data["tier"]),
            str(data["verdict"]),
            str(data["detail"]),
        )
    except KeyError as exc:
        raise LedgerError(f"{what} has no {exc.args[0]!r}") from exc


def _timeout_dict(timeout: EntryTimeout | None) -> dict[str, Any] | None:
    return dataclasses.asdict(timeout) if timeout is not None else None


def _timeout_from(data: Any) -> EntryTimeout | None:
    if not isinstance(data, dict) or not isinstance(data.get("seconds"), int):
        return None
    return EntryTimeout(
        seconds=data["seconds"],
        source=str(data.get("source", "default")),
        declared=_positive_seconds(data.get("declared")),
        learned=_positive_seconds(data.get("learned")),
    )


def _entry_run_dict(result: EntryRun) -> dict[str, Any]:
    return {
        "ordinal": result.ordinal,
        "id": result.entry_id,
        "outcome": _outcome_dict(result.outcome),
        "loaded": result.loaded,
        "duration": result.duration,
        "timeout": _timeout_dict(result.timeout),
        "longest": result.longest,
        "obs": result.obs,
        "content_hash": result.content_hash,
        "browser": result.browser,
        "promotion": result.promotion,
        "tier_warning": result.tier_warning,
        "rerun": _outcome_dict(result.rerun) if result.rerun is not None else None,
    }


def _entry_run_from(data: Any, where: str) -> EntryRun:
    if not isinstance(data, dict):
        raise LedgerError(f"an entry in {where} is not an object")
    ordinal = data.get("ordinal")
    entry_id = data.get("id")
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not isinstance(entry_id, str)
    ):
        raise LedgerError(f"an entry in {where} has no ordinal/id")
    rerun = data.get("rerun")
    longest = data.get("longest")
    duration = data.get("duration")
    obs = data.get("obs")
    return EntryRun(
        ordinal=ordinal,
        entry_id=entry_id,
        outcome=_outcome_from(data.get("outcome"), f"{entry_id} in {where}"),
        loaded=data.get("loaded") is not False,
        duration=float(duration) if isinstance(duration, (int, float)) else 0.0,
        timeout=_timeout_from(data.get("timeout")),
        longest=float(longest)
        if isinstance(longest, (int, float)) and not isinstance(longest, bool)
        else None,
        obs=obs if isinstance(obs, int) and not isinstance(obs, bool) else 0,
        content_hash=data.get("content_hash")
        if isinstance(data.get("content_hash"), str)
        else None,
        browser=data.get("browser") is True,
        promotion=data.get("promotion")
        if isinstance(data.get("promotion"), str)
        else None,
        tier_warning=data.get("tier_warning")
        if isinstance(data.get("tier_warning"), str)
        else None,
        rerun=_outcome_from(rerun, f"{entry_id}'s re-run in {where}")
        if rerun is not None
        else None,
    )


def run_shard(
    config: LedgerConfig,
    index: int,
    count: int,
    *,
    weights: Mapping[str, float],
    previously_passing: frozenset[str] = frozenset(),
    deadline: float | None = None,
    confirm_until: float | None = None,
) -> dict[str, Any]:
    """Judge slice ``index`` of ``count`` and return its shard result.

    The main pass is the serial run's (``_main_pass``, with the same deadline
    rule). Every entry of the slice that passed on the previous run and is
    INCONSISTENT now is re-run once, alone, and the re-run is recorded (not
    applied): whether it counts -- a suspect run re-runs nothing -- is decided
    by ``merge`` over every shard's results, as a serial run decides it over
    every entry.
    """
    manifest, items = _ledger_items(config)
    all_ids = [_entry_display_id(entry) for _, (_fn, entry) in items]
    partition = shard_assignment(all_ids, weights, count)
    mine = set(partition[index - 1])
    my_items = [item for item in items if _entry_display_id(item[1][1]) in mine]
    history = read_entry_history(config.state_dir / "runs.jsonl")
    started_at = time.time()
    results, unfinished, timed_out = _main_pass(
        config,
        manifest,
        my_items,
        history,
        deadline=deadline,
        run_first=previous_unfinished_ids(config.state_dir / "runs.jsonl") & mine,
    )
    _annotate_entry_runs(results, my_items, manifest, config.base_dir)
    by_ordinal = dict(my_items)
    if timed_out != "interrupted":
        for result in results:
            if not (
                result.loaded
                and result.outcome.verdict == "INCONSISTENT"
                and result.entry_id in previously_passing
            ):
                continue
            needs = max(result.duration, _entry_needs(result.timeout, manifest))
            if confirm_until is not None and _monotonic() + needs >= confirm_until:
                continue
            run_fn, entry = by_ordinal[result.ordinal]
            try:
                with tempfile.TemporaryDirectory(prefix="acceptance-confirm-") as tmp:
                    result.rerun, _obs = _run_entry_isolated(
                        run_fn,
                        manifest,
                        entry,
                        dataclasses.replace(
                            config,
                            tmp_root=Path(tmp),
                            entry_timeout_seconds=(
                                result.timeout.seconds if result.timeout else None
                            ),
                            invoke_seconds=None,
                        ),
                    )
            except KeyboardInterrupt:
                timed_out = "interrupted"
                break
    return {
        "format": SHARD_FORMAT,
        "shard": {"index": index, "count": count},
        "partition": partition,
        "commit": _git_commit(config.base_dir),
        "branch": _git_branch(config.base_dir),
        "started_at": started_at,
        "finished_at": time.time(),
        "entries": [_entry_run_dict(r) for r in results],
        "unfinished_ids": unfinished,
        "timed_out": timed_out,
        "step_budget_seconds": None,
        "environment_error": None,
        "install": None,
    }


def load_shard_results(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Read shard result files; LedgerError naming the file when one is not one."""
    payloads = []
    for path in paths:
        data = _read_json_object(Path(path), "shard result")
        if data.get("format") != SHARD_FORMAT:
            raise LedgerError(f"{path} is not a {SHARD_FORMAT} shard result")
        data["_path"] = str(path)
        payloads.append(data)
    return payloads


def _validate_shards(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[int, list[list[str]], str | None, str | None]:
    """(count, partition, commit, branch) every shard agrees on; else LedgerError.

    Refuses a set that is not exactly shards 1..N of one split of one commit,
    or a shard that judged an entry outside its slice or left one of its
    slice unaccounted for (neither judged nor unfinished): a merge must never
    read a gap as a clean run.
    """
    if not payloads:
        raise LedgerError("merge needs at least one shard result")
    shard = payloads[0].get("shard")
    count = shard.get("count") if isinstance(shard, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise LedgerError(f"{payloads[0].get('_path')} has no shard count")
    partition = payloads[0].get("partition")
    commit = payloads[0].get("commit")
    branch = payloads[0].get("branch")
    seen: set[int] = set()
    for payload in payloads:
        where = payload.get("_path", "a shard result")
        meta = payload.get("shard") if isinstance(payload.get("shard"), dict) else {}
        index = meta.get("index")
        if meta.get("count") != count:
            raise LedgerError(
                f"{where} is a shard of {meta.get('count')}, not of {count}"
            )
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 1 <= index <= count
        ):
            raise LedgerError(f"{where} has no valid shard index")
        if index in seen:
            raise LedgerError(f"shard {index}/{count} was given twice")
        seen.add(index)
        if payload.get("partition") != partition:
            raise LedgerError(
                f"{where} split the ledger differently from the other shards; "
                "every shard of a run must read the same durations"
            )
        if payload.get("commit") != commit:
            raise LedgerError(
                f"{where} ran on commit {payload.get('commit')!r}, another shard "
                f"on {commit!r}: not one run"
            )
        if payload.get("branch") != branch:
            branch = None
        if not isinstance(partition, list) or len(partition) != count:
            raise LedgerError(f"{where} carries no {count}-way partition")
        slice_ids = set(partition[index - 1])
        entries = payload.get("entries")
        unfinished = payload.get("unfinished_ids")
        if not isinstance(entries, list) or not isinstance(unfinished, list):
            raise LedgerError(f"{where} has no entries/unfinished_ids lists")
        if payload.get("environment_error"):
            continue
        judged = {e.get("id") for e in entries if isinstance(e, dict)}
        accounted = judged | set(unfinished)
        if not judged <= slice_ids or not set(unfinished) <= slice_ids:
            raise LedgerError(f"{where} judged entries outside its slice")
        if accounted != slice_ids:
            missing = sorted(slice_ids - accounted)[:5]
            raise LedgerError(
                f"{where} neither judged nor listed as unfinished: {missing}"
            )
    if seen != set(range(1, count + 1)):
        missing = sorted(set(range(1, count + 1)) - seen)
        raise LedgerError(f"shard(s) {missing} of {count} are missing")
    return (
        count,
        partition,
        commit if isinstance(commit, str) else None,
        branch if isinstance(branch, str) else None,
    )


def merge_shard_results(
    config: LedgerConfig,
    payloads: Sequence[Mapping[str, Any]],
    *,
    source: str = "shards",
) -> tuple[RunReport, str | None, str | None]:
    """Fold shard results into one ``RunReport``, as a serial run would read it.

    Returns (report, commit, branch): the commit and branch the shards ran
    on, for ``record_run``. The run's history (previous passing ids, the
    suspect streak, learned durations, perf baselines) is read from
    ``config.state_dir``, the state the merged run is appended to.
    """
    count, _partition, commit, branch = _validate_shards(payloads)
    by_index = sorted(payloads, key=lambda p: p["shard"]["index"])
    started_at = min(
        (
            p["started_at"]
            for p in by_index
            if isinstance(p.get("started_at"), (int, float))
        ),
        default=time.time(),
    )
    install = next(
        (p.get("install") for p in by_index if isinstance(p.get("install"), dict)), None
    )
    budget = max(
        (
            p["step_budget_seconds"]
            for p in by_index
            if _positive_seconds(p.get("step_budget_seconds")) is not None
        ),
        default=None,
    )
    env_error = next(
        (p["environment_error"] for p in by_index if p.get("environment_error")), None
    )
    if env_error:
        now = time.time()
        report = RunReport(
            outcomes=[],
            barren=True,
            started_at=started_at,
            finished_at=now,
            environment_error=str(env_error),
            install=install,
            source=source,
            shards=count,
            step_budget_seconds=budget,
        )
        return report, commit, branch
    results: list[EntryRun] = []
    unfinished: list[str] = []
    timed_out: str | None = None
    for payload in by_index:
        where = str(payload.get("_path", "a shard result"))
        results.extend(_entry_run_from(e, where) for e in payload["entries"])
        unfinished.extend(i for i in payload["unfinished_ids"] if isinstance(i, str))
        reason = payload.get("timed_out")
        if reason == "interrupted" or (reason == "deadline" and timed_out is None):
            timed_out = reason
    ordinals = [r.ordinal for r in results]
    if len(set(ordinals)) != len(ordinals):
        raise LedgerError("two shards judged the same entry")
    results.sort(key=lambda r: r.ordinal)
    runs_path = config.state_dir / "runs.jsonl"
    report = _assemble_report(
        results,
        manifest={},
        config=config,
        previously_passing=previous_passing_ids(runs_path),
        suspect_streak=environment_suspect_streak(runs_path),
        confirm_until=None,
        history=read_entry_history(runs_path),
        baselines=read_perf_baselines(config.state_dir / PERF_FINDINGS_FILENAME),
        started_at=started_at,
        unfinished=sorted(set(unfinished)),
        timed_out=timed_out,
        replay=True,
    )
    report.install = install
    report.source = source
    report.shards = count
    report.step_budget_seconds = budget
    return report, commit, branch


def apply_merged_run(
    config: LedgerConfig,
    payloads: Sequence[Mapping[str, Any]],
    *,
    source: str = "shards",
    junit_path: Path | None = None,
    branch_override: str | None = None,
) -> int:
    """Merge shard results and write every record a serial ``run`` writes.

    The attempt record is updated as for a run (``begin_attempt`` /
    ``end_attempt``). ``branch_override``: the caller verified the commit is
    on the default branch (``acceptance_ci``) and records it as such.
    """
    report, commit, branch = merge_shard_results(config, payloads, source=source)
    begin_attempt(config.state_dir, report.step_budget_seconds)
    code = _emit_report(
        report,
        config,
        config.base_dir,
        junit_path,
        commit=commit,
        branch=branch_override or branch or UNKNOWN_BRANCH,
    )
    end_attempt(config.state_dir, _attempt_outcome(report))
    return code


def _attempt_outcome(report: RunReport) -> str:
    if report.environment_error:
        return "environment_error"
    return "partial" if report.partial else "complete"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _step_budget(args: argparse.Namespace, config: LedgerConfig) -> float | None:
    """The run's own budget in seconds; None for no deadline.

    ``--step-timeout`` when given (the daemon passes ``scheduled_step_budget``;
    0 means no deadline, for a run by hand or a CI job with its own limit),
    else ``acceptance.stepTimeoutSeconds`` -- what the daemon kills the step
    at when it passes nothing.
    """
    value = getattr(args, "step_timeout", None)
    if value is None:
        return config.step_timeout_seconds
    return _positive_seconds(value)


def _deadlines(budget: float | None) -> tuple[float | None, float | None]:
    """(confirm_until on ``_monotonic``, main-pass deadline on
    ``_main_pass_monotonic``): the budget less a reserve for the records."""
    if budget is None:
        return None, None
    usable = budget - confirm_reserve_seconds(budget)
    return _monotonic() + usable, _main_pass_monotonic() + usable


def _environment_error_report(install: InstallResult) -> RunReport:
    now = time.time()
    return RunReport(
        outcomes=[],
        barren=True,
        started_at=now,
        finished_at=now,
        environment_error=(
            f"dependency install failed ({install.reason}): {install.error}"
        ),
        install=install.as_dict(),
    )


def _config_or_refuse(base_dir: Path) -> "LedgerConfig | None":
    """``LedgerConfig.for_project``, or None after printing why a config file
    made the command refuse (it never runs on a config it cannot trust)."""
    try:
        return LedgerConfig.for_project(base_dir)
    except LedgerError as exc:
        print(f"acceptance-ledger: error: {exc}", file=sys.stderr)
        return None


def cmd_run(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = _config_or_refuse(base_dir)
    if config is None:
        return 1
    shard_spec = getattr(args, "shard", None)
    if shard_spec is None and not getattr(args, "local", False):
        # LEDGER-AS-SHARDED-RUNNER-JOB: acceptance.runner "ci" -- the verdicts
        # come from the product's sharded CI workflow; a local run is only the
        # fallback when CI has had no fresh result for 2 x intervalHours.
        code = _maybe_ingest_ci(base_dir, config, args)
        if code is not None:
            return code
    budget = _step_budget(args, config)
    if budget is not None:
        config = dataclasses.replace(config, step_timeout_seconds=budget)
    # The daemon kills this step at its budget (SIGINT, then SIGKILL 10 s
    # later). The main pass stops starting entries that no longer fit before
    # the budget less a reserve for writing the records, and confirmation
    # re-runs use whatever the main pass leaves of that (CONFIRM_RESERVE_*).
    confirm_until, deadline = _deadlines(budget)
    if shard_spec is not None:
        return _cmd_run_shard(args, config, shard_spec, confirm_until, deadline, budget)
    runs_path = config.state_dir / "runs.jsonl"
    junit_path = Path(args.junit) if args.junit else None
    begin_attempt(config.state_dir, budget)
    guard = _InterruptGuard()
    with guard:
        install = ensure_dependencies(base_dir, config.state_dir)
        if install.action == "installed":
            print(f"acceptance-ledger: reinstalled dependencies ({install.reason})")
        if install.action == "failed":
            # No entry runs against dependencies that do not match the lockfile:
            # the record says why, and opens no divergence and credits no goal.
            guard.writing = True
            report = _environment_error_report(install)
            report.step_budget_seconds = budget
            code = _emit_report(report, config, base_dir, junit_path)
            end_attempt(config.state_dir, "environment_error")
            return code
        try:
            report = run_ledger(
                config,
                previous_passing_ids(runs_path),
                confirm_until,
                environment_suspect_streak(runs_path),
                deadline=deadline,
                run_first=previous_unfinished_ids(runs_path),
            )
        except LedgerError as exc:
            guard.writing = True
            print(f"acceptance-ledger: error: {exc}", file=sys.stderr)
            end_attempt(config.state_dir, "error")
            return 1
        except KeyboardInterrupt:
            # Interrupted outside an entry (loading, cleanup): nothing judged
            # can be recorded, but the attempt still counts as timed out.
            guard.writing = True
            print("acceptance-ledger: interrupted before any entry was recorded")
            end_attempt(config.state_dir, "partial")
            return PARTIAL_EXIT_CODE
        guard.writing = True
        report.install = install.as_dict()
        report.step_budget_seconds = budget
        code = _emit_report(report, config, base_dir, junit_path)
        end_attempt(config.state_dir, _attempt_outcome(report))
        return code


def _maybe_ingest_ci(
    base_dir: Path, config: LedgerConfig, args: argparse.Namespace
) -> int | None:
    """None: run locally. Otherwise the exit code of a CI ingest (or no-op)."""
    try:
        runner = ci_runner_mode(base_dir)
    except LedgerError as exc:
        print(f"acceptance-ledger: error: {exc}", file=sys.stderr)
        return 1
    if runner != "ci":
        return None
    try:
        # An optional SIBLING, loaded by name only on this path: CI imports
        # this file standalone from origin/main (stdlib only) and never takes
        # it (its jobs pass --shard / --local).
        hooks_dir = str(Path(__file__).resolve().parent)
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        acceptance_ci = importlib.import_module("acceptance_ci")
    except ImportError as exc:
        print(
            f'acceptance-ledger: acceptance.runner is "ci" but acceptance_ci '
            f"could not be imported ({exc}); running locally"
        )
        return None
    decision = acceptance_ci.scheduled_ingest(base_dir, config)
    print(f"acceptance-ledger: runner=ci — {decision.action}: {decision.reason}")
    if decision.action == "fallback_local":
        return None
    return decision.exit_code


def ci_runner_mode(base_dir: Path) -> str:
    """``acceptance.runner``: "ci" or "daemon" (default; any other value reads
    as "daemon"). An unreadable config file raises ``LedgerError``."""
    value = _governing_setting(base_dir, "acceptance.runner")
    return "ci" if value == "ci" else "daemon"


def _cmd_run_shard(
    args: argparse.Namespace,
    config: LedgerConfig,
    shard_spec: str,
    confirm_until: float | None,
    deadline: float | None,
    budget: float | None,
) -> int:
    """``run --shard i/N``: judge one slice, write a shard result, nothing else."""
    try:
        index, count = parse_shard_spec(shard_spec)
        runs_path = config.state_dir / "runs.jsonl"
        weights = (
            load_durations_file(Path(args.durations))
            if getattr(args, "durations", None)
            else recorded_entry_seconds(runs_path)
        )
    except LedgerError as exc:
        print(f"acceptance-ledger: error: {exc}", file=sys.stderr)
        return 1
    output = (
        Path(args.output)
        if getattr(args, "output", None)
        else config.state_dir / f"shard-{index}-of-{count}.json"
    )
    guard = _InterruptGuard()
    with guard:
        install = ensure_dependencies(config.base_dir, config.state_dir)
        if install.action == "failed":
            guard.writing = True
            payload: dict[str, Any] = {
                "format": SHARD_FORMAT,
                "shard": {"index": index, "count": count},
                "partition": None,
                "commit": _git_commit(config.base_dir),
                "branch": _git_branch(config.base_dir),
                "started_at": time.time(),
                "finished_at": time.time(),
                "entries": [],
                "unfinished_ids": [],
                "timed_out": None,
                "step_budget_seconds": budget,
                "environment_error": _environment_error_report(
                    install
                ).environment_error,
                "install": install.as_dict(),
            }
            try:
                payload["partition"] = shard_assignment(
                    ledger_entry_ids(config), weights, count
                )
            except LedgerError:
                pass
        else:
            try:
                payload = run_shard(
                    config,
                    index,
                    count,
                    weights=weights,
                    previously_passing=previous_passing_ids(runs_path),
                    deadline=deadline,
                    confirm_until=confirm_until,
                )
            except LedgerError as exc:
                print(f"acceptance-ledger: error: {exc}", file=sys.stderr)
                return 1
            guard.writing = True
            payload["install"] = install.as_dict()
            payload["step_budget_seconds"] = budget
        _write_json_atomic(output, payload)
    judged = len(payload["entries"])
    print(
        f"acceptance-ledger: shard {index}/{count}: {judged} entries judged, "
        f"{len(payload['unfinished_ids'])} unfinished -> {output}"
    )
    return 0


def _emit_report(
    report: RunReport,
    config: LedgerConfig,
    base_dir: Path,
    junit_path: Path | None,
    *,
    commit: str | None = None,
    branch: str | None = None,
) -> int:
    """Print a run's findings and write its records; return ``run``'s exit code."""
    junit = junit_path or config.state_dir / "junit.xml"
    if report.environment_error:
        print(
            f"acceptance-ledger: ENVIRONMENT ERROR — {report.environment_error}; "
            "no entry was run",
            file=sys.stderr,
        )
        write_junit_xml(report, junit)
        write_last_run_json(report, config.state_dir / "last_run.json", base_dir)
        record_run(
            report,
            base_dir,
            config.state_dir / "runs.jsonl",
            commit=commit,
            branch=branch,
        )
        # Last line, short: the daemon's WARNING shows the last 200 characters.
        print(
            "acceptance-ledger: ENVIRONMENT ERROR — dependency install failed, "
            "no entry was run (last_run.json: environment_error)"
        )
        return ENVIRONMENT_EXIT_CODE
    if report.environment_suspect:
        print(
            f"acceptance-ledger: WARNING: ENVIRONMENT SUSPECT — "
            f"{report.environment_suspect}; this run opens no divergence and "
            "credits no goal",
            file=sys.stderr,
        )
    if report.environment_suspect_expired:
        print(
            "acceptance-ledger: WARNING: ENVIRONMENT SUSPECT FLAG EXPIRED — "
            f"{report.environment_suspect_expired}; this run's failures open "
            "divergences",
            file=sys.stderr,
        )

    for entry_id, warning in sorted(report.tier_warnings.items()):
        print(f"TIER: {entry_id} — {warning}", file=sys.stderr)
    for outcome in report.outcomes:
        if outcome.verdict == "INCONSISTENT":
            print(
                f"INCONSISTENT: {outcome.entry_id} ({outcome.kind}) — {outcome.detail}"
            )
        elif outcome.verdict == "BARREN":
            print(f"BARREN: {outcome.entry_id} ({outcome.kind}) — {outcome.detail}")
        elif outcome.verdict == "ERROR":
            print(f"ERROR: {outcome.entry_id} ({outcome.kind}) — {outcome.detail}")
    for entry_id, detail in sorted(report.flakes.items()):
        print(
            f"FLAKE: {entry_id} — failed ({detail}), then passed when re-run alone; "
            "recorded as a flake, not a divergence"
        )
    for entry_id in report.confirmed:
        print(f"CONFIRMED: {entry_id} — failed again when re-run alone")
    for entry_id in report.unconfirmed:
        print(
            f"UNCONFIRMED: {entry_id} — not re-run, too little of the step "
            f"timeout ({config.step_timeout_seconds:.0f}s) was left after the "
            "main pass; one unconfirmed failure opens no divergence"
        )

    for entry_id, finding in sorted(report.perf_regressions.items()):
        print(
            f"PERF REGRESSION: {entry_id} — its last {finding.get('window', 1)} "
            f"run(s) median {finding['seconds']:.0f}s against "
            f"{_perf_reference_text(finding)}; not learned into its timeout "
            f"({config.state_dir.name}/{PERF_FINDINGS_FILENAME})"
        )
    for entry_id, near in sorted(report.near_limit.items()):
        print(
            f"NEAR LIMIT: {entry_id} — took {near['seconds']:.0f}s of its "
            f"{near['timeout']}s timeout ({near['source']}); declare "
            "timeoutSeconds in the entry if the spec requires the longer flow"
        )
    if report.step_budget_warning:
        print(
            f"acceptance-ledger: WARNING: STEP BUDGET — {report.step_budget_warning}",
            file=sys.stderr,
        )
    if report.partial:
        print(
            f"acceptance-ledger: WARNING: PARTIAL RUN ({report.timed_out}) — "
            f"{len(report.unfinished)} entries not judged, kept at their previous "
            f"verdict ({len(report.stale_passing)} stale passes): "
            + ", ".join(report.unfinished[:10])
            + (" …" if len(report.unfinished) > 10 else ""),
            file=sys.stderr,
        )

    write_junit_xml(report, junit)
    write_last_run_json(report, config.state_dir / "last_run.json", base_dir)
    recorded = record_run(
        report, base_dir, config.state_dir / "runs.jsonl", commit=commit, branch=branch
    )
    update_perf_findings(
        report, config.state_dir / PERF_FINDINGS_FILENAME, recorded.get("commit")
    )

    # One line an operator can read without opening the JUnit file: a run
    # that observed nothing must never read the same as a clean run.
    print(
        f"acceptance-ledger: {len(report.outcomes)} entries, "
        f"{len(report.inconsistent)} inconsistent, "
        f"{len(report.barren_entries)} barren, "
        f"{len(report.errored)} errored, "
        + (f"{len(report.unfinished)} unfinished, " if report.partial else "")
        + f"{'RUN OBSERVED NOTHING' if report.barren else 'not barren'}"
    )
    if report.environment_suspect:
        # Last line, non-zero exit: the daemon logs a sub-step that did not
        # exit 0 as a WARNING with the tail of its output; an exit 0 is logged
        # as "ok" and the flag would be visible only in last_run.json. Short:
        # the WARNING shows the last 200 characters.
        print(
            "acceptance-ledger: ENVIRONMENT SUSPECT — no divergence opened, no "
            "goal credited (last_run.json: environment_suspect)"
        )
        return ENVIRONMENT_EXIT_CODE
    if report.partial:
        print(
            f"acceptance-ledger: PARTIAL — {len(report.unfinished)} entries not "
            f"judged within the {report.step_budget_seconds or 0:.0f}s budget "
            "(last_run.json: unfinished_ids)"
        )
        return PARTIAL_EXIT_CODE
    # increment 1: every entry is advisory — an entry verdict never fails the run
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """``merge FILES...``: fold shard results into last_run.json/runs.jsonl."""
    base_dir = Path(args.project_dir).resolve()
    config = _config_or_refuse(base_dir)
    if config is None:
        return 1
    try:
        payloads = load_shard_results([Path(p) for p in args.files])
        return apply_merged_run(
            config,
            payloads,
            junit_path=Path(args.junit) if args.junit else None,
        )
    except LedgerError as exc:
        print(f"acceptance-ledger: merge refused: {exc}", file=sys.stderr)
        return 1


def _read_health(state_dir: Path) -> tuple[str | None, str | None]:
    """Top-level ``(status, reason)`` from acceptance_health.json, or
    ``(None, None)`` when the file is absent or unreadable — a missing
    health file is "unknown", never a failure."""
    path = state_dir / "acceptance_health.json"
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    status = data.get("status")
    reason = data.get("reason")
    if not isinstance(status, str):
        return None, None
    return status, reason if isinstance(reason, str) else None


def cmd_status(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = _config_or_refuse(base_dir)
    if config is None:
        return 1
    last_run_path = config.state_dir / "last_run.json"
    if not last_run_path.is_file():
        print("acceptance-ledger: no run recorded yet")
        return 1
    payload = json.loads(last_run_path.read_text())
    print(f"acceptance-ledger status ({base_dir})")
    print(f"  entries: {payload['entry_count']}")
    print(f"  inconsistent: {payload['inconsistent_count']}")
    print(f"  barren: {payload.get('barren_count', 0)}")
    print(f"  errored: {payload.get('error_count', 0)}")
    print(f"  run observed nothing: {'yes' if payload['barren'] else 'no'}")
    if payload.get("environment_error"):
        print(f"  ENVIRONMENT ERROR: {payload['environment_error']}")
    if payload.get("environment_suspect"):
        print(f"  ENVIRONMENT SUSPECT: {payload['environment_suspect']}")
    if payload.get("partial"):
        unfinished = payload.get("unfinished_ids") or []
        print(
            f"  PARTIAL RUN ({payload.get('timed_out')}): {len(unfinished)} "
            "entries not judged, kept at their previous verdict"
        )
    if payload.get("source") and payload.get("source") != "local":
        print(f"  source: {payload['source']} ({payload.get('shards')} shards)")
    if payload["inconsistent_ids"]:
        print("  divergent ids: " + ", ".join(payload["inconsistent_ids"]))
    perf = payload.get("perf_regressions")
    if isinstance(perf, dict) and perf:
        print("  perf regressions: " + ", ".join(sorted(perf)))
    near = payload.get("near_limit")
    if isinstance(near, dict) and near:
        print("  near their timeout: " + ", ".join(sorted(near)))
    if payload.get("step_budget_warning"):
        print(f"  STEP BUDGET: {payload['step_budget_warning']}")
    if payload.get("barren_ids"):
        print("  barren ids: " + ", ".join(payload["barren_ids"]))
    promotions = payload.get("promotions")
    if isinstance(promotions, dict) and promotions:
        print(
            "  required: "
            + ", ".join(
                f"{entry_id} (promotion: {how})"
                for entry_id, how in sorted(promotions.items())
            )
        )
    tier_warnings = payload.get("tier_warnings")
    if isinstance(tier_warnings, dict):
        for entry_id, warning in sorted(tier_warnings.items()):
            print(f"  tier warning: {entry_id} — {warning}")

    health_status, health_reason = _read_health(config.state_dir)
    if health_status is None:
        print("  health: unknown (no acceptance_health.json)")
    else:
        line = f"  health: {health_status}"
        if health_reason:
            line += f" ({health_reason})"
        print(line)
    # A dark or error ledger is a hard fail so an operator script or CI step
    # can key on the exit code; degraded/healthy/unknown leave it at 0.
    if health_status in HEALTH_FAILING_STATUSES:
        return 1
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = _config_or_refuse(base_dir)
    if config is None:
        return 1
    try:
        record = promote_entry(args.entry_id, config)
    except LedgerError as exc:
        print(f"acceptance-ledger: promotion refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"acceptance-ledger: promoted {args.entry_id!r} to "
        f"{TIER_REQUIRED!r} (canary catch rate "
        f"{record['canary_catch_rate']!r})"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acceptance-ledger",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the acceptance ledger for a project")
    run_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    run_parser.add_argument(
        "--junit",
        default=None,
        help="path to write JUnit XML (default: .company/state/acceptance/junit.xml)",
    )
    run_parser.add_argument(
        "--step-timeout",
        type=float,
        default=None,
        help=(
            "the run's budget in seconds: entries that no longer fit are left "
            "unfinished and the run records what it judged (default: "
            "acceptance.stepTimeoutSeconds; 0: no budget)"
        ),
    )
    run_parser.add_argument(
        "--shard",
        default=None,
        help=(
            "i/N (1-based): judge only slice i of N and write a shard result "
            "(--output) instead of last_run.json/runs.jsonl"
        ),
    )
    run_parser.add_argument(
        "--durations",
        default=None,
        help=(
            "with --shard: JSON of per-entry seconds (a last_run.json or "
            "{id: seconds}) the split is balanced on (default: runs.jsonl)"
        ),
    )
    run_parser.add_argument(
        "--output",
        default=None,
        help="with --shard: where to write the shard result",
    )
    run_parser.add_argument(
        "--local",
        action="store_true",
        help='run here even when acceptance.runner is "ci"',
    )
    run_parser.set_defaults(func=cmd_run)

    merge_parser = sub.add_parser(
        "merge",
        help="fold shard results into last_run.json/runs.jsonl as one run",
    )
    merge_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    merge_parser.add_argument(
        "--junit",
        default=None,
        help="path to write JUnit XML (default: .company/state/acceptance/junit.xml)",
    )
    merge_parser.add_argument("files", nargs="+", help="shard result files")
    merge_parser.set_defaults(func=cmd_merge)

    status_parser = sub.add_parser("status", help="print the last recorded run")
    status_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    status_parser.set_defaults(func=cmd_status)

    promote_parser = sub.add_parser(
        "promote", help="promote an entry's tier from advisory to required"
    )
    promote_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    promote_parser.add_argument(
        "--entry-id", required=True, help="id of the relation/case entry to promote"
    )
    promote_parser.set_defaults(func=cmd_promote)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
