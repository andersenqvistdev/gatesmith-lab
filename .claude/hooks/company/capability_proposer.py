#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P30 Capability Proposer — Enhancement Proposal Generation System.

Generates enhancement proposals from improvement detections, converting
detected opportunities into actionable proposals for system evolution.

Key Capabilities:
    - Generate enhancement proposals from detection results
    - Propose new commands for missing functionality
    - Propose agent improvements for underperforming agents
    - Propose workflow changes for escalation patterns
    - Convert proposals to initiative_engine format
    - Filter and deduplicate proposals by value and similarity

Proposal Types:
    - new_command: Missing command detected from user requests
    - new_agent: Need for new specialist capability
    - hook_adjustment: Hook configuration or behavior change
    - workflow_change: Process or workflow optimization
    - capability_addition: Extend existing agent capabilities

Approval Tiers:
    - auto: Low-risk changes, auto-approved
    - human: Structural changes requiring human review
    - executive: Major architectural changes requiring executive approval

Usage:
    # Generate proposals from detection results
    python capability_proposer.py generate --detections file.json

    # Preview proposals without writing
    python capability_proposer.py generate --detections file.json --dry-run

    # Convert proposal to initiative format
    python capability_proposer.py convert --proposal-id "enh-..."

    # Show help
    python capability_proposer.py help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# =============================================================================
# Configuration
# =============================================================================

PROPOSALS_FILE = "enhancement_proposals.json"
MIN_VALUE_THRESHOLD = 0.3  # Minimum estimated_value to keep proposal
TITLE_SIMILARITY_THRESHOLD = 0.85  # Title similarity for deduplication

# Enhancement type configurations
ENHANCEMENT_TYPES = {
    "new_command": {
        "default_effort_hours": 4.0,
        "default_approval_tier": "human",
        "description": "New command to add missing functionality",
    },
    "new_agent": {
        "default_effort_hours": 8.0,
        "default_approval_tier": "human",
        "description": "New agent for specialized capability",
    },
    "hook_adjustment": {
        "default_effort_hours": 2.0,
        "default_approval_tier": "auto",
        "description": "Adjustment to hook behavior or configuration",
    },
    "workflow_change": {
        "default_effort_hours": 6.0,
        "default_approval_tier": "executive",
        "description": "Optimization of workflow or process",
    },
    "capability_addition": {
        "default_effort_hours": 3.0,
        "default_approval_tier": "human",
        "description": "Extension of existing agent capabilities",
    },
}

# Detection type to enhancement type mapping
DETECTION_TO_ENHANCEMENT = {
    "missing_command": "new_command",
    "capability_gap": "new_agent",
    "capability_mismatch": "new_agent",
    "escalation_pattern": "workflow_change",
    "agent_underperformance": "capability_addition",
    "workflow_inefficiency": "workflow_change",
    "hook_misconfiguration": "hook_adjustment",
}

# =============================================================================
# Lazy Imports
# =============================================================================

_company_resolver = None
_improvement_detector = None


def _ensure_company_resolver():
    """Lazily import company_resolver module."""
    global _company_resolver
    if _company_resolver is not None:
        return _company_resolver

    try:
        from . import company_resolver as cr

        _company_resolver = cr
    except ImportError:
        try:
            import company_resolver as cr  # type: ignore[no-redef]

            _company_resolver = cr
        except ImportError:
            _company_resolver = None

    return _company_resolver


def _ensure_improvement_detector():
    """Lazily import improvement_detector module."""
    global _improvement_detector
    if _improvement_detector is not None:
        return _improvement_detector

    try:
        from . import improvement_detector as id_module

        _improvement_detector = id_module
    except ImportError:
        try:
            import improvement_detector as id_module  # type: ignore[no-redef]

            _improvement_detector = id_module
        except ImportError:
            _improvement_detector = None

    return _improvement_detector


def get_company_dir() -> Path:
    """Get the company directory path."""
    cr = _ensure_company_resolver()
    if cr:
        return cr.get_company_dir()
    return Path.cwd() / ".company"


