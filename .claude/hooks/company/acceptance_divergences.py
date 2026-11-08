#!/usr/bin/env python3
"""Acceptance Ledger divergence register — what has been failing, and since when.

GAP-7 (`.planning/GAP7-ACCEPTANCE-LEDGER-2026-08-30.md`, increment 5a). A ledger
run leaves two records: ``last_run.json`` (the ids of the entries that came out
INCONSISTENT, BARREN or ERROR) and one appended line in ``runs.jsonl`` (the ids
of the entries that came out CONSISTENT). Neither remembers the run before it,
so the fact that an entry has been failing since a given day is gone the moment
the next run overwrites or outgrows it.

This module folds those two records into ``divergences.json``, one record per
entry id, and writes nothing else. A record is ``open``, ``repaired`` or
``withdrawn``:

* an id in the run's ``inconsistent_ids`` becomes an open record, or stays one:
  ``first_seen`` is kept, ``last_seen``, ``commit`` and the entry's ``tier``,
  ``authority`` and ``spec_cite`` are refreshed from the entry file. A record
  that was repaired or withdrawn starts over with a new ``first_seen``;
* an open record whose id is among the run's passing ids becomes ``repaired``,
  stamped with the run's time and commit;
* a record whose entry file is no longer under ``acceptance/`` becomes
  ``withdrawn``;
* barren and errored ids open nothing and repair nothing: an entry that could
  not judge has not diverged, and has not stopped diverging either;
* a run flagged ``environment_error`` or ``environment_suspect`` by the ledger
  (``acceptance_ledger.environment_flag``: the dependency install failed, or
  the browser entries all fell over at once) is not folded at all: it opens,
  repairs and counts nothing, and the next fold compares against the run
  before it.

A new regression is re-run once, alone, by the ledger run itself before it
writes its records (``acceptance_ledger._confirm_new_regressions``). An entry
whose re-run passed is in the run's ``flaky_ids`` and among its passing ids, so
it opens nothing and repairs an open record like any pass; the fold counts it in
the register's ``flakes`` map (``count``, ``first_seen``, ``last_seen``,
``commit``, ``detail``: the first run's account) and appends one line to
``state/autofill_brief_skips.jsonl`` per flake per run.

A new regression the ledger had no time to re-run is in the run's
``unconfirmed_ids``. One failure is not evidence (DIVERGENCE-UNCONFIRMED-NOT-
OPENED: webapp minted repair tasks, and got PRs, for entries that passed on the
runs before and after), so the fold opens nothing for it: it records an
``unconfirmed`` marker (``first_seen``, ``last_seen``, ``commit``) and one skip
line. The rule for opening is therefore: **a confirmed failure (its isolated
re-run failed too), or a failure on two consecutive folded runs.** The second
half needs no counter: an entry that failed on the previous run did not pass
on it, so it is not a new regression on this one, is never re-run, and is never
unconfirmed — its INCONSISTENT verdict opens a record as any verdict does, with
``first_seen`` taken from the marker. An unconfirmed id with a record already
open is left as it is, and a marker is dropped on any run where the id is not
unconfirmed again. So that an entry failing unconfirmed every other run cannot
hide forever, each unconfirmed failure is also counted (``unconfirmed_recent``)
with the flakes: the third of them inside the window opens an ``intermittent``
record. One pass does not repair an intermittent record; a pass with the window
gone quiet does, and so do ``INTERMITTENT_REPAIR_PASSES`` consecutive clean
passes on main since its ``last_seen`` (read from ``runs.jsonl``), which also
clears its failure window (DIVERGENCE-FOLD-REPAIRED).

A confirmed, repeated failure still opens nothing when the entry has never
been CONSISTENT on main in ANY recorded run (NO-DIVERGENCE-FOR-UNBUILT-
ENTRIES: webapp minted six repair tasks/PRs for entries naming an unbuilt
feature -- INVITE-* before its goal merged, CONTACT-CARD-* for a goal never
built -- and the daemon's queue auto-fill starved for ~40h while those PRs sat
open). A divergence means "regressed from passing"; an entry that has never
passed is a goal gap, owned by its goal's own queue-fill task, not a
divergence. ``runs_ever_passing`` reads the full ``runs.jsonl`` history (every
run's ``passing_ids``, environment-flagged runs excluded, same as
``runs_previous_passing``) and the fold refuses to OPEN a new record for an id
outside that set; an id already open (a real, earlier-confirmed regression)
keeps tracking regardless -- this only gates the transition into ``open``.

Times are the run's own (``last_run.json``'s ``generated_at``), not the clock at
update time, so folding the same run twice writes the same record. A run older
than the last one folded is not folded.

This records; it blocks nothing and changes no verdict, tier or exit code that a
gate reads. ``divergences.json`` is what the backlog signal and the goal
annotation of increment 5b read (``read_register``, ``open_records``). The CLI
(``divergences``, the daemon's sub-step) also drops, after the fold, any still
QUEUED repair task whose record is no longer open
(``backlog_generator.retire_resolved_divergence_tasks``); ``update`` itself
writes only the register and the skip lines.

Dependency-free apart from ``acceptance_ledger`` (stdlib beyond that). Every
path is derived from ``DivergenceConfig.base_dir`` — no cwd-relative defaults —
so test isolation is one ``base_dir=tmp_path`` override.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import math
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_ledger as ledger  # noqa: E402

STATUS_OPEN = "open"
STATUS_REPAIRED = "repaired"
STATUS_WITHDRAWN = "withdrawn"
_STATUSES = (STATUS_OPEN, STATUS_REPAIRED, STATUS_WITHDRAWN)

REGISTER_VERSION = 1
LOCK_TIMEOUT_SECONDS = 15

# last_run.json and the runs.jsonl line are written one after the other by the
# same ``run``, milliseconds apart. A runs.jsonl line further away than this is
# from a different run (``record_run`` swallows a write error, so the last line
# can be a previous run's), and its passing ids would repair records on evidence
# from the wrong run.
RUN_CORRELATION_WINDOW_SECONDS = 60.0

# A run stamped later than this past the clock at update time is refused. Folded,
# its timestamp would become the register's high-water mark, and every real run
# after it would read as older and be skipped.
FUTURE_RUN_SLACK_SECONDS = 3600.0

_ENTRY_SUBDIRS = ("relations", "cases")


class DivergenceError(Exception):
    """Raised when the run records or the ledger cannot be folded; nothing is written."""


@dataclass(frozen=True)
class DivergenceConfig:
    """Anchors every path off ``base_dir`` — no cwd-relative defaults.

    Test isolation is a single ``base_dir=tmp_path`` override (per CLAUDE.md's
    test-isolation rule for path-defaulting configs). The paths are the ones
    ``acceptance_ledger.LedgerConfig.for_project`` derives from the same
    directory.
    """

    base_dir: Path

    @classmethod
    def for_project(cls, base_dir: Path) -> "DivergenceConfig":
        return cls(base_dir=base_dir.resolve())

    @property
    def ledger_dir(self) -> Path:
        return self.base_dir / "acceptance"

    @property
    def state_dir(self) -> Path:
        return self.base_dir / ".company" / "state" / "acceptance"

    @property
    def last_run_path(self) -> Path:
        return self.state_dir / "last_run.json"

    @property
    def runs_path(self) -> Path:
        return self.state_dir / "runs.jsonl"

    @property
    def divergences_path(self) -> Path:
        return self.state_dir / "divergences.json"

    @property
    def junit_path(self) -> Path:
        """Where ``gatesmith-acceptance run`` writes JUnit XML unless ``--junit`` says otherwise."""
        return self.state_dir / "junit.xml"


@dataclass(frozen=True)
class RunSummary:
    """The one run being folded, from ``last_run.json`` plus its ``runs.jsonl`` line."""

    timestamp: float
    commit: str | None
    inconsistent_ids: tuple[str, ...]
    passing_ids: frozenset[str]
    # Entry id -> the first run's account, for new regressions whose isolated
    # re-run passed. Absent from runs written before the re-run existed.
    flakes: dict[str, str] = field(default_factory=dict)
    # Entry id -> the ERROR's account, from ``error_ids`` (and ``errors`` where
    # the run wrote it).
    errors: dict[str, str] = field(default_factory=dict)
    # Why the run says nothing about the product (``environment_flag``); a
    # run with this set is not folded.
    environment: str | None = None
    # New regressions the ledger did not re-run (``unconfirmed_ids``): a
    # single failure, which opens nothing. Absent from older runs.
    unconfirmed: frozenset[str] = frozenset()
    # A partial run's unfinished entries that passed before it
    # (``stale_passing_ids``, LEDGER-STEP-TIMEOUT-SCALES-WITH-ENTRIES): they
    # keep that pass for the next fold's errored-after-pass check, but repair
    # nothing on this one.
    stale_passing: frozenset[str] = frozenset()


@dataclass
class UpdateReport:
    """What one update did. ``notes`` are the things an operator should read."""

    opened: list[str] = field(default_factory=list)
    kept_open: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    flaked: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    errored_after_pass: list[str] = field(default_factory=list)
    # Confirmed, repeated failures left unopened because the entry has never
    # passed on main in any recorded run (NO-DIVERGENCE-FOR-UNBUILT-ENTRIES).
    never_passed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    register: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Reading the run records
# --------------------------------------------------------------------------


def _string_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DivergenceError(f"{what} is not a list of entry ids")
    return value


def _number(value: Any, what: str) -> float:
    # bool is an int subclass; a True timestamp is a corrupt record, not 1.0.
    # json.loads accepts NaN and Infinity, and both defeat every comparison
    # the guards below make.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise DivergenceError(f"{what} is not a finite number")
    return float(value)


def read_run(config: DivergenceConfig) -> RunSummary:
    """Read ``last_run.json`` and the last line of ``runs.jsonl`` as one run.

    Refuses, naming the file, when either is missing or unreadable, or when the
    last ``runs.jsonl`` line is not the line ``last_run.json``'s run appended.
    """
    if not config.last_run_path.is_file():
        raise DivergenceError(
            f"no last_run.json at {config.last_run_path}: run `gatesmith-acceptance run` first"
        )
    try:
        last_run = ledger._read_json_object(config.last_run_path, "last_run.json")
        run_line = ledger._last_run_record(config.runs_path)
    except ledger.LedgerError as exc:
        raise DivergenceError(str(exc)) from exc
    except ValueError as exc:  # UnicodeDecodeError: the ledger reads these as UTF-8
        raise DivergenceError(
            f"{config.last_run_path} or {config.runs_path} is not valid UTF-8 text: {exc}"
        ) from exc
    if run_line is None:
        raise DivergenceError(
            f"no run recorded in {config.runs_path}: its passing ids are needed "
            "to repair a record"
        )

    where = f"{config.last_run_path}"
    generated_at = _number(last_run.get("generated_at"), f"generated_at in {where}")
    line_at = _number(run_line.get("timestamp"), f"timestamp in {config.runs_path}")
    if abs(generated_at - line_at) > RUN_CORRELATION_WINDOW_SECONDS:
        raise DivergenceError(
            f"the last line of {config.runs_path} (t={line_at:.0f}) is not from the "
            f"run in {where} (t={generated_at:.0f}); the run's line was not "
            "recorded, so nothing is folded"
        )
    inconsistent_ids = _string_list(
        last_run.get("inconsistent_ids"), f"inconsistent_ids in {where}"
    )
    passing_ids = _string_list(
        run_line.get("passing_ids"), f"passing_ids in {config.runs_path}"
    )
    _check_counts_agree(last_run, run_line, passing_ids, config)
    commit = run_line.get("commit")
    return RunSummary(
        timestamp=generated_at,
        commit=commit if isinstance(commit, str) else None,
        inconsistent_ids=tuple(inconsistent_ids),
        passing_ids=frozenset(passing_ids),
        flakes=_flakes(last_run, where),
        errors=_errors(last_run, where),
        environment=ledger.environment_flag(last_run)
        or ledger.environment_flag(run_line),
        unconfirmed=frozenset(
            _string_list(last_run["unconfirmed_ids"], f"unconfirmed_ids in {where}")
            if "unconfirmed_ids" in last_run
            else ()
        ),
        stale_passing=frozenset(
            _string_list(
                run_line["stale_passing_ids"],
                f"stale_passing_ids in {config.runs_path}",
            )
            if "stale_passing_ids" in run_line
            else ()
        ),
    )


def _errors(last_run: dict[str, Any], where: str) -> dict[str, str]:
    """``error_ids`` with each ERROR's account; empty when absent."""
    if "error_ids" not in last_run:
        return {}
    ids = _string_list(last_run.get("error_ids"), f"error_ids in {where}")
    accounts = last_run.get("errors")
    if not isinstance(accounts, dict):
        accounts = {}
    return {
        entry_id: account if isinstance(account, str) else ""
        for entry_id in ids
        for account in [accounts.get(entry_id)]
    }


