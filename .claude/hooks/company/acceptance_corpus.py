#!/usr/bin/env python3
"""Acceptance Corpus — real-document harvesting for the acceptance ledger.

GAP-7 (`.planning/GAP7-ACCEPTANCE-LEDGER-2026-08-30.md`) increment 3, part a.
`acceptance_ledger.py` only ever runs the inputs a relation's own JSON file
carries — hand-authored fixtures. A relation can report CONSISTENT forever
while the real-world shape it claims to detect never once occurs in an actual
document, and nothing distinguishes that from a ledger whose inputs quietly
stopped exercising anything at all.

This module's ``harvest`` command builds the missing input: it walks
configured source roots (default: this project's ``docs/`` and ``README.md``),
collects files matching an extension allowlist (default ``.md``), and stores
them content-addressed by sha256 under
``.company/state/acceptance/corpus/objects/``, deduplicated by content. It
writes ``corpus/manifest.json`` recording, per unit, its repo-relative path,
sha256, size, and provenance (``external`` — harvested from the filesystem —
versus ``synthetic``, reserved for ledger-authored inputs a later step may
catalogue). Two caps bound it: a per-file byte cap (a file larger than the
cap is recorded as skipped, never read fully into memory) and a unit cap on
the number of files (excess files are recorded and the manifest is marked
truncated). Both have a hard ceiling no ``gatesmith.json`` value can
exceed.

``confirm`` (increment 3 part b) invokes the product's own CLI against this
corpus and derives two health invariants into the shared
``acceptance_health.json`` under a ``sources.corpus`` key: a run-level
**barren ratio** (the fraction of real documents that produced zero
observations — a ratio over a code-default threshold is ``degraded``, a ratio
of 1.0 is ``dark``) and **unconfirmed-detection clusters** (an observer on
which three or more relations went unconfirmed against real content). It
reports, it does not block a merge — like every increment, a ``degraded`` or
``dark`` verdict is surfaced, not enforced. ``harvest`` always exits 0 once it
has written a manifest; ``confirm`` exits 1 on ``dark``/``error`` so an
operator or CI step can key on it, matching ``gatesmith-acceptance status``.

Dependency-free by design (stdlib only). ``acceptance_ledger.py`` never
imports this module, so its "``git show`` this file from ``origin/main`` and
run it standalone" contract for the CI ledger job is untouched.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_ledger as ledger  # noqa: E402

DEFAULT_SOURCE_ROOTS: tuple[str, ...] = ("docs", "README.md")
DEFAULT_EXTENSIONS: tuple[str, ...] = (".md",)
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_UNITS = 500
# Hard ceilings regardless of what gatesmith.json requests: a trusted
# config file should still not be able to make harvest() read an unbounded
# file into memory or walk an unbounded number of files.
MAX_ALLOWED_FILE_BYTES = 50_000_000
MAX_ALLOWED_UNITS = 10_000

# Provenance of a corpus unit. "external" is a real file harvested from the
# filesystem; "synthetic" is reserved for ledger-authored inputs (a relation's
# own fixtures) that a later step may catalogue alongside the harvested ones,
# so a confirm pass can weight real-world coverage against toy coverage.
PROVENANCE_EXTERNAL = "external"
PROVENANCE_SYNTHETIC = "synthetic"

# confirm() health statuses, written under ``sources.corpus`` in the shared
# acceptance_health.json. Same four as acceptance_canary.HEALTH_PRECEDENCE, worst
# first: "error" (the tool could not be invoked — no evidence, not even silence)
# beats "dark" (the tool ran and said nothing on everything) beats "degraded" (a
# quality warning: real documents exercise too little) beats "healthy". A "dark"
# or "error" status makes `gatesmith-acceptance status` exit 1 (see #612); "degraded"
# does not.
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_DARK = "dark"
STATUS_ERROR = "error"
_STATUS_SEVERITY = {
    STATUS_HEALTHY: 0,
    STATUS_DEGRADED: 1,
    STATUS_DARK: 2,
    STATUS_ERROR: 3,
}
HEALTH_SOURCE = "corpus"
LOCK_TIMEOUT_SECONDS = 15

# A run-level barren ratio above this (but below 1.0) is a quality warning:
# real documents exercised too few of the relations' observers. A ratio of
# exactly 1.0 — every file silent — is "dark", not "degraded": the tool said
# nothing about anything, which cannot be told apart from a broken detector.
DEFAULT_BARREN_RATIO_THRESHOLD = 0.5

# An "unconfirmed detection cluster" is an observer (`observe`) on which three
# or more relation entries went unconfirmed — i.e. the corpus never exercised
# what that family of relations claims to detect (see _detect_clusters, which
# groups by observer). One unconfirmed entry is not a cluster (real docs may
# simply lack that one shape); a whole family unconfirmed together is a signal
# the corpus does not cover what the ledger claims to catch.
CLUSTER_MIN = 3


class CorpusError(Exception):
    """Raised for a misconfigured corpus or a harvest that cannot proceed."""


@dataclass(frozen=True)
class CorpusConfig:
    """Anchors every path off ``base_dir`` — no cwd-relative defaults.

    Test isolation is a single ``base_dir=tmp_path`` override (per CLAUDE.md's
    test-isolation rule for path-defaulting configs), matching
    ``acceptance_ledger.LedgerConfig``.
    """

    base_dir: Path
    corpus_dir: Path
    health_path: Path | None = None
    source_roots: tuple[str, ...] = DEFAULT_SOURCE_ROOTS
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_units: int = DEFAULT_MAX_UNITS
    barren_ratio_threshold: float = DEFAULT_BARREN_RATIO_THRESHOLD

    @classmethod
    def for_project(cls, base_dir: Path) -> "CorpusConfig":
        base_dir = base_dir.resolve()
        state_dir = base_dir / ".company" / "state" / "acceptance"
        roots, extensions, max_file_bytes, max_units = _load_corpus_settings(base_dir)
        return cls(
            base_dir=base_dir,
            corpus_dir=state_dir / "corpus",
            health_path=state_dir / "acceptance_health.json",
            source_roots=roots,
            extensions=extensions,
            max_file_bytes=max_file_bytes,
            max_units=max_units,
            barren_ratio_threshold=_load_barren_ratio_threshold(base_dir),
        )


def _load_corpus_settings(
    base_dir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], int, int]:
    """Read the corpus block of gatesmith.json, falling back to code
    defaults for an absent, malformed, or out-of-range value (mirrors
    ``acceptance_ledger._load_allowed_binaries``). gatesmith.json is
    human-protected, so the defaults ship in code, not in the checked-in file.
    """
    config_path = base_dir / "gatesmith.json"
    if not config_path.is_file():
        return (
            DEFAULT_SOURCE_ROOTS,
            DEFAULT_EXTENSIONS,
            DEFAULT_MAX_FILE_BYTES,
            DEFAULT_MAX_UNITS,
        )
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return (
            DEFAULT_SOURCE_ROOTS,
            DEFAULT_EXTENSIONS,
            DEFAULT_MAX_FILE_BYTES,
            DEFAULT_MAX_UNITS,
        )
    acceptance = data.get("acceptance", {})
    if not isinstance(acceptance, dict):
        acceptance = {}

    roots = acceptance.get("corpusSourceRoots")
    if not (
        isinstance(roots, list) and roots and all(isinstance(r, str) for r in roots)
    ):
        roots = list(DEFAULT_SOURCE_ROOTS)

    extensions = acceptance.get("corpusExtensions")
    if not (
        isinstance(extensions, list)
        and extensions
        and all(isinstance(e, str) for e in extensions)
    ):
        extensions = list(DEFAULT_EXTENSIONS)

    max_bytes = acceptance.get("corpusMaxFileBytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        max_bytes = DEFAULT_MAX_FILE_BYTES
    max_bytes = min(max_bytes, MAX_ALLOWED_FILE_BYTES)

    max_units = acceptance.get("corpusMaxUnits")
    if not isinstance(max_units, int) or isinstance(max_units, bool) or max_units <= 0:
        max_units = DEFAULT_MAX_UNITS
    max_units = min(max_units, MAX_ALLOWED_UNITS)

    return tuple(roots), tuple(extensions), max_bytes, max_units


def _load_barren_ratio_threshold(base_dir: Path) -> float:
    """The ``acceptance.corpusBarrenRatioThreshold`` in gatesmith.json,
    clamped to [0, 1], falling back to the code default for an absent,
    malformed, or out-of-range value (gatesmith.json is human-protected)."""
    config_path = base_dir / "gatesmith.json"
    if not config_path.is_file():
        return DEFAULT_BARREN_RATIO_THRESHOLD
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_BARREN_RATIO_THRESHOLD
    acceptance = data.get("acceptance", {})
    if not isinstance(acceptance, dict):
        return DEFAULT_BARREN_RATIO_THRESHOLD
    value = acceptance.get("corpusBarrenRatioThreshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_BARREN_RATIO_THRESHOLD
    if value < 0 or value > 1:
        return DEFAULT_BARREN_RATIO_THRESHOLD
    return float(value)


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------


def _contained(anchor: Path, target: Path) -> bool:
    """True if ``target`` resolves to a path inside ``anchor``.

    Resolve then check ``relative_to``, so a symlink planted inside ``anchor``
    that points outside it is caught the same way a literal ``..`` would be.
    """
    try:
        target.resolve().relative_to(anchor.resolve())
        return True
    except ValueError:
        return False


def _iter_candidate_files(config: CorpusConfig) -> list[Path]:
    """Every real file under the configured source roots matching an extension.

    Roots and every file found are containment-checked against ``base_dir``
    even though roots come from ``gatesmith.json`` (trusted config, not
    request input) — a symlink *under* a root pointing outside the repo must
    not be harvested either, and ``os.walk(followlinks=False)`` alone does not
    stop a root itself from being a symlink escape.
    """
    found: list[Path] = []
    for root_name in config.source_roots:
        root = (config.base_dir / root_name).resolve()
        if not _contained(config.base_dir, root):
            continue
        if root.is_file():
            if root.suffix in config.extensions:
                found.append(root)
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(dirnames)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.suffix not in config.extensions:
                    continue
                if not _contained(config.base_dir, path):
                    continue
                found.append(path)
    # Stable order regardless of root iteration, and de-duplicated in case two
    # configured roots overlap (e.g. a root and a file beneath it). The unit
    # cap is applied to this sorted order in harvest(), so which files are kept
    # under a cap is deterministic, not iteration-order dependent.
    return sorted(set(found))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_object(corpus_dir: Path, digest: str, data: bytes) -> Path:
    object_path = corpus_dir / "objects" / digest[:2] / digest
    if object_path.is_file():
        return object_path
    object_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(object_path.parent), prefix=digest + "_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, object_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return object_path


@dataclass
class HarvestReport:
    harvested_at: float
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped_too_large: list[str] = field(default_factory=list)
    skipped_over_unit_cap: list[str] = field(default_factory=list)
    skipped_unreadable: list[str] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    truncated: bool = False

    def to_dict(
        self, base_dir: Path, source_roots: tuple[str, ...], caps: dict[str, int]
    ) -> dict[str, Any]:
        return {
            "harvested_at": self.harvested_at,
            "source_roots": list(source_roots),
            "project_dir": str(base_dir),
            "caps": caps,
            "truncated": self.truncated,
            "files": self.files,
            "skipped_too_large": self.skipped_too_large,
            "skipped_over_unit_cap": self.skipped_over_unit_cap,
            "skipped_unreadable": self.skipped_unreadable,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
        }


def harvest(config: CorpusConfig) -> HarvestReport:
    """Walk the configured source roots and populate the content-addressed corpus.

    Idempotent and incremental: an unchanged file re-hashes to the same object
    and is not rewritten; a changed file is stored under its new hash, and the
    old object is left in place (garbage collection is out of scope for this
    increment — the store only ever grows).

    Two caps bound the work. A file over ``max_file_bytes`` is recorded in
    ``skipped_too_large`` via a ``stat()`` size-gate before any read (never
    pulled into memory) and never stored. Once ``max_units`` files have been
    harvested, every remaining candidate is recorded in ``skipped_over_unit_cap``
    and the manifest is marked ``truncated`` — the cap is applied to the sorted
    candidate order so the kept set is deterministic.
    """
    report = HarvestReport(harvested_at=time.time())
    for path in _iter_candidate_files(config):
        rel = str(path.relative_to(config.base_dir))
        if report.total_files >= config.max_units:
            report.skipped_over_unit_cap.append(rel)
            report.truncated = True
            continue
        # Size-gate on stat() BEFORE reading, so an over-cap file is never
        # pulled into memory (the property the docstring and docs promise;
        # the old order read the whole file first, then measured it).
        try:
            size = path.stat().st_size
        except OSError:
            report.skipped_unreadable.append(rel)
            continue
        if size > config.max_file_bytes:
            report.skipped_too_large.append(rel)
            continue
        try:
            data = path.read_bytes()
        except OSError:
            report.skipped_unreadable.append(rel)
            continue
        # Backstop: a file that grew between the stat and the read, or a stat
        # that under-reported, must still not slip past the cap.
        if len(data) > config.max_file_bytes:
            report.skipped_too_large.append(rel)
            continue
        digest = _sha256_bytes(data)
        _store_object(config.corpus_dir, digest, data)
        report.files.append(
            {
                "path": rel,
                "sha256": digest,
                "size": len(data),
                "provenance": PROVENANCE_EXTERNAL,
            }
        )
        report.total_files += 1
        report.total_bytes += len(data)

    caps = {
        "max_file_bytes": config.max_file_bytes,
        "max_units": config.max_units,
    }
    manifest_path = config.corpus_dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(report.to_dict(config.base_dir, config.source_roots, caps), indent=2)
        + "\n"
    )
    return report


def load_corpus_manifest(config: CorpusConfig) -> dict[str, Any] | None:
    manifest_path = config.corpus_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_object(config: CorpusConfig, digest: str) -> bytes:
    """The stored bytes for a harvested unit, by its sha256 digest."""
    object_path = config.corpus_dir / "objects" / digest[:2] / digest
    return object_path.read_bytes()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Confirm — cross-reference relation filters against real-document observations
# --------------------------------------------------------------------------


@dataclass
class ConfirmReport:
    status: str
    total_files: int
    barren_files: int
    barren_ratio: float
    confirmed: list[str]
    unconfirmed: list[str]
    clusters: list[dict[str, Any]]
    errored_files: list[dict[str, str]]
    reason: str
    generated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "total_files": self.total_files,
            "barren_files": self.barren_files,
            "barren_ratio": self.barren_ratio,
            "confirmed": sorted(self.confirmed),
            "unconfirmed": sorted(self.unconfirmed),
            "unconfirmed_detection_clusters": self.clusters,
            "errored_files": self.errored_files,
            "generated_at": self.generated_at,
        }


def _relation_targets(ledger_dir: Path) -> list[dict[str, Any]]:
    """Relations that declare a ``filter`` and an ``observe`` — the only
    entries a real document can confirm or fail to confirm. Entries with no
    ``filter`` (determinism, contract_shape, no_crash, ...) assert structural
    properties, not that a specific rule fires on real content."""
    targets = []
    for entry in ledger.load_entries(ledger_dir, "relations"):
        if "_load_error" in entry:
            continue
        # The filter must be a scalar rule id (a string): it is matched against
        # a set of observed rule values, and a non-scalar (e.g. a list) is both
        # unhashable — it would raise TypeError on the membership test and take
        # the whole run down, which cmd_confirm does not catch — and not a thing
        # a real document can confirm. A malformed entry is skipped here, not
        # crashed on, matching load_entries' own one-bad-entry tolerance.
        if isinstance(entry.get("filter"), str) and entry.get("observe") is not None:
            targets.append(entry)
    return targets


def _observer_key_fields(manifest: dict[str, Any], observer_name: str) -> list[str]:
    """The observer's declared ``key`` fields; empty for a keyless observer
    (``exit_code``) or a malformed ``key``."""
    observer = manifest.get("observers", {}).get(observer_name)
    if observer is None:
        return []
    key = observer.get("key")
    if isinstance(key, list) and key and all(isinstance(f, str) for f in key):
        return list(key)
    return []


def _invoke_corpus_file(
    ledger_config: ledger.LedgerConfig,
    manifest: dict[str, Any],
    content: str,
    filename: str,
) -> ledger.InvokeResult:
    assert ledger_config.tmp_root is not None
    workdir, input_path = ledger.materialize_workdir(
        ledger_config.tmp_root, None, filename, content
    )
    timeout_seconds = min(
        manifest.get("timeoutSeconds", ledger.DEFAULT_TIMEOUT_SECONDS),
        ledger.MAX_TIMEOUT_SECONDS,
    )
    return ledger.run_invoke(
        manifest["invoke"],
        workdir,
        input_path,
        workdir,
        timeout_seconds,
        ledger_config.allowed_invoke_binaries,
        ledger_config.base_dir,
    )


def _detect_clusters(
    targets: list[dict[str, Any]], unconfirmed_ids: set[str]
) -> list[dict[str, Any]]:
    """An unconfirmed-detection cluster is an observer (``observe``) for which
    ``CLUSTER_MIN`` or more relation entries went unconfirmed. One unconfirmed
    entry is not a cluster — a real corpus may simply lack that one shape; a
    whole family unconfirmed together says the corpus does not exercise what
    the ledger claims to detect on that observer."""
    by_observer: dict[str, list[str]] = {}
    for entry in targets:
        if entry["id"] in unconfirmed_ids:
            by_observer.setdefault(entry["observe"], []).append(entry["id"])
    clusters = []
    for observer_name, ids in sorted(by_observer.items()):
        if len(ids) >= CLUSTER_MIN:
            clusters.append({"observer": observer_name, "entry_ids": sorted(ids)})
    return clusters


def confirm(config: CorpusConfig, ledger_dir: Path) -> ConfirmReport:
    """Run the product's own CLI against every harvested file and derive two
    health invariants: a run-level barren ratio and unconfirmed-detection
    clusters.

    Reuses ``acceptance_ledger``'s own invocation path (``run_invoke``,
    ``materialize_workdir``), so corpus invocations are bound by the exact
    same allowlist and minimal environment as every ledger-driven invocation
    — nothing here loosens that boundary. A broken invocation (bad binary,
    disallowed argv[0], an unreadable object) is recorded in ``errored_files``
    and forces ``error``; it is never folded into "barren", because a tool
    that could not run and a tool that ran and found nothing are different
    facts, and conflating them is the failure this ledger exists to catch.
    """
    manifest = ledger.load_manifest(ledger_dir)
    targets = _relation_targets(ledger_dir)
    corpus_manifest = load_corpus_manifest(config)
    now = time.time()

    if not corpus_manifest or not corpus_manifest.get("files"):
        return ConfirmReport(
            status=STATUS_DARK,
            total_files=0,
            barren_files=0,
            barren_ratio=1.0,
            confirmed=[],
            unconfirmed=[entry["id"] for entry in targets],
            clusters=[],
            errored_files=[],
            reason="corpus is empty — run `gatesmith-acceptance harvest` first",
            generated_at=now,
        )

    ledger_config = ledger.LedgerConfig.for_project(config.base_dir)
    observed_by_observer: dict[str, set[Any]] = {}
    errored_files: list[dict[str, str]] = []
    files = corpus_manifest["files"]
    barren_files = 0

    with tempfile.TemporaryDirectory(prefix="acceptance-corpus-") as tmp_root:
        run_tmp_root = Path(tmp_root)
        for file_entry in files:
            try:
                data = read_object(config, file_entry["sha256"])
                content = data.decode("utf-8", errors="replace")
            except OSError as exc:
                errored_files.append({"path": file_entry["path"], "error": str(exc)})
                continue
            filename = Path(file_entry["path"]).name or "input.md"
            ledger_config_run = ledger.LedgerConfig(
                base_dir=ledger_config.base_dir,
                ledger_dir=ledger_config.ledger_dir,
                state_dir=ledger_config.state_dir,
                allowed_invoke_binaries=ledger_config.allowed_invoke_binaries,
                tmp_root=run_tmp_root,
            )
            try:
                result = _invoke_corpus_file(
                    ledger_config_run, manifest, content, filename
                )
            except ledger.LedgerError as exc:
                errored_files.append({"path": file_entry["path"], "error": str(exc)})
                continue
            # A file is barren when it produced no DETECTION evidence. Count
            # only keyed observations (a `key` projection, e.g. a rule id),
            # never a keyless observer like `exit_code` — which the shipped
            # text_linter archetype declares and which yields an observation on
            # every run, clean or not. Counting it would pin barren_ratio at
            # 0.0 forever and make `dark` unreachable, exactly the "count
            # post-filter evidence, not pre-filter" rule acceptance_ledger.
            # filter_and_project documents. One stderr line or a bare exit 0 is
            # not a detection and must leave the file barren.
            #
            # An observation is detection evidence when it carries ALL of the
            # observer's declared key fields, so a composite-key observer
            # (`["file", "rule"]`) counts too. Only the confirmation bucket
            # needs a single-field key: acceptance_ledger refuses a `filter` on
            # a multi-field observer, so such an observer is unconfirmable —
            # but a tool detecting on every document must not read as silent.
            file_obs = 0
            for observer_name, observer in manifest.get("observers", {}).items():
                key_fields = _observer_key_fields(manifest, observer_name)
                if not key_fields:
                    continue
                observations = ledger.apply_observer(observer, result)
                matched = [
                    obs for obs in observations if all(f in obs for f in key_fields)
                ]
                file_obs += len(matched)
                if len(key_fields) != 1:
                    continue
                key_field = key_fields[0]
                bucket = observed_by_observer.setdefault(observer_name, set())
                for obs in matched:
                    bucket.add(obs.get(key_field))
            if file_obs == 0:
                barren_files += 1

    total_files = len(files)
    barren_ratio = barren_files / total_files if total_files else 1.0

    confirmed: list[str] = []
    unconfirmed: list[str] = []
    for entry in targets:
        observed_values = observed_by_observer.get(entry["observe"], set())
        if entry["filter"] in observed_values:
            confirmed.append(entry["id"])
        else:
            unconfirmed.append(entry["id"])
    clusters = _detect_clusters(targets, set(unconfirmed))

    if errored_files:
        status = STATUS_ERROR
        reason = (
            f"{len(errored_files)} corpus file(s) could not be invoked — "
            "the tool never ran, which is not the same as finding nothing"
        )
    elif barren_files == total_files:
        status = STATUS_DARK
        reason = (
            f"every one of {total_files} real documents produced zero "
            "detection observations — the tool cannot be told apart from one "
            "that stopped detecting anything"
        )
    elif barren_ratio > config.barren_ratio_threshold or clusters:
        status = STATUS_DEGRADED
        bits = []
        if barren_ratio > config.barren_ratio_threshold:
            bits.append(
                f"barren ratio {barren_ratio:.2f} over threshold "
                f"{config.barren_ratio_threshold:.2f}"
            )
        if clusters:
            names = ", ".join(c["observer"] for c in clusters)
            bits.append(f"unconfirmed detection cluster(s) on {names}")
        reason = "; ".join(bits)
    else:
        status = STATUS_HEALTHY
        reason = (
            f"{total_files - barren_files} of {total_files} real documents "
            "produced detection observations; no unconfirmed detection cluster"
        )

    return ConfirmReport(
        status=status,
        total_files=total_files,
        barren_files=barren_files,
        barren_ratio=barren_ratio,
        confirmed=confirmed,
        unconfirmed=unconfirmed,
        clusters=clusters,
        errored_files=errored_files,
        reason=reason,
        generated_at=now,
    )


# --------------------------------------------------------------------------
# Shared, multi-producer acceptance_health.json
# --------------------------------------------------------------------------


class _HealthFileLock:
    """Cross-process ``fcntl.flock`` on a sibling ``.lock`` file.

    Guards the read-merge-write of ``acceptance_health.json``: the mutant
    canary (``acceptance_canary.py``) writes the same file under its own
    ``sources.canary`` key, so a read-merge-write here must not lose that
    producer's key under a concurrent run. Mirrors ``work_allocator.QueueLock``'s
    cross-process file-lock idiom.
    """

    def __init__(self, lock_path: Path, timeout: int = LOCK_TIMEOUT_SECONDS):
        self.lock_path = lock_path
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "_HealthFileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    self._handle.close()
                    raise TimeoutError(
                        f"could not acquire lock on {self.lock_path} within "
                        f"{self.timeout}s"
                    ) from exc
                time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


def merge_health_source(
    health_path: Path, source_name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Merge ``payload`` under ``sources.<source_name>`` in the shared health
    file and recompute the top-level status as the worst of every source, using
    the same severity order as ``acceptance_canary.HEALTH_PRECEDENCE`` (error >
    dark > degraded > healthy). Written atomically (mkstemp + os.replace) so a
    reader never sees a torn file, and the flock serialises concurrent corpus
    writers. Note: ``acceptance_canary.write_health_json`` currently writes this
    file with a plain ``write_text`` and no lock, so a canary run racing a
    corpus run could still clobber ``sources.corpus`` — giving the canary this
    same lock is a tracked follow-up."""
    lock_path = health_path.parent / (health_path.name + ".lock")
    with _HealthFileLock(lock_path):
        if health_path.is_file():
            try:
                doc = json.loads(health_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                doc = {}
        else:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        if not isinstance(doc.get("sources"), dict):
            doc["sources"] = {}
        doc["sources"][source_name] = payload
        worst_status = STATUS_HEALTHY
        worst_reason = None
        worst_sev = -1
        for name, src in doc["sources"].items():
            if not isinstance(src, dict):
                continue
            sev = _STATUS_SEVERITY.get(src.get("status"), 0)
            if sev > worst_sev:
                worst_sev = sev
                worst_status = src.get("status", STATUS_HEALTHY)
                src_reason = src.get("reason")
                worst_reason = f"{name}: {src_reason}" if src_reason else None
        doc["status"] = worst_status
        doc["reason"] = worst_reason
        doc["updated_at"] = time.time()

        health_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix=health_path.stem + "_", dir=str(health_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, health_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return doc


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_harvest(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = CorpusConfig.for_project(base_dir)
    report = harvest(config)
    print(f"acceptance-corpus harvest ({base_dir})")
    print(f"  harvested: {report.total_files} files, {report.total_bytes} bytes")
    if report.skipped_too_large:
        print(f"  skipped (too large): {len(report.skipped_too_large)}")
    if report.skipped_over_unit_cap:
        print(
            f"  skipped (over unit cap {config.max_units}): "
            f"{len(report.skipped_over_unit_cap)}"
        )
    if report.skipped_unreadable:
        print(f"  skipped (unreadable): {len(report.skipped_unreadable)}")
    print(f"  manifest: {config.corpus_dir / 'manifest.json'}")
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = CorpusConfig.for_project(base_dir)
    ledger_dir = base_dir / "acceptance"
    try:
        report = confirm(config, ledger_dir)
    except ledger.LedgerError as exc:
        print(f"acceptance-corpus: confirm could not run: {exc}", file=sys.stderr)
        return 1
    assert config.health_path is not None
    merge_health_source(config.health_path, HEALTH_SOURCE, report.to_dict())
    print(f"acceptance-corpus confirm ({base_dir})")
    print(f"  status: {report.status} ({report.reason})")
    print(
        f"  barren: {report.barren_files}/{report.total_files} files "
        f"(ratio {report.barren_ratio:.2f}, threshold "
        f"{config.barren_ratio_threshold:.2f})"
    )
    print(
        f"  confirmed: {len(report.confirmed)}  unconfirmed: {len(report.unconfirmed)}"
    )
    if report.clusters:
        for c in report.clusters:
            print(f"  cluster: {c['observer']} — {', '.join(c['entry_ids'])}")
    if report.errored_files:
        print(f"  errored files: {len(report.errored_files)}")
    print(f"  health: {config.health_path}")
    # error/dark is a hard fail an operator or CI step can key on, matching
    # `gatesmith-acceptance status`; degraded/healthy leave the exit code at 0.
    return 1 if report.status in (STATUS_ERROR, STATUS_DARK) else 0


def cmd_corpus_status(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = CorpusConfig.for_project(base_dir)
    manifest = load_corpus_manifest(config)
    if manifest is None:
        print("acceptance-corpus: no corpus harvested yet")
        return 1
    print(f"acceptance-corpus status ({base_dir})")
    print(f"  harvested files: {manifest.get('total_files', 0)}")
    print(f"  total bytes: {manifest.get('total_bytes', 0)}")
    print(f"  truncated: {'yes' if manifest.get('truncated') else 'no'}")
    assert config.health_path is not None
    if config.health_path.is_file():
        try:
            health = json.loads(config.health_path.read_text())
        except (OSError, json.JSONDecodeError):
            health = {}
        src = (health.get("sources") or {}).get(HEALTH_SOURCE)
        if isinstance(src, dict):
            print(f"  last confirm: {src.get('status')} ({src.get('reason')})")
        else:
            print("  last confirm: none recorded")
    else:
        print("  last confirm: none recorded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acceptance-corpus",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    harvest_parser = sub.add_parser(
        "harvest", help="populate the content-addressed corpus from source roots"
    )
    harvest_parser.add_argument(
        "--project-dir", default=".", help="project root to harvest from"
    )
    harvest_parser.set_defaults(func=cmd_harvest)

    confirm_parser = sub.add_parser(
        "confirm",
        help="run the artifact against the corpus; write the barren-ratio and "
        "unconfirmed-cluster invariants to acceptance_health.json",
    )
    confirm_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    confirm_parser.set_defaults(func=cmd_confirm)

    status_parser = sub.add_parser(
        "corpus-status", help="print the harvested corpus summary and last confirm"
    )
    status_parser.add_argument(
        "--project-dir", default=".", help="project root to report on"
    )
    status_parser.set_defaults(func=cmd_corpus_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