def get_proposals_path() -> Path:
    """Get the enhancement proposals file path."""
    return get_company_dir() / PROPOSALS_FILE


def ensure_company_dir():
    """Ensure company directory exists."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class EnhancementProposal:
    """
    A proposal for system enhancement based on detection results.

    Attributes:
        proposal_id: Unique identifier (format: "enh-{type}-{timestamp}-{hash}")
        enhancement_type: Type of enhancement (new_command, new_agent, etc.)
        title: Human-readable title
        description: Detailed description of what/why
        rationale: Evidence-based reasoning from detection
        estimated_effort_hours: Estimated implementation effort
        estimated_value: Value score (0.0-1.0 scale)
        approval_tier: Required approval level (auto, human, executive)
        source_detections: List of detection result IDs that led to this
        implementation_hints: Guidance for implementer
        created_at: ISO timestamp of creation
        status: Proposal status (pending, approved, rejected, implemented)
        priority: Priority level (1=Critical, 2=High, 3=Normal, 4=Low)
        tags: Optional categorization tags
    """

    proposal_id: str
    enhancement_type: str
    title: str
    description: str
    rationale: str
    estimated_effort_hours: float
    estimated_value: float
    approval_tier: str
    source_detections: list[str]
    implementation_hints: list[str]
    created_at: str
    status: str = "pending"
    priority: int = 3
    tags: list[str] = field(default_factory=list)

    @property
    def roi_score(self) -> float:
        """Calculate ROI as value per hour of effort.

        Higher score = better return on investment.
        """
        if self.estimated_effort_hours <= 0:
            return 0.0
        return self.estimated_value / self.estimated_effort_hours

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "proposal_id": self.proposal_id,
            "enhancement_type": self.enhancement_type,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "estimated_effort_hours": self.estimated_effort_hours,
            "estimated_value": self.estimated_value,
            "roi_score": self.roi_score,
            "approval_tier": self.approval_tier,
            "source_detections": self.source_detections,
            "implementation_hints": self.implementation_hints,
            "created_at": self.created_at,
            "status": self.status,
            "priority": self.priority,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnhancementProposal:
        """Create from dictionary."""
        return cls(
            proposal_id=data["proposal_id"],
            enhancement_type=data["enhancement_type"],
            title=data["title"],
            description=data["description"],
            rationale=data["rationale"],
            estimated_effort_hours=data["estimated_effort_hours"],
            estimated_value=data["estimated_value"],
            approval_tier=data["approval_tier"],
            source_detections=data.get("source_detections", []),
            implementation_hints=data.get("implementation_hints", []),
            created_at=data["created_at"],
            status=data.get("status", "pending"),
            priority=data.get("priority", 3),
            tags=data.get("tags", []),
        )


# =============================================================================
# ID Generation
# =============================================================================


def _generate_proposal_id(enhancement_type: str, title: str = "") -> str:
    """Generate a unique proposal ID.

    Format: enh-{type_prefix}-{timestamp}-{hash}
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    type_prefix = enhancement_type[:4].upper() if enhancement_type else "UNKN"

    # Create hash from timestamp + type + title for uniqueness
    hash_input = f"{timestamp}{type_prefix}{title}"
    hash_suffix = hashlib.md5(hash_input.encode()).hexdigest()[:6]

    return f"enh-{type_prefix}-{timestamp}-{hash_suffix}"


# =============================================================================
# Proposal Generation Functions
# =============================================================================