def _flakes(last_run: dict[str, Any], where: str) -> dict[str, str]:
    """``flaky_ids`` with the first run's account of each; empty when absent."""
    if "flaky_ids" not in last_run:
        return {}
    ids = _string_list(last_run.get("flaky_ids"), f"flaky_ids in {where}")
    accounts = last_run.get("flakes")
    if not isinstance(accounts, dict):
        accounts = {}
    return {
        entry_id: account if isinstance(account, str) else ""
        for entry_id in ids
        for account in [accounts.get(entry_id)]
    }


def _check_counts_agree(
    last_run: dict[str, Any],
    run_line: dict[str, Any],
    passing_ids: list[str],
    config: DivergenceConfig,
) -> None:
    """Refuse when the two records disagree about the run's size.

    The timestamp check alone pairs two runs that finished within its window;
    both records also carry counts, and they are the same run's counts only
    when they match. A count that is absent from either record is not checked.
    """
    pairs = (
        ("entry_count", last_run.get("entry_count"), run_line.get("entry_count")),
        ("consistent_count", last_run.get("consistent_count"), len(passing_ids)),
    )
    for name, from_last_run, from_runs_line in pairs:
        if isinstance(from_last_run, bool) or not isinstance(from_last_run, int):
            continue
        if isinstance(from_runs_line, bool) or not isinstance(from_runs_line, int):
            continue
        if from_last_run != from_runs_line:
            raise DivergenceError(
                f"{name} is {from_last_run} in {config.last_run_path} but "
                f"{from_runs_line} in the last line of {config.runs_path}: the "
                "two records are not from the same run, so nothing is folded"
            )


