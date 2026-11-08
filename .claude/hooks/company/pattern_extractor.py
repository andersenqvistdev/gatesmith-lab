#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
Pattern Extractor — Learn from successful task completions.

Analyzes completed tasks in .company/results/ to extract reusable patterns:
what approaches worked, which tools/files were involved, and in what sequence.
Appends deduplicated patterns to .company/knowledge/patterns.json.

Designed to run after each successful PR merge (e.g., via post-merge hook).

Usage:
    # Analyze all results and update patterns library
    python pattern_extractor.py extract

    # Analyze a specific task result
    python pattern_extractor.py extract --task-id task-20260328045910-98aa41

    # Show current pattern library summary
    python pattern_extractor.py list

    # Show help
    python pattern_extractor.py help
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

PATTERNS_JSON_FILE = "knowledge/patterns.json"
RESULTS_DIR = "results"

# Minimum confidence to add a pattern
MIN_CONFIDENCE = 0.6

# Minimum successes before a pattern is considered reliable
MIN_OCCURRENCES = 2

# Maximum patterns to keep per category (prevents unbounded growth)
MAX_PATTERNS_PER_CATEGORY = 50

# Jaccard similarity (on stopword-filtered title signatures) at/above which two
# patterns are considered "the same phenomenon" for merge/dedup purposes. Verified
# against test_patterns_similar_overlapping_words (0.5) and
# test_patterns_not_similar_few_words (0.0).
SIMILARITY_JACCARD_THRESHOLD = 0.3


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ExtractedPattern:
    """A pattern extracted from successful task completions."""

    pattern_id: str
    category: str
    title: str
    description: str
    approach: str
    tools_used: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    employee_roles: list[str] = field(default_factory=list)
    success_count: int = 1
    first_seen: str = ""
    last_seen: str = ""
    source_task_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedPattern":
        valid_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid_fields)


@dataclass
class TaskSummary:
    """Extracted summary of a completed task result."""

    task_id: str
    title: str
    employee_id: str
    complexity: str
    source: str
    files_changed: list[str]
    file_extensions: list[str]
    insertions: int
    deletions: int
    pr_url: str
    timestamp: str
    duration_seconds: float
    hooks_passed: list[str]
    plan_score: int
    tags: list[str] = field(default_factory=list)


# =============================================================================
# Company Directory Resolution
# =============================================================================


def _get_company_dir() -> Path:
    """Find .company directory, searching upward from cwd."""
    try:
        from company_paths import COMPANY_ROOT  # type: ignore[import]

        return COMPANY_ROOT
    except ImportError:
        pass

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / ".company"
        if candidate.is_dir():
            return candidate
    return cwd / ".company"


def _get_patterns_path(company_dir: Path | None = None) -> Path:
    """Return path to patterns.json, creating parent dirs if needed."""
    if company_dir is None:
        company_dir = _get_company_dir()
    path = company_dir / PATTERNS_JSON_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _get_results_dir(company_dir: Path | None = None) -> Path:
    """Return path to results directory."""
    if company_dir is None:
        company_dir = _get_company_dir()
    return company_dir / RESULTS_DIR


# =============================================================================
# Patterns JSON I/O
# =============================================================================


def _empty_patterns_data() -> dict[str, Any]:
    return {
        "version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "patterns": [],
    }


def load_patterns(company_dir: Path | None = None) -> dict[str, Any]:
    """Load patterns library from disk."""
    path = _get_patterns_path(company_dir)
    if not path.exists():
        return _empty_patterns_data()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load patterns.json: %s — starting fresh", e)
        return _empty_patterns_data()


def save_patterns_atomic(
    data: dict[str, Any],
    company_dir: Path | None = None,
) -> bool:
    """Atomically save patterns library to disk."""
    path = _get_patterns_path(company_dir)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2)
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        logger.error("Failed to save patterns.json: %s", e)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return False


# =============================================================================
# Task Result Parsing
# =============================================================================


def _infer_file_extensions(files_changed: list[str]) -> list[str]:
    """Extract unique file extensions from changed file list."""
    exts: set[str] = set()
    for f in files_changed:
        suffix = Path(f).suffix
        if suffix:
            exts.add(suffix.lstrip("."))
    return sorted(exts)