def propose_new_command(detection: dict[str, Any]) -> EnhancementProposal:
    """
    Generate proposal for a missing command.

    Args:
        detection: Detection result dict with evidence of missing command

    Returns:
        EnhancementProposal for the new command
    """
    # Extract detection details
    detection_id = detection.get("detection_id", "unknown")
    evidence = detection.get("evidence", {})
    recommendations = detection.get("recommendations", [])

    # Determine command name from evidence
    command_name = evidence.get("command_name", "unknown")
    user_requests = evidence.get("user_requests", [])
    request_count = evidence.get("request_count", len(user_requests))

    # Build title and description
    title = f"Add /{command_name} command"
    description = (
        f"Users have requested the /{command_name} command {request_count} times. "
        f"This functionality is not currently available in the system."
    )

    if user_requests:
        description += "\n\nRecent requests:\n"
        for req in user_requests[:3]:
            description += f"- {req}\n"

    # Build rationale from evidence
    rationale = (
        f"Detection {detection_id} identified missing command '{command_name}' "
        f"based on {request_count} user request(s). "
        f"Adding this command will improve user experience and reduce friction."
    )

    # Implementation hints
    hints = [
        f"Create command file at .claude/commands/{command_name}.md",
        "Follow existing command patterns in the commands directory",
        "Include usage examples and parameter documentation",
        "Add tests for the new command",
    ]
    hints.extend(recommendations)

    # Estimate effort based on command complexity
    effort_hours = ENHANCEMENT_TYPES["new_command"]["default_effort_hours"]
    if evidence.get("complexity") == "complex":
        effort_hours *= 1.5
    elif evidence.get("complexity") == "simple":
        effort_hours *= 0.5

    # Estimate value based on request frequency
    base_value = 0.5
    if request_count >= 5:
        base_value = 0.8
    elif request_count >= 3:
        base_value = 0.7
    elif request_count >= 2:
        base_value = 0.6

    # Priority based on request frequency
    priority = 3
    if request_count >= 5:
        priority = 2
    elif request_count >= 10:
        priority = 1

    return EnhancementProposal(
        proposal_id=_generate_proposal_id("new_command", command_name),
        enhancement_type="new_command",
        title=title,
        description=description,
        rationale=rationale,
        estimated_effort_hours=effort_hours,
        estimated_value=base_value,
        approval_tier="human",  # Structural change requires human review
        source_detections=[detection_id],
        implementation_hints=hints,
        created_at=datetime.now(timezone.utc).isoformat(),
        priority=priority,
        tags=["command", command_name],
    )


