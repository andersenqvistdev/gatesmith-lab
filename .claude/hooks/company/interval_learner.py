#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P18 Adaptive Intervals — Session value scoring for optimal executive timing.

This module provides session scoring to help learn optimal executive loop intervals.
Sessions with low value scores indicate wasted cycles (no work submitted, no decisions).
Sessions with high value scores indicate productive timing.

Scoring Formula (WS-018):
    value = 0.5*(work_submitted>0) + 0.3*(decisions>0) + 0.2*(queue_empty)
    penalty = -0.2 if queue_pending_at_start > 5 else 0
    final_score = clamp(value + penalty, 0.0, 1.0)

Factor Breakdown:
    - work_submitted: 0.5 points if any work items were submitted
    - decisions: 0.3 points if any decisions were made
    - queue_empty: 0.2 points if queue was empty at session start
    - high_queue_penalty: -0.2 penalty if >5 items pending (backlog building)

Usage:
    # Score a session by ID
    python interval_learner.py score --session-id "sess-123"

    # Score session data directly (JSON)
    python interval_learner.py score-raw --session '{"work_submitted": 2, "decisions_count": 1}'

    # Detect patterns from session history
    python interval_learner.py patterns

    # Get optimal interval recommendation
    python interval_learner.py recommend
    python interval_learner.py recommend --current-interval 6.0

    # Show help
    python interval_learner.py help
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

# Lazy imports for sibling modules
efficiency_tracker = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global efficiency_tracker
    if efficiency_tracker is not None:
        return

    try:
        from . import efficiency_tracker as et

        efficiency_tracker = et
    except ImportError:
        import efficiency_tracker as et  # type: ignore[no-redef]

        efficiency_tracker = et


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Scoring weights (WS-018 decision)
WEIGHT_WORK_SUBMITTED = 0.5
WEIGHT_DECISIONS = 0.3
WEIGHT_QUEUE_EMPTY = 0.2

# Penalty thresholds
HIGH_QUEUE_THRESHOLD = 5
HIGH_QUEUE_PENALTY = 0.2

# Interval bounds (P18.5)
MIN_INTERVAL_HOURS = 1.0
MAX_INTERVAL_HOURS = 24.0
DEFAULT_INTERVAL_HOURS = 4.0

# Interval adjustment thresholds
LOW_VALUE_THRESHOLD = 0.3  # Below this, increase interval (runs wasteful)
HIGH_VALUE_THRESHOLD = 0.7  # Above this, decrease interval (runs valuable)

# Interval multipliers for adjustment
INCREASE_INTERVAL_FACTOR = 1.5  # Multiplier when avg value is low
DECREASE_INTERVAL_FACTOR = 0.75  # Multiplier when avg value is high

# Auto-apply thresholds (WS-018: disabled by default until validated)
AUTO_APPLY_CONFIDENCE_THRESHOLD = 0.8
AUTO_APPLY_SAVINGS_THRESHOLD = 10.0  # percent

# Minimum sessions for reliable recommendation
MIN_SESSIONS_FOR_RECOMMENDATION = 10


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class SessionScore:
    """Score result for an executive session.

    Attributes:
        session_id: Identifier for the session (timestamp or explicit ID)
        value_score: Final score in 0.0-1.0 range
        factors: Dict showing what contributed to the score
    """

    session_id: str
    value_score: float
    factors: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class ActivityPattern:
    """Detected pattern from session history analysis.

    Attributes:
        pattern_type: Type identifier (e.g., "queue_state", "time_of_day")
        description: Human-readable explanation of the pattern
        confidence: Confidence level in 0.0-1.0 range
        data_points: Number of sessions that contributed to this pattern
    """

    pattern_type: str
    description: str
    confidence: float
    data_points: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class IntervalRecommendation:
    """Recommendation for optimal executive loop interval.

    Attributes:
        current_interval_hours: Current interval setting
        suggested_interval_hours: Recommended interval (bounded 1-24h)
        confidence: Confidence level in 0.0-1.0 range
        reasoning: Human-readable explanation of the recommendation
        patterns_used: Pattern types that influenced the recommendation
        expected_savings_percent: Expected efficiency improvement
        auto_apply: True only if confidence > 0.8 AND savings > 10%
    """

    current_interval_hours: float
    suggested_interval_hours: float
    confidence: float
    reasoning: str
    patterns_used: list[str] = field(default_factory=list)
    expected_savings_percent: float = 0.0
    auto_apply: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


