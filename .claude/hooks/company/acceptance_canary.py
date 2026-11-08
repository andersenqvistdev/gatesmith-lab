#!/usr/bin/env python3
"""Acceptance Ledger mutant canary — GAP-7 increment 2c.

Evidence driving this module: a CLEAN run of ``acceptance_ledger.py`` cannot
be told apart from a runner that has stopped detecting anything. The BARREN
verdict (increment 1) catches an entry that observes nothing; it cannot
catch an entry whose relation logic still "fires" on every run but has
quietly stopped discriminating correct behavior from buggy behavior — every
member still agrees, the control still fires, and nothing in a CONSISTENT
verdict says whether that agreement means the artifact is correct or means
the check would agree with anything.

The canary answers a different question than the ledger does: not "does
this artifact's output look consistent" but "if this artifact were broken in
a specific, small way, would any entry notice at all". It does this by
mutating the ARTIFACT under test, not the ledger's own logic — one
``ast.NodeTransformer`` pass per mutant, flipping a single comparison
operator, dropping a single ``continue``, or inverting a single boolean
literal, re-parsed fresh from source text for every mutant (the "copy" in
"NodeTransformer on a copy") so no mutant can leak state into another. Each
mutant is run through the SAME acceptance-ledger entries used for the real
artifact (by importing ``acceptance_ledger`` and driving its per-entry
runner directly — never through ``run_ledger()``, which reloads
``manifest.json`` from disk and would silently re-target the unmutated
artifact for every "mutant" run).

What counts as a catch, and why there are two numbers
-----------------------------------------------------
A mutant "flips" an entry when that entry's verdict differs from a baseline
run against the unmutated artifact. Not every flip is evidence that the
entry detects anything. A mutant that silences the artifact — makes it
crash, time out, or simply print nothing — flips EVERY entry to BARREN at
once, including entries that have nothing to do with the mutated line and
entries that cannot discriminate correct output from buggy output at all
(a ``determinism`` relation re-runs one input and compares it with itself:
it is structurally incapable of telling a correct linter from a broken one,
yet a silencing mutant hands it a flip).

So a flip is DISCRIMINATING only when the mutant run still produced
observations somewhere — the artifact was talking, and this entry disagreed
with the baseline about what it said. A flip in a mutant run whose total
observation count is zero across every entry is silence, not detection.
``canary.json`` therefore reports, per entry:

* ``catch_count`` / ``catch_rate`` — every flip, silence flips included;
* ``flips_by_transition`` — the from/to verdict breakdown, so a run whose
  "catches" are all ``CONSISTENT->BARREN`` is visibly different from one
  where relations really disagreed;
* ``discriminating_catch_count`` — flips from mutant runs that were not
  silent;
* ``caught`` — true only when ``discriminating_catch_count`` is non-zero.

``caught`` is the field ``acceptance_ledger.promote_entry`` (increment 2b)
reads before granting an entry the ``required`` tier. Promotion must not be
purchasable with silence; that is the failure this module exists to catch,
one layer in.

Only entries with a healthy baseline verdict (CONSISTENT or OBSERVED) are
eligible to flip at all — an entry that was already BARREN, ERROR, or
INCONSISTENT before any mutation touched anything cannot demonstrate that
the canary detects new breakage.

Naming the artifact in the manifest
-----------------------------------
The canary has to know which argv token names the file to mutate. It first
looks for an explicit ``{ARTIFACT}`` placeholder, the same way
``acceptance_ledger._substitute_argv`` handles ``{INPUT}``/``{FIXTURES}``.
If there is none, it falls back to the argv element that RESOLVES to the
``--artifact`` path (absolute, or relative to the project root). The
fallback matters: ``_substitute_argv`` does not substitute ``{ARTIFACT}``,
so a manifest written with the placeholder makes an ordinary
``gatesmith-acceptance run`` invoke a file literally named ``{ARTIFACT}`` and
observe nothing — and promotion needs BOTH a passing run and a canary
catch. One manifest must serve both commands.

For the manifest to serve both, that element must be ABSOLUTE. The canary
resolves a relative element against the project root and will happily mutate
it, but ``run_invoke`` executes with ``cwd`` set to a throwaway per-invocation
workdir and only resolves argv[0] against the project root, so a relative
element past argv[0] is not found at run time — the canary would report
catches for a manifest whose ordinary run observes nothing, and promotion
would still refuse. Naming an existing but relative element is accepted here
rather than rejected: identifying the artifact is this function's job, and
the run-side path handling is ``acceptance_ledger``'s.

There is no positional inference (e.g. "argv[0] must be the artifact"):
``invoke`` may legitimately be ``.venv/bin/mdlint``, in which case neither
the placeholder nor the fallback identifies a mutable Python source and the
canary refuses by name rather than guessing. A token placed at argv[0] runs
through the same ``check_invoke_allowed`` gate as everything else — it fails
closed (an ERROR outcome for that entry), it does not bypass anything.

Output: ``.company/state/acceptance/canary.json`` (per-entry catch data plus
full per-mutant detail) and ``.company/state/acceptance/acceptance_health.json``,
whose ``sources`` object this module merges into rather than overwriting, so
another producer's verdict (increment 3's ``degraded``) survives a canary run
and vice versa. ``dark`` outranks ``degraded`` outranks ``healthy``.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import acceptance_ledger as al  # noqa: E402

# Bounds canary runtime the same way acceptance_ledger.py's MAX_TIMEOUT_SECONDS
# bounds a single invocation: deterministic, not a random sample — the first
# N mutable sites in source order.
MAX_MUTANTS = 25

# The preferred way for a manifest to name the artifact: an explicit
# placeholder, substituted with each mutant's temp source path. Optional —
# see the module docstring on the resolved-path fallback.
ARTIFACT_TOKEN = "{ARTIFACT}"

# A mutant can only "catch" against an entry whose baseline verdict already
# demonstrates the entry looks at something real. BARREN/ERROR/INCONSISTENT
# baselines are excluded — see module docstring.
_HEALTHY_BASELINE_VERDICTS = frozenset({"CONSISTENT", "OBSERVED"})

# acceptance_health.json precedence, worst first. Every producer writes its
# own entry under "sources"; the top-level status is the worst of them.
# Worst first. "error" (a producer could not run — e.g. increment 3's corpus
# harvester failing) outranks "dark" (the ledger cannot tell it still
# detects anything); both are hard-fail. "degraded" is a quality warning.
HEALTH_PRECEDENCE = ("error", "dark", "degraded", "healthy")
HEALTH_SOURCE = "canary"

_COMPARISON_FLIPS: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


class CanaryError(Exception):
    """Raised when a canary run cannot proceed at all (bad manifest, no mutable site)."""


# --------------------------------------------------------------------------
# Mutation
# --------------------------------------------------------------------------


class _MutantTransformer(ast.NodeTransformer):
    """Applies exactly one mutation: the ``target``-th mutable site in a pre-order walk.

    ``target=-1`` never matches anything — used to count mutable sites
    without mutating any of them (``mutation_count`` after a ``visit()``).
    """

    def __init__(self, target: int) -> None:
        self._target = target
        self.mutation_count = 0
        self.description: str | None = None

    def _take(self, description: str) -> bool:
        hit = self.mutation_count == self._target
        self.mutation_count += 1
        if hit:
            self.description = description
        return hit

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        new_ops = []
        for op in node.ops:
            flipped = _COMPARISON_FLIPS.get(type(op))
            if flipped is not None and self._take(
                f"comparison: {type(op).__name__} -> {flipped.__name__} (line {node.lineno})"
            ):
                new_ops.append(flipped())
            else:
                new_ops.append(op)
        node.ops = new_ops
        return node

    def visit_Continue(self, node: ast.Continue) -> ast.AST:
        if self._take(f"drop continue (line {node.lineno})"):
            return ast.copy_location(ast.Pass(), node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool) and self._take(
            f"invert bool {node.value} (line {node.lineno})"
        ):
            return ast.copy_location(ast.Constant(value=not node.value), node)
        return node


def count_mutation_sites(source: str) -> int:
    """How many mutable sites exist in ``source``, without mutating any of them."""
    counter = _MutantTransformer(target=-1)
    counter.visit(ast.parse(source))
    return counter.mutation_count


def apply_mutation(source: str, ordinal: int) -> tuple[str, str]:
    """Re-parses ``source`` fresh and applies exactly the ``ordinal``-th mutation.

    Re-parsing (rather than deep-copying a shared tree) is the "copy" in
    "NodeTransformer on a copy" — two mutants built from the same call site
    can never share mutable AST state.
    """
    tree = ast.parse(source)
    transformer = _MutantTransformer(target=ordinal)
    mutated = transformer.visit(tree)
    if transformer.description is None:
        raise CanaryError(f"mutation ordinal {ordinal} is out of range for this source")
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated), transformer.description


# --------------------------------------------------------------------------
# Driving the ledger against a swapped-in artifact
# --------------------------------------------------------------------------


def resolve_artifact_marker(
    manifest: dict[str, Any], artifact_path: Path, base_dir: Path
) -> str:
    """Which argv element names the artifact: the token, else a resolved path.

    Returns the exact string to swap for each mutant's temp source path.
    Raises ``CanaryError`` when the manifest names the artifact neither way,
    naming both options rather than guessing a position.
    """
    invoke = manifest.get("invoke") or []
    for arg in invoke:
        if ARTIFACT_TOKEN in str(arg):
            return ARTIFACT_TOKEN
    for arg in invoke:
        text = str(arg)
        if not text or "{" in text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            if candidate.resolve() == artifact_path:
                return text
        except OSError:
            continue
    raise CanaryError(
        f"manifest 'invoke' {list(invoke)!r} does not name the artifact "
        f"{artifact_path}: no {ARTIFACT_TOKEN} placeholder, and no argv element "
        f"resolves to that path (relative elements resolve against {base_dir}). "
        "Point invoke at the artifact source, or add the placeholder — note "
        f"that {ARTIFACT_TOKEN} is NOT substituted by 'gatesmith-acceptance run', "
        "so a manifest that only carries the placeholder cannot produce the "
        "passing run promotion also requires."
    )


def _manifest_with_artifact(
    manifest: dict[str, Any], artifact_path: Path, marker: str
) -> dict[str, Any]:
    """``manifest`` with ``marker`` pointed at ``artifact_path``.

    The placeholder is substituted inside an element (it may be embedded in a
    longer argument); a resolved-path marker replaces the whole element, so a
    path that happens to be a substring of another argument is left alone.
    """
    invoke = []
    for arg in manifest["invoke"]:
        text = str(arg)
        if marker == ARTIFACT_TOKEN:
            invoke.append(text.replace(ARTIFACT_TOKEN, str(artifact_path)))
        elif text == marker:
            invoke.append(str(artifact_path))
        else:
            invoke.append(text)
    return {**manifest, "invoke": invoke}


def _run_all_entries(
    manifest: dict[str, Any],
    relations: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    config: al.LedgerConfig,
) -> tuple[dict[str, al.Outcome], int]:
    """Mirrors ``run_ledger``'s per-entry loop against an in-memory manifest.

    ``run_ledger`` cannot be reused directly — it calls ``load_manifest``
    itself, always re-reading the real ``invoke`` from disk, which would
    silently point every "mutant" run back at the unmutated artifact. This
    drives ``_run_entry_isolated`` directly instead, the same way
    ``run_ledger`` does internally, over the SAME already-loaded entries.

    Returns the outcomes and the run's total observation count. That count is
    what separates a mutant the entries detected from a mutant that merely
    silenced the artifact — ``run_ledger`` uses the same number for its
    run-level ``barren`` flag.
    """
    outcomes: dict[str, al.Outcome] = {}
    total_obs = 0
    with tempfile.TemporaryDirectory(prefix="acceptance-canary-run-") as tmp_root:
        run_config = dataclasses.replace(config, tmp_root=Path(tmp_root))
        for entry, run_fn in [(e, al.run_relation_entry) for e in relations] + [
            (e, al.run_case_entry) for e in cases
        ]:
            if "_load_error" in entry:
                outcomes[al._entry_display_id(entry)] = al._load_error_outcome(entry)
                continue
            outcome, obs_count = al._run_entry_isolated(
                run_fn, manifest, entry, run_config
            )
            outcomes[outcome.entry_id] = outcome
            total_obs += obs_count
    return outcomes, total_obs


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclasses.dataclass
class MutantResult:
    ordinal: int
    description: str
    caught: bool
    silent: bool
    flipped_entries: dict[str, dict[str, str]]

    @property
    def discriminating(self) -> bool:
        """A catch that is not merely the artifact having gone quiet."""
        return self.caught and not self.silent


@dataclasses.dataclass
class CanaryReport:
    artifact_path: Path
    mutants_total: int
    mutants: list[MutantResult]
    baseline: dict[str, al.Outcome]
    entry_catch_counts: dict[str, int]
    entry_discriminating_counts: dict[str, int]
    entry_transitions: dict[str, dict[str, int]]

    @property
    def mutants_caught(self) -> int:
        return sum(1 for mutant in self.mutants if mutant.caught)

    @property
    def mutants_caught_discriminating(self) -> int:
        return sum(1 for mutant in self.mutants if mutant.discriminating)

    @property
    def eligible_entry_ids(self) -> list[str]:
        return sorted(
            entry_id
            for entry_id, outcome in self.baseline.items()
            if outcome.verdict in _HEALTHY_BASELINE_VERDICTS
        )


def run_canary(
    config: al.LedgerConfig, artifact_path: Path, max_mutants: int = MAX_MUTANTS
) -> CanaryReport:
    manifest = al.load_manifest(config.ledger_dir)
    relations = al.load_entries(config.ledger_dir, "relations")
    cases = al.load_entries(config.ledger_dir, "cases")
    if not relations and not cases:
        raise CanaryError(f"no relations or cases found under {config.ledger_dir}")
    marker = resolve_artifact_marker(manifest, artifact_path, config.base_dir)

    source = artifact_path.read_text()
    total_sites = count_mutation_sites(source)
    mutant_count = max(0, min(total_sites, max_mutants))

    baseline_manifest = _manifest_with_artifact(manifest, artifact_path, marker)
    baseline, _baseline_obs = _run_all_entries(
        baseline_manifest, relations, cases, config
    )
    eligible = {
        entry_id
        for entry_id, outcome in baseline.items()
        if outcome.verdict in _HEALTHY_BASELINE_VERDICTS
    }
    entry_catch_counts: dict[str, int] = {entry_id: 0 for entry_id in eligible}
    entry_discriminating_counts: dict[str, int] = {entry_id: 0 for entry_id in eligible}
    entry_transitions: dict[str, dict[str, int]] = {
        entry_id: {} for entry_id in eligible
    }
    mutants: list[MutantResult] = []

    if mutant_count > 0:
        with tempfile.TemporaryDirectory(
            prefix="acceptance-canary-mutants-"
        ) as mutant_root:
            mutant_dir = Path(mutant_root)
            suffix = artifact_path.suffix or ".py"
            for ordinal in range(mutant_count):
                mutated_source, description = apply_mutation(source, ordinal)
                mutant_path = mutant_dir / f"mutant_{ordinal:03d}{suffix}"
                mutant_path.write_text(mutated_source)
                mutant_manifest = _manifest_with_artifact(manifest, mutant_path, marker)
                mutant_outcomes, mutant_obs = _run_all_entries(
                    mutant_manifest, relations, cases, config
                )
                # Zero observations anywhere means the artifact went quiet:
                # every flip in this run is silence, not detection.
                silent = mutant_obs == 0

                flipped: dict[str, dict[str, str]] = {}
                for entry_id in eligible:
                    baseline_verdict = baseline[entry_id].verdict
                    mutant_outcome = mutant_outcomes.get(entry_id)
                    mutant_verdict = (
                        mutant_outcome.verdict if mutant_outcome else "MISSING"
                    )
                    if mutant_verdict != baseline_verdict:
                        flipped[entry_id] = {
                            "baseline": baseline_verdict,
                            "mutant": mutant_verdict,
                        }
                        entry_catch_counts[entry_id] += 1
                        transition = f"{baseline_verdict}->{mutant_verdict}"
                        entry_transitions[entry_id][transition] = (
                            entry_transitions[entry_id].get(transition, 0) + 1
                        )
                        if not silent:
                            entry_discriminating_counts[entry_id] += 1
                mutants.append(
                    MutantResult(
                        ordinal=ordinal,
                        description=description,
                        caught=bool(flipped),
                        silent=silent,
                        flipped_entries=flipped,
                    )
                )

    return CanaryReport(
        artifact_path=artifact_path,
        mutants_total=mutant_count,
        mutants=mutants,
        baseline=baseline,
        entry_catch_counts=entry_catch_counts,
        entry_discriminating_counts=entry_discriminating_counts,
        entry_transitions=entry_transitions,
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_canary_json(report: CanaryReport, path: Path, project_dir: Path) -> None:
    """Write the per-entry catch record ``promote_entry`` reads.

    ``catch_count``/``catch_rate`` count EVERY flip, silence flips included —
    they answer "how often did this entry's verdict move", which is what an
    operator triaging a run wants to see. ``caught`` is narrower on purpose:
    it is the promotion input, and it is true only when at least one flip came
    from a mutant run in which the artifact still produced observations.
    """
    entries = {}
    for entry_id in report.eligible_entry_ids:
        count = report.entry_catch_counts.get(entry_id, 0)
        discriminating = report.entry_discriminating_counts.get(entry_id, 0)
        entries[entry_id] = {
            "baseline_verdict": report.baseline[entry_id].verdict,
            "caught": discriminating > 0,
            "catch_count": count,
            "catch_rate": count / report.mutants_total if report.mutants_total else 0.0,
            "discriminating_catch_count": discriminating,
            "discriminating_catch_rate": (
                discriminating / report.mutants_total if report.mutants_total else 0.0
            ),
            "flips_by_transition": dict(
                sorted(report.entry_transitions.get(entry_id, {}).items())
            ),
        }
    payload = {
        "generated_at": time.time(),
        "project_dir": str(project_dir),
        "artifact_path": str(report.artifact_path),
        "mutants_total": report.mutants_total,
        "mutants_caught": report.mutants_caught,
        "mutants_caught_discriminating": report.mutants_caught_discriminating,
        "overall_catch_rate": (
            report.mutants_caught / report.mutants_total
            if report.mutants_total
            else 0.0
        ),
        "entries": entries,
        "mutants": [
            {
                "ordinal": mutant.ordinal,
                "description": mutant.description,
                "caught": mutant.caught,
                "silent": mutant.silent,
                "flipped_entries": mutant.flipped_entries,
            }
            for mutant in report.mutants
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def health_verdict(report: CanaryReport) -> tuple[str, str | None]:
    """The canary's own ``(status, reason)`` — not the merged file status."""
    eligible_count = len(report.eligible_entry_ids)
    if eligible_count == 0:
        return "dark", (
            "no entry had a healthy (CONSISTENT/OBSERVED) baseline verdict — "
            "there is nothing for the canary to test against"
        )
    if report.mutants_total == 0:
        return "dark", (
            "no mutant was generated — the artifact has no mutable "
            "comparison/continue/boolean site, or the mutant cap is zero"
        )
    if report.mutants_caught == 0:
        return "dark", (
            f"0 of {report.mutants_total} injected mutants were caught by any of "
            f"{eligible_count} eligible entries — the ledger cannot be told apart "
            "from a runner that has stopped detecting anything"
        )
    if report.mutants_caught_discriminating == 0:
        return "dark", (
            f"all {report.mutants_caught} of {report.mutants_total} caught mutants "
            "only silenced the artifact — every flip is CONSISTENT->BARREN noise, "
            "so no entry has demonstrated it discriminates correct output from buggy "
            "output"
        )
    return "healthy", None