def propose_agent_improvement(detection: dict[str, Any]) -> EnhancementProposal:
    """
    Generate proposal for agent improvement.

    Handles both underperforming agents and capability gaps.

    Args:
        detection: Detection result dict with agent performance data

    Returns:
        EnhancementProposal for agent improvement
    """
    detection_id = detection.get("detection_id", "unknown")
    detection_type = detection.get("detection_type", "agent_underperformance")
    gap_type = detection.get("gap_type", "")
    evidence = detection.get("evidence", {})
    recommendations = detection.get("recommendations", [])

    agent_id = evidence.get("agent_id", evidence.get("employee_id", "unknown"))
    success_rate = evidence.get("success_rate", 0.0)
    task_count = evidence.get("task_count", 0)
    # improvement_detector.detect_capability_gaps() emits gap_type
    # "capability_mismatch" with a single evidence["capability"] string, not
    # the legacy detection_type "capability_gap" + evidence["capability_gaps"]
    # list shape — recognize both so real detections route to a hire proposal
    # instead of silently falling into the underperformance branch below.
    capability_gaps = evidence.get("capability_gaps", [])
    if not capability_gaps and evidence.get("capability"):
        capability_gaps = [evidence["capability"]]
    weak_areas = evidence.get("weak_areas", [])

    # Determine enhancement type
    if (
        detection_type == "capability_gap"
        or gap_type == "capability_mismatch"
        or capability_gaps
    ):
        enhancement_type = "new_agent"
        title = (
            f"Add agent capability: {', '.join(capability_gaps[:2]) or 'specialized'}"
        )
        description = (
            f"Capability gap detected. Tasks requiring {capability_gaps} "
            f"have been falling through to fallback handlers."
        )
        approval_tier = "human"  # New agent requires human review
        effort_hours = ENHANCEMENT_TYPES["new_agent"]["default_effort_hours"]
    else:
        enhancement_type = "capability_addition"
        title = f"Improve {agent_id} performance"
        description = (
            f"Agent {agent_id} has a {success_rate:.0%} success rate "
            f"over {task_count} tasks. "
            f"Performance improvements are recommended."
        )
        if weak_areas:
            description += f"\n\nWeak areas: {', '.join(weak_areas)}"
        approval_tier = "auto" if success_rate > 0.5 else "human"
        effort_hours = ENHANCEMENT_TYPES["capability_addition"]["default_effort_hours"]

    # Build rationale
    rationale = (
        f"Detection {detection_id} identified {'capability gap' if enhancement_type == 'new_agent' else 'underperformance'} "
        f"for {agent_id}. "
    )
    if success_rate < 1.0:
        rationale += f"Current success rate is {success_rate:.0%}. "
    if capability_gaps:
        rationale += f"Missing capabilities: {', '.join(capability_gaps)}. "

    # Implementation hints
    hints = []
    if enhancement_type == "new_agent":
        hints.extend(
            [
                "Consider creating a specialized agent for the missing capability",
                "Review existing agents for potential capability extension",
                "Update routing rules to direct relevant tasks to new agent",
            ]
        )
    else:
        hints.extend(
            [
                f"Review {agent_id} agent definition for potential improvements",
                "Analyze failed tasks for common patterns",
                "Consider adding more specific instructions or examples",
                "Evaluate if task complexity matches agent capabilities",
            ]
        )
    hints.extend(recommendations)

    # Estimate value
    base_value = 0.6
    if success_rate < 0.5:
        base_value = 0.8  # High value to fix poorly performing agent
    elif capability_gaps:
        base_value = 0.7  # Medium-high value for capability gaps

    # Priority based on severity
    priority = 3
    if success_rate < 0.4:
        priority = 1  # Critical
    elif success_rate < 0.6:
        priority = 2  # High
    elif capability_gaps:
        priority = 2  # High for capability gaps

    return EnhancementProposal(
        proposal_id=_generate_proposal_id(enhancement_type, agent_id),
        enhancement_type=enhancement_type,
        title=title,
        description=description,
        rationale=rationale,
        estimated_effort_hours=effort_hours,
        estimated_value=base_value,
        approval_tier=approval_tier,
        source_detections=[detection_id],
        implementation_hints=hints,
        created_at=datetime.now(timezone.utc).isoformat(),
        priority=priority,
        tags=["agent", agent_id],
    )