def load_entry_index(
    config: DivergenceConfig,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Entry id -> entry over every relation and case file, and the unreadable files.

    An entry file that failed to parse is indexed by ``load_entries`` under its
    file name, not the id inside it, so its real id is not known here; its path
    is returned in the second list.

    Raises when there is not one entry file: a checkout without ``acceptance/``
    would otherwise read as every entry deleted and withdraw the whole register.
    """
    index: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    try:
        for subdir in _ENTRY_SUBDIRS:
            for entry in ledger.load_entries(config.ledger_dir, subdir):
                if "_load_error" in entry:
                    unreadable.append(str(entry.get("_source", entry.get("id"))))
                entry_id = entry.get("id")
                if isinstance(entry_id, str):
                    index[entry_id] = entry
    except (ValueError, AttributeError, TypeError) as exc:
        # load_entries assumes every file holds a JSON object; one that holds a
        # list or a number raises out of the whole load.
        raise DivergenceError(
            f"an entry file under {config.ledger_dir} could not be loaded: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not index:
        raise DivergenceError(
            f"no relation or case files under {config.ledger_dir}: nothing to "
            "compare the register against, so nothing is withdrawn"
        )
    return index, unreadable


# --------------------------------------------------------------------------
# The register file
# --------------------------------------------------------------------------


def _fresh_register() -> dict[str, Any]:
    return {
        "version": REGISTER_VERSION,
        "updated_at": None,
        "last_run": None,
        "divergences": {},
        "flakes": {},
        "unconfirmed": {},
        "unconfirmed_recent": {},
        "errored_after_pass": {},
    }


def _load_register(path: Path, notes: list[str]) -> dict[str, Any]:
    """The register on disk; an unreadable one is set aside and replaced, with a note.

    A corrupt file must not stop the register from being rebuilt from the next
    run, so it is moved to ``<name>.corrupt`` (one copy, overwritten by a later
    one) and the fold starts empty. The records in it are not recoverable
    from the run records; the copy is for a human to read.
    """
    if not path.is_file():
        return _fresh_register()
    try:
        doc = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
        if not isinstance(doc, dict) or not isinstance(doc.get("divergences"), dict):
            raise ValueError("not a register object with a 'divergences' object")
    except (OSError, ValueError) as exc:
        aside = path.with_name(path.name + ".corrupt")
        try:
            os.replace(path, aside)
            kept = f"; the old file is at {aside}"
        except OSError:
            kept = ""
        notes.append(
            f"{path.name} was unreadable ({type(exc).__name__}: {exc}) and was "
            f"replaced; first-seen history started over{kept}"
        )
        return _fresh_register()
    doc["divergences"] = _wellformed_records(doc["divergences"], path.name, notes)
    return doc


def _wellformed_records(
    records: dict[str, Any], name: str, notes: list[str]
) -> dict[str, dict[str, Any]]:
    """The records that carry a known status, each stamped with its own id."""
    good: dict[str, dict[str, Any]] = {}
    for entry_id, record in records.items():
        if isinstance(record, dict) and record.get("status") in _STATUSES:
            record["id"] = entry_id
            good[entry_id] = record
        else:
            notes.append(f"dropped the malformed record for {entry_id!r} from {name}")
    return good


class _RegisterLock:
    """Cross-process ``fcntl.flock`` on a sibling ``.lock`` file.

    Same shape as ``acceptance_corpus._HealthFileLock``: two updates racing on
    ``divergences.json`` would each read the file, fold, and the later write
    would drop the earlier one's records.
    """

    def __init__(self, lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
        self.lock_path = lock_path
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "_RegisterLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self._handle.close()
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


# --------------------------------------------------------------------------
# The fold
# --------------------------------------------------------------------------


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _entry_facts(entry: dict[str, Any]) -> dict[str, str | None]:
    # ``tier`` is the entry's EFFECTIVE tier as load_entries settled it, and
    # ``promotion`` says how a required one got there: "operator" (declared in
    # the entry, for an artifact Gatesmith's canary cannot mutate), "canary"
    # ('gatesmith-acceptance promote', recorded) or "unrecorded". None on an advisory entry.
    return {
        "tier": _text_or_none(entry.get("tier")),
        "promotion": _text_or_none(entry.get("_promotion")),
        "authority": _text_or_none(entry.get("authority")),
        "spec_cite": _text_or_none(entry.get("spec_cite")),
    }


def _previous_passing(previous: Any) -> frozenset[str] | None:
    """The passing ids the previous folded run recorded; None when not recorded."""
    ids = previous.get("passing_ids") if isinstance(previous, dict) else None
    if not isinstance(ids, list):
        return None
    return frozenset(i for i in ids if isinstance(i, str))


def runs_previous_passing(runs_path: Path) -> frozenset[str]:
    """The passing ids of the latest ``runs.jsonl`` line before the last that has them.

    The last line is the run being folded (``read_run`` checks that). This is
    the fallback when the register holds no previous passing set: the first
    fold after an upgrade (a register written before ``last_run.passing_ids``
    existed) or a register that was torn and replaced. Without it, an entry
    already reading ERROR would never start an errored-after-pass streak.
    Unreadable lines are skipped; empty when no earlier line has the ids.
    """
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    lines = [line for line in lines if line.strip()]
    for line in reversed(lines[:-1]):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if ledger.environment_flag(record):
            continue
        ids = record.get("passing_ids") if isinstance(record, dict) else None
        if isinstance(ids, list):
            return frozenset(i for i in ids if isinstance(i, str))
    return frozenset()


def runs_ever_passing(runs_path: Path) -> frozenset[str]:
    """Every id that has been in some recorded run's ``passing_ids``, ever.

    Unlike ``runs_previous_passing`` (one run back, read as a fallback for the
    errored-after-pass streak), this is the FULL history in ``runs.jsonl``:
    the source of truth for NO-DIVERGENCE-FOR-UNBUILT-ENTRIES's rule that an
    entry which has never passed on main is a goal gap, not a divergence, and
    so never opens one. A run flagged ``environment_flag`` counts nothing,
    same as ``runs_previous_passing`` -- it is not product evidence. Torn or
    unreadable lines are skipped; empty when the file is missing or unreadable
    (every entry then reads as never-passed, which is the safe direction: no
    history on disk means no evidence of a pass, never evidence of one).
    """
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    ever: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or ledger.environment_flag(record):
            continue
        ids = record.get("passing_ids")
        if isinstance(ids, list):
            ever.update(i for i in ids if isinstance(i, str))
    return frozenset(ever)


@dataclass(frozen=True)
class EntryEvidence:
    """What the main runs recorded after a point in time say about one entry.

    ``latest_at``/``latest_commit``/``latest_passed`` describe the latest main
    run after ``since`` that judged the entry (None/False when there is none);
    ``consecutive_passes`` counts the clean passes at the end of that history:
    every judged run that did not pass cleanly (failed, errored, flaked)
    resets it.
    """

    latest_at: float | None = None
    latest_commit: str | None = None
    latest_passed: bool = False
    consecutive_passes: int = 0


def load_main_runs(runs_path: Path) -> list[dict[str, Any]]:
    """The ``runs.jsonl`` records that are product evidence from main, in order.

    A run on any other branch (``acceptance_ledger.MAIN_BRANCH`` is the one a
    divergence is about) or flagged by ``environment_flag`` is left out, and
    so are torn lines and lines without a finite timestamp. Empty when the
    file is missing or unreadable.
    """
    try:
        lines = runs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    runs: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or ledger.environment_flag(record):
            continue
        if record.get("branch") != ledger.MAIN_BRANCH:
            continue
        at = record.get("timestamp")
        if (
            isinstance(at, bool)
            or not isinstance(at, (int, float))
            or not math.isfinite(at)
        ):
            continue
        runs.append(record)
    return runs


def _id_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, str))


def entry_evidence(
    runs: list[dict[str, Any]], entry_id: str, since: float
) -> EntryEvidence:
    """``EntryEvidence`` for ``entry_id`` from main runs stamped after ``since``.

    ``runs`` is ``load_main_runs``'s list. A run that did not judge the entry
    (``unfinished_ids``, or a pass carried forward in ``stale_passing_ids``)
    is skipped: it neither counts nor resets. A clean pass is in
    ``passing_ids`` and not in ``flaky_ids``; any other judged run is a
    failure of some kind.
    """
    latest_at: float | None = None
    latest_commit: str | None = None
    latest_passed = False
    streak = 0
    for record in sorted(runs, key=lambda r: float(r["timestamp"])):
        at = float(record["timestamp"])
        if at <= since:
            continue
        if entry_id in _id_set(record.get("unfinished_ids")) or entry_id in _id_set(
            record.get("stale_passing_ids")
        ):
            continue
        passed = entry_id in _id_set(
            record.get("passing_ids")
        ) and entry_id not in _id_set(record.get("flaky_ids"))
        streak = streak + 1 if passed else 0
        latest_at = at
        commit = record.get("commit")
        latest_commit = commit if isinstance(commit, str) else None
        latest_passed = passed
    return EntryEvidence(latest_at, latest_commit, latest_passed, streak)


def _open_intermittent_streaks(
    register: dict[str, Any], runs_path: Path
) -> dict[str, int]:
    """Entry id -> clean main passes since ``last_seen``, per open intermittent record."""
    wanted = [
        (entry_id, _as_number(record.get("last_seen")))
        for entry_id, record in register.get("divergences", {}).items()
        if isinstance(record, dict)
        and record.get("status") == STATUS_OPEN
        and record.get("kind") == KIND_INTERMITTENT
    ]
    if not wanted:
        return {}
    runs = load_main_runs(runs_path)
    return {
        entry_id: entry_evidence(runs, entry_id, since).consecutive_passes
        for entry_id, since in wanted
    }


def fold_run(
    register: dict[str, Any],
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    now: float,
    report: UpdateReport,
    unreadable: list[str] | None = None,
    fallback_passing: frozenset[str] = frozenset(),
    ever_passed: frozenset[str] = frozenset(),
    pass_streaks: dict[str, int] | None = None,
) -> None:
    """Fold one run and the entry files into ``register['divergences']`` in place.

    ``unreadable`` are entry files that failed to load. While there are any, an
    id with no entry is not withdrawn: it may be the id inside one of them.
    ``pass_streaks`` (see ``_repair_quiet_intermittents``) lets an open
    intermittent record repair on a run of clean passes on main.
    """
    records: dict[str, dict[str, Any]] = register["divergences"]
    previous = register.get("last_run")
    previous_at = previous.get("timestamp") if isinstance(previous, dict) else None
    is_stale = (
        isinstance(previous_at, (int, float))
        and not isinstance(previous_at, bool)
        and math.isfinite(previous_at)
        and run.timestamp < previous_at
    )

    if is_stale:
        report.notes.append(
            f"the run at t={run.timestamp:.0f} is older than the one already "
            f"folded (t={previous_at:.0f}); it was not folded"
        )
    elif run.environment:
        # Not the register's ``last_run`` either: the next fold compares
        # against the last run that judged the product.
        report.notes.append(
            f"the run at t={run.timestamp:.0f} was not folded: environment "
            f"failure, not product evidence ({run.environment})"
        )
    else:
        previous_passing = _previous_passing(previous)
        if previous_passing is None:
            previous_passing = fallback_passing
        _fold_verdicts(
            records,
            run,
            entries,
            report,
            _marker_map(register, "unconfirmed"),
            # The current run's own passes count too: an id that just passed
            # for the first time here is not "never passed" for a sibling
            # check within the same fold.
            ever_passed | run.passing_ids,
        )
        _fold_flakes(register, run, entries, report)
        _fold_unconfirmed_history(register, run, entries, report)
        _repair_quiet_intermittents(register, run, report, pass_streaks)
        _fold_errored_after_pass(register, run, entries, previous_passing, report)
        register["last_run"] = {
            "timestamp": run.timestamp,
            "commit": run.commit,
            # Read by the next fold: an entry that passed here and errors there
            # starts an errored-after-pass streak.
            "passing_ids": sorted(run.passing_ids | run.stale_passing),
        }

    for entry_id, record in sorted(records.items()):
        if entry_id in entries or record["status"] == STATUS_WITHDRAWN:
            continue
        if unreadable:
            report.notes.append(
                f"{entry_id} has no readable entry file, and {len(unreadable)} entry "
                f"file(s) failed to load ({', '.join(unreadable)}); not withdrawn"
            )
            continue
        record["status"] = STATUS_WITHDRAWN
        record["withdrawn_at"] = now
        report.withdrawn.append(entry_id)
        if entry_id in report.repaired:
            report.repaired.remove(entry_id)
    register["updated_at"] = now


FLAKE_DETAIL_MAX_CHARS = 500

# A record the fold opens without an INCONSISTENT verdict carries a ``kind``
# and a ``reason`` the repair task quotes.
KIND_ERRORED_AFTER_PASS = "errored-after-pass"
KIND_INTERMITTENT = "intermittent"

# An entry that passed and then reads ERROR stops judging: an ERROR opens no
# divergence, so a product that no longer boots would otherwise stay silent.
# On this many consecutive ERROR runs it is surfaced (one skip line and the
# ``errored_after_pass`` marker); on the next it gets a record, and a repair.
ERRORED_AFTER_PASS_SURFACE_RUNS = 2
ERRORED_AFTER_PASS_OPEN_RUNS = 3

# An entry that flakes this many times inside the window gets a record: each
# flake passed its re-run, but an entry that needs one every other day is a
# defect in the entry or the product.
INTERMITTENT_FLAKES = 3
INTERMITTENT_WINDOW_SECONDS = 7 * 86_400.0
_FLAKE_RECENT_MAX = 50

# DIVERGENCE-FOLD-REPAIRED (webapp #297, #352, #357, 2026-10-19..29): an open
# ``intermittent`` record was repaired only once the window had gone quiet
# (fewer than INTERMITTENT_FLAKES failures in the last seven days), so an entry
# that flaked seven times and then passed sixteen consecutive main runs after
# its fix stayed open for up to a week, and the backlog minted a repair task
# (and got a redundant PR) for an entry passing on main. It is now also
# repaired after this many consecutive main runs that pass it cleanly (in
# ``passing_ids``, not in ``flaky_ids``) since its last recorded failure.
#
# Why 10: in webapp's 250-run history (2026-10-30, ~5 main runs a day) the
# clean-pass streak BETWEEN two failures of a flaky entry has a median of 3 and
# reaches 10 in about a quarter of cases (15 of 54), 20 in about a tenth. A
# smaller N would repair half of the still-flaky entries between two flakes;
# a larger one is three to four days of passes on main, most of the week the
# quiet-window rule already waits. Ten is about two days of main runs. A
# repair by streak also clears the entry's failure window, so the record
# re-opens only after INTERMITTENT_FLAKES new failures, not on the next one.
INTERMITTENT_REPAIR_PASSES = 10


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def _marker_map(register: dict[str, Any], key: str) -> dict[str, Any]:
    markers = register.get(key)
    if not isinstance(markers, dict):
        markers = register[key] = {}
    return markers


def _open_kind_record(
    records: dict[str, dict[str, Any]],
    entry_id: str,
    kind: str,
    reason: str,
    since: float,
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    report: UpdateReport,
) -> None:
    """Open a ``kind`` record for ``entry_id``, or refresh the reason of one open.

    An entry with no entry file gets nothing, as for an INCONSISTENT verdict. A
    record already open for another cause (an INCONSISTENT verdict) is left as
    it is: it already asks for a repair.
    """
    entry = entries.get(entry_id)
    if entry is None:
        return
    existing = records.get(entry_id)
    if existing is not None and existing["status"] == STATUS_OPEN:
        if existing.get("kind") == kind:
            existing["reason"] = reason
            existing["last_seen"] = run.timestamp
            existing["commit"] = run.commit
        return
    records[entry_id] = {
        "id": entry_id,
        "status": STATUS_OPEN,
        "first_seen": since,
        "last_seen": run.timestamp,
        "commit": run.commit,
        "kind": kind,
        "reason": reason,
        **_entry_facts(entry),
    }
    report.opened.append(entry_id)


def _fold_flakes(
    register: dict[str, Any],
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    report: UpdateReport,
) -> None:
    """Count each of the run's flakes once in ``register['flakes']``.

    A replayed run is the same observation: an entry's count moves only for a
    run later than the last one that counted it. ``recent`` keeps the flake
    times inside ``INTERMITTENT_WINDOW_SECONDS``; at ``INTERMITTENT_FLAKES`` of
    them the entry gets an ``intermittent`` record.
    """
    records: dict[str, dict[str, Any]] = register["divergences"]
    flakes = _marker_map(register, "flakes")
    for entry_id, detail in sorted(run.flakes.items()):
        marker = flakes.get(entry_id)
        if not isinstance(marker, dict):
            marker = flakes[entry_id] = {"count": 0, "first_seen": run.timestamp}
        if run.timestamp <= _as_number(marker.get("last_seen")):
            continue
        count = marker.get("count")
        marker["count"] = (
            count if isinstance(count, int) and not isinstance(count, bool) else 0
        ) + 1
        marker["last_seen"] = run.timestamp
        marker["commit"] = run.commit
        marker["detail"] = detail[:FLAKE_DETAIL_MAX_CHARS]
        recent = _recent_flakes(marker, run.timestamp)
        recent.append(run.timestamp)
        marker["recent"] = recent[-_FLAKE_RECENT_MAX:]
        report.flaked.append(entry_id)
        failures = _recent_failures(register, entry_id, run.timestamp)
        if len(failures) >= INTERMITTENT_FLAKES:
            _open_kind_record(
                records,
                entry_id,
                KIND_INTERMITTENT,
                _intermittent_reason(register, entry_id, run.timestamp),
                failures[0],
                run,
                entries,
                report,
            )


def _fold_unconfirmed_history(
    register: dict[str, Any],
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    report: UpdateReport,
) -> None:
    """Count each unconfirmed failure, so an alternating one cannot hide forever.

    The ``unconfirmed`` marker lives one run, so an entry that fails unconfirmed,
    passes, and fails unconfirmed again would never open. ``unconfirmed_recent``
    keeps each id's unconfirmed failure times inside
    ``INTERMITTENT_WINDOW_SECONDS``; together with its flakes, at
    ``INTERMITTENT_FLAKES`` of them the entry gets an ``intermittent`` record,
    as a flaky one does. A replayed run is the same observation.
    """
    records: dict[str, dict[str, Any]] = register["divergences"]
    history = _marker_map(register, "unconfirmed_recent")
    for entry_id in list(report.unconfirmed):
        times = _in_window(history.get(entry_id), run.timestamp)
        if run.timestamp not in times:
            times.append(run.timestamp)
        history[entry_id] = times[-_FLAKE_RECENT_MAX:]
        failures = _recent_failures(register, entry_id, run.timestamp)
        if len(failures) < INTERMITTENT_FLAKES:
            continue
        _open_kind_record(
            records,
            entry_id,
            KIND_INTERMITTENT,
            _intermittent_reason(register, entry_id, run.timestamp),
            failures[0],
            run,
            entries,
            report,
        )
        if entry_id in report.opened:
            # Open now: no longer a lone failure waiting for the next run.
            report.unconfirmed.remove(entry_id)
            _marker_map(register, "unconfirmed").pop(entry_id, None)
    for entry_id in list(history):
        kept = _in_window(history[entry_id], run.timestamp)
        if kept:
            history[entry_id] = kept
        else:
            del history[entry_id]


def _in_window(times: object, at: float) -> list[float]:
    """The numeric times in ``times`` inside the window that ends at ``at``."""
    if not isinstance(times, list):
        return []
    return [
        float(t)
        for t in times
        if isinstance(t, (int, float))
        and not isinstance(t, bool)
        and at - INTERMITTENT_WINDOW_SECONDS < t <= at
    ]


def _recent_failures(register: dict[str, Any], entry_id: str, at: float) -> list[float]:
    """Sorted times an entry failed without a confirmed failure, inside the window:
    its flakes (re-run passed) and its unconfirmed failures (never re-run)."""
    flake = _marker_map(register, "flakes").get(entry_id)
    flakes = _recent_flakes(flake, at) if isinstance(flake, dict) else []
    unconfirmed = _in_window(
        _marker_map(register, "unconfirmed_recent").get(entry_id), at
    )
    return sorted(set(flakes) | set(unconfirmed))


def _intermittent_reason(register: dict[str, Any], entry_id: str, at: float) -> str:
    flake = _marker_map(register, "flakes").get(entry_id)
    flakes = len(_recent_flakes(flake, at)) if isinstance(flake, dict) else 0
    unconfirmed = len(
        _in_window(_marker_map(register, "unconfirmed_recent").get(entry_id), at)
    )
    failures = _recent_failures(register, entry_id, at)
    since = _day(failures[0]) if failures else _day(at)
    if not unconfirmed:
        return f"intermittent: flaked {flakes} times since {since}"
    return (
        f"intermittent: failed {flakes + unconfirmed} times since {since} "
        f"({flakes} flaked, {unconfirmed} not re-run), passing in between"
    )


def _recent_flakes(marker: dict[str, Any], at: float) -> list[float]:
    """The marker's flake times inside the window that ends at ``at``."""
    return _in_window(marker.get("recent"), at)


def _repair_quiet_intermittents(
    register: dict[str, Any],
    run: RunSummary,
    report: UpdateReport,
    pass_streaks: dict[str, int] | None = None,
) -> None:
    """Repair an ``intermittent`` record that passed with the window gone quiet,
    or after ``INTERMITTENT_REPAIR_PASSES`` consecutive clean passes on main.

    A single pass does not repair it (passing is what a flaky entry mostly
    does); a pass on a run with fewer than ``INTERMITTENT_FLAKES`` flakes in the
    window before it does. Unconfirmed failures (``unconfirmed_recent``) count
    as flakes here.

    ``pass_streaks`` is entry id -> consecutive clean main passes since the
    record's ``last_seen``, this run included (``update`` reads it from
    ``runs.jsonl``, ``entry_evidence``). A repair by streak clears the entry's
    failure window, so it re-opens only on ``INTERMITTENT_FLAKES`` new ones.
    """
    streaks = pass_streaks or {}
    for entry_id, record in sorted(register["divergences"].items()):
        if record["status"] != STATUS_OPEN or record.get("kind") != KIND_INTERMITTENT:
            continue
        if entry_id not in run.passing_ids:
            continue
        quiet = len(_recent_failures(register, entry_id, run.timestamp)) < (
            INTERMITTENT_FLAKES
        )
        # A flake on this run is a failure, whatever runs.jsonl says.
        streak = 0 if entry_id in run.flakes else streaks.get(entry_id, 0)
        if not quiet and streak < INTERMITTENT_REPAIR_PASSES:
            continue
        record["status"] = STATUS_REPAIRED
        record["repaired_at"] = run.timestamp
        record["repaired_commit"] = run.commit
        if not quiet:
            record["repaired_by"] = (
                f"{streak} consecutive passing main runs "
                f"(>= {INTERMITTENT_REPAIR_PASSES})"
            )
            flake = _marker_map(register, "flakes").get(entry_id)
            if isinstance(flake, dict):
                flake["recent"] = []
            _marker_map(register, "unconfirmed_recent").pop(entry_id, None)
        report.repaired.append(entry_id)


def _fold_errored_after_pass(
    register: dict[str, Any],
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    previous_passing: frozenset[str],
    report: UpdateReport,
) -> None:
    """Track entries that passed on the previous folded run and ERROR since.

    ``register['errored_after_pass']`` holds one marker per such entry:
    ``streak`` (consecutive ERROR runs), ``since`` (the first), ``last_seen``,
    ``commit`` and ``detail`` (the latest ERROR's account). Any run in which the
    entry does not ERROR ends the streak and drops the marker. At
    ``ERRORED_AFTER_PASS_SURFACE_RUNS`` the entry is reported for one skip line;
    at ``ERRORED_AFTER_PASS_OPEN_RUNS`` it gets an ``errored-after-pass``
    record. A replayed run moves nothing.
    """
    records: dict[str, dict[str, Any]] = register["divergences"]
    markers = _marker_map(register, "errored_after_pass")
    errored = set(run.errors)
    for entry_id in [e for e in markers if e not in errored]:
        del markers[entry_id]
    for entry_id in sorted(errored):
        marker = markers.get(entry_id)
        if not isinstance(marker, dict):
            if entry_id not in previous_passing:
                continue
            marker = markers[entry_id] = {"streak": 0, "since": run.timestamp}
        if run.timestamp <= _as_number(marker.get("last_seen")):
            continue
        streak = marker.get("streak")
        streak = (
            streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
        ) + 1
        marker.update(
            {
                "streak": streak,
                "last_seen": run.timestamp,
                "commit": run.commit,
                "detail": run.errors[entry_id][:FLAKE_DETAIL_MAX_CHARS],
            }
        )
        since = _as_number(marker.get("since"))
        if streak == ERRORED_AFTER_PASS_SURFACE_RUNS:
            report.errored_after_pass.append(entry_id)
        if streak >= ERRORED_AFTER_PASS_OPEN_RUNS:
            _open_kind_record(
                records,
                entry_id,
                KIND_ERRORED_AFTER_PASS,
                f"errored-after-pass: passed, then ERROR on {streak} consecutive "
                f"ledger runs since {_day(since)}; last: {marker['detail']}",
                since,
                run,
                entries,
                report,
            )


def _skip_line(entry_id: str, reason: str) -> str:
    return (
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "goal_id": "",
                "missing": [reason],
                "action": f"Repair required acceptance divergence {entry_id}"[:200],
                "lane": "acceptance_divergences",
            }
        )
        + "\n"
    )