def _merge_health_sources(
    existing: dict[str, Any], status: str, reason: str | None
) -> dict[str, Any]:
    """Merge this run's verdict into ``existing`` without dropping other producers.

    Each producer owns one key under ``sources``; the top-level status is the
    worst of them by ``HEALTH_PRECEDENCE`` (error beats dark beats degraded
    beats healthy),
    so increment 3's ``degraded`` and the canary's ``dark`` cannot clobber each
    other in either direction, and a later healthy canary run clears only the
    canary's own dark.
    """
    payload = dict(existing)
    sources = payload.get("sources")
    sources = dict(sources) if isinstance(sources, dict) else {}
    sources[HEALTH_SOURCE] = {
        "status": status,
        "reason": reason,
        "generated_at": time.time(),
    }
    payload["sources"] = sources

    worst_status = "healthy"
    worst_reason: str | None = None
    worst_rank = HEALTH_PRECEDENCE.index("healthy")
    for name, source in sources.items():
        if not isinstance(source, dict):
            continue
        source_status = source.get("status")
        if source_status not in HEALTH_PRECEDENCE:
            continue
        rank = HEALTH_PRECEDENCE.index(source_status)
        if rank < worst_rank:
            worst_rank = rank
            worst_status = source_status
            source_reason = source.get("reason")
            worst_reason = f"{name}: {source_reason}" if source_reason else None
    payload["status"] = worst_status
    payload["reason"] = worst_reason
    return payload