def propose_workflow_change(detection: dict[str, Any]) -> EnhancementProposal:
    """
    Generate proposal for workflow optimization.

    Handles escalation patterns and workflow inefficiencies.

    Args:
        detection: Detection result dict with escalation/workflow data

    Returns:
        EnhancementProposal for workflow change
    """
    detection_id = detection.get("detection_id", "unknown")
    detection_type = detection.get("detection_type", "escalation_pattern")
    evidence = detection.get("evidence", {})
    recommendations = detection.get("recommendations", [])

    escalation_count = evidence.get("escalation_count", 0)
    escalation_types = evidence.get("escalation_types", [])
    affected_tasks = evidence.get("affected_tasks", [])
    bottleneck_stage = evidence.get("bottleneck_stage", "")
    avg_delay_minutes = evidence.get("avg_delay_minutes", 0)

    # Build title and description
    if detection_type == "escalation_pattern":
        title = f"Reduce escalations: {escalation_types[0] if escalation_types else 'general'}"
        description = (
            f"Detected {escalation_count} escalations of type "
            f"'{escalation_types[0] if escalation_types else 'various'}'. "
            f"This indicates a systematic issue requiring workflow adjustment."
        )
    else:
        title = f"Optimize workflow: {bottleneck_stage or 'pipeline'}"
        description = (
            f"Workflow inefficiency detected. "
            f"Average delay of {avg_delay_minutes:.1f} minutes at stage '{bottleneck_stage}'."
        )

    if affected_tasks:
        description += f"\n\nAffected tasks: {len(affected_tasks)}"

    # Build rationale
    rationale = (
        f"Detection {detection_id} identified workflow issue. "
        f"{'Escalation pattern suggests task routing or capability mismatch. ' if detection_type == 'escalation_pattern' else ''}"
        f"Optimizing this workflow will improve throughput and reduce delays."
    )

    # Implementation hints
    hints = []
    if detection_type == "escalation_pattern":
        hints.extend(
            [
                "Review escalation triggers for root cause",
                "Consider adding pre-task validation",
                "Evaluate task decomposition to reduce complexity",
                "Assess if additional capabilities are needed",
            ]
        )
    else:
        hints.extend(
            [
                f"Focus optimization on '{bottleneck_stage}' stage",
                "Consider parallel execution where possible",
                "Review resource allocation for bottleneck stage",
                "Implement caching or pre-computation if applicable",
            ]
        )
    hints.extend(recommendations)

    # Estimate effort and value
    effort_hours = ENHANCEMENT_TYPES["workflow_change"]["default_effort_hours"]

    # Higher value for more impactful issues
    base_value = 0.6
    if escalation_count >= 5:
        base_value = 0.85
    elif escalation_count >= 3:
        base_value = 0.75
    elif avg_delay_minutes > 30:
        base_value = 0.8

    # Priority based on impact
    priority = 3
    if escalation_count >= 5 or avg_delay_minutes > 60:
        priority = 1
    elif escalation_count >= 3 or avg_delay_minutes > 30:
        priority = 2

    return EnhancementProposal(
        proposal_id=_generate_proposal_id(
            "workflow_change", bottleneck_stage or "escalation"
        ),
        enhancement_type="workflow_change",
        title=title,
        description=description,
        rationale=rationale,
        estimated_effort_hours=effort_hours,
        estimated_value=base_value,
        approval_tier="executive",  # Workflow changes need executive approval
        source_detections=[detection_id],
        implementation_hints=hints,
        created_at=datetime.now(timezone.utc).isoformat(),
        priority=priority,
        tags=["workflow", detection_type],
    )


def propose_hook_adjustment(detection: dict[str, Any]) -> EnhancementProposal:
    """
    Generate proposal for hook configuration adjustment.

    Args:
        detection: Detection result dict with hook issue data

    Returns:
        EnhancementProposal for hook adjustment
    """
    detection_id = detection.get("detection_id", "unknown")
    evidence = detection.get("evidence", {})
    recommendations = detection.get("recommendations", [])

    hook_name = evidence.get("hook_name", "unknown")
    issue_type = evidence.get("issue_type", "misconfiguration")
    occurrence_count = evidence.get("occurrence_count", 0)

    title = f"Adjust hook: {hook_name}"
    description = (
        f"Hook '{hook_name}' requires adjustment due to {issue_type}. "
        f"Issue has occurred {occurrence_count} times."
    )

    rationale = (
        f"Detection {detection_id} identified hook issue. "
        f"Adjusting {hook_name} will improve system reliability."
    )

    hints = [
        f"Review {hook_name} configuration in .claude/hooks/",
        "Check hook exit codes and error handling",
        "Verify hook dependencies are met",
    ]
    hints.extend(recommendations)

    effort_hours = ENHANCEMENT_TYPES["hook_adjustment"]["default_effort_hours"]
    base_value = 0.5
    if occurrence_count >= 5:
        base_value = 0.7

    return EnhancementProposal(
        proposal_id=_generate_proposal_id("hook_adjustment", hook_name),
        enhancement_type="hook_adjustment",
        title=title,
        description=description,
        rationale=rationale,
        estimated_effort_hours=effort_hours,
        estimated_value=base_value,
        approval_tier="auto",  # Hook adjustments are typically low-risk
        source_detections=[detection_id],
        implementation_hints=hints,
        created_at=datetime.now(timezone.utc).isoformat(),
        priority=3,
        tags=["hook", hook_name],
    )


# =============================================================================
# Main Generation Function
# =============================================================================