def _record_skips(config: DivergenceConfig, report: UpdateReport) -> None:
    """``autofill_brief_skips.jsonl`` lines for what this update held back.

    One per flake counted and one per entry whose ERROR streak after a pass
    reached ``ERRORED_AFTER_PASS_SURFACE_RUNS``. Same file and shape as
    ``backlog_generator._append_skip_record``, so the operator reads one log
    for why repair work was or was not minted. Best-effort, and only into a
    ``.company`` that already exists.
    """
    company_dir = config.base_dir / ".company"
    lines = (
        [
            _skip_line(
                entry_id,
                f"entry {entry_id} failed on the ledger run, then passed when re-run "
                "alone on the same commit: recorded as a flake, no divergence opened",
            )
            for entry_id in report.flaked
        ]
        + [
            _skip_line(
                entry_id,
                f"entry {entry_id} failed on the ledger run and was not re-run (the "
                "confirmation budget was spent): one unconfirmed failure opens no "
                "divergence; a repair is minted if the next run fails it too",
            )
            for entry_id in report.unconfirmed
        ]
        + [
            _skip_line(
                entry_id,
                f"entry {entry_id} passed, then read ERROR on "
                f"{ERRORED_AFTER_PASS_SURFACE_RUNS} consecutive ledger runs "
                "(errored-after-pass): an ERROR opens no divergence; a repair is "
                "minted if the next run errors too",
            )
            for entry_id in report.errored_after_pass
        ]
        + [
            _skip_line(
                entry_id,
                f"entry {entry_id} failed on the ledger run (confirmed) but has "
                "never been CONSISTENT on main in any recorded run: a goal gap, "
                "not a divergence -- left unopened, owned by its goal's own "
                "queue-fill task (NO-DIVERGENCE-FOR-UNBUILT-ENTRIES)",
            )
            for entry_id in report.never_passed
        ]
    )
    if not lines or not company_dir.is_dir():
        return
    path = company_dir / "state" / "autofill_brief_skips.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines(lines)
    except OSError:
        report.notes.append(f"could not append the skip lines to {path}")


