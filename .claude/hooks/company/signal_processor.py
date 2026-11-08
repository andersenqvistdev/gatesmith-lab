#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
WS-067-001: External Signal Processor

Processes external signals (Stripe, Sentry, GitHub) to compute priority boosts
for work queue tasks. This module is READ-ONLY — it computes boosts but does
not modify the queue directly.

Design principles:
1. Additive only — signals boost priority, never reduce
2. Configurable — all rules in priority_rules.json
3. Isolated — no side effects on queue or other systems
4. Graceful degradation — returns empty boosts if signals unavailable

Integration:
    The daemon calls get_signal_boosts() during queue reordering.
    Boosts are added to existing task priorities, not replacing them.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default signal age limit (ignore signals older than this)
DEFAULT_SIGNAL_MAX_AGE_HOURS = 24

# Default priority rules if none configured
DEFAULT_PRIORITY_RULES: dict[str, Any] = {
    "stripe": {
        "charge.failed": {
            "priority_boost": 30,
            "keywords": ["payment", "billing", "checkout", "stripe"],
        },
        "subscription.canceled": {
            "priority_boost": 20,
            "keywords": ["subscription", "billing", "churn"],
        },
        "invoice.payment_failed": {
            "priority_boost": 25,
            "keywords": ["invoice", "payment", "billing"],
        },
    },
    "sentry": {
        "error": {
            "priority_boost_per_100_events": 10,
            "max_boost": 50,
            "keywords": ["bug", "fix", "error", "crash"],
        },
        "spike": {
            "priority_boost": 40,
            "keywords": ["urgent", "production", "critical"],
        },
    },
    "github": {
        "labels": {
            "customer-reported": {"priority_boost": 25},
            "security": {"priority_boost": 100},
            "p0": {"priority_boost": 75},
            "p1": {"priority_boost": 50},
            "bug": {"priority_boost": 15},
        },
    },
}


@dataclass
class Signal:
    """Represents an external signal."""

    source: str  # stripe, sentry, github
    event_type: str  # charge.failed, error, issue.opened
    timestamp: str  # ISO format
    data: dict  # Event-specific data
    processed: bool = False


@dataclass
class PriorityBoost:
    """Represents a priority boost for a task."""

    task_id: str | None  # Specific task, or None for keyword match
    keywords: list[str]  # Keywords to match in task title/description
    boost: float  # Priority boost amount
    reason: str  # Human-readable reason
    source: str  # Signal source
    expires_at: str | None  # ISO timestamp when boost expires