def _get_proposer_for_detection(gap_type: str):
    """Get the appropriate proposer function for a gap type.

    Maps gap_type values from improvement_detector to proposer functions.
    """
    proposer_map = {
        # Direct matches from improvement_detector
        "missing_command": propose_new_command,
        "underperforming_agent": propose_agent_improvement,
        "capability_mismatch": propose_agent_improvement,
        "repeated_escalation": propose_workflow_change,
        "bottleneck": propose_workflow_change,
        # Legacy detection_type mappings (for compatibility)
        "capability_gap": propose_agent_improvement,
        "agent_underperformance": propose_agent_improvement,
        "escalation_pattern": propose_workflow_change,
        "workflow_inefficiency": propose_workflow_change,
        "hook_misconfiguration": propose_hook_adjustment,
    }
    return proposer_map.get(gap_type)


def _title_similarity(title1: str, title2: str) -> float:
    """Calculate similarity ratio between two titles."""
    return SequenceMatcher(None, title1.lower(), title2.lower()).ratio()


def _deduplicate_proposals(
    proposals: list[EnhancementProposal],
) -> list[EnhancementProposal]:
    """Remove duplicate proposals based on title similarity.

    Keeps the proposal with the higher ROI score when duplicates are found.
    """
    if not proposals:
        return []

    unique_proposals: list[EnhancementProposal] = []

    for proposal in proposals:
        is_duplicate = False

        for i, existing in enumerate(unique_proposals):
            similarity = _title_similarity(proposal.title, existing.title)

            if similarity >= TITLE_SIMILARITY_THRESHOLD:
                is_duplicate = True
                # Keep the one with higher ROI
                if proposal.roi_score > existing.roi_score:
                    unique_proposals[i] = proposal
                    # Merge source detections
                    unique_proposals[i].source_detections = list(
                        set(existing.source_detections + proposal.source_detections)
                    )
                else:
                    # Just merge source detections into existing
                    unique_proposals[i].source_detections = list(
                        set(existing.source_detections + proposal.source_detections)
                    )
                break

        if not is_duplicate:
            unique_proposals.append(proposal)

    return unique_proposals


def generate_proposals(
    detections: list[dict[str, Any]],
    min_value: float = MIN_VALUE_THRESHOLD,
) -> list[EnhancementProposal]:
    """
    Generate enhancement proposals from detection results.

    Processes each detection through the appropriate proposer function,
    filters out low-value proposals, deduplicates by title similarity,
    and returns sorted by ROI (value/effort).

    Args:
        detections: List of detection result dicts from improvement_detector
        min_value: Minimum estimated_value to keep proposal (default: 0.3)

    Returns:
        List of EnhancementProposal objects sorted by ROI (highest first)
    """
    proposals: list[EnhancementProposal] = []

    for detection in detections:
        # Support both gap_type (from improvement_detector) and detection_type (legacy)
        gap_type = detection.get("gap_type") or detection.get("detection_type", "")

        # Get appropriate proposer
        proposer = _get_proposer_for_detection(gap_type)
        if proposer is None:
            # Skip unknown gap types
            continue

        try:
            proposal = proposer(detection)
            proposals.append(proposal)
        except Exception as e:
            # Log error but continue processing
            print(
                f"Warning: Failed to generate proposal for {gap_type}: {e}",
                file=sys.stderr,
            )
            continue

    # Filter out low-value proposals
    proposals = [p for p in proposals if p.estimated_value >= min_value]

    # Deduplicate by title similarity
    proposals = _deduplicate_proposals(proposals)

    # Sort by ROI score (highest first)
    proposals.sort(key=lambda p: p.roi_score, reverse=True)

    return proposals


# =============================================================================
# Initiative Engine Integration
# =============================================================================