def _fold_verdicts(
    records: dict[str, dict[str, Any]],
    run: RunSummary,
    entries: dict[str, dict[str, Any]],
    report: UpdateReport,
    unconfirmed_markers: dict[str, Any] | None = None,
    ever_passed: frozenset[str] = frozenset(),
) -> None:
    # A flake's re-run passed; the ledger never lists it as inconsistent, and
    # a record written by hand that does is not opened for it either.
    inconsistent = set(run.inconsistent_ids) - set(run.flakes)
    markers = {} if unconfirmed_markers is None else unconfirmed_markers
    # One failure the ledger had no time to confirm opens nothing (see the
    # module docstring); the next run that fails it opens it.
    unconfirmed = inconsistent & run.unconfirmed
    inconsistent -= unconfirmed
    # A marker lives one run: the id is unconfirmed again, or it fails again
    # (and opens below, from the marker's first_seen), or the streak is over.
    previous_markers = {entry_id: markers.pop(entry_id) for entry_id in list(markers)}
    for entry_id in sorted(unconfirmed):
        existing = records.get(entry_id)
        if entry_id not in entries or (
            existing is not None and existing["status"] == STATUS_OPEN
        ):
            continue  # nothing to open, or already open: left as it is
        # Unconfirmed means it passed on the run before: a new streak.
        markers[entry_id] = {
            "first_seen": run.timestamp,
            "last_seen": run.timestamp,
            "commit": run.commit,
        }
        report.unconfirmed.append(entry_id)
    for entry_id in sorted(inconsistent):
        entry = entries.get(entry_id)
        if entry is None:
            report.notes.append(
                f"{entry_id} was inconsistent in the run but has no entry file now; "
                "not opened"
            )
            continue
        existing = records.get(entry_id)
        if existing is not None and existing["status"] == STATUS_OPEN:
            existing.update(_entry_facts(entry))
            existing["last_seen"] = run.timestamp
            existing["commit"] = run.commit
            # Now a plain divergence: the next pass repairs it.
            existing.pop("kind", None)
            existing.pop("reason", None)
            report.kept_open.append(entry_id)
            continue
        if entry_id not in ever_passed:
            # NO-DIVERGENCE-FOR-UNBUILT-ENTRIES: a confirmed, repeated failure
            # on an entry that has never been CONSISTENT on main in any
            # recorded run is a goal gap (the feature is not built yet), not
            # a regression. Left unopened -- whatever is already in `records`
            # for this id (nothing, or an old repaired/withdrawn record) is
            # untouched, so a later real pass-then-regress still opens one.
            report.never_passed.append(entry_id)
            continue
        # Failed unconfirmed on the run before, and again now: open since then.
        marker = previous_markers.get(entry_id)
        first_seen = run.timestamp
        if isinstance(marker, dict) and _as_number(marker.get("first_seen")) > 0:
            first_seen = min(first_seen, _as_number(marker.get("first_seen")))
        records[entry_id] = {
            "id": entry_id,
            "status": STATUS_OPEN,
            "first_seen": first_seen,
            "last_seen": run.timestamp,
            "commit": run.commit,
            **_entry_facts(entry),
        }
        report.opened.append(entry_id)

    for entry_id, record in sorted(records.items()):
        if record["status"] != STATUS_OPEN or entry_id in inconsistent:
            continue
        if entry_id in unconfirmed:
            continue  # failed again: not a pass, and not evidence to repair
        if record.get("kind") == KIND_INTERMITTENT:
            continue  # _repair_quiet_intermittents decides
        if entry_id in run.passing_ids:
            record["status"] = STATUS_REPAIRED
            record["repaired_at"] = run.timestamp
            record["repaired_commit"] = run.commit
            report.repaired.append(entry_id)