# -----------------------------------------------------------------------------
# Scoring Functions
# -----------------------------------------------------------------------------


def score_session(session: dict[str, Any]) -> SessionScore:
    """Score an executive session based on value delivered.

    Scoring formula (WS-018):
        value = 0.5*(work_submitted>0) + 0.3*(decisions>0) + 0.2*(queue_empty)
        penalty = -0.2 if queue_pending_at_start > 5 else 0
        final_score = clamp(value + penalty, 0.0, 1.0)

    Args:
        session: Session dict with keys:
            - timestamp: ISO timestamp (used as session_id if no explicit id)
            - trigger: What triggered the session
            - duration_minutes: How long the session ran
            - decisions_count: Number of decisions made
            - work_submitted: Number of work items submitted
            - estimated_tokens: Token estimate for the session
            - queue_pending_at_start: Pending tasks when session started (P18.1)

    Returns:
        SessionScore with value_score in 0.0-1.0 range and factors dict
    """
    # Extract values with safe defaults for missing fields
    session_id = session.get("session_id") or session.get("timestamp", "unknown")
    work_submitted = session.get("work_submitted", 0) or 0
    decisions_count = session.get("decisions_count", 0) or 0
    queue_pending = session.get("queue_pending_at_start")

    # Handle None queue_pending (field may be missing in old sessions)
    if queue_pending is None:
        queue_pending = 0
        queue_data_available = False
    else:
        queue_data_available = True

    # Calculate base score components
    work_score = WEIGHT_WORK_SUBMITTED if work_submitted > 0 else 0.0
    decisions_score = WEIGHT_DECISIONS if decisions_count > 0 else 0.0
    queue_empty_score = WEIGHT_QUEUE_EMPTY if queue_pending == 0 else 0.0

    base_score = work_score + decisions_score + queue_empty_score

    # Apply penalty for high queue backlog
    penalty = HIGH_QUEUE_PENALTY if queue_pending > HIGH_QUEUE_THRESHOLD else 0.0
    raw_score = base_score - penalty

    # Clamp to 0.0-1.0 range
    final_score = max(0.0, min(1.0, raw_score))

    # Build factors dict for transparency
    factors = {
        "work_submitted": work_submitted,
        "work_score": work_score,
        "decisions_count": decisions_count,
        "decisions_score": decisions_score,
        "queue_pending_at_start": queue_pending,
        "queue_empty_score": queue_empty_score,
        "queue_data_available": queue_data_available,
        "high_queue_penalty": penalty,
        "base_score": base_score,
        "raw_score": raw_score,
    }

    return SessionScore(
        session_id=str(session_id),
        value_score=round(final_score, 3),
        factors=factors,
    )


def get_session_by_id(session_id: str) -> dict[str, Any] | None:
    """Retrieve a session from efficiency_tracker by ID or timestamp.

    Args:
        session_id: Session ID or timestamp to find

    Returns:
        Session dict if found, None otherwise
    """
    _ensure_imports()

    try:
        data = efficiency_tracker.load_efficiency_data()
        sessions = data.get("executive_sessions", [])

        # Search by timestamp (most common identifier)
        for session in sessions:
            if session.get("timestamp") == session_id:
                return session
            if session.get("session_id") == session_id:
                return session

        return None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Pattern Detection
# -----------------------------------------------------------------------------

# Minimum sessions required for reliable pattern detection
DEFAULT_MIN_SESSIONS = 20

# Thresholds for pattern confidence
QUEUE_SCORE_DIFF_THRESHOLD = 0.15  # Min diff to consider queue state significant
TIME_SCORE_DIFF_THRESHOLD = 0.20  # Min diff to consider time-of-day significant
MIN_SAMPLES_PER_GROUP = 3  # Min sessions in a group for comparison