def convert_to_initiative(proposal: EnhancementProposal) -> dict[str, Any]:
    """
    Convert EnhancementProposal to initiative_engine Proposal format.

    Args:
        proposal: The enhancement proposal to convert

    Returns:
        Dict compatible with initiative_engine.Proposal.from_dict()
    """
    # Map enhancement type to initiative ProposalType
    proposal_type_map = {
        "new_command": "documentation_update",  # Commands are documented
        "new_agent": "hiring_recommendation",  # New agent = new capability
        "hook_adjustment": "code_quality",
        "workflow_change": "performance_optimization",
        "capability_addition": "task_investigation",
    }

    # Map approval tier to initiative ApprovalTier
    approval_tier_map = {
        "auto": "auto_approve",
        "human": "human_approve",
        "executive": "human_approve",  # Executive maps to human in initiative
    }

    # Convert effort hours to minutes
    effort_minutes = int(proposal.estimated_effort_hours * 60)

    return {
        "proposal_id": proposal.proposal_id,
        "proposal_type": proposal_type_map.get(
            proposal.enhancement_type, "task_investigation"
        ),
        "title": proposal.title,
        "description": proposal.description,
        "rationale": proposal.rationale,
        "estimated_effort_minutes": effort_minutes,
        "estimated_value": proposal.estimated_value,
        "approval_tier": approval_tier_map.get(proposal.approval_tier, "human_approve"),
        "source_data": {
            "enhancement_type": proposal.enhancement_type,
            "source_detections": proposal.source_detections,
            "implementation_hints": proposal.implementation_hints,
            "tags": proposal.tags,
            "original_approval_tier": proposal.approval_tier,
        },
        "created_at": proposal.created_at,
        "status": "pending",
        "priority": proposal.priority,
    }


# =============================================================================
# Storage Functions
# =============================================================================