_TAG_KEYWORDS: list[tuple[str, str]] = [
    ("test", "testing"),
    ("fix", "bug-fix"),
    ("bug", "bug-fix"),
    ("feat", "feature"),
    ("refactor", "refactoring"),
    ("doc", "documentation"),
    ("race", "concurrency"),
    ("lock", "concurrency"),
    ("atomic", "atomicity"),
    ("audit", "enterprise"),
    ("coverage", "testing"),
    ("lint", "code-quality"),
    ("ruff", "code-quality"),
    ("security", "security"),
    ("pr", "workflow"),
    ("merge", "workflow"),
    ("daemon", "infrastructure"),
    ("heartbeat", "infrastructure"),
    ("queue", "infrastructure"),
    ("pattern", "architecture"),
]


def _infer_tags(task: TaskSummary) -> list[str]:
    """Infer semantic tags from task attributes."""
    tags: list[str] = []
    title_lower = task.title.lower()

    for keyword, tag in _TAG_KEYWORDS:
        if keyword in title_lower and tag not in tags:
            tags.append(tag)

    if "py" in task.file_extensions and "python" not in tags:
        tags.append("python")
    if "md" in task.file_extensions and "documentation" not in tags:
        tags.append("documentation")
    if "json" in task.file_extensions and "configuration" not in tags:
        tags.append("configuration")

    if task.complexity in ("complex", "epic"):
        tags.append("complex-task")

    if task.source == "self":
        tags.append("autonomous")

    return tags


