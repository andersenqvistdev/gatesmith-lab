"""Task-admission gate (Phase 1, autonomy plan).

Pure, unit-testable admission logic for *self/auto-generated* tasks. Rejects
redundant, invalid, or already-done work BEFORE it enters the work queue.

Motivation (see ``.planning/autonomy/phase-1-task-admission.md``): the
proactive/initiative/ideation engine had no admission gate, so it produced the
origins of nearly every phantom — duplicate-title artifacts
(``"Document: Document: Document:"``), tests for already-shipped features,
wrong-target tests, and references to symbols that do not exist
(``build_panel_data`` — the real symbol is ``build_demo_data``).

Design rules:
  * ``admit_task`` is a PURE function (no side effects, no logging) so it is
    unit-testable without the daemon. The wiring layer in ``work_allocator``
    does the logging + shadow-mode handling.
  * ``human``-sourced tasks ALWAYS bypass the gate — never block a human.
  * Fail CLOSED (reject) only when a check is *definitive*; fail OPEN (admit)
    when a check is merely uncertain. A too-aggressive gate that drops real
    work is worse than a few phantoms.

Public API:
    admit_task(task, queue=None, repo_root=None, config=None) -> (bool, reason|None)
    load_admission_config(repo_root) -> dict
    log_rejection(repo_root, task, reason, *, shadow=False) -> None
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

# Sources that are subject to the gate. Everything else (notably ``human``, but
# also ``escalation``/``feedback_monitor``/``cron`` recovery lanes) bypasses —
# we only police machine-proposed work. Unknown sources fail OPEN (bypass).
GATED_SOURCES = frozenset(
    {
        "self",
        "planning",
        "roadmap",
        "workshop",
        "employee_initiative",
        "employee_ideation",
        "gap_analysis",
    }
)


def _is_gated_source(source: str | None) -> bool:
    """Whether a task source is subject to the gate.

    Matches both bare sources (``"self"``, ``"planning"``) and project-suffixed
    variants seen in the real queue (``"roadmap-P85"``, ``"workshop-ws124"``).
    Human and recovery lanes (``human``/``escalation``/``auto_merge_escalation``/
    ``feedback_monitor``/``cron``) bypass — they fail OPEN.
    """
    s = (source or "self").lower()
    return any(s == p or s.startswith(p + "-") for p in GATED_SOURCES)


DEFAULT_CONFIG = {
    "enabled": True,
    # Shadow mode is enforced by the wiring layer, not here; surfaced in the
    # config so the wiring can read it from the same place.
    "shadowMode": False,
    "checkDuplicateTitle": True,
    "checkTargetsExist": True,
    "checkNotAlreadyDone": True,
    "valueFloor": True,
    # Phase 3: consult learned anti-patterns (confirmed-phantom classes) so the gate
    # stops re-admitting work the system has already shipped-but-didn't-deliver.
    "checkAntiPatterns": True,
    # Concrete-deliverable gate: GATED-source tasks that use abstract improvement
    # verbs ("Improve X", "Enhance X") or are short abstract noun phrases
    # ("Developer Experience", "Test Automation") must name a concrete target
    # (a file path, code symbol, or named binary) to be admitted.
    "checkConcreteDeliverable": True,
    # Execute-only gate: a task whose deliverable is to RUN/EXECUTE tests (or
    # "verify tests pass") produces no code diff, so any PR is a guaranteed
    # phantom — usually a worker WRITING new tests to have something in the diff
    # ("Run tests" -> adds test file; calibration 2026-07-08: PRs #136, #170).
    # Reject unless the task also carries a code-change verb.
    "checkExecuteOnly": True,
    # Capability-already-shipped gate (2026-08-01, G7-SUCCESS-RATE-SECURITY-
    # ASSESSMENT-20260725.md MEDIUM-1): rejects a task that claims to CREATE
    # a named file which already exists on disk and was merged within the
    # last 14 days by a PR that shipped THAT capability (it added the file,
    # or its title shares the brief's distinctive words; 2026-09-26: an
    # unrelated edit to the file is not evidence) — catches
    # independently-generated tasks (scout vs.
    # organic queue-fill) that duplicate the same not-yet-built capability,
    # which would otherwise dispatch, correctly no-op, and log a
    # success_detection_failure phantom.
    "checkCapabilityAlreadyShipped": True,
    # Measured-evidence gate (2026-07-27, extends PR #322's brief-quality
    # standard to every machine mint path): an improvement-GRADIENT claim
    # ("improve X", "optimize Y", "reduce Z") that names a target but carries
    # no baseline measurement is not actionable evidence — the 07-24
    # controlled experiment showed the same goal produced a judge-rejected
    # phantom from the unquantified brief and a real fix from the measured
    # one. Fix-shaped claims ("fix the failing test in X") are exempt: a
    # named broken observable IS the evidence, no quantity needed. Structured
    # specs (acceptance criteria / target file / test requirements) and
    # greenfield repos are exempt.
    "checkMeasuredEvidence": True,
    # PB-2 semantic relevance gate: the deterministic checks above catch malformed
    # and duplicate work but are blind to "novel, plausible, and pointless" — the
    # process-navel-gazing an idle company generates about itself. This LLM-judge
    # asks whether a gated task concretely advances an ACTIVE goal from vision.md,
    # and rejects it if not. It runs LAST (after every cheap check passes) so the
    # model is invoked rarely, and DEGRADES to admit on any error / no-goals / no
    # claude CLI — a judge outage must never hard-block the whole company.
    "checkSemanticRelevance": True,
    "semanticTimeoutSeconds": 60,
    # Model alias/id for `claude --model`; null => omit the flag (CLI default).
    "semanticModel": None,
    # Repeat-rejection escalation (2026-08-17). A rejection is a dead end for
    # ONE task_id, but the planner re-mints the same work under a NEW id every
    # cycle, so a permanently-rejected item loops forever: minted, rejected,
    # logged, forgotten. Every existing counter (retry budget, escalation
    # threshold) is keyed on task_id and therefore resets to zero on each
    # re-mint, which makes the loop invisible to all of them. Observed the day
    # this was written: SCOUT-20260814-3 rejected 40 times in 12 hours, once
    # every ~18 minutes, while the queue read "pending: 0" and nothing paged.
    # Counting by TITLE across re-mints is what makes the loop visible.
    "escalateRepeatRejections": True,
    "repeatRejectionThreshold": 3,
    "repeatRejectionWindowHours": 24,
    # Don't re-page for the same title while an operator is presumably looking.
    "repeatRejectionDedupHours": 12,
}

# Tail of the append-only rejection log scanned when counting repeats. The log
# is unbounded; only recent entries can fall inside the window, so the scan is
# capped instead of growing with file size.
_REJECTION_SCAN_LINES = 2000

# Directories never worth scanning for symbol/file validity checks.
_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".forge-worktrees",
        "htmlcov",
    }
)

# A leading ``"<prefix>: "`` segment repeated two or more times, e.g.
# ``"Document: Document: Document: [WS-124-002]"``.
_DUP_PREFIX_RE = re.compile(r"^\s*(.{2,60}?:\s+)(?:\1)+", re.IGNORECASE)

# Task operates on *existing* code (so its named targets ought to exist).
_OPERATE_ON_EXISTING_RE = re.compile(
    r"\b(test|tests|testing|document|docs|docstring|fix|refactor|cover|coverage)\b",
    re.IGNORECASE,
)

# Test-coverage intent ("add tests for X", "improve test coverage for X").
_TEST_INTENT_RE = re.compile(
    r"\b(add|write|improve|increase|expand)\b.{0,40}\b(test|tests|coverage)\b",
    re.IGNORECASE,
)

# Incremental-coverage intent — legitimate work even when a test file already
# exists ("backfill", "raise coverage from 50%", "missing/remaining tests").
# These must NOT be rejected as "already has tests" (that would over-block).
_INCREMENTAL_INTENT_RE = re.compile(
    r"\b(backfill|increase|raise|additional|more|missing|remaining|uncovered|"
    r"edge[\s-]?cases?)\b|coverage gap|currently at|\b\d{1,3}(\.\d+)?\s*%",
    re.IGNORECASE,
)

# Trivially-scoped work (value floor).
# "line"/"word" are only matched when paired with an explicit size marker
# ("one" or "single") — bare "a" collides with ordinary usage
# ("add a timestamped line", "add a config line").
# "docstring"/"typo"/"comment" remain trivial with any single-item quantifier.
_TRIVIAL_RE = re.compile(
    r"\b(expand|add|fix|update|tweak)\b.{0,30}\b(one|a|single)\b.{0,25}\b(docstring|typo|comment)\b"
    r"|\b(expand|add|fix|update|tweak)\b.{0,30}\b(one|single)\b.{0,25}\b(line|word)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Concrete-deliverable gate (closes the "novel, plausible, pointless" gap)
# --------------------------------------------------------------------------- #

# Abstract improvement verbs — "Improve X", "Enhance X", "Optimize X", etc.
# When a task uses one of these and names no concrete target, it is a broad
# process idea, not an actionable engineering task.
_ABSTRACT_IMPROVEMENT_VERB_RE = re.compile(
    r"^\s*(improve|enhance|optimize|boost|strengthen|advance|refine|"
    r"elevate|augment|uplift|streamline|modernize|overhaul|expand)\b",
    re.IGNORECASE,
)

# Abstract domain nouns — when a short all-TitleCase phrase (2–4 words) ends
# up containing one of these, it is an OKR/initiative concept, not a task.
_ABSTRACT_DOMAIN_NOUNS = frozenset(
    {
        "automation",
        "experience",
        "quality",
        "velocity",
        "efficiency",
        "productivity",
        "excellence",
        "maturity",
        "culture",
        "governance",
        "adoption",
        "cohesion",
    }
)

# A file path that carries a known source/config/doc extension.
_CONCRETE_FILE_RE = re.compile(
    r"\b[\w][\w./-]*\.(?:py|md|json|yaml|yml|toml|sh|txt|js|ts|html|css|sql)\b",
    re.IGNORECASE,
)

# A bin/ command path.
_CONCRETE_BIN_RE = re.compile(r"\bbin/[a-z][a-z0-9_-]+")

# ``identifier()`` call tokens (snake_case / camelCase function references).
_PARENS_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\(\)")
# Backtick-wrapped identifiers (no dots — those are paths, handled separately).
_BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`")

# --------------------------------------------------------------------------- #
# Negation-context detection for backtick symbols
#
# "no `mcpServers` config found" or "evaluate whether `X` should be added"
# must NOT be treated as a required-to-exist target: the text explicitly notes
# the identifier is absent or under evaluation.  The check runs per-occurrence
# (skipped symbols are NOT added to `seen`), so a symbol that appears once in
# negation context and once without is still validated on the second occurrence.
#
# Intentional omissions:
#   "without" — "implement Y without `X`" is ambiguous (X may exist); omitted
#   to avoid false negatives on real targets.  The fail-open gate posture means
#   a genuine missing-target task that slips through the symbol check is still
#   caught by _extract_py_paths or the semantic-relevance judge.
# --------------------------------------------------------------------------- #

# Characters scanned before/after a backtick match to detect absence context.
_NEGATION_CTX_WINDOW = 80

# Absence language immediately before the opening backtick:
#   "no `X`", "missing `X`", "absent `X`", "zero `X`"
#   "evaluate whether `X`", "assess whether to add `X`"
# Uses DOTALL so the window can contain newlines in multi-line descriptions.
_NEGATION_PRE_RE = re.compile(
    r"\b(?:no|zero|missing|absent)\s*$"
    r"|\b(?:evaluate|assess)\b.{0,60}?\bwhether\b[^`]*$",
    re.IGNORECASE | re.DOTALL,
)

# Absence language immediately after the closing backtick:
#   "`X` not found", "`X` does not exist", "`X` is not yet …"
_NEGATION_POST_RE = re.compile(
    r"^\s*(?:not\s+found|does\s+not\s+exist|is\s+not\b|not\s+yet\b)",
    re.IGNORECASE,
)
# Python file paths. The lookbehind (not ``\b``) keeps a leading ``.`` so
# ``.claude/hooks/x.py`` is captured whole, not as ``claude/hooks/x.py``.
_PY_PATH_RE = re.compile(r"(?<![\w./-])([\w./-]+\.py)\b")

# --------------------------------------------------------------------------- #
# Reference-only path mentions (2026-09-26, PR-SCRATCH-GUARD false positives)
#
# A brief names .py paths in two roles: TARGETS (the files the work edits, which
# must exist) and REFERENCES (evidence of what happened, examples, patterns,
# things declared absent). Only targets may fail "target file not found". The
# operator had to reword one brief three times because the gate treated these
# as targets: a scratch file a past PR "added" (since deleted), a pattern
# (``_probe.py``), and an example path ("e.g. ``.claude/hooks/company/x.py``").
# A path is skipped only when EVERY mention of it is a reference, so a file
# cited as evidence AND named again as the fix location is still validated —
# that second mention is what catches a brief minted from a stale snapshot.
# --------------------------------------------------------------------------- #

# Gap between a context marker and the path: same sentence (no ". ", ";",
# "!", "?", newline), bounded. Dots inside paths and "e.g." are allowed.
_SAME_CLAUSE_GAP = r"(?:(?!\.\s)[^;!?\n]){0,%d}$"

# "e.g. x.py", "for example x.py", "such as x.py", "a pattern like _probe.py".
_EXAMPLE_PRE_RE = re.compile(
    # Bare "like"/"examples" are NOT markers: "things like timeouts belong in
    # x.py" and "document the usage examples in x.py" name x.py as the target.
    r"(?:\be\.?g\.|\bfor (?:example|instance)\b|\bsuch as\b)" + _SAME_CLAUSE_GAP % 40,
    re.IGNORECASE,
)

# Past-tense evidence: "#661 added a probe script _probe_x.py", "PR #600
# shipped foo.py". Describes what already happened, not what to edit now.
# Only when the path is the verb's DIRECT object: a preposition in the gap
# ("introduced in x.py", "left in x.py", "added to x.py", "moved to x.py")
# makes the path the LOCATION of the work — the ordinary way to name the file
# to fix — so it stays a target (challenger on #676).
_PAST_EVIDENCE_PRE_RE = re.compile(
    r"\b(?:added|shipped|introduced|committed|created|leaked|left|wrote|pushed|"
    r"landed|included|contained|removed|deleted|renamed|dropped|moved)\b"
    r"(?:(?!\.\s|\b(?:in|into|to|from|at|of|on|by|within|inside|under)\b)"
    r"[^;!?\n]){0,60}$",
    re.IGNORECASE,
)

# Declared absent / hypothetical: "no x.py", "a nonexistent file x.py".
_ABSENT_PRE_RE = re.compile(
    r"\b(?:no|zero|missing|absent)[\s`'\"]*$"
    r"|\b(?:nonexistent|non-existent|hypothetical|fictional|made-up|imaginary|"
    r"placeholder)\b" + _SAME_CLAUSE_GAP % 30,
    re.IGNORECASE,
)
_ABSENT_POST_RE = re.compile(
    r"^[`'\")\s]*(?:not\s+found|does\s+not\s+exist|doesn't\s+exist|"
    r"no\s+longer\s+exists?|(?:is|was)\s+(?:removed|deleted)|is\s+not\s+yet\b)",
    re.IGNORECASE,
)

# Placeholder stems that name no real file ("x.py", "foo.py").
_PLACEHOLDER_STEMS = frozenset(
    {"x", "y", "z", "foo", "bar", "baz", "xyz", "example", "placeholder"}
)

# The creation verb must GOVERN the path for the capability-already-shipped
# check: "create pkg/foo.py", "scaffold a new `foo.py` module". A verb elsewhere
# in a ±150-char window is not enough — the PR-SCRATCH-GUARD brief was rejected
# because a QUOTED error message ("Refusing to create PR: ...") sat within 150
# chars of pr_output_manager.py, which the brief names only as a location.
# The gap may not cross a clause break, a quote, a paren or a colon, and is at
# most 80 chars: a verbatim re-mint of #647 ("Create a per-session scratch
# directory outside the worktree in employee_activator.py") has 56.
_CREATION_GOVERNS_PRE_RE = re.compile(
    r"\b(?:create|creates|creating|scaffold|scaffolds|scaffolding|generate|"
    r"generates|generating)\b(?:(?!\.\s)[^;:!?\n\"()]){0,%d}$" % 80,
    re.IGNORECASE,
)

# Words that carry no capability meaning when comparing a brief with a merged
# PR's title (conventional-commit scopes, daemon-title boilerplate, fillers).
_OVERLAP_STOPWORDS = frozenset(
    {
        "feat", "fix", "fixes", "chore", "docs", "test", "tests", "refactor",
        "daemon", "task", "tasks", "evidence", "found", "during", "work",
        "with", "from", "that", "this", "these", "those", "into", "onto",
        "when", "then", "than", "only", "never", "every", "until", "before",
        "after", "must", "should", "would", "could", "will", "have", "does",
        "each", "what", "where", "which", "while", "about", "over", "under",
        "again", "still", "make", "made", "used", "uses", "using", "also",
        "them", "they", "their", "there", "here", "more", "less", "file",
        "files", "code", "line", "lines", "added", "adds", "add", "read",
        "first", "same", "other", "not", "no", "longer", "operator", "queued",
        "claude", "hooks", "company", "py", "pr", "prs", "merged", "merge",
    }
)  # fmt: skip
_OVERLAP_TOKEN_RE = re.compile(r"[a-z][a-z0-9]+")
_PR_TITLE_NOISE_RE = re.compile(
    r"\[task-[^\]]*\]|\(#\d+\)|^\s*\w+(?:\([^)]*\))?!?:\s*", re.IGNORECASE
)
# A merged PR "shipped the capability a brief asks for" by title when at
# least this many distinctive title words recur in the brief, covering at
# least this share of the title's distinctive words.
_OVERLAP_MIN_SHARED = 3
_OVERLAP_MIN_SHARE = 0.6
# "for X" / "for: X" target capture (test-coverage targets).
_FOR_TARGET_RE = re.compile(r"\bfor[:\s]+`?([A-Za-z_][\w.]+)`?", re.IGNORECASE)
# A plausible lowercase Python module name (snake_case or single lowercase word).
_MODULE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{3,}")

# Verbs that indicate a .py path is a greenfield DELIVERABLE, not a missing target.
# "create scripts/foo.py", "scaffold new_module.py", "generate parser.py" → deliverable.
# Bare "add" is excluded: "add tests for scripts/foo.py" names an existing target.
_DELIVERABLE_CREATION_VERB_RE = re.compile(
    r"\b(create|scaffold|generate)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Execute-only gate (running tests produces no diff → any PR is a phantom)
# --------------------------------------------------------------------------- #

# Intent to merely RUN/EXECUTE tests (or verify they pass). Matches "run tests",
# "run the test suite", "execute pytest", "rerun the tests", "run the flaky tests",
# "verify the tests pass", "confirm all tests/CI/build are green". Deliberately
# requires a test/suite/pytest/ci object so "run the migration" / "run the linter"
# don't match. One optional adjective slot (``\w+``) before the object closes the
# "run the <adjective> tests" evasion of the exact class this gate blocks.
_EXECUTE_TESTS_RE = re.compile(
    r"\b(?:re-?run|run|execute)\s+(?:the\s+|all\s+|these\s+|existing\s+|your\s+)?"
    r"(?:\w+\s+){0,2}(?:tests?|test\s+suite|suite|pytest)\b"
    r"|\b(?:verify|validate|check|confirm|ensure|make\s+sure)\s+(?:that\s+|all\s+)?"
    r"(?:the\s+)?(?:tests?|test\s+suite|suite|pytest|ci|build|pipeline)\s+"
    r"(?:are\s+|is\s+|still\s+|all\s+)*(?:pass|passes|passing|green)\b",
    re.IGNORECASE,
)

# Any code-change / deliverable verb. If the task carries one of these, it has a
# real diff-producing deliverable beyond mere execution, so it is NOT reject-worthy
# as execute-only. This is the false-positive FLOOR — its only failure mode that
# matters is a FALSE reject (silently dropping real work), so it is deliberately
# over-inclusive: an optional ``re`` prefix + inflectional suffix group make it
# morphology-aware (regenerate/rewriting/adding/implemented all match), and it
# lists a broad set of code- and test-maintenance verbs. e-ending stems are
# listed WITHOUT the trailing "e" so the suffix group restores it (writ -> write/
# writing/writes) — see the adversarial-verify workflow (2026-07-16).
_CODE_CHANGE_VERB_RE = re.compile(
    r"\b(?:re)?(?:"
    r"fix|add|writ|implement|creat|refactor|updat|repair|build|scaffold|generat|"
    r"resolv|patch|modify|correct|improv|enhanc|increas|rais|expand|backfill|"
    r"migrat|remov|delet|renam|introduc|wir|harden|document|extend|replac|restor|"
    r"reduc|adjust|convert|port|rework|rewrit|extract|inlin|consolidat|releas|"
    r"tag|publish|deploy|ship|cut|"
    # test-maintenance verbs (all fail-open-safe: they only reduce rejects).
    # e-ending stems keep the trailing consonant so the suffix group restores the
    # word (parametriz -> parametrize/parametrizing; deduplicat -> deduplicate).
    r"parametriz|mock|stub|deflak|flak|stabiliz|quarantin|split|isolat|decoupl|"
    r"prun|trim|clean|cover|assert|group|flatten|restructur|reorganiz|annotat|"
    r"deduplicat|tidy|speed|enabl|disabl|unskip|skip|configur|schedul|cach"
    r")(?:e|es|ed|ing|d|s)?\b"
    r"|\bset[\s-]?up\b",  # CI/job "set up ..." (two-word verb)
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Repo searcher (cached, pure-Python; no dependency on ripgrep being present)
# --------------------------------------------------------------------------- #


class RepoSearcher:
    """Scans a repo's ``.py`` files once and answers symbol/file existence.

    Cheap enough for an occasional admission check; the concatenated text and
    basename set are built lazily on first use and reused for the life of the
    instance.
    """

    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root)
        self._py_files: list[Path] | None = None
        self._all_text: str | None = None
        self._basenames: set[str] | None = None

    @property
    def py_files(self) -> list[Path]:
        if self._py_files is None:
            files: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(self.repo_root):
                dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(Path(dirpath) / fn)
            self._py_files = files
        return self._py_files

    @property
    def all_py_text(self) -> str:
        if self._all_text is None:
            parts: list[str] = []
            for f in self.py_files:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
            self._all_text = "\n".join(parts)
        return self._all_text

    @property
    def basenames(self) -> set[str]:
        if self._basenames is None:
            self._basenames = {f.name for f in self.py_files}
        return self._basenames

    def has_symbol(self, symbol: str) -> bool:
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        return bool(pattern.search(self.all_py_text))

    def has_file(self, basename: str) -> bool:
        return basename in self.basenames

    def test_exists_for(self, name: str) -> bool:
        """True if a dedicated test file under ``tests/`` already targets ``name``.

        Strict on purpose: requires a file whose stem is exactly
        ``test_<name>`` (case-insensitive) AND whose content references ``name``
        as a whole word. Earlier substring matching over-blocked — e.g. the
        candidate ``Document`` matched ``test_document_approvals.py`` and
        ``Write`` matched ``test_task_result_writer.py``.
        """
        tests_dir = self.repo_root / "tests"
        if not tests_dir.is_dir():
            return False
        target_stem = f"test_{name.lower()}"
        word = re.compile(r"\b" + re.escape(name) + r"\b")
        for f in tests_dir.rglob("test_*.py"):
            if f.stem.lower() == target_stem:
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if word.search(content):
                    return True
        return False


_SEARCHER_CACHE: dict[str, tuple[RepoSearcher, float]] = {}
_SEARCHER_TTL = 30.0  # seconds — short enough to pick up newly-added files


def _get_searcher(repo_root: Path | str) -> RepoSearcher:
    key = str(repo_root)
    now = time.monotonic()
    cached = _SEARCHER_CACHE.get(key)
    if cached is not None and (now - cached[1]) < _SEARCHER_TTL:
        return cached[0]
    searcher = RepoSearcher(repo_root)
    _SEARCHER_CACHE[key] = (searcher, now)
    return searcher


# --------------------------------------------------------------------------- #
# Individual checks (each returns a reject reason string, or None to admit)
# --------------------------------------------------------------------------- #


def _check_duplicate_title_artifact(title: str) -> str | None:
    match = _DUP_PREFIX_RE.match(title or "")
    if match:
        segment = match.group(1).strip()
        return f"duplicate-title artifact: repeated prefix {segment!r}"
    return None


def _check_value_floor(task: dict) -> str | None:
    text = f"{task.get('title', '')}\n{task.get('description', '')}"
    if _TRIVIAL_RE.search(text):
        return "trivial scope: below value floor"
    return None


def _has_concrete_target(text: str) -> bool:
    r"""True if *text* (title + description) names a concrete deliverable.

    A concrete target is any of: a file with a known extension
    (``scripts/chlog.py``), a code symbol with parentheses
    (``operation_loop()``), a backtick-wrapped identifier (``\`widget\``), or
    a ``bin/`` command path.  The check is intentionally broad so that an
    abstract-framed title like "Enhance performance" is admitted when the
    description says "Profile ``build_demo_data()`` in ``control_panel.py``".
    """
    return bool(
        _CONCRETE_FILE_RE.search(text)
        or _PARENS_SYMBOL_RE.search(text)
        or _BACKTICK_SYMBOL_RE.search(text)
        or _CONCRETE_BIN_RE.search(text)
    )


def _check_concrete_deliverable(title: str, description: str) -> str | None:
    """Reject abstract 'improve X' tasks that name no concrete deliverable.

    Closes the "novel, plausible, and pointless" admission-gate gap: the
    ideation engine produces broad process-improvement ideas that are novel
    (don't match anti-patterns), name no existing target (don't trigger the
    nonexistent-target check), and are not trivially scoped (don't hit the
    value floor).  Two patterns are caught:

    * **Rule A** — abstract improvement verb with no concrete target:
      ``"Improve Developer Experience"``, ``"Optimize Build Performance"``.
    * **Rule B** — short all-TitleCase noun phrase containing an abstract
      domain concept: ``"Developer Experience"``, ``"Test Automation"``.

    A concrete target anywhere in title or description — a file path, a code
    symbol, or a ``bin/`` path — exempts the task from both rules. Human-sourced
    tasks bypass this check entirely via ``admit_task``.
    """
    text = f"{title}\n{description}"
    if _has_concrete_target(text):
        return None

    _REJECT = (
        "abstract task: names no concrete deliverable"
        " (specify a file path, function, or named feature)"
    )

    # Rule A: abstract improvement verb → must name a concrete target.
    if _ABSTRACT_IMPROVEMENT_VERB_RE.match(title.strip()):
        return _REJECT

    # Rule B: short all-TitleCase noun phrase with an abstract domain concept.
    words = title.strip().split()
    if 1 < len(words) <= 4 and all(w and w[0].isupper() for w in words):
        lower_words = {w.lower() for w in words}
        if lower_words & _ABSTRACT_DOMAIN_NOUNS:
            return _REJECT

    return None


def _check_execute_only_task(title: str, description: str) -> str | None:
    """Reject tasks whose only deliverable is to RUN/EXECUTE tests.

    Executing tests produces no code diff, so a worker that opens a PR for such a
    task necessarily ships a phantom — most often by WRITING new tests just to
    have something in the diff (the "Run tests" -> adds a test file class;
    calibration 2026-07-08 flagged PRs #136 and #170 exactly this way). Running
    tests is CI's job (it happens on every PR), not code-change work.

    Fails CLOSED only when the ask is unambiguously execution-only: the text
    expresses a run/verify-tests intent AND carries neither a code-change verb NOR
    a concrete target (a file path / code symbol). Those two exemptions are the
    false-positive floor — a legitimate ``"add tests for foo.py"``, ``"fix the
    failing test"``, ``"parametrize the login tests"``, ``"split test_daemon.py"``,
    or ``"run tests and fix failures"`` always trips one of them and is admitted.
    Genuinely ambiguous cases fall through to admit (fail-open posture); missed
    execute-only phantoms are still caught downstream by the deliverable gate.
    Precision-first by design: NEVER drop real work, per the adversarial-verify
    workflow (2026-07-16, 166 cases).
    """
    text = f"{title}\n{description}"
    if not _EXECUTE_TESTS_RE.search(text):
        return None
    if _CODE_CHANGE_VERB_RE.search(text):
        return None
    if _has_concrete_target(text):
        return None
    return (
        "execute-only task: running or verifying tests produces no code change, so "
        "any PR would be a phantom — tests already run in CI on every PR"
    )


# Measured-evidence gate (2026-07-27). Gradient verbs assert "make X better"
# — a direction without a baseline. Fix-verbs (fix/repair/resolve) are NOT
# here: a fix claim names a broken observable, which is evidence by itself.
_GRADIENT_CLAIM_RE = re.compile(
    r"\b(improv\w*|enhanc\w*|optimi[sz]\w*|increas\w*|reduc\w*|streamlin\w*|boost\w*)\b",
    re.IGNORECASE,
)
# Pointer digits must not masquerade as measurements (PR #322 rule):
# task IDs, PR refs, and internal identifiers (G7, P25, WS-120, E8) are
# stripped before looking for a number.
_TASK_ID_POINTER_RE = re.compile(r"task-\d{14}-[0-9a-f]{6}")
_PR_REF_POINTER_RE = re.compile(r"(?:PR\s*)?#\d{2,}\b")
_INTERNAL_ID_RE = re.compile(r"\b(?:G|P|E|T|WS-?)\d+(?:\.\d+)?\b")
_ANY_DIGIT_RE = re.compile(r"\d")
# A structured spec is a deliberate brief shape, not a vague action — it
# passes without a number (initiative tasks with acceptance criteria, the
# autofill briefs enriched by PR #322).
_STRUCTURED_SPEC_MARKERS = (
    "**acceptance criteria:**",
    "**target file:**",
    "**test requirements:**",
    "**verified pointers**",
)
_GREENFIELD_INFRA_NAMES = frozenset({"conftest.py", "setup.py", "__init__.py"})


def _check_measured_evidence(
    title: str, description: str, repo_root: Path | str | None
) -> str | None:
    """Reject an unquantified improvement-gradient claim (extends PR #322).

    Triggers ONLY when all of these hold: the text makes a gradient claim
    (improve/optimize/reduce/...), carries no number once pointer digits are
    stripped, has no structured spec section, and the repo is not greenfield.
    The 07-24 experiment: the same goal produced a judge-rejected phantom
    from "improve success rate" and a real fix from the brief that said
    which rate, measured from what. Fails OPEN on any error — a broken glob
    must never block work.
    """
    text = f"{title}\n{description}"
    if not _GRADIENT_CLAIM_RE.search(text):
        return None
    lower = text.lower()
    if any(marker in lower for marker in _STRUCTURED_SPEC_MARKERS):
        return None
    cleaned = _TASK_ID_POINTER_RE.sub(" ", text)
    cleaned = _PR_REF_POINTER_RE.sub(" ", cleaned)
    cleaned = _INTERNAL_ID_RE.sub(" ", cleaned)
    cleaned = _CONCRETE_FILE_RE.sub(" ", cleaned)
    if _ANY_DIGIT_RE.search(cleaned):
        return None
    # Greenfield exemption: nothing exists to measure yet — the success
    # metric is the spec (same rule as the PR #322 autofill gate).
    try:
        root = Path(repo_root) if repo_root else _default_repo_root()
        has_source = any(
            f.name not in _GREENFIELD_INFRA_NAMES and ".claude" not in str(f)
            for f in list(root.glob("*.py")) + list(root.glob("src/**/*.py"))
        )
        if not has_source:
            return None
    except OSError:
        return None
    return (
        "unquantified improvement claim: the brief asserts a gradient "
        "(improve/optimize/reduce) but carries no baseline measurement — "
        "state what the evidence measures (rate, count, failing IDs) or add "
        "acceptance criteria"
    )


def _is_code_identifier(symbol: str) -> bool:
    """Filter out plain English words; keep things that look like code symbols."""
    if "_" in symbol:
        return True
    if re.search(r"[a-z][A-Z]", symbol):  # camelCase
        return True
    return False


def _symbol_in_negation_context(match: re.Match, text: str) -> bool:
    """True when a backtick symbol match is declared absent by surrounding text.

    Checks a short window before and after the match for absence/negation
    language so identifiers like `mcpServers` in "no `mcpServers` config found"
    are not treated as required-to-exist targets.

    Only called for _BACKTICK_SYMBOL_RE matches, not for parenthesised calls.
    """
    pre = text[max(0, match.start() - _NEGATION_CTX_WINDOW) : match.start()]
    post = text[match.end() : match.end() + _NEGATION_CTX_WINDOW]
    return bool(_NEGATION_PRE_RE.search(pre) or _NEGATION_POST_RE.match(post))


def _extract_symbols(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for regex in (_PARENS_SYMBOL_RE, _BACKTICK_SYMBOL_RE):
        for match in regex.finditer(text):
            sym = match.group(1)
            if sym in seen:
                continue
            if _is_code_identifier(sym):
                # Backtick identifiers in negation context (e.g. "no `mcpServers`
                # found") are skipped — but NOT added to `seen`, so a later
                # non-negated occurrence of the same symbol is still validated.
                if regex is _BACKTICK_SYMBOL_RE and _symbol_in_negation_context(
                    match, text
                ):
                    continue
                seen.add(sym)
                found.append(sym)
    return found


def _extract_py_paths(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PY_PATH_RE.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _is_reference_occurrence(match: re.Match, text: str) -> bool:
    """True when this one mention of a .py path is evidence/example, not a target.

    See the "Reference-only path mentions" block above for the categories.
    Windows are bounded (≤120 chars) so every regex runs on short input.
    """
    path = match.group(1)
    basename = path.rsplit("/", 1)[-1]
    stem = basename[: -len(".py")]
    start, end = match.start(1), match.end(1)
    # Glob / template shapes: "*_probe.py", "<name>.py", "{x}.py".
    if start > 0 and text[start - 1] in "*<{":
        return True
    # Leading-underscore bare name ("_probe.py", "_dbg_x.py"): a pattern or a
    # scratch name, not a module path. Dunder files (__init__.py) excluded.
    if "/" not in path and stem.startswith("_") and not stem.startswith("__"):
        return True
    if stem.lower() in _PLACEHOLDER_STEMS:
        return True
    pre = text[max(0, start - 120) : start]
    pre = pre.rsplit("\n", 1)[-1]  # never look across a line break
    post = text[end : end + 40]
    if _EXAMPLE_PRE_RE.search(pre):
        return True
    if _PAST_EVIDENCE_PRE_RE.search(pre):
        return True
    if _ABSENT_PRE_RE.search(pre) or _ABSENT_POST_RE.match(post):
        return True
    return False


def _target_py_paths(text: str) -> list[str]:
    """``.py`` paths with at least one mention in a TARGET role.

    A path mentioned only as evidence/example/pattern/absent is dropped; a
    path with even one plain mention is kept (fail toward validating it).
    """
    roles: dict[str, bool] = {}
    for match in _PY_PATH_RE.finditer(text):
        path = match.group(1)
        is_target = not _is_reference_occurrence(match, text)
        roles[path] = roles.get(path, False) or is_target
    return [p for p, is_target in roles.items() if is_target]


def _creation_verb_governs_path(path: str, text: str) -> bool:
    """True when a creation verb directly governs a TARGET mention of *path*.

    Stricter than :func:`_has_creation_verb_near_path` (which stays the lenient
    greenfield signal for the target-existence check, where a false "yes" only
    admits): here a false "yes" REJECTS, so the verb must precede the path in
    the same clause ("create pkg/foo.py", "scaffold a new `foo.py`").
    """
    for match in _PY_PATH_RE.finditer(text):
        if match.group(1) != path or _is_reference_occurrence(match, text):
            continue
        pre = text[max(0, match.start(1) - 160) : match.start(1)]
        if _CREATION_GOVERNS_PRE_RE.search(pre.rsplit("\n", 1)[-1]):
            return True
    return False


def _overlap_tokens(text: str, exclude: set[str]) -> set[str]:
    return {
        tok
        for tok in _OVERLAP_TOKEN_RE.findall(text.lower())
        if len(tok) >= 4 and tok not in _OVERLAP_STOPWORDS and tok not in exclude
    }


def _title_overlap(pr_title: str, task_text: str, path: str) -> list[str]:
    """Distinctive words the merged PR's title shares with the brief.

    Returns the shared words when they clear both thresholds, else ``[]``.
    The file's own name is excluded: both texts naming the file is exactly
    the non-evidence that caused the PR-SCRATCH-GUARD false positive.
    """
    stem = path.rsplit("/", 1)[-1][: -len(".py")]
    exclude = set(_OVERLAP_TOKEN_RE.findall(stem.lower()))
    pr_tokens = _overlap_tokens(_PR_TITLE_NOISE_RE.sub(" ", pr_title or ""), exclude)
    if not pr_tokens:
        return []
    shared = sorted(pr_tokens & _overlap_tokens(task_text, exclude))
    if len(shared) >= _OVERLAP_MIN_SHARED and len(shared) >= _OVERLAP_MIN_SHARE * len(
        pr_tokens
    ):
        return shared
    return []


def _has_creation_verb_near_path(path: str, text: str) -> bool:
    """True when a creation verb appears within ±150 chars of *path* in *text*.

    Tries the full path and the basename so both "create scripts/foo.py" and
    "create foo.py" match.  Fails silently (returns False) on any error.
    """
    targets = {path, path.rsplit("/", 1)[-1]}
    for target in targets:
        pat = re.escape(target)
        for m in re.finditer(pat, text, re.IGNORECASE):
            window = text[max(0, m.start() - 150) : m.end() + 150]
            if _DELIVERABLE_CREATION_VERB_RE.search(window):
                return True
    return False


def _is_goal_deliverable(path: str, repo_root: Path | str | None) -> bool:
    """True when *path*'s basename is named in any active goal's description or metric.

    Reuses ``_load_active_goals`` (fail-open on error).  Matches by basename so
    ``scripts/snipstash.py`` matches a goal that mentions ``snipstash.py``.
    """
    try:
        goals = _load_active_goals(repo_root)
        if not goals:
            return False
        basename = path.rsplit("/", 1)[-1]
        for goal in goals:
            combined = (
                f"{getattr(goal, 'description', '')} "
                f"{getattr(goal, 'success_metric', '')}"
            )
            if basename in combined:
                return True
    except Exception:
        pass
    return False


def _check_targets_exist(
    title: str,
    description: str,
    searcher: RepoSearcher,
    repo_root: Path | str | None = None,
) -> str | None:
    text = f"{title}\n{description}"
    # Only validate targets for tasks that operate on existing code. An
    # "implement new X" task legitimately names things that do not exist yet.
    if not _OPERATE_ON_EXISTING_RE.search(text):
        return None

    for symbol in _extract_symbols(text):
        if not searcher.has_symbol(symbol):
            return f"target not found: {symbol}"

    # Only paths named in a TARGET role: evidence ("#661 added _probe_x.py"),
    # examples ("e.g. hooks/x.py"), patterns and declared-absent files are
    # not work targets, so their absence proves nothing about staleness.
    for path in _target_py_paths(text):
        basename = path.rsplit("/", 1)[-1]
        # Test deliverable files may legitimately not exist yet (they are the
        # thing being created); only validate non-test source paths.
        if path.startswith("tests/") or basename.startswith("test_"):
            continue
        if not searcher.has_file(basename):
            # Greenfield deliverable: a creation verb adjacent to the path, or the
            # path is named as a deliverable in an active goal's description/metric.
            # Both signals treat the missing file as expected output, not a typo.
            if _has_creation_verb_near_path(path, text):
                continue
            if _is_goal_deliverable(path, repo_root):
                continue
            return f"target file not found: {path}"
    return None


def _candidate_test_targets(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for symbol in _extract_symbols(text):
        if symbol not in seen:
            seen.add(symbol)
            names.append(symbol)
    for match in _FOR_TARGET_RE.finditer(text):
        raw = match.group(1)
        name = raw.rsplit(".", 1)[0] if raw.endswith(".py") else raw
        name = name.rsplit("/", 1)[-1]
        # Only plausible lowercase module identifiers — drops Title-case English
        # noise ("Document"/"Write"/"Workers") and tracker tokens ("WS-124")
        # pulled out of malformed artifact titles.
        if name not in seen and _MODULE_NAME_RE.fullmatch(name):
            seen.add(name)
            names.append(name)
    return names


def _check_not_already_done(
    title: str, description: str, searcher: RepoSearcher
) -> str | None:
    text = f"{title}\n{description}"
    if _TEST_INTENT_RE.search(text):
        # Incremental-coverage work (backfill / raise-from-N% / missing tests)
        # is legitimate even when a test file already exists — don't block it.
        if _INCREMENTAL_INTENT_RE.search(text):
            return None
        for name in _candidate_test_targets(text):
            if searcher.test_exists_for(name):
                return f"already has tests: tests/test_{name}.py"
    return None


def _check_capability_already_shipped(
    title: str,
    description: str,
    searcher: RepoSearcher,
    repo_root: Path | str | None = None,
) -> str | None:
    """Reject a task that claims to CREATE a capability which already exists
    on disk AND was merged within the last two weeks.

    Closes the gap documented in docs/G7-SUCCESS-RATE-SECURITY-ASSESSMENT-
    20260725.md (MEDIUM-1): ``_check_not_already_done`` only matches
    test-file duplication; it has no path for "a hook/control matching this
    task's described capability already exists." Two independently-generated
    tasks (e.g. a scout proposal and an unrelated organic queue-fill task)
    can name the same not-yet-built capability; whichever is dispatched
    second finds the file already there, correctly no-ops, and the no-op is
    logged as a ``success_detection_failure`` phantom that deflates the
    measured success rate without any real defect (docs example:
    ``network_egress_guard.py``, task-20260722195847-38db6d).

    Deliberately narrower than the assessment's remediation sketch: it fires
    only when the task also uses a creation verb near the path (mirrors
    ``_check_targets_exist``'s ``_has_creation_verb_near_path``), not on any
    named path recently touched by a merged PR. Actively-maintained files
    (``work_allocator.py``, ``operation_loop.py``, ...) get merged PRs
    touching them multiple times a week as ordinary maintenance; without the
    creation-verb gate this check would reject most tasks naming them.
    Requiring "claims to build X" + "X already exists" + "X shipped
    recently" keeps it targeted at the actual duplicate-creation failure
    mode instead of blocking routine incremental work.

    2026-09-26 (PR-SCRATCH-GUARD false positive): "a merged PR touched the
    file" is not "the capability shipped". The brief asked for a NEW check in
    ``pr_output_manager.py`` and was refused because PR #654 had added an
    unrelated label there, with the creation verb taken from a quoted error
    message 150 chars away. Now all three must hold:
      1. the creation verb GOVERNS a target mention of the path
         (:func:`_creation_verb_governs_path`);
      2. the file exists and a PR merged in the last 14 days touched it;
      3. that PR shipped THIS capability: it ADDED the file (GitHub file
         status ``added`` — the duplicate-creation case this check exists
         for), or its title shares the brief's distinctive words
         (:func:`_title_overlap` — a re-minted brief whose earlier copy the
         daemon already shipped; daemon PR titles are the brief's opening).
    Every recent PR touching the file is tried, not only the newest.

    Fails OPEN on any error, matching every other admission check.
    """
    text = f"{title}\n{description}"
    try:
        from . import work_allocator as wa
    except ImportError:
        import work_allocator as wa  # type: ignore[no-redef]

    for path in _target_py_paths(text):
        basename = path.rsplit("/", 1)[-1]
        if not searcher.has_file(basename):
            continue
        if not _creation_verb_governs_path(path, text):
            continue

        evidence: dict[object, str | None] = {}

        def _evidence(hit: dict, _path: str = path) -> str | None:
            key = hit.get("pr_number")
            if key in evidence:
                return evidence[key]
            found: str | None = None
            shared = _title_overlap(str(hit.get("title") or ""), text, _path)
            if shared:
                found = f"title matches the brief: {', '.join(shared)}"
            else:
                added = hit.get("added")
                if added is None:
                    try:
                        added = wa.pr_added_file(
                            key, hit.get("path") or "", project_root=repo_root
                        )
                    except Exception:
                        added = None
                if added is True:
                    found = f"it added {hit.get('path') or _path}"
            evidence[key] = found
            return found

        try:
            hit = wa.check_file_recently_shipped(
                basename,
                project_root=repo_root,
                predicate=lambda h: _evidence(h) is not None,
            )
        except Exception:
            continue
        # Re-check: a stand-in lookup may ignore the predicate.
        why = _evidence(hit) if hit else None
        if why:
            return (
                f"capability already shipped: {basename} "
                f"(PR {hit.get('pr_number')}: {why})"
            )
    return None


# --------------------------------------------------------------------------- #
# Config + logging
# --------------------------------------------------------------------------- #


def _default_repo_root() -> Path:
    """Best-effort repo-root resolution (``<repo>/.company`` -> ``<repo>``)."""
    try:
        try:
            from . import company_resolver as cr  # type: ignore[attr-defined]
        except ImportError:
            import company_resolver as cr  # type: ignore[no-redef]
        return cr.get_company_dir().parent
    except Exception:
        return Path.cwd()


def load_admission_config(repo_root: Path | str | None) -> dict:
    """Load ``taskAdmission`` config from the canonical root ``gatesmith.json``.

    Root config is canonical since #1052. Unknown keys are ignored; missing
    keys fall back to ``DEFAULT_CONFIG``.
    """
    config = dict(DEFAULT_CONFIG)
    if repo_root is None:
        repo_root = _default_repo_root()
    cfg_path = Path(repo_root) / "gatesmith.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        section = raw.get("taskAdmission")
        if isinstance(section, dict):
            config.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
    except (OSError, json.JSONDecodeError):
        pass
    return config


def log_rejection(
    repo_root: Path | str,
    task: dict,
    reason: str,
    *,
    shadow: bool = False,
) -> None:
    """Append a rejection record so Phase 3 can learn from it. Never raises."""
    try:
        path = (
            Path(repo_root) / ".company" / "state" / "task_admission_rejections.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "source": task.get("source"),
            "reason": reason,
            "shadow": shadow,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass

    if not shadow:
        _maybe_escalate_repeat_rejection(repo_root, task, reason)


def _rejection_identity(title: object) -> str:
    """Stable key for 'the same work, re-minted under a new task_id'."""
    return " ".join(str(title or "").lower().split())


def _count_recent_rejections(path: Path, identity: str, window_hours: float) -> int:
    """Non-shadow rejections of this identity inside the window. Never raises."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    count = 0
    try:
        with open(path, encoding="utf-8") as handle:
            # The log is append-only and unbounded; only the tail can be in
            # window, so cap the scan rather than growing with file size.
            lines = handle.readlines()[-_REJECTION_SCAN_LINES:]
    except OSError:
        return 0
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if row.get("shadow"):
            continue
        if _rejection_identity(row.get("title")) != identity:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            count += 1
    return count


def _maybe_escalate_repeat_rejection(
    repo_root: Path | str,
    task: dict,
    reason: str,
) -> None:
    """Page a human once the same work has been rejected repeatedly.

    A single rejection is a correct, quiet outcome. The same title rejected
    N times means the planner and the gate disagree permanently, and neither
    side can break the tie — the work is either wrongly scoped (planner should
    stop minting it) or wrongly gated (a real item the gate cannot see the
    value of). Both readings need a human, and until this existed neither
    produced any signal at all. Never raises: admission must not fail because
    escalation bookkeeping did.
    """
    try:
        config = load_admission_config(repo_root)
        if not config.get("escalateRepeatRejections", True):
            return

        identity = _rejection_identity(task.get("title"))
        if not identity:
            return

        company = Path(repo_root) / ".company"
        log_path = company / "state" / "task_admission_rejections.jsonl"
        threshold = int(config.get("repeatRejectionThreshold", 3))
        window = float(config.get("repeatRejectionWindowHours", 24))

        if _count_recent_rejections(log_path, identity, window) < threshold:
            return

        slug = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
        esc_dir = company / "escalations"
        dedup_hours = float(config.get("repeatRejectionDedupHours", 12))
        now = datetime.now(timezone.utc)

        # One page per title per dedup window, however many times it re-mints.
        for existing in esc_dir.glob(f"admission-repeat-{slug}-*.json"):
            try:
                prior = json.loads(existing.read_text(encoding="utf-8"))
                when = datetime.fromisoformat(
                    str(prior.get("created_at", "")).replace("Z", "+00:00")
                )
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if (now - when).total_seconds() < dedup_hours * 3600:
                    return
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue

        record_id = f"admission-repeat-{slug}-{now.strftime('%Y%m%d%H%M')}"
        title = str(task.get("title") or "")[:120]
        record = {
            "task_id": record_id,
            "current_tier": 2,
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "original_agent": "task_admission",
            "current_agent": None,
            "trigger": "repeat_admission_rejection",
            "trigger_details": {
                "title": title,
                "identity": identity,
                "rejections_in_window": _count_recent_rejections(
                    log_path, identity, window
                ),
                "threshold": threshold,
                "window_hours": window,
                "latest_reason": str(reason)[:400],
                "latest_task_id": task.get("task_id"),
                "note": (
                    "The planner keeps re-minting this work under a new task_id "
                    "and the gate keeps rejecting it. Decide which side is wrong: "
                    "stop minting it, or admit it."
                ),
            },
            "events": [
                {
                    "timestamp": now.isoformat(),
                    "tier": 2,
                    "trigger": "repeat_admission_rejection",
                    "action_taken": f"Repeatedly rejected: {title}",
                    "resolved": False,
                }
            ],
            "metadata": {
                "title": f"Repeatedly rejected by admission: {title}",
                "condition": "repeat_admission_rejection",
            },
        }
        esc_dir.mkdir(parents=True, exist_ok=True)
        (esc_dir / f"{record_id}.json").write_text(json.dumps(record, indent=2))
    except Exception:
        return

    # Fire the same opt-in page silence_sentinel uses (osascript / webhook,
    # both off by default). Writing the file alone only makes the escalation
    # discoverable by someone already looking at the directory.
    try:
        try:
            from . import escalation as escalation_mod  # type: ignore[attr-defined]
        except ImportError:
            import escalation as escalation_mod  # type: ignore[no-redef]

        escalation_mod.fire_escalation_opt_in_notifications(
            escalation_mod.EscalationRecord(
                task_id=record["task_id"],
                current_tier=2,
                status="pending",
                created_at=record["created_at"],
                updated_at=record["updated_at"],
                original_agent="task_admission",
                trigger="repeat_admission_rejection",
            ),
            record["metadata"]["title"],
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _load_antipatterns(repo_root: Path | str | None) -> list[dict]:
    """Best-effort load of the learned anti-pattern store (empty on any error)."""
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la.load_antipatterns(repo_root)
    except Exception:
        return []


def _check_anti_patterns(task: dict, antipatterns: list[dict]) -> str | None:
    """Reject if the task matches a learned anti-pattern (confirmed phantom class)."""
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la.match_task(task, antipatterns)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# PB-2 — semantic relevance gate (the missing upstream gate)
#
# The deterministic checks above are blind to "novel, plausible, and pointless":
# a gated task like "Developer Experience" or "Streamline the workflow" names no
# concrete target, matches no anti-pattern, and is not a duplicate — so it sails
# through, which is exactly how an idle company's ideation navel-gazes its way
# into phantom work. This gate asks an LLM judge whether the task concretely
# advances an ACTIVE product goal (from .company/vision.md). It runs LAST, only
# after every cheap check has passed, and it DEGRADES to admit on any failure —
# no goals to judge against, no claude CLI, timeout, bad JSON — because a judge
# outage must never hard-block the whole company. It only ever *rejects* on a
# confident, parseable "not relevant" verdict.
# --------------------------------------------------------------------------- #

# Cache verdicts within a process run, keyed by (task, active-goals) signature,
# so re-admission attempts and shadow/real double-calls don't re-invoke the model.
_SEMANTIC_CACHE: dict[str, str | None] = {}


def _claude_prefix() -> list[str]:
    """Canonical Claude CLI invocation prefix, or [] if unavailable.

    Mirrors deliverable_judge._claude_cmd_prefix; kept local so this module has no
    heavy import dependency on the judge.
    """
    if shutil.which("uv"):
        return ["uv", "run", "claude"]
    if shutil.which("claude"):
        return ["claude"]
    return []


def _load_active_goals(repo_root: Path | str | None) -> list:
    """Active goals parsed from .company/vision.md, or [] (fail-open).

    Reuses goal_tracker's vision parser. Returns [] when the file is absent, has
    no parseable goal table, or on any error — the caller treats [] as
    'nothing to judge against' and admits.
    """
    try:
        root = Path(repo_root) if repo_root is not None else _default_repo_root()
        vision = root / ".company" / "vision.md"
        if not vision.exists():
            return []
        try:
            from . import goal_tracker as gt  # type: ignore[attr-defined]
        except ImportError:
            import goal_tracker as gt  # type: ignore[no-redef]
        default_goals = getattr(gt, "DEFAULT_GOALS", object())
        goals = gt.parse_goals_from_vision(vision, period="active")
        if goals is default_goals:
            goals = gt.parse_goals_from_vision(vision, period="all")
        if not goals or goals is default_goals:
            return []
        return list(goals)
    except Exception:
        return []


def _relevance_prompt(title: str, description: str, goals: list) -> str:
    lines = []
    for g in goals:
        gid = getattr(g, "id", "?")
        name = getattr(g, "name", "")
        desc = getattr(g, "description", "")
        metric = getattr(g, "success_metric", "")
        lines.append(f"- {gid}: {name} — {desc} (success metric: {metric})")
    goal_block = "\n".join(lines)
    return (
        "You are the admission gate for an autonomous software company. Decide "
        "whether a PROPOSED TASK does concrete work toward one of the company's "
        "ACTIVE product goals. Building, testing, or documenting the product "
        "counts. Vague process/organisational improvement that does not advance a "
        "listed goal does NOT count.\n\n"
        f"ACTIVE GOALS:\n{goal_block}\n\n"
        f"PROPOSED TASK:\nTitle: {title}\nDescription: {description}\n\n"
        "Reply with ONE JSON object and nothing else: "
        '{"relevant": true|false, "reason": "<one short sentence>"}'
    )


def _check_semantic_relevance(
    title: str, description: str, repo_root: Path | str | None, config: dict
) -> str | None:
    """Reject a gated task the LLM judges irrelevant to active goals; else None.

    Degrades to None (admit) on every failure path.
    """
    goals = _load_active_goals(repo_root)
    if not goals:
        return None  # nothing to judge against — don't block

    sig = hashlib.sha1(
        (
            title
            + "\x1f"
            + description
            + "\x1f"
            + "|".join(str(getattr(g, "id", "")) for g in goals)
        ).encode("utf-8")
    ).hexdigest()
    if sig in _SEMANTIC_CACHE:
        return _SEMANTIC_CACHE[sig]

    prefix = _claude_prefix()
    if not prefix:
        _SEMANTIC_CACHE[sig] = None  # no CLI — degrade to admit
        return None

    cmd = list(prefix)
    model = config.get("semanticModel")
    if model:
        cmd.extend(["--model", str(model)])
    cmd.extend(["-p", _relevance_prompt(title, description, goals)])

    verdict: str | None = None
    # 2026-07-06 fork-bomb guard: the semantic gate launches a real claude process.
    assert_spawn_allowed("task_admission._check_semantic_relevance", subprocess.run)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(config.get("semanticTimeoutSeconds", 60)),
        )
        if result.returncode == 0 and result.stdout:
            out = result.stdout.strip()
            m = re.search(r"\{.*\}", out, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if data.get("relevant") is False:
                    why = str(data.get("reason", "")).strip()
                    verdict = "not relevant to any active product goal" + (
                        f": {why}" if why else ""
                    )
    except (
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ):
        verdict = None  # any failure degrades to admit

    _SEMANTIC_CACHE[sig] = verdict
    return verdict


# roadmap_scheduler appends this block (task id, wave, `Source:` path) to
# every roadmap task. It names ROADMAP.md and numbers, which satisfied the
# concrete-deliverable and measured-evidence checks for a brief that had
# neither; the gate judges the brief alone (book chapter 4, 2026-10-30).
_ROADMAP_FOOTER = re.compile(r"\n---\n\*\*Roadmap Task ID:\*\*.*\Z", re.DOTALL)


def _strip_roadmap_footer(description: str) -> str:
    return _ROADMAP_FOOTER.sub("", description)


def admit_task(
    task: dict,
    queue: dict | None = None,
    repo_root: Path | str | None = None,
    config: dict | None = None,
    antipatterns: list[dict] | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a task may enter the queue.

    Returns ``(True, None)`` to admit, or ``(False, reason)`` to reject. Pure with
    respect to side effects: no logging, no queue writes, no shadow-mode handling
    (the wiring layer owns those). ``human``-sourced tasks always admit.

    ``antipatterns`` may be passed for testability; if None and the anti-pattern
    check is enabled it is loaded lazily from ``repo_root`` (fail-open on error).
    """
    if not _is_gated_source(task.get("source")):
        return True, None

    if config is None:
        config = load_admission_config(repo_root)
    if not config.get("enabled", True):
        return True, None

    title = task.get("title") or ""
    description = _strip_roadmap_footer(task.get("description") or "")

    # Cheap, IO-free checks first.
    if config.get("checkDuplicateTitle", True):
        reason = _check_duplicate_title_artifact(title)
        if reason:
            return False, reason

    if config.get("valueFloor", True):
        reason = _check_value_floor(task)
        if reason:
            return False, reason

    if config.get("checkConcreteDeliverable", True):
        reason = _check_concrete_deliverable(title, description)
        if reason:
            return False, reason

    if config.get("checkExecuteOnly", True):
        reason = _check_execute_only_task(title, description)
        if reason:
            return False, reason

    # Learned anti-patterns (Phase 3) — cheap signature compare. Fail OPEN on error.
    if config.get("checkAntiPatterns", True):
        if antipatterns is None:
            antipatterns = _load_antipatterns(repo_root)
        if antipatterns:
            reason = _check_anti_patterns(task, antipatterns)
            if reason:
                return False, reason

    # IO checks — fail OPEN on any error (a flaky scan must never block work).
    if config.get("checkNotAlreadyDone", True) or config.get("checkTargetsExist", True):
        if repo_root is None:
            repo_root = _default_repo_root()
        searcher = _get_searcher(repo_root)

        if config.get("checkNotAlreadyDone", True):
            try:
                reason = _check_not_already_done(title, description, searcher)
                if reason:
                    return False, reason
            except Exception:
                pass

        if config.get("checkTargetsExist", True):
            try:
                reason = _check_targets_exist(title, description, searcher, repo_root)
                if reason:
                    return False, reason
            except Exception:
                pass

        if config.get("checkCapabilityAlreadyShipped", True):
            try:
                reason = _check_capability_already_shipped(
                    title, description, searcher, repo_root
                )
                if reason:
                    return False, reason
            except Exception:
                pass

    # Measured-evidence gate (2026-07-27, PR #322 standard) — resolves its own
    # repo_root, so it runs regardless of the IO-check flags above.
    if config.get("checkMeasuredEvidence", True):
        try:
            reason = _check_measured_evidence(title, description, repo_root)
            if reason:
                return False, reason
        except Exception:
            pass

    # PB-2 semantic relevance — runs LAST (most expensive; an LLM call). Only
    # reached when every cheap/IO check has passed. Degrades to admit on any
    # failure, so a judge outage never hard-blocks the company.
    if config.get("checkSemanticRelevance", True):
        try:
            reason = _check_semantic_relevance(title, description, repo_root, config)
            if reason:
                return False, reason
        except Exception:
            pass

    return True, None