def detect_patterns(
    sessions: list[dict[str, Any]], min_sessions: int = DEFAULT_MIN_SESSIONS
) -> list[ActivityPattern]:
    """Detect activity patterns from session history.

    Analyzes executive sessions to find patterns that correlate with
    high-value outcomes. Primary signal is queue-state correlation,
    secondary is time-of-day patterns.

    Args:
        sessions: List of session dicts with value_score and metadata
        min_sessions: Minimum sessions required for pattern detection

    Returns:
        List of ActivityPattern sorted by confidence descending.
        Returns empty list if insufficient data.
    """
    if len(sessions) < min_sessions:
        return []

    patterns: list[ActivityPattern] = []

    # Primary signal: Queue-state correlation
    queue_pattern = _detect_queue_state_pattern(sessions)
    if queue_pattern:
        patterns.append(queue_pattern)

    # Secondary signal: Time-of-day patterns
    time_pattern = _detect_time_of_day_pattern(sessions)
    if time_pattern:
        patterns.append(time_pattern)

    # Sort by confidence descending
    patterns.sort(key=lambda p: p.confidence, reverse=True)

    return patterns


def _detect_queue_state_pattern(
    sessions: list[dict[str, Any]],
) -> ActivityPattern | None:
    """Detect correlation between queue state and session value.

    Compares average value_score when queue is empty (0 pending)
    versus when queue is backlogged (>5 pending).

    Args:
        sessions: List of session dicts

    Returns:
        ActivityPattern if significant correlation found, None otherwise
    """
    # Separate sessions by queue state
    empty_queue_scores: list[float] = []
    high_queue_scores: list[float] = []

    for session in sessions:
        value_score = session.get("value_score")
        queue_pending = session.get("queue_pending_at_start")

        # Skip sessions without required data
        if value_score is None or queue_pending is None:
            continue

        if queue_pending == 0:
            empty_queue_scores.append(value_score)
        elif queue_pending > HIGH_QUEUE_THRESHOLD:
            high_queue_scores.append(value_score)

    # Need enough samples in both groups for comparison
    if (
        len(empty_queue_scores) < MIN_SAMPLES_PER_GROUP
        or len(high_queue_scores) < MIN_SAMPLES_PER_GROUP
    ):
        return None

    # Calculate averages
    avg_empty = sum(empty_queue_scores) / len(empty_queue_scores)
    avg_high = sum(high_queue_scores) / len(high_queue_scores)
    score_diff = avg_empty - avg_high

    # Check if difference is significant
    if abs(score_diff) < QUEUE_SCORE_DIFF_THRESHOLD:
        return None

    # Calculate confidence based on:
    # - Magnitude of difference (bigger diff = higher confidence)
    # - Sample size (more samples = higher confidence, capped at 1.0)
    total_samples = len(empty_queue_scores) + len(high_queue_scores)
    sample_factor = min(1.0, total_samples / 40)  # Cap at 40 samples
    diff_factor = min(1.0, abs(score_diff) / 0.5)  # Cap at 0.5 diff
    confidence = round((sample_factor * 0.5) + (diff_factor * 0.5), 3)

    # Build description
    if score_diff > 0:
        description = (
            f"Sessions with empty queue average {avg_empty:.2f} value score, "
            f"vs {avg_high:.2f} when queue > {HIGH_QUEUE_THRESHOLD}. "
            f"Consider running executive loop when queue is clear."
        )
    else:
        description = (
            f"Sessions with high queue average {avg_high:.2f} value score, "
            f"vs {avg_empty:.2f} when queue empty. "
            f"Executive loop may be more valuable when work is pending."
        )

    return ActivityPattern(
        pattern_type="queue_state",
        description=description,
        confidence=confidence,
        data_points=total_samples,
    )