def parse_task_result(result_file: Path) -> TaskSummary | None:
    """Parse a task result JSON file into a TaskSummary.

    Returns None for non-completed tasks (escalated/failed/in_progress).
    """
    try:
        with open(result_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Skipping %s: %s", result_file.name, e)
        return None

    if data.get("status") != "completed":
        return None

    task_id = data.get("task_id", result_file.stem)
    git = data.get("git", {})
    quality = data.get("quality", {})
    files_changed = git.get("files_changed", [])

    summary = TaskSummary(
        task_id=task_id,
        title=data.get("title", ""),
        employee_id=data.get("employee", {}).get("id", "unknown"),
        complexity=data.get("complexity", "standard"),
        source=data.get("source", "human"),
        files_changed=files_changed,
        file_extensions=_infer_file_extensions(files_changed),
        insertions=git.get("insertions", 0),
        deletions=git.get("deletions", 0),
        pr_url=git.get("pr_url", ""),
        timestamp=data.get("timestamp", ""),
        duration_seconds=data.get("duration_seconds", 0.0),
        hooks_passed=quality.get("hooks_passed", []),
        plan_score=quality.get("plan_score", 0),
    )
    summary.tags = _infer_tags(summary)
    return summary


def load_all_results(company_dir: Path | None = None) -> list[TaskSummary]:
    """Load all completed task results from .company/results/."""
    results_dir = _get_results_dir(company_dir)
    if not results_dir.exists():
        return []

    summaries: list[TaskSummary] = []
    for result_file in sorted(results_dir.glob("task-*.json")):
        summary = parse_task_result(result_file)
        if summary:
            summaries.append(summary)

    return summaries


# =============================================================================
# Pattern Extraction Logic
# =============================================================================


def _generate_pattern_id(category: str, title: str) -> str:
    """Generate a stable, human-readable pattern ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    cat_slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")
    return f"pat-{cat_slug}-{slug}"


def _classify_task(summary: TaskSummary) -> str:
    """Classify a task into a pattern category."""
    tags = summary.tags

    if "testing" in tags:
        return "Testing"
    if "bug-fix" in tags:
        return "Bug Fix"
    if "concurrency" in tags or "atomicity" in tags:
        return "Concurrency"
    if "enterprise" in tags:
        return "Enterprise"
    if "infrastructure" in tags:
        return "Infrastructure"
    if "architecture" in tags:
        return "Architecture"
    if "feature" in tags:
        return "Feature Development"
    if "documentation" in tags:
        return "Documentation"
    if "code-quality" in tags or "refactoring" in tags:
        return "Code Quality"
    if "workflow" in tags:
        return "Workflow"
    return "General"


def _extract_approach(summary: TaskSummary) -> str:
    """Derive a human-readable approach description from task metadata."""
    parts: list[str] = []

    if summary.file_extensions:
        ext_list = ", ".join(f".{e}" for e in summary.file_extensions)
        parts.append(f"Modified {ext_list} files")

    if summary.insertions > 0 and summary.deletions > 0:
        parts.append(f"+{summary.insertions}/-{summary.deletions} lines")
    elif summary.insertions > 0:
        parts.append(f"+{summary.insertions} lines added")

    if summary.hooks_passed:
        parts.append(f"Passed hooks: {', '.join(summary.hooks_passed)}")

    if summary.plan_score > 0:
        parts.append(f"Plan score: {summary.plan_score}")

    if not parts:
        return "Task completed successfully with no tracked code changes."

    return ". ".join(parts) + "."


def _title_signature(title: str) -> list[str]:
    """Discriminating tokens for title grouping/similarity.

    Delegates to learned_antipatterns.signature(), which strips stopwords
    (e.g. "for", "test", "coverage", "improve") that would otherwise make
    unrelated tasks sharing only connector-word phrasing (e.g. "... for: X")
    look like a repeated pattern class — see idea-20260329151059-69bca0,
    fixed for employee_ideation.py in PR #232. Falls back to a naive split
    if the import fails, so this never hard-fails the caller.
    """
    try:
        try:
            from . import learned_antipatterns as la  # type: ignore[attr-defined]
        except ImportError:
            import learned_antipatterns as la  # type: ignore[no-redef]
        return la.signature(title)
    except Exception:
        return [w for w in title.lower().split() if len(w) > 3]


def extract_patterns_from_results(
    summaries: list[TaskSummary],
) -> list[ExtractedPattern]:
    """Extract patterns from a list of task summaries.

    Groups similar tasks together and identifies recurring patterns.
    """
    groups: dict[str, list[TaskSummary]] = {}

    for summary in summaries:
        category = _classify_task(summary)
        ext_key = ",".join(sorted(summary.file_extensions)) or "misc"
        # Discriminating title signature for similarity grouping. Falls back to
        # the coarse first-4-words prefix only when the signature is empty
        # (degenerate/very short title) so multiple such tasks don't all
        # collapse onto the same empty-string key.
        sig = _title_signature(summary.title)
        title_key = (
            " ".join(sig) if sig else " ".join(summary.title.lower().split()[:4])
        )
        group_key = f"{category}|{ext_key}|{title_key}"
        groups.setdefault(group_key, []).append(summary)

    now = datetime.now(timezone.utc).isoformat()
    patterns: list[ExtractedPattern] = []

    for _group_key, group_summaries in groups.items():
        if not group_summaries:
            continue

        # Representative summary: most recent
        rep = sorted(group_summaries, key=lambda s: s.timestamp, reverse=True)[0]
        category = _classify_task(rep)

        employee_roles = sorted(
            {s.employee_id for s in group_summaries if s.employee_id != "unknown"}
        )
        all_exts = sorted({ext for s in group_summaries for ext in s.file_extensions})
        all_hooks = sorted({h for s in group_summaries for h in s.hooks_passed})
        all_tags = sorted({tag for s in group_summaries for tag in s.tags})
        source_ids = [s.task_id for s in group_summaries]

        # Confidence rises with more occurrences and hook coverage
        raw_confidence = min(1.0, len(group_summaries) / MIN_OCCURRENCES * 0.5 + 0.5)
        if all_hooks:
            raw_confidence = min(1.0, raw_confidence + 0.1)

        if raw_confidence < MIN_CONFIDENCE:
            continue

        timestamps = [s.timestamp for s in group_summaries if s.timestamp]
        first_seen = min(timestamps) if timestamps else now
        last_seen = max(timestamps) if timestamps else now

        pattern_id = _generate_pattern_id(category, rep.title[:60])
        title = _make_pattern_title(rep, group_summaries)
        description = _make_pattern_description(rep, group_summaries)
        approach = _extract_approach(rep)

        patterns.append(
            ExtractedPattern(
                pattern_id=pattern_id,
                category=category,
                title=title,
                description=description,
                approach=approach,
                tools_used=all_hooks,
                file_types=all_exts,
                employee_roles=employee_roles,
                success_count=len(group_summaries),
                first_seen=first_seen,
                last_seen=last_seen,
                source_task_ids=source_ids,
                confidence=round(raw_confidence, 2),
                tags=all_tags,
            )
        )

    return patterns


def _make_pattern_title(rep: TaskSummary, group: list[TaskSummary]) -> str:
    """Generate a concise pattern title."""
    base = rep.title[:60].rstrip(".")
    count = len(group)
    if count > 1:
        return f"{base} ({count} occurrences)"
    return base


def _make_pattern_description(rep: TaskSummary, group: list[TaskSummary]) -> str:
    """Generate a description summarizing the pattern."""
    employees = sorted({s.employee_id for s in group if s.employee_id != "unknown"})
    parts = [f"Pattern observed in {len(group)} successful task(s)."]
    if employees:
        parts.append(f"Handled by: {', '.join(employees)}.")
    if rep.complexity:
        parts.append(f"Typical complexity: {rep.complexity}.")
    if rep.source:
        parts.append(f"Source: {rep.source}.")
    return " ".join(parts)


# =============================================================================
# Deduplication & Merge
# =============================================================================


def _patterns_are_similar(a: ExtractedPattern, b: ExtractedPattern) -> bool:
    """Return True if two patterns represent the same phenomenon."""
    if a.pattern_id == b.pattern_id:
        return True
    if a.category != b.category:
        return False
    # Compare discriminating title signatures (stopword-filtered), not raw
    # word overlap — two titles that only share a connector phrase like
    # "improve test coverage for: " would otherwise register as similar
    # regardless of what they're actually about.
    a_sig = set(_title_signature(a.title))
    b_sig = set(_title_signature(b.title))
    if not a_sig or not b_sig:
        # Signature too thin to be reliable — fall back to the old raw
        # first-5-word overlap check.
        a_words = set(a.title.lower().split()[:5])
        b_words = set(b.title.lower().split()[:5])
        return len(a_words & b_words) >= 3  # noqa: PLR2004
    union = a_sig | b_sig
    return (len(a_sig & b_sig) / len(union)) >= SIMILARITY_JACCARD_THRESHOLD


def merge_patterns(
    existing: list[dict[str, Any]],
    new_patterns: list[ExtractedPattern],
) -> list[dict[str, Any]]:
    """Merge new patterns into existing library with deduplication.

    For duplicates: increments success_count and updates last_seen.
    New unique patterns are appended. Returns the merged list.
    """
    existing_by_id: dict[str, dict[str, Any]] = {p["pattern_id"]: p for p in existing}
    result = list(existing)

    for new_pat in new_patterns:
        # Exact ID match
        if new_pat.pattern_id in existing_by_id:
            entry = existing_by_id[new_pat.pattern_id]
            entry["success_count"] = (
                entry.get("success_count", 1) + new_pat.success_count
            )
            entry["last_seen"] = new_pat.last_seen
            merged_ids = set(entry.get("source_task_ids", []))
            merged_ids.update(new_pat.source_task_ids)
            entry["source_task_ids"] = sorted(merged_ids)
            entry["confidence"] = min(
                1.0, max(entry.get("confidence", 0.0), new_pat.confidence)
            )
            continue

        # Similarity match
        matched = False
        for entry in result:
            try:
                existing_pat = ExtractedPattern.from_dict(entry)
            except (TypeError, KeyError):
                continue
            if _patterns_are_similar(existing_pat, new_pat):
                entry["success_count"] = (
                    entry.get("success_count", 1) + new_pat.success_count
                )
                entry["last_seen"] = new_pat.last_seen
                merged_ids = set(entry.get("source_task_ids", []))
                merged_ids.update(new_pat.source_task_ids)
                entry["source_task_ids"] = sorted(merged_ids)
                entry["confidence"] = min(
                    1.0, max(entry.get("confidence", 0.0), new_pat.confidence)
                )
                matched = True
                break

        if not matched:
            new_dict = new_pat.to_dict()
            result.append(new_dict)
            existing_by_id[new_pat.pattern_id] = new_dict

    # Enforce per-category limit (keep highest confidence)
    by_category: dict[str, list[dict[str, Any]]] = {}
    for p in result:
        by_category.setdefault(p.get("category", "General"), []).append(p)

    trimmed: list[dict[str, Any]] = []
    for cat_patterns in by_category.values():
        sorted_pats = sorted(
            cat_patterns,
            key=lambda p: (p.get("confidence", 0.0), p.get("success_count", 0)),
            reverse=True,
        )
        trimmed.extend(sorted_pats[:MAX_PATTERNS_PER_CATEGORY])

    return trimmed


# =============================================================================
# Public API
# =============================================================================


def extract_and_save(
    company_dir: Path | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Extract patterns from completed tasks and save to patterns.json.

    Args:
        company_dir: Path to .company directory. Auto-detected if None.
        task_id: If provided, only process this specific task result.

    Returns:
        Summary dict with keys: new_patterns, updated_patterns, total_patterns,
        tasks_analyzed, message.
    """
    if company_dir is None:
        company_dir = _get_company_dir()

    if task_id:
        result_file = _get_results_dir(company_dir) / f"{task_id}.json"
        summary = parse_task_result(result_file)
        summaries = [summary] if summary else []
    else:
        summaries = load_all_results(company_dir)

    if not summaries:
        return {
            "new_patterns": 0,
            "updated_patterns": 0,
            "total_patterns": 0,
            "tasks_analyzed": 0,
            "message": "No completed tasks found.",
        }

    new_patterns = extract_patterns_from_results(summaries)

    data = load_patterns(company_dir)
    existing = data.get("patterns", [])
    before_count = len(existing)

    merged = merge_patterns(existing, new_patterns)
    data["patterns"] = merged
    after_count = len(merged)
    save_patterns_atomic(data, company_dir)

    added = max(0, after_count - before_count)
    updated = max(0, len(new_patterns) - added)

    return {
        "new_patterns": added,
        "updated_patterns": updated,
        "total_patterns": after_count,
        "tasks_analyzed": len(summaries),
        "message": (
            f"Extracted {len(new_patterns)} pattern(s) from {len(summaries)} task(s). "
            f"Library: {after_count} patterns ({added} new, {updated} updated)."
        ),
    }


def list_patterns(company_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return all patterns from the library."""
    return load_patterns(company_dir).get("patterns", [])


# =============================================================================
# CLI
# =============================================================================


def _cmd_extract(args: argparse.Namespace) -> None:
    task_id = getattr(args, "task_id", None)
    result = extract_and_save(task_id=task_id)
    print(result["message"])
    print(f"  Tasks analyzed  : {result.get('tasks_analyzed', 0)}")
    print(f"  New patterns    : {result['new_patterns']}")
    print(f"  Updated         : {result['updated_patterns']}")
    print(f"  Total in library: {result['total_patterns']}")


def _cmd_list(_args: argparse.Namespace) -> None:
    patterns = list_patterns()
    if not patterns:
        print("No patterns in library yet. Run `extract` first.")
        return
    print(f"Pattern Library ({len(patterns)} patterns)\n{'=' * 40}")
    by_category: dict[str, list[dict[str, Any]]] = {}
    for p in patterns:
        by_category.setdefault(p.get("category", "General"), []).append(p)
    for cat, cat_pats in sorted(by_category.items()):
        print(f"\n[{cat}] ({len(cat_pats)} patterns)")
        for p in cat_pats:
            conf = p.get("confidence", 0.0)
            count = p.get("success_count", 0)
            print(f"  • {p.get('title', '?')[:70]} (conf={conf:.2f}, seen={count})")


def _cmd_help(_args: argparse.Namespace) -> None:
    print(__doc__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Pattern Extractor — Learn from successful task completions",
    )
    subparsers = parser.add_subparsers(dest="command")

    extract_parser = subparsers.add_parser(
        "extract", help="Extract patterns from task results"
    )
    extract_parser.add_argument(
        "--task-id", default=None, dest="task_id", help="Process only this task ID"
    )
    extract_parser.set_defaults(func=_cmd_extract)

    list_parser = subparsers.add_parser("list", help="List all patterns in the library")
    list_parser.set_defaults(func=_cmd_list)

    help_parser = subparsers.add_parser("help", help="Show detailed help")
    help_parser.set_defaults(func=_cmd_help)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