def write_health_json(report: CanaryReport, path: Path) -> None:
    status, reason = health_verdict(report)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded

    payload = _merge_health_sources(existing, status, reason)
    payload.update(
        {
            "generated_at": time.time(),
            "mutants_total": report.mutants_total,
            "mutants_caught": report.mutants_caught,
            "mutants_caught_discriminating": report.mutants_caught_discriminating,
            "eligible_entry_count": len(report.eligible_entry_ids),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be at least 1 (got {value}) — a canary that injects no mutant "
            "reports dark and proves nothing"
        )
    return value


def cmd_canary(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    config = al.LedgerConfig.for_project(project_dir)
    artifact_path = Path(args.artifact).resolve()
    if not artifact_path.is_file():
        print(
            f"acceptance-canary: error: artifact not found: {artifact_path}",
            file=sys.stderr,
        )
        return 1

    try:
        report = run_canary(config, artifact_path, max_mutants=args.max_mutants)
    except (al.LedgerError, CanaryError) as exc:
        print(f"acceptance-canary: error: {exc}", file=sys.stderr)
        return 1
    except SyntaxError as exc:
        # Only Python can be mutated; a shell script or a built binary named as
        # the artifact must say so, not raise a traceback naming nothing.
        print(
            f"acceptance-canary: error: {artifact_path} is not parseable Python, "
            f"so no AST mutant can be built from it ({exc})",
            file=sys.stderr,
        )
        return 1
    except UnicodeDecodeError as exc:
        print(
            f"acceptance-canary: error: {artifact_path} is not UTF-8 text — the "
            f"canary mutates Python source, not a compiled artifact ({exc})",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            f"acceptance-canary: error: could not read {artifact_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    write_canary_json(report, config.state_dir / "canary.json", project_dir)
    write_health_json(report, config.state_dir / "acceptance_health.json")

    print(
        f"acceptance-canary: {report.mutants_caught}/{report.mutants_total} mutants "
        f"caught ({report.mutants_caught_discriminating} discriminating) across "
        f"{len(report.eligible_entry_ids)} eligible entries"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acceptance-canary",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    canary_parser = sub.add_parser(
        "canary", help="inject AST mutants into an artifact and record the catch rate"
    )
    canary_parser.add_argument(
        "--project-dir", default=".", help="project root containing acceptance/"
    )
    canary_parser.add_argument(
        "--artifact", required=True, help="path to the Python source file to mutate"
    )
    canary_parser.add_argument(
        "--max-mutants",
        type=_positive_int,
        default=MAX_MUTANTS,
        help="cap on mutants generated (at least 1)",
    )
    canary_parser.set_defaults(func=cmd_canary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