def load_proposals() -> list[dict[str, Any]]:
    """Load stored enhancement proposals."""
    path = get_proposals_path()

    if not path.exists():
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("proposals", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_proposals(proposals: list[EnhancementProposal]):
    """Save enhancement proposals to file."""
    ensure_company_dir()
    path = get_proposals_path()

    # Convert to dicts
    proposal_dicts = [p.to_dict() for p in proposals]

    data = {
        "proposals": proposal_dicts,
        "metadata": {
            "count": len(proposal_dicts),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Write atomically (prevents corruption under parallel workers)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        os.unlink(tmp_path)
        raise


def add_proposals(new_proposals: list[EnhancementProposal]) -> int:
    """
    Add new proposals to storage, deduplicating against existing.

    Args:
        new_proposals: New proposals to add

    Returns:
        Number of proposals actually added
    """
    existing_dicts = load_proposals()
    existing = [EnhancementProposal.from_dict(d) for d in existing_dicts]

    # Combine and deduplicate
    all_proposals = existing + new_proposals
    deduplicated = _deduplicate_proposals(all_proposals)

    # Count how many were actually added
    added_count = len(deduplicated) - len(existing)

    save_proposals(deduplicated)
    return added_count


# =============================================================================
# CLI Interface
# =============================================================================


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate proposals from detection results."""
    detections_path = Path(args.detections)

    if not detections_path.exists():
        print(f"Error: Detections file not found: {detections_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(detections_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error: Failed to load detections: {e}", file=sys.stderr)
        sys.exit(1)

    # Handle both direct list and wrapped format
    if isinstance(data, list):
        detections = data
    else:
        detections = data.get("detections", data.get("results", []))

    print(f"Processing {len(detections)} detection(s)...")

    proposals = generate_proposals(detections)

    print(f"\nGenerated {len(proposals)} proposal(s):\n")

    for i, proposal in enumerate(proposals, 1):
        print(f"{i}. [{proposal.enhancement_type}] {proposal.title}")
        print(
            f"   ROI: {proposal.roi_score:.2f} | "
            f"Effort: {proposal.estimated_effort_hours:.1f}h | "
            f"Value: {proposal.estimated_value:.2f} | "
            f"Approval: {proposal.approval_tier}"
        )
        print(f"   {proposal.rationale[:80]}...")
        print()

    if args.dry_run:
        print("Dry run - proposals not saved.")
        result = {
            "success": True,
            "dry_run": True,
            "proposals_generated": len(proposals),
            "proposals": [p.to_dict() for p in proposals],
        }
    else:
        added = add_proposals(proposals)
        print(f"Added {added} new proposal(s) to storage.")
        result = {
            "success": True,
            "proposals_generated": len(proposals),
            "proposals_added": added,
            "storage_path": str(get_proposals_path()),
        }

    if args.json:
        print(json.dumps(result, indent=2))


def cmd_convert(args: argparse.Namespace) -> None:
    """Convert a proposal to initiative format."""
    proposal_id = args.proposal_id

    # Load proposals
    proposals_dicts = load_proposals()

    # Find the proposal
    found = None
    for p_dict in proposals_dicts:
        if p_dict.get("proposal_id") == proposal_id:
            found = p_dict
            break

    if not found:
        print(f"Error: Proposal not found: {proposal_id}", file=sys.stderr)
        sys.exit(1)

    proposal = EnhancementProposal.from_dict(found)
    initiative = convert_to_initiative(proposal)

    print(json.dumps(initiative, indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    """List stored proposals."""
    proposals_dicts = load_proposals()

    # Filter by status if specified
    if args.status:
        proposals_dicts = [p for p in proposals_dicts if p.get("status") == args.status]

    # Filter by type if specified
    if args.type:
        proposals_dicts = [
            p for p in proposals_dicts if p.get("enhancement_type") == args.type
        ]

    result = {
        "success": True,
        "count": len(proposals_dicts),
        "filters": {
            "status": args.status,
            "type": args.type,
        },
        "proposals": proposals_dicts,
    }

    print(json.dumps(result, indent=2))


def cmd_help(args: argparse.Namespace) -> None:
    """Show help information."""
    help_text = """
Capability Proposer - P30 Enhancement Proposal Generation

Commands:
    generate    Generate proposals from detection results
    convert     Convert proposal to initiative_engine format
    list        List stored proposals
    help        Show this help

Generate options:
    --detections FILE   Path to detection results JSON file (required)
    --dry-run           Preview proposals without saving
    --json              Output results as JSON

Convert options:
    --proposal-id ID    Proposal ID to convert (required)

List options:
    --status STATUS     Filter by status (pending, approved, rejected, implemented)
    --type TYPE         Filter by enhancement type

Enhancement Types:
    new_command         - New slash command
    new_agent           - New agent for capability
    hook_adjustment     - Hook configuration change
    workflow_change     - Workflow optimization
    capability_addition - Extend existing agent

Approval Tiers:
    auto                - Low-risk, auto-approved
    human               - Requires human review
    executive           - Requires executive approval

Examples:
    # Generate proposals from detections
    python capability_proposer.py generate --detections detections.json

    # Preview without saving
    python capability_proposer.py generate --detections detections.json --dry-run

    # Convert proposal to initiative format
    python capability_proposer.py convert --proposal-id "enh-NEWC-20260226-abc123"

    # List pending proposals
    python capability_proposer.py list --status pending
"""
    print(help_text)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="P30 Capability Proposer - Enhancement Proposal Generation"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Generate command
    gen_parser = subparsers.add_parser(
        "generate", help="Generate proposals from detections"
    )
    gen_parser.add_argument(
        "--detections", required=True, help="Path to detection results JSON file"
    )
    gen_parser.add_argument(
        "--dry-run", action="store_true", help="Preview without saving"
    )
    gen_parser.add_argument("--json", action="store_true", help="Output as JSON")
    gen_parser.set_defaults(func=cmd_generate)

    # Convert command
    conv_parser = subparsers.add_parser("convert", help="Convert to initiative format")
    conv_parser.add_argument(
        "--proposal-id", required=True, help="Proposal ID to convert"
    )
    conv_parser.set_defaults(func=cmd_convert)

    # List command
    list_parser = subparsers.add_parser("list", help="List stored proposals")
    list_parser.add_argument("--status", help="Filter by status")
    list_parser.add_argument("--type", help="Filter by enhancement type")
    list_parser.set_defaults(func=cmd_list)

    # Help command
    help_parser = subparsers.add_parser("help", help="Show help")
    help_parser.set_defaults(func=cmd_help)

    args = parser.parse_args()

    if hasattr(args, "func"):
        args.func(args)
    else:
        cmd_help(args)


if __name__ == "__main__":
    main()
