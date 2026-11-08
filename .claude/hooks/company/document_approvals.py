#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""
Document Approvals — Universal document approval system for Gatesmith.

P23 implementation: Automatic discovery, routing, and tracking of documents
requiring sign-off based on YAML frontmatter metadata.

Approval Tiers:
    - team_lead: Single approver signs off
    - c_level: ALL listed approvers must approve
    - board: Majority quorum (>50%) of listed approvers

Usage:
    # Scan for documents needing approval
    python document_approvals.py scan

    # Show pending approvals
    python document_approvals.py pending

    # Record a vote
    python document_approvals.py vote --doc-id <id> --approver <id> --decision approve

    # Check approval status
    python document_approvals.py status --doc-id <id>

    # Update document after approval/rejection
    python document_approvals.py finalize --doc-id <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
except ImportError:
    yaml = None  # Will fail gracefully if PyYAML not available


# -----------------------------------------------------------------------------
# Type Definitions
# -----------------------------------------------------------------------------

ApprovalTier = Literal["team_lead", "c_level", "board"]
DocumentType = Literal[
    "goals",
    "roadmap",
    "policy",
    "decision",
    "budget",
    "architecture",
    "process",
    "strategy",
]
DocumentStatus = Literal[
    "draft", "proposed", "under_review", "approved", "rejected", "superseded"
]
VoteDecision = Literal["approve", "reject", "abstain"]


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------