def update(config: DivergenceConfig, now: float | None = None) -> UpdateReport:
    """Fold the last run into ``divergences.json`` and return what changed.

    Raises ``DivergenceError`` — before writing anything — when the run records
    or the ledger's entry files cannot be read.
    """
    run = read_run(config)
    entries, unreadable = load_entry_index(config)
    when = time.time() if now is None else now
    if run.timestamp > when + FUTURE_RUN_SLACK_SECONDS:
        raise DivergenceError(
            f"the run in {config.last_run_path} is stamped t={run.timestamp:.0f}, "
            f"later than now (t={when:.0f}); it was not folded, because it would "
            "make every later run read as older"
        )

    report = UpdateReport()
    with _RegisterLock(
        config.divergences_path.with_name(config.divergences_path.name + ".lock")
    ):
        register = _load_register(config.divergences_path, report.notes)
        fold_run(
            register,
            run,
            entries,
            when,
            report,
            unreadable,
            runs_previous_passing(config.runs_path),
            runs_ever_passing(config.runs_path),
            _open_intermittent_streaks(register, config.runs_path),
        )
        ledger._write_json_atomic(config.divergences_path, register)
    _record_skips(config, report)
    report.register = register
    return report


# --------------------------------------------------------------------------
# Reading the register
# --------------------------------------------------------------------------