def _detect_time_of_day_pattern(
    sessions: list[dict[str, Any]],
) -> ActivityPattern | None:
    """Detect time-of-day patterns in session value.

    Groups sessions by hour of day and identifies peak value hours.

    Args:
        sessions: List of session dicts

    Returns:
        ActivityPattern if significant time pattern found, None otherwise
    """
    from datetime import datetime

    # Group sessions by hour of day
    hour_scores: dict[int, list[float]] = {h: [] for h in range(24)}

    for session in sessions:
        value_score = session.get("value_score")
        timestamp = session.get("timestamp")

        if value_score is None or timestamp is None:
            continue

        try:
            # Parse ISO timestamp and extract hour
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            hour = dt.hour
            hour_scores[hour].append(value_score)
        except (ValueError, AttributeError):
            continue

    # Filter hours with enough samples
    valid_hours = {
        h: scores
        for h, scores in hour_scores.items()
        if len(scores) >= MIN_SAMPLES_PER_GROUP
    }

    if len(valid_hours) < 2:
        return None

    # Calculate average for each hour
    hour_avgs = {h: sum(scores) / len(scores) for h, scores in valid_hours.items()}

    if not hour_avgs:
        return None

    # Find best and worst hours
    best_hour = max(hour_avgs, key=lambda h: hour_avgs[h])
    worst_hour = min(hour_avgs, key=lambda h: hour_avgs[h])
    best_avg = hour_avgs[best_hour]
    worst_avg = hour_avgs[worst_hour]
    score_diff = best_avg - worst_avg

    # Check if difference is significant
    if score_diff < TIME_SCORE_DIFF_THRESHOLD:
        return None

    # Calculate confidence based on difference and sample size
    total_samples = sum(len(scores) for scores in valid_hours.values())
    sample_factor = min(1.0, total_samples / 50)  # Cap at 50 samples
    diff_factor = min(1.0, score_diff / 0.4)  # Cap at 0.4 diff
    confidence = round((sample_factor * 0.4) + (diff_factor * 0.6), 3)

    # Format hour ranges for description
    description = (
        f"Peak value hour: {best_hour:02d}:00 (avg {best_avg:.2f}), "
        f"lowest: {worst_hour:02d}:00 (avg {worst_avg:.2f}). "
        f"Consider scheduling executive loop around {best_hour:02d}:00."
    )

    return ActivityPattern(
        pattern_type="time_of_day",
        description=description,
        confidence=confidence,
        data_points=total_samples,
    )


def get_patterns_from_history(
    limit: int = 100, min_sessions: int = DEFAULT_MIN_SESSIONS
) -> list[ActivityPattern]:
    """Convenience function to detect patterns from stored session history.

    Args:
        limit: Max sessions to analyze
        min_sessions: Minimum sessions required for pattern detection

    Returns:
        List of detected patterns
    """
    _ensure_imports()

    try:
        sessions = efficiency_tracker.get_scored_sessions(limit=limit)
        return detect_patterns(sessions, min_sessions=min_sessions)
    except Exception:
        return []


# -----------------------------------------------------------------------------
# Interval Recommendation (P18.5)
# -----------------------------------------------------------------------------