@dataclass
class ApprovalVote:
    """Individual approval vote record."""

    approver_id: str
    decision: VoteDecision
    comments: str = ""
    recorded_at: str = ""

    def __post_init__(self):
        if not self.recorded_at:
            self.recorded_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "approver_id": self.approver_id,
            "decision": self.decision,
            "comments": self.comments,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalVote":
        return cls(
            approver_id=data["approver_id"],
            decision=data["decision"],
            comments=data.get("comments", ""),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass
class DocumentApproval:
    """Document approval tracking record."""

    doc_id: str
    file_path: str
    title: str
    doc_type: DocumentType
    status: DocumentStatus
    approval_tier: ApprovalTier
    approval_required: list[str]
    author: str = ""
    proposed_at: str = ""
    votes: list[ApprovalVote] = field(default_factory=list)
    approved_at: str | None = None
    rejected_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "file_path": self.file_path,
            "title": self.title,
            "doc_type": self.doc_type,
            "status": self.status,
            "approval_tier": self.approval_tier,
            "approval_required": self.approval_required,
            "author": self.author,
            "proposed_at": self.proposed_at,
            "votes": [v.to_dict() for v in self.votes],
            "approved_at": self.approved_at,
            "rejected_at": self.rejected_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentApproval":
        return cls(
            doc_id=data["doc_id"],
            file_path=data["file_path"],
            title=data["title"],
            doc_type=data["doc_type"],
            status=data["status"],
            approval_tier=data["approval_tier"],
            approval_required=data["approval_required"],
            author=data.get("author", ""),
            proposed_at=data.get("proposed_at", ""),
            votes=[ApprovalVote.from_dict(v) for v in data.get("votes", [])],
            approved_at=data.get("approved_at"),
            rejected_at=data.get("rejected_at"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class ApprovalState:
    """Global approval state tracking."""

    schema_version: str = "1.0"
    documents: list[DocumentApproval] = field(default_factory=list)
    pending_review: list[str] = field(default_factory=list)  # doc_ids
    history: list[dict[str, Any]] = field(default_factory=list)
    last_scan: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "documents": [d.to_dict() for d in self.documents],
            "pending_review": self.pending_review,
            "history": self.history,
            "last_scan": self.last_scan,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalState":
        return cls(
            schema_version=data.get("schema_version", "1.0"),
            documents=[
                DocumentApproval.from_dict(d) for d in data.get("documents", [])
            ],
            pending_review=data.get("pending_review", []),
            history=data.get("history", []),
            last_scan=data.get("last_scan"),
        )


# -----------------------------------------------------------------------------
# Path Resolution
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    # Try to import company_resolver
    try:
        from . import company_resolver

        return company_resolver.get_company_dir()
    except ImportError:
        pass

    # Fallback: look for .company in current directory or parents
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        company_dir = parent / ".company"
        if company_dir.exists():
            return company_dir

    # Default to current directory
    return cwd / ".company"


def get_state_path() -> Path:
    """Get the path to document_approvals.json."""
    return get_company_dir() / "document_approvals.json"


def get_scan_directories() -> list[Path]:
    """Get directories to scan for documents."""
    company_dir = get_company_dir()
    project_root = company_dir.parent

    dirs = []

    # .planning directory
    planning_dir = project_root / ".planning"
    if planning_dir.exists():
        dirs.append(planning_dir)

    # .company/knowledge directory
    knowledge_dir = company_dir / "knowledge"
    if knowledge_dir.exists():
        dirs.append(knowledge_dir)

    return dirs


# -----------------------------------------------------------------------------
# State Management
# -----------------------------------------------------------------------------


def load_state() -> ApprovalState:
    """Load approval state from file."""
    state_path = get_state_path()

    if not state_path.exists():
        return ApprovalState()

    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        return ApprovalState.from_dict(data)
    except (json.JSONDecodeError, OSError, TypeError):
        return ApprovalState()


def save_state(state: ApprovalState) -> None:
    """Save approval state to file."""
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Write atomically (prevents corruption under parallel workers)
    fd, tmp_path = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.replace(tmp_path, str(state_path))
    except BaseException:
        os.unlink(tmp_path)
        raise


def add_history_entry(
    state: ApprovalState, action: str, doc_id: str, actor: str, details: str
) -> None:
    """Add an entry to the history log."""
    state.history.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "doc_id": doc_id,
            "actor": actor,
            "details": details,
        }
    )


# -----------------------------------------------------------------------------
# YAML Frontmatter Parsing
# -----------------------------------------------------------------------------


def _normalize_yaml_value(value: Any) -> Any:
    """Normalize YAML value to JSON-serializable type.

    PyYAML parses dates as datetime.date objects, which aren't JSON-serializable.
    This function converts them to ISO format strings.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_yaml_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_yaml_value(v) for k, v in value.items()}
    return value


def parse_document_frontmatter(file_path: Path) -> dict[str, Any] | None:
    """
    Parse YAML frontmatter from a markdown document.

    Args:
        file_path: Path to the markdown file.

    Returns:
        Parsed frontmatter dict, or None if no valid frontmatter found.
    """
    if yaml is None:
        # Fallback to simple regex parsing if PyYAML not available
        return _parse_frontmatter_fallback(file_path)

    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()

        # Check for YAML frontmatter (--- ... ---)
        if not content.startswith("---"):
            return None

        # Find the closing ---
        end_match = re.search(r"\n---\s*\n", content[3:])
        if not end_match:
            return None

        frontmatter_text = content[3 : 3 + end_match.start()]
        parsed = yaml.safe_load(frontmatter_text)

        # Normalize all values to JSON-serializable types
        if parsed:
            parsed = _normalize_yaml_value(parsed)

        return parsed

    except (OSError, yaml.YAMLError):
        return None


def _parse_frontmatter_fallback(file_path: Path) -> dict[str, Any] | None:
    """Fallback frontmatter parser without PyYAML."""
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()

        if not content.startswith("---"):
            return None

        end_match = re.search(r"\n---\s*\n", content[3:])
        if not end_match:
            return None

        frontmatter_text = content[3 : 3 + end_match.start()]

        # Simple key-value parsing
        result = {}
        for line in frontmatter_text.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()

                # Handle lists
                if value.startswith("["):
                    # Simple list parsing
                    items = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', value[1:-1])
                    result[key] = [item[0] or item[1] or item[2] for item in items]
                elif value.startswith("-"):
                    # Multi-line list (collect following lines)
                    result[key] = []
                else:
                    # Remove quotes
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    result[key] = value

        return result

    except OSError:
        return None


def generate_doc_id(file_path: Path) -> str:
    """Generate a unique document ID from file path."""
    # Use relative path hash for consistency
    rel_path = str(file_path)
    hash_part = hashlib.md5(rel_path.encode()).hexdigest()[:8]
    stem = file_path.stem.lower().replace(" ", "-")[:20]
    return f"doc-{stem}-{hash_part}"


# -----------------------------------------------------------------------------
# Document Discovery
# -----------------------------------------------------------------------------


def scan_documents_for_approval() -> list[DocumentApproval]:
    """
    Scan directories for documents with approval metadata.

    Returns:
        List of DocumentApproval objects for documents needing approval.
    """
    discovered = []
    scan_dirs = get_scan_directories()

    for scan_dir in scan_dirs:
        for md_file in scan_dir.glob("**/*.md"):
            frontmatter = parse_document_frontmatter(md_file)

            if not frontmatter:
                continue

            # Check for required approval fields
            if not all(
                key in frontmatter
                for key in ["status", "approval_tier", "approval_required"]
            ):
                # Also check for legacy format (Status: PROPOSED, Approval Required: ...)
                if "Status" in str(frontmatter) or "status" in frontmatter:
                    frontmatter = _normalize_legacy_frontmatter(frontmatter, md_file)
                    if not frontmatter:
                        continue
                else:
                    continue

            # Only process documents with status "proposed"
            status = frontmatter.get("status", "").lower()
            if status != "proposed":
                continue

            # Validate approval_tier
            tier = frontmatter.get("approval_tier", "").lower()
            if tier not in ("team_lead", "c_level", "board"):
                continue

            # Validate approval_required is a list
            required = frontmatter.get("approval_required", [])
            if isinstance(required, str):
                required = [r.strip() for r in required.split(",")]
            if not required:
                continue

            # Create DocumentApproval
            # Convert dates to strings if needed (PyYAML parses dates as datetime.date)
            proposed_at = frontmatter.get("proposed_at", "")
            if hasattr(proposed_at, "isoformat"):
                proposed_at = proposed_at.isoformat()
            elif proposed_at is None:
                proposed_at = ""

            doc = DocumentApproval(
                doc_id=generate_doc_id(md_file),
                file_path=str(md_file),
                title=str(frontmatter.get("title", md_file.stem)),
                doc_type=_validate_doc_type(frontmatter.get("type", "policy")),
                status="proposed",
                approval_tier=tier,  # type: ignore
                approval_required=required,
                author=str(frontmatter.get("author", "")),
                proposed_at=str(proposed_at),
            )
            discovered.append(doc)

    return discovered


def _normalize_legacy_frontmatter(
    frontmatter: dict, file_path: Path
) -> dict[str, Any] | None:
    """Normalize legacy frontmatter format to new schema."""
    # Handle the existing Q2-GOALS.md format
    # Status: PROPOSED, Approval Required: CEO, CTO

    result = {}

    # Check for Status field
    for key in frontmatter:
        if key.lower() == "status":
            status = frontmatter[key]
            if isinstance(status, str):
                result["status"] = status.lower()

        if "approval" in key.lower() and "required" in key.lower():
            req = frontmatter[key]
            if isinstance(req, str):
                # Parse "CEO, CTO" format
                result["approval_required"] = [
                    _normalize_approver_id(r.strip()) for r in req.split(",")
                ]
            elif isinstance(req, list):
                result["approval_required"] = [_normalize_approver_id(r) for r in req]

    # If we found approval_required, infer tier
    if result.get("approval_required"):
        approvers = result["approval_required"]
        # Check if approvers are C-level
        if any("ceo" in a or "cto" in a or "cfo" in a for a in approvers):
            result["approval_tier"] = "c_level"
        elif len(approvers) >= 3:
            result["approval_tier"] = "board"
        else:
            result["approval_tier"] = "team_lead"

        # Copy other fields
        result["title"] = frontmatter.get(
            "title", frontmatter.get("Title", file_path.stem)
        )
        result["type"] = frontmatter.get("type", "goals")
        result["author"] = frontmatter.get("author", frontmatter.get("Author", ""))

        return result

    return None


def _normalize_approver_id(approver: str) -> str:
    """Normalize approver name to ID format."""
    # "CEO" -> "forge-ceo", "CTO" -> "forge-cto"
    approver = approver.lower().strip()

    if approver in ("ceo", "cto", "cfo", "coo"):
        return f"forge-{approver}"

    # Already in ID format
    if re.match(r"^[a-z][a-z0-9-]*$", approver):
        return approver

    # Convert "Some Name" to "some-name"
    return re.sub(r"[^a-z0-9]+", "-", approver.lower()).strip("-")


def _validate_doc_type(doc_type: str) -> DocumentType:
    """Validate and normalize document type."""
    valid_types = [
        "goals",
        "roadmap",
        "policy",
        "decision",
        "budget",
        "architecture",
        "process",
        "strategy",
    ]
    doc_type = doc_type.lower()
    if doc_type in valid_types:
        return doc_type  # type: ignore
    return "policy"


# -----------------------------------------------------------------------------
# Approval Routing
# -----------------------------------------------------------------------------


def route_for_approval(doc: DocumentApproval, state: ApprovalState) -> dict[str, Any]:
    """
    Route a document for approval based on its tier.

    Args:
        doc: DocumentApproval to route.
        state: Current approval state.

    Returns:
        Routing result with approvers and instructions.
    """
    result = {
        "doc_id": doc.doc_id,
        "tier": doc.approval_tier,
        "approvers": doc.approval_required,
        "routing_logic": "",
        "instructions": "",
    }

    if doc.approval_tier == "team_lead":
        result["routing_logic"] = "Single approver sign-off required"
        result["instructions"] = f"Requires approval from: {doc.approval_required[0]}"

    elif doc.approval_tier == "c_level":
        result["routing_logic"] = "ALL listed executives must approve"
        result["instructions"] = (
            f"Requires approval from ALL: {', '.join(doc.approval_required)}"
        )

    elif doc.approval_tier == "board":
        quorum = len(doc.approval_required) // 2 + 1
        result["routing_logic"] = (
            f"Majority quorum required ({quorum} of {len(doc.approval_required)})"
        )
        result["instructions"] = (
            f"Requires {quorum}+ approvals from: {', '.join(doc.approval_required)}"
        )

    # Add to state if not already tracked
    existing = next((d for d in state.documents if d.doc_id == doc.doc_id), None)
    if not existing:
        state.documents.append(doc)
        if doc.doc_id not in state.pending_review:
            state.pending_review.append(doc.doc_id)
        add_history_entry(
            state,
            "document_routed",
            doc.doc_id,
            "system",
            f"Routed for {doc.approval_tier} approval",
        )

    return result


# -----------------------------------------------------------------------------
# Voting
# -----------------------------------------------------------------------------


def record_approval_vote(
    state: ApprovalState,
    doc_id: str,
    approver_id: str,
    decision: VoteDecision,
    comments: str = "",
) -> dict[str, Any]:
    """
    Record an approval vote for a document.

    Args:
        state: Current approval state.
        doc_id: Document ID.
        approver_id: ID of the approver voting.
        decision: Vote decision (approve/reject/abstain).
        comments: Optional comments.

    Returns:
        Result dict with success status and any state changes.
    """
    # Find document
    doc = next((d for d in state.documents if d.doc_id == doc_id), None)
    if not doc:
        return {"success": False, "error": f"Document not found: {doc_id}"}

    # Check if approver is authorized
    if approver_id not in doc.approval_required:
        return {
            "success": False,
            "error": f"Approver {approver_id} not in required list: {doc.approval_required}",
        }

    # Check for duplicate vote
    existing_vote = next((v for v in doc.votes if v.approver_id == approver_id), None)
    if existing_vote:
        return {
            "success": False,
            "error": f"Approver {approver_id} has already voted: {existing_vote.decision}",
        }

    # Record vote
    vote = ApprovalVote(
        approver_id=approver_id,
        decision=decision,
        comments=comments,
    )
    doc.votes.append(vote)
    doc.updated_at = datetime.now(timezone.utc).isoformat()

    add_history_entry(
        state,
        "vote_recorded",
        doc_id,
        approver_id,
        f"Voted {decision}" + (f": {comments}" if comments else ""),
    )

    # Check if approval is complete
    completion = check_approval_complete(doc)

    result = {
        "success": True,
        "vote": vote.to_dict(),
        "completion": completion,
    }

    # Update status if complete
    if completion["complete"]:
        if completion["outcome"] == "approved":
            doc.status = "approved"
            doc.approved_at = datetime.now(timezone.utc).isoformat()
            if doc.doc_id in state.pending_review:
                state.pending_review.remove(doc.doc_id)
            add_history_entry(
                state,
                "document_approved",
                doc_id,
                "system",
                f"Approved by {completion['approvers']}",
            )
        elif completion["outcome"] == "rejected":
            doc.status = "rejected"
            doc.rejected_at = datetime.now(timezone.utc).isoformat()
            if doc.doc_id in state.pending_review:
                state.pending_review.remove(doc.doc_id)
            add_history_entry(
                state,
                "document_rejected",
                doc_id,
                "system",
                f"Rejected by {completion['rejectors']}",
            )

    return result


def check_approval_complete(doc: DocumentApproval) -> dict[str, Any]:
    """
    Check if approval process is complete based on tier logic.

    Args:
        doc: Document to check.

    Returns:
        Completion status with outcome.
    """
    approves = [v.approver_id for v in doc.votes if v.decision == "approve"]
    rejects = [v.approver_id for v in doc.votes if v.decision == "reject"]
    total_votes = len(doc.votes)
    required_count = len(doc.approval_required)

    result = {
        "complete": False,
        "outcome": None,
        "approvers": approves,
        "rejectors": rejects,
        "votes_cast": total_votes,
        "votes_required": required_count,
    }

    if doc.approval_tier == "team_lead":
        # Single approver - one vote decides
        if len(approves) >= 1:
            result["complete"] = True
            result["outcome"] = "approved"
        elif len(rejects) >= 1:
            result["complete"] = True
            result["outcome"] = "rejected"

    elif doc.approval_tier == "c_level":
        # ALL must approve; any rejection fails
        if len(rejects) >= 1:
            result["complete"] = True
            result["outcome"] = "rejected"
        elif len(approves) == required_count:
            result["complete"] = True
            result["outcome"] = "approved"

    elif doc.approval_tier == "board":
        # Majority quorum
        quorum = required_count // 2 + 1

        if len(approves) >= quorum:
            result["complete"] = True
            result["outcome"] = "approved"
        elif len(rejects) >= quorum:
            result["complete"] = True
            result["outcome"] = "rejected"
        # Also complete if remaining votes can't change outcome
        remaining = required_count - total_votes
        if len(approves) + remaining < quorum and len(rejects) > 0:
            result["complete"] = True
            result["outcome"] = "rejected"
        elif len(rejects) + remaining < quorum and len(approves) > 0:
            # This shouldn't trigger early - wait for quorum
            pass

    return result


# -----------------------------------------------------------------------------
# Document Status Update
# -----------------------------------------------------------------------------


def update_document_status(doc: DocumentApproval) -> dict[str, Any]:
    """
    Update the document's YAML frontmatter with approval result.

    Args:
        doc: DocumentApproval with final status.

    Returns:
        Result dict with success status.
    """
    file_path = Path(doc.file_path)

    if not file_path.exists():
        return {"success": False, "error": f"File not found: {doc.file_path}"}

    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()

        # Find frontmatter bounds
        if not content.startswith("---"):
            return {"success": False, "error": "No frontmatter found in document"}

        end_match = re.search(r"\n---\s*\n", content[3:])
        if not end_match:
            return {"success": False, "error": "Malformed frontmatter"}

        frontmatter_end = 3 + end_match.end()
        frontmatter_text = content[3 : 3 + end_match.start()]
        body = content[frontmatter_end:]

        # Update frontmatter
        approvers = [v.approver_id for v in doc.votes if v.decision == "approve"]

        new_frontmatter = _update_frontmatter_yaml(
            frontmatter_text,
            {
                "status": doc.status,
                "approved_at": doc.approved_at,
                "rejected_at": doc.rejected_at,
                "approved_by": approvers if doc.status == "approved" else None,
            },
        )

        # Write updated file atomically (prevents truncation race under parallel workers)
        new_content = f"---\n{new_frontmatter}\n---\n{body}"

        fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, str(file_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return {"success": True, "file_path": str(file_path)}

    except OSError as e:
        return {"success": False, "error": str(e)}


def _update_frontmatter_yaml(frontmatter: str, updates: dict[str, Any]) -> str:
    """Update YAML frontmatter with new values."""
    lines = frontmatter.strip().split("\n")
    result_lines = []
    updated_keys = set()

    for line in lines:
        if ":" in line:
            key = line.split(":")[0].strip()
            if key in updates and updates[key] is not None:
                value = updates[key]
                if isinstance(value, list):
                    result_lines.append(f"{key}:")
                    for item in value:
                        result_lines.append(f"  - {item}")
                else:
                    result_lines.append(f"{key}: {value}")
                updated_keys.add(key)
                continue
        result_lines.append(line)

    # Add new keys that weren't in original
    for key, value in updates.items():
        if key not in updated_keys and value is not None:
            if isinstance(value, list):
                result_lines.append(f"{key}:")
                for item in value:
                    result_lines.append(f"  - {item}")
            else:
                result_lines.append(f"{key}: {value}")

    return "\n".join(result_lines)


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan for documents needing approval."""
    state = load_state()

    print("Scanning for documents with approval metadata...")
    discovered = scan_documents_for_approval()

    if not discovered:
        print("No documents found requiring approval.")
        return 0

    print(f"\nFound {len(discovered)} document(s):\n")

    for doc in discovered:
        # Route each document
        routing = route_for_approval(doc, state)

        print(f"  [{doc.doc_id}]")
        print(f"    Title: {doc.title}")
        print(f"    Path: {doc.file_path}")
        print(f"    Tier: {doc.approval_tier}")
        print(f"    Required: {', '.join(doc.approval_required)}")
        print(f"    Routing: {routing['routing_logic']}")
        print()

    state.last_scan = datetime.now(timezone.utc).isoformat()
    save_state(state)

    if args.json:
        print(
            json.dumps(
                {
                    "success": True,
                    "discovered": [d.to_dict() for d in discovered],
                    "count": len(discovered),
                },
                indent=2,
            )
        )

    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """Show pending approvals."""
    state = load_state()

    pending_docs = [d for d in state.documents if d.doc_id in state.pending_review]

    if not pending_docs:
        print("No documents pending approval.")
        if args.json:
            print(json.dumps({"pending": [], "count": 0}))
        return 0

    print(f"Documents pending approval ({len(pending_docs)}):\n")

    for doc in pending_docs:
        completion = check_approval_complete(doc)

        print(f"  [{doc.doc_id}]")
        print(f"    Title: {doc.title}")
        print(f"    Tier: {doc.approval_tier}")
        print(f"    Votes: {completion['votes_cast']}/{completion['votes_required']}")
        print(f"    Approved by: {', '.join(completion['approvers']) or 'none'}")
        print(f"    Rejected by: {', '.join(completion['rejectors']) or 'none'}")
        print()

    if args.json:
        print(
            json.dumps(
                {
                    "pending": [d.to_dict() for d in pending_docs],
                    "count": len(pending_docs),
                },
                indent=2,
            )
        )

    return 0


def cmd_vote(args: argparse.Namespace) -> int:
    """Record an approval vote."""
    state = load_state()

    result = record_approval_vote(
        state=state,
        doc_id=args.doc_id,
        approver_id=args.approver,
        decision=args.decision,
        comments=args.comments or "",
    )

    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    save_state(state)

    print(f"Vote recorded: {args.approver} -> {args.decision}")

    completion = result["completion"]
    if completion["complete"]:
        print(f"Approval complete: {completion['outcome']}")

        # Update document file
        doc = next(d for d in state.documents if d.doc_id == args.doc_id)
        update_result = update_document_status(doc)
        if update_result["success"]:
            print(f"Document updated: {update_result['file_path']}")
        else:
            print(f"Warning: Failed to update document: {update_result['error']}")

        save_state(state)

    if args.json:
        print(json.dumps(result, indent=2))

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Check document approval status."""
    state = load_state()

    doc = next((d for d in state.documents if d.doc_id == args.doc_id), None)
    if not doc:
        print(f"Document not found: {args.doc_id}", file=sys.stderr)
        return 1

    completion = check_approval_complete(doc)

    print(f"Document: {doc.title}")
    print(f"  ID: {doc.doc_id}")
    print(f"  Status: {doc.status}")
    print(f"  Tier: {doc.approval_tier}")
    print(f"  Required: {', '.join(doc.approval_required)}")
    print(f"  Votes: {completion['votes_cast']}/{completion['votes_required']}")
    print(f"  Complete: {completion['complete']}")
    if completion["complete"]:
        print(f"  Outcome: {completion['outcome']}")

    print("\nVotes:")
    for vote in doc.votes:
        print(f"  - {vote.approver_id}: {vote.decision}")
        if vote.comments:
            print(f"    Comment: {vote.comments}")

    if args.json:
        print(
            json.dumps(
                {
                    "document": doc.to_dict(),
                    "completion": completion,
                },
                indent=2,
            )
        )

    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    """Finalize and update document after approval."""
    state = load_state()

    doc = next((d for d in state.documents if d.doc_id == args.doc_id), None)
    if not doc:
        print(f"Document not found: {args.doc_id}", file=sys.stderr)
        return 1

    result = update_document_status(doc)

    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    print(f"Document updated: {result['file_path']}")

    if args.json:
        print(json.dumps(result, indent=2))

    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Document Approvals - Universal document approval system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # scan command
    scan_parser = subparsers.add_parser(
        "scan", help="Scan for documents needing approval"
    )
    scan_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # pending command
    pending_parser = subparsers.add_parser("pending", help="Show pending approvals")
    pending_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # vote command
    vote_parser = subparsers.add_parser("vote", help="Record an approval vote")
    vote_parser.add_argument("--doc-id", required=True, help="Document ID")
    vote_parser.add_argument("--approver", required=True, help="Approver ID")
    vote_parser.add_argument(
        "--decision", required=True, choices=["approve", "reject", "abstain"]
    )
    vote_parser.add_argument("--comments", help="Optional comments")
    vote_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # status command
    status_parser = subparsers.add_parser(
        "status", help="Check document approval status"
    )
    status_parser.add_argument("--doc-id", required=True, help="Document ID")
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # finalize command
    finalize_parser = subparsers.add_parser(
        "finalize", help="Update document after approval"
    )
    finalize_parser.add_argument("--doc-id", required=True, help="Document ID")
    finalize_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "scan":
        return cmd_scan(args)
    elif args.command == "pending":
        return cmd_pending(args)
    elif args.command == "vote":
        return cmd_vote(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "finalize":
        return cmd_finalize(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