def read_register(path: Path) -> dict[str, Any] | None:
    """The register as it is on disk, or None when there is none to read.

    Read-only, unlike ``_load_register``: a corrupt file is not moved aside and
    nothing is written. Readers of the register (the backlog signal, the goal
    note) must not change state that ``update`` owns. None means "no readable
    register", never "no divergences" — a caller must not report either from it.
    """
    try:
        doc = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except (
        OSError,
        ValueError,
        RecursionError,
    ):  # RecursionError: a deeply nested file
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("divergences"), dict):
        return None
    doc["divergences"] = _wellformed_records(doc["divergences"], path.name, [])
    return doc


# A run of the ledger's own entries writes a few KB; anything this large was not
# written by the runner and is not parsed.
JUNIT_MAX_BYTES = 2_000_000

_INCONSISTENT_PREFIX = "INCONSISTENT: "


def failure_messages(junit_path: Path) -> dict[str, str]:
    """Entry id -> the runner's account of how it disagreed, from ``junit.xml``.

    Only ``INCONSISTENT`` failures: ``write_junit_xml`` also files BARREN
    entries as failures, and those have not diverged. Empty when the file is
    absent, oversized or not XML — the account is evidence a task can do
    without, so a missing one is never an error. The strings are the entry's
    own output and reach a worker's prompt: callers bound and sanitise them.
    """
    try:
        if junit_path.stat().st_size > JUNIT_MAX_BYTES:
            return {}
        raw = junit_path.read_bytes()
        # write_junit_xml never emits a DTD. Refusing one keeps entity expansion
        # off the table on any expat, not only the ones with amplification limits.
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            return {}
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError, ValueError, RecursionError):
        return {}
    messages: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name")
        failure = case.find("failure")
        if not name or failure is None:
            continue
        message = failure.get("message") or failure.text or ""
        if message.startswith(_INCONSISTENT_PREFIX):
            messages[name] = message[len(_INCONSISTENT_PREFIX) :]
    return messages