def load_signals(
    signals_dir: Path,
    max_age_hours: int = DEFAULT_SIGNAL_MAX_AGE_HOURS,
) -> list[Signal]:
    """
    Load recent signals from JSONL files.

    Args:
        signals_dir: Path to .company/signals/ directory
        max_age_hours: Ignore signals older than this

    Returns:
        List of Signal objects
    """
    signals = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    if not signals_dir.exists():
        return signals

    for signal_file in signals_dir.glob("*.jsonl"):
        try:
            source = signal_file.stem  # stripe, sentry, github
            with open(signal_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        timestamp = data.get("timestamp", "")
                        if timestamp:
                            signal_time = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                            if signal_time < cutoff:
                                continue  # Skip old signals

                        signals.append(
                            Signal(
                                source=source,
                                event_type=data.get("event_type", "unknown"),
                                timestamp=timestamp,
                                data=data,
                            )
                        )
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception as e:
            logger.debug(f"Error reading {signal_file}: {e}")

    return signals


def load_priority_rules(signals_dir: Path) -> dict[str, Any]:
    """
    Load priority rules from configuration.

    Args:
        signals_dir: Path to .company/signals/ directory

    Returns:
        Priority rules dictionary
    """
    rules_file = signals_dir / "priority_rules.json"
    if rules_file.exists():
        try:
            return json.loads(rules_file.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Error loading priority rules: {e}")

    return DEFAULT_PRIORITY_RULES


def _process_stripe_signal(
    signal: Signal,
    rules: dict[str, Any],
) -> PriorityBoost | None:
    """Process a Stripe webhook signal."""
    stripe_rules = rules.get("stripe", {})
    event_rules = stripe_rules.get(signal.event_type, {})

    if not event_rules:
        return None

    boost = event_rules.get("priority_boost", 0)
    keywords = event_rules.get("keywords", [])

    if boost <= 0:
        return None

    # Scale boost by amount if available (check higher threshold first)
    amount = signal.data.get("amount", 0)
    if amount > 50000:  # > $500
        boost = int(boost * 2.0)
    elif amount > 10000:  # > $100
        boost = int(boost * 1.5)

    return PriorityBoost(
        task_id=None,
        keywords=keywords,
        boost=boost,
        reason=f"Stripe {signal.event_type}",
        source="stripe",
        expires_at=None,
    )


def _process_sentry_signal(
    signal: Signal,
    rules: dict[str, Any],
) -> PriorityBoost | None:
    """Process a Sentry error/alert signal."""
    sentry_rules = rules.get("sentry", {})

    # Check for error spike
    if signal.event_type in ("error", "spike", "alert"):
        event_rules = sentry_rules.get(signal.event_type, sentry_rules.get("error", {}))

        count = signal.data.get("count", signal.data.get("event_count", 1))
        affected_users = signal.data.get("affected_users", 0)

        # Calculate boost based on severity
        if signal.event_type == "spike":
            boost = event_rules.get("priority_boost", 40)
        else:
            boost_per_100 = event_rules.get("priority_boost_per_100_events", 10)
            max_boost = event_rules.get("max_boost", 50)
            boost = min(int(count / 100) * boost_per_100, max_boost)

        # Extra boost for many affected users
        if affected_users > 100:
            boost = int(boost * 1.5)

        if boost <= 0:
            return None

        keywords = event_rules.get("keywords", ["bug", "fix", "error"])

        # Try to extract file path for more specific matching
        error_file = signal.data.get("file", signal.data.get("filename", ""))
        if error_file:
            # Add file-specific keyword
            keywords = keywords + [Path(error_file).stem]

        return PriorityBoost(
            task_id=None,
            keywords=keywords,
            boost=boost,
            reason=f"Sentry {signal.event_type} ({count} events)",
            source="sentry",
            expires_at=None,
        )

    return None


def _process_github_signal(
    signal: Signal,
    rules: dict[str, Any],
) -> PriorityBoost | None:
    """Process a GitHub issue/PR signal."""
    github_rules = rules.get("github", {})
    label_rules = github_rules.get("labels", {})

    labels = signal.data.get("labels", [])
    if isinstance(labels, list) and labels and isinstance(labels[0], dict):
        labels = [lbl.get("name", "") for lbl in labels]

    max_boost = 0
    matched_label = ""

    for label in labels:
        label_lower = label.lower()
        if label_lower in label_rules:
            boost = label_rules[label_lower].get("priority_boost", 0)
            if boost > max_boost:
                max_boost = boost
                matched_label = label

    if max_boost <= 0:
        return None

    # Extract keywords from issue title
    title = signal.data.get("title", "")
    keywords = _extract_keywords(title)

    return PriorityBoost(
        task_id=signal.data.get("linked_task_id"),  # If we linked it
        keywords=keywords,
        boost=max_boost,
        reason=f"GitHub label: {matched_label}",
        source="github",
        expires_at=None,
    )


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text."""
    # Remove common words and extract meaningful terms
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "and",
        "but",
        "or",
        "nor",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "not",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "also",
        "now",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "any",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "myself",
        "we",
        "our",
        "ours",
        "ourselves",
        "you",
        "your",
    }

    # Extract words
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text.lower())

    # Filter and return meaningful words
    keywords = [w for w in words if w not in stop_words and len(w) > 2]

    return keywords[:10]  # Limit to 10 keywords


def process_signals(
    signals: list[Signal],
    rules: dict[str, Any],
) -> list[PriorityBoost]:
    """
    Process signals and compute priority boosts.

    Args:
        signals: List of signals to process
        rules: Priority rules

    Returns:
        List of PriorityBoost objects
    """
    boosts = []

    processors = {
        "stripe": _process_stripe_signal,
        "sentry": _process_sentry_signal,
        "github": _process_github_signal,
    }

    for signal in signals:
        processor = processors.get(signal.source)
        if processor:
            boost = processor(signal, rules)
            if boost:
                boosts.append(boost)

    return boosts


def match_boost_to_task(
    task: dict[str, Any],
    boosts: list[PriorityBoost],
) -> float:
    """
    Calculate total priority boost for a task based on signals.

    Args:
        task: Task dictionary
        boosts: List of available priority boosts

    Returns:
        Total priority boost (additive)
    """
    total_boost = 0.0
    task_id = task.get("task_id", "")
    title = task.get("title", "").lower()
    description = task.get("description", "").lower()
    task_text = f"{title} {description}"

    for boost in boosts:
        # Direct task match
        if boost.task_id and boost.task_id == task_id:
            total_boost += boost.boost
            continue

        # Keyword match
        if boost.keywords:
            matched = any(kw.lower() in task_text for kw in boost.keywords)
            if matched:
                total_boost += boost.boost

    return total_boost


def get_signal_boosts(
    company_dir: Path,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """
    Get priority boosts for tasks based on external signals.

    This is the main entry point for daemon integration.

    Args:
        company_dir: Path to .company directory
        tasks: Optional list of tasks to compute boosts for

    Returns:
        Dictionary mapping task_id to priority boost
    """
    signals_dir = company_dir / "signals"

    if not signals_dir.exists():
        return {}

    try:
        # Load signals and rules
        signals = load_signals(signals_dir)
        rules = load_priority_rules(signals_dir)

        if not signals:
            return {}

        # Process signals into boosts
        boosts = process_signals(signals, rules)

        if not boosts:
            return {}

        # If no tasks provided, return general boosts keyed by keywords
        if not tasks:
            return {
                f"keyword:{','.join(b.keywords[:3])}": b.boost
                for b in boosts
                if b.keywords
            }

        # Compute per-task boosts
        result = {}
        for task in tasks:
            task_id = task.get("task_id", "")
            if task_id:
                boost = match_boost_to_task(task, boosts)
                if boost > 0:
                    result[task_id] = boost

        return result

    except Exception as e:
        logger.debug(f"Signal processing error: {e}")
        return {}


def write_signal(
    signals_dir: Path,
    source: str,
    event_type: str,
    data: dict[str, Any],
) -> bool:
    """
    Write a signal to the appropriate JSONL file.

    Used by webhook receiver to record incoming signals.

    Args:
        signals_dir: Path to .company/signals/ directory
        source: Signal source (stripe, sentry, github)
        event_type: Event type
        data: Event data

    Returns:
        True if written successfully
    """
    signals_dir.mkdir(parents=True, exist_ok=True)

    signal_file = signals_dir / f"{source}.jsonl"

    entry = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }

    try:
        with open(signal_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return True
    except Exception as e:
        logger.error(f"Failed to write signal: {e}")
        return False


def cleanup_old_signals(
    signals_dir: Path,
    max_age_hours: int = 72,
) -> int:
    """
    Remove signals older than max_age_hours.

    Args:
        signals_dir: Path to .company/signals/ directory
        max_age_hours: Remove signals older than this

    Returns:
        Number of signals removed
    """
    if not signals_dir.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    removed = 0

    for signal_file in signals_dir.glob("*.jsonl"):
        try:
            kept_lines = []
            with open(signal_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        timestamp = data.get("timestamp", "")
                        if timestamp:
                            signal_time = datetime.fromisoformat(
                                timestamp.replace("Z", "+00:00")
                            )
                            if signal_time >= cutoff:
                                kept_lines.append(line)
                            else:
                                removed += 1
                        else:
                            kept_lines.append(line)
                    except (json.JSONDecodeError, ValueError):
                        kept_lines.append(line)

            # Rewrite file with kept lines
            with open(signal_file, "w") as f:
                for line in kept_lines:
                    f.write(line + "\n")

        except Exception as e:
            logger.debug(f"Error cleaning {signal_file}: {e}")

    return removed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Signal Processor")
    parser.add_argument("command", choices=["show", "cleanup", "test"])
    parser.add_argument("--source", help="Signal source filter")
    args = parser.parse_args()

    company_dir = Path(__file__).resolve().parent.parent.parent.parent / ".company"
    signals_dir = company_dir / "signals"

    if args.command == "show":
        signals = load_signals(signals_dir)
        print(f"Found {len(signals)} signals:")
        for s in signals:
            print(f"  [{s.source}] {s.event_type}: {s.data.get('title', s.data)[:50]}")

    elif args.command == "cleanup":
        removed = cleanup_old_signals(signals_dir)
        print(f"Removed {removed} old signals")

    elif args.command == "test":
        # Write a test signal
        write_signal(
            signals_dir,
            "test",
            "test_event",
            {"title": "Test signal", "priority": 10},
        )
        print("Test signal written")
        boosts = get_signal_boosts(company_dir)
        print(f"Boosts: {boosts}")