def find_optimal_interval(
    sessions: list[dict[str, Any]],
    patterns: list[ActivityPattern],
    current_interval: float = DEFAULT_INTERVAL_HOURS,
) -> IntervalRecommendation:
    """Find optimal executive loop interval based on session value and patterns.

    Algorithm (WS-018):
        1. Return current interval if insufficient data (<10 sessions)
        2. Calculate average value_score from sessions
        3. Adjust interval based on avg value:
           - If avg < 0.3: suggest interval * 1.5 (runs wasteful, reduce frequency)
           - If avg > 0.7: suggest interval * 0.75 (runs valuable, increase frequency)
           - Otherwise: suggest current interval (reasonable)
        4. Apply bounds: min 1h, max 24h
        5. Calculate expected savings
        6. Set auto_apply = (confidence > 0.8 AND expected_savings > 10%)

    Args:
        sessions: List of session dicts with value_score
        patterns: List of detected ActivityPatterns
        current_interval: Current interval in hours (default: 4.0)

    Returns:
        IntervalRecommendation with suggested interval and metadata
    """
    # Insufficient data case
    if len(sessions) < MIN_SESSIONS_FOR_RECOMMENDATION:
        return IntervalRecommendation(
            current_interval_hours=current_interval,
            suggested_interval_hours=current_interval,
            confidence=0.0,
            reasoning=f"Insufficient data: {len(sessions)} sessions "
            f"(need {MIN_SESSIONS_FOR_RECOMMENDATION} for recommendation)",
            patterns_used=[],
            expected_savings_percent=0.0,
            auto_apply=False,
        )

    # Calculate average value_score
    value_scores = [
        s.get("value_score", 0.0) for s in sessions if s.get("value_score") is not None
    ]

    if not value_scores:
        return IntervalRecommendation(
            current_interval_hours=current_interval,
            suggested_interval_hours=current_interval,
            confidence=0.0,
            reasoning="No sessions with value_score found",
            patterns_used=[],
            expected_savings_percent=0.0,
            auto_apply=False,
        )

    avg_value = sum(value_scores) / len(value_scores)

    # Determine base adjustment
    if avg_value < LOW_VALUE_THRESHOLD:
        # Runs are wasteful, increase interval to reduce frequency
        raw_suggested = current_interval * INCREASE_INTERVAL_FACTOR
        adjustment_reason = (
            f"Average value score {avg_value:.2f} < {LOW_VALUE_THRESHOLD} "
            f"suggests runs are wasteful. Increasing interval to reduce frequency."
        )
        adjustment_direction = "increase"
    elif avg_value > HIGH_VALUE_THRESHOLD:
        # Runs are valuable, decrease interval to increase frequency
        raw_suggested = current_interval * DECREASE_INTERVAL_FACTOR
        adjustment_reason = (
            f"Average value score {avg_value:.2f} > {HIGH_VALUE_THRESHOLD} "
            f"suggests runs are valuable. Decreasing interval to increase frequency."
        )
        adjustment_direction = "decrease"
    else:
        # Value is reasonable, keep current interval
        raw_suggested = current_interval
        adjustment_reason = (
            f"Average value score {avg_value:.2f} is in acceptable range "
            f"({LOW_VALUE_THRESHOLD}-{HIGH_VALUE_THRESHOLD}). "
            f"Current interval appears optimal."
        )
        adjustment_direction = "none"

    # Apply bounds
    suggested = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, raw_suggested))

    # Build reasoning with bounds info if applied
    if raw_suggested != suggested:
        if raw_suggested < MIN_INTERVAL_HOURS:
            adjustment_reason += (
                f" (bounded to minimum {MIN_INTERVAL_HOURS}h from {raw_suggested:.1f}h)"
            )
        elif raw_suggested > MAX_INTERVAL_HOURS:
            adjustment_reason += (
                f" (bounded to maximum {MAX_INTERVAL_HOURS}h from {raw_suggested:.1f}h)"
            )

    # Calculate expected savings
    # Savings come from fewer wasted runs when increasing interval,
    # or from capturing more value when decreasing interval
    if adjustment_direction == "increase":
        # Fewer runs = less wasted cycles
        # Savings = (1 - 1/factor) as percentage (e.g., 1.5x interval = 33% fewer runs)
        expected_savings = (1 - current_interval / suggested) * 100
    elif adjustment_direction == "decrease":
        # More runs = more value captured (proportional to value increase potential)
        # Estimate based on how much value we're missing
        value_gap = 1.0 - avg_value
        expected_savings = value_gap * (current_interval / suggested - 1) * 100
    else:
        expected_savings = 0.0

    expected_savings = max(0.0, round(expected_savings, 1))

    # Calculate confidence based on sample size and value score consistency
    sample_confidence = min(1.0, len(value_scores) / 50)  # Cap at 50 sessions

    # Add pattern confidence boost
    pattern_types_used: list[str] = []
    pattern_confidence_boost = 0.0
    for pattern in patterns:
        pattern_types_used.append(pattern.pattern_type)
        # Higher confidence patterns provide more boost
        pattern_confidence_boost += pattern.confidence * 0.1  # 10% boost per pattern

    # Calculate standard deviation for consistency score
    if len(value_scores) > 1:
        mean = avg_value
        variance = sum((x - mean) ** 2 for x in value_scores) / len(value_scores)
        std_dev = variance**0.5
        # Lower std_dev = more consistent = higher confidence
        consistency = 1.0 - min(1.0, std_dev * 2)
    else:
        consistency = 0.5  # Neutral if only one sample

    # Combine confidence factors
    base_confidence = (sample_confidence * 0.5) + (consistency * 0.5)
    total_confidence = min(1.0, base_confidence + pattern_confidence_boost)
    confidence = round(total_confidence, 3)

    # Determine auto_apply (WS-018: conservative defaults)
    auto_apply = (
        confidence > AUTO_APPLY_CONFIDENCE_THRESHOLD
        and expected_savings > AUTO_APPLY_SAVINGS_THRESHOLD
    )

    return IntervalRecommendation(
        current_interval_hours=current_interval,
        suggested_interval_hours=round(suggested, 2),
        confidence=confidence,
        reasoning=adjustment_reason,
        patterns_used=pattern_types_used,
        expected_savings_percent=expected_savings,
        auto_apply=auto_apply,
    )