def open_records(register: dict[str, Any]) -> list[dict[str, Any]]:
    """Open records, required tier first, then oldest first, then by id."""
    opened = [
        r
        for r in register.get("divergences", {}).values()
        if r["status"] == STATUS_OPEN
    ]
    return sorted(
        opened,
        key=lambda r: (
            r.get("tier") != ledger.TIER_REQUIRED,
            _as_number(r.get("first_seen")),
            r["id"],
        ),
    )


def _as_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _safe(text: Any) -> str:
    """Printable text only: an entry file's strings reach a terminal."""
    if text is None:
        return "-"
    return "".join(c if c.isprintable() else "?" for c in str(text))


def _when(timestamp: Any) -> str:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return "-"
    try:
        stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "-"
    return stamp.strftime("%Y-%m-%d %H:%MZ")


def _print_record(record: dict[str, Any]) -> None:
    commit = record.get("commit")
    promotion = (
        f"promotion {_safe(record.get('promotion'))}, "
        if record.get("promotion")
        else ""
    )
    print(
        f"  [{_safe(record.get('tier'))}] {_safe(record['id'])} — {promotion}"
        f"authority {_safe(record.get('authority'))}, "
        f"first seen {_when(record.get('first_seen'))}, "
        f"last seen {_when(record.get('last_seen'))}, "
        f"commit {_safe(commit[:12] if isinstance(commit, str) else None)}"
    )
    print(f"      spec: {_safe(record.get('spec_cite'))}")


def cmd_divergences(args: argparse.Namespace) -> int:
    base_dir = Path(args.project_dir).resolve()
    config = DivergenceConfig.for_project(base_dir)
    try:
        report = update(config)
    except (DivergenceError, TimeoutError, OSError) as exc:
        print(f"acceptance-divergences: error: {_safe(exc)}", file=sys.stderr)
        return 1

    for note in report.notes:
        print(f"note: {_safe(note)}", file=sys.stderr)
    records = report.register["divergences"].values()
    counts = {s: sum(1 for r in records if r["status"] == s) for s in _STATUSES}
    print(f"acceptance-divergences ({base_dir})")
    print(
        f"  this update: {len(report.opened)} opened, {len(report.kept_open)} "
        f"still open, {len(report.repaired)} repaired, {len(report.withdrawn)} withdrawn, "
        f"{len(report.flaked)} flaky, {len(report.unconfirmed)} unconfirmed, "
        f"{len(report.never_passed)} never-passed"
    )
    for entry_id in report.never_passed:
        print(
            f"  never-passed: {_safe(entry_id)} failed (confirmed) but has never "
            "been CONSISTENT on main; a goal gap, not a divergence -- no "
            "divergence opened"
        )
    for entry_id in report.flaked:
        print(
            f"  flake: {_safe(entry_id)} failed, then passed when re-run alone; "
            "no divergence opened"
        )
    for entry_id in report.unconfirmed:
        print(
            f"  unconfirmed: {_safe(entry_id)} failed once and was not re-run; "
            "no divergence opened (it opens if the next run fails it too)"
        )
    for entry_id in report.errored_after_pass:
        print(
            f"  errored-after-pass: {_safe(entry_id)} passed, then ERROR on "
            f"{ERRORED_AFTER_PASS_SURFACE_RUNS} consecutive runs; a repair is "
            "minted if the next run errors too"
        )
    print(
        f"  register: {counts[STATUS_OPEN]} open, {counts[STATUS_REPAIRED]} repaired, "
        f"{counts[STATUS_WITHDRAWN]} withdrawn"
    )
    opened = open_records(report.register)
    if opened:
        print("open divergences (required tier first):")
        for record in opened:
            _print_record(record)
    print(f"  file: {config.divergences_path}")
    retired = _retire_resolved_tasks(base_dir)
    if retired:
        print(
            f"  dropped {len(retired)} queued repair task(s) whose divergence is "
            f"no longer open: {', '.join(_safe(t) for t in retired)}"
        )
    # A record, not a gate: nothing in the register changes an exit code.
    return 0


def _retire_resolved_tasks(base_dir: Path) -> list[str]:
    """Drop queued repair tasks for records this fold closed; never raises.

    The fold is the only moment a record stops being open, so it is the moment
    a still-queued repair task goes stale (``backlog_generator.
    retire_resolved_divergence_tasks``, which owns the task title).
    """
    try:
        import backlog_generator  # noqa: PLC0415

        return backlog_generator.retire_resolved_divergence_tasks(base_dir)
    except Exception:
        return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acceptance-divergences",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    div_parser = sub.add_parser(
        "divergences",
        help="fold the last run into divergences.json and list the open records",
    )
    div_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    div_parser.set_defaults(func=cmd_divergences)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