def get_interval_recommendation(
    current_interval: float = DEFAULT_INTERVAL_HOURS,
    limit: int = 100,
) -> IntervalRecommendation:
    """Convenience function to get interval recommendation from stored history.

    Args:
        current_interval: Current interval in hours
        limit: Max sessions to analyze

    Returns:
        IntervalRecommendation
    """
    _ensure_imports()

    try:
        sessions = efficiency_tracker.get_scored_sessions(limit=limit)
        patterns = detect_patterns(sessions)
        return find_optimal_interval(sessions, patterns, current_interval)
    except Exception as e:
        return IntervalRecommendation(
            current_interval_hours=current_interval,
            suggested_interval_hours=current_interval,
            confidence=0.0,
            reasoning=f"Error loading session data: {e}",
            patterns_used=[],
            expected_savings_percent=0.0,
            auto_apply=False,
        )


# -----------------------------------------------------------------------------
# CLI Commands
# -----------------------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    """Score a session by ID."""
    session = get_session_by_id(args.session_id)
    if session is None:
        print(json.dumps({"error": f"Session not found: {args.session_id}"}))
        return 1

    result = score_session(session)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_score_raw(args: argparse.Namespace) -> int:
    """Score a session from raw JSON input."""
    try:
        session = json.loads(args.session)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        return 1

    result = score_session(session)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_help(_args: argparse.Namespace) -> int:
    """Show help information."""
    print(__doc__)
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    """Detect and display patterns from session history."""
    min_sessions = getattr(args, "min_sessions", DEFAULT_MIN_SESSIONS)
    limit = getattr(args, "limit", 100)

    patterns = get_patterns_from_history(limit=limit, min_sessions=min_sessions)

    if not patterns:
        # Check if we have enough sessions
        _ensure_imports()
        try:
            sessions = efficiency_tracker.get_scored_sessions(limit=limit)
            session_count = len(sessions)
        except Exception:
            session_count = 0

        if session_count < min_sessions:
            result = {
                "patterns": [],
                "message": f"Insufficient data: {session_count} sessions "
                f"(need {min_sessions} for pattern detection)",
            }
        else:
            result = {
                "patterns": [],
                "message": "No significant patterns detected",
            }
        print(json.dumps(result, indent=2))
        return 0

    result = {
        "patterns": [p.to_dict() for p in patterns],
        "count": len(patterns),
    }
    print(json.dumps(result, indent=2))
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    """Get interval recommendation based on session history."""
    current_interval = getattr(args, "current_interval", DEFAULT_INTERVAL_HOURS)
    limit = getattr(args, "limit", 100)

    recommendation = get_interval_recommendation(
        current_interval=current_interval,
        limit=limit,
    )

    print(json.dumps(recommendation.to_dict(), indent=2))
    return 0


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------


def main() -> int:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="P18 Adaptive Intervals — Session value scoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # score command
    score_parser = subparsers.add_parser("score", help="Score a session by ID")
    score_parser.add_argument(
        "--session-id", required=True, help="Session ID or timestamp to score"
    )
    score_parser.set_defaults(func=cmd_score)

    # score-raw command
    raw_parser = subparsers.add_parser(
        "score-raw", help="Score a session from raw JSON"
    )
    raw_parser.add_argument("--session", required=True, help="Session JSON data")
    raw_parser.set_defaults(func=cmd_score_raw)

    # help command
    help_parser = subparsers.add_parser("help", help="Show help information")
    help_parser.set_defaults(func=cmd_help)

    # patterns command
    patterns_parser = subparsers.add_parser(
        "patterns", help="Detect patterns from session history"
    )
    patterns_parser.add_argument(
        "--min-sessions",
        type=int,
        default=DEFAULT_MIN_SESSIONS,
        help=f"Minimum sessions required (default: {DEFAULT_MIN_SESSIONS})",
    )
    patterns_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max sessions to analyze (default: 100)",
    )
    patterns_parser.set_defaults(func=cmd_patterns)

    # recommend command
    recommend_parser = subparsers.add_parser(
        "recommend", help="Get optimal interval recommendation"
    )
    recommend_parser.add_argument(
        "--current-interval",
        type=float,
        default=DEFAULT_INTERVAL_HOURS,
        help=f"Current interval in hours (default: {DEFAULT_INTERVAL_HOURS})",
    )
    recommend_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max sessions to analyze (default: 100)",
    )
    recommend_parser.set_defaults(func=cmd_recommend)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
