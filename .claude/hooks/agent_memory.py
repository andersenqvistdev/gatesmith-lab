#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Agent Memory Archival System — prevent unbounded memory growth.

Manages memory file rotation and cleanup for company agents:
- Archives memory when it exceeds MAX_LINES
- Retains recent content for context continuity
- Cleans up archives older than ARCHIVE_RETENTION_DAYS

Usage:
    # Append to memory and auto-rotate if needed
    python agent_memory.py append <agent_id> "New memory content"

    # Force rotation (archive current, keep recent)
    python agent_memory.py rotate <agent_id>

    # Cleanup old archives
    python agent_memory.py cleanup <agent_id>

    # Check memory status
    python agent_memory.py status <agent_id>
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Configuration
MAX_LINES = 1000
KEEP_RECENT = 200
ARCHIVE_RETENTION_DAYS = 30

# Base paths
COMPANY_DIR = ".company"
AGENTS_DIR = os.path.join(COMPANY_DIR, "agents")


def get_memory_path(agent_id: str) -> Path:
    """Get the path to an agent's memory file."""
    return Path(os.getcwd()) / AGENTS_DIR / agent_id / "memory.md"


def get_archive_dir(agent_id: str) -> Path:
    """Get the archive directory for an agent."""
    return Path(os.getcwd()) / AGENTS_DIR / agent_id / "archive"


def count_lines(file_path: Path) -> int:
    """Count lines in a memory file."""
    if not file_path.exists():
        return 0

    with open(file_path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def rotate_memory(agent_id: str) -> dict:
    """
    Archive and rotate memory when exceeds max.

    Returns dict with:
        - rotated: bool
        - archived_to: str (archive path if rotated)
        - lines_archived: int
        - lines_kept: int
    """
    memory_path = get_memory_path(agent_id)

    if not memory_path.exists():
        return {
            "rotated": False,
            "reason": "memory file does not exist",
            "agent_id": agent_id,
        }

    line_count = count_lines(memory_path)

    if line_count <= MAX_LINES:
        return {
            "rotated": False,
            "reason": f"line count ({line_count}) within limit ({MAX_LINES})",
            "agent_id": agent_id,
            "current_lines": line_count,
        }

    # Read all content
    with open(memory_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Create archive directory
    archive_dir = get_archive_dir(agent_id)
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Generate archive filename with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"memory_{timestamp}.md"

    # Calculate split points
    lines_to_archive = len(lines) - KEEP_RECENT
    archived_lines = lines[:lines_to_archive]
    kept_lines = lines[lines_to_archive:]

    # Write archive with header
    archive_header = f"# Archived Memory - {agent_id}\n"
    archive_header += f"# Archived: {datetime.now(timezone.utc).isoformat()}\n"
    archive_header += f"# Original lines: 1-{lines_to_archive}\n"
    archive_header += "---\n\n"

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(archive_header)
        f.writelines(archived_lines)

    # Write kept lines back to memory (with continuity note)
    continuity_note = (
        f"\n<!-- Memory rotated on {datetime.now(timezone.utc).isoformat()} -->\n"
    )
    continuity_note += f"<!-- Previous content archived to: {archive_path.name} -->\n\n"

    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(continuity_note)
        f.writelines(kept_lines)

    return {
        "rotated": True,
        "agent_id": agent_id,
        "archived_to": str(archive_path),
        "lines_archived": lines_to_archive,
        "lines_kept": len(kept_lines),
        "timestamp": timestamp,
    }


def cleanup_old_archives(
    agent_id: str, retention_days: int = ARCHIVE_RETENTION_DAYS
) -> dict:
    """
    Delete archives older than retention period.

    Returns dict with:
        - deleted: list of deleted archive names
        - retained: list of retained archive names
        - cutoff_date: datetime string
    """
    archive_dir = get_archive_dir(agent_id)

    if not archive_dir.exists():
        return {
            "deleted": [],
            "retained": [],
            "reason": "no archive directory",
            "agent_id": agent_id,
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = []
    retained = []

    for archive_file in archive_dir.glob("memory_*.md"):
        # Parse timestamp from filename: memory_YYYYMMDD_HHMMSS.md
        try:
            filename = archive_file.stem  # memory_20250115_143022
            timestamp_str = filename.replace("memory_", "")  # 20250115_143022
            file_date = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            file_date = file_date.replace(tzinfo=timezone.utc)

            if file_date < cutoff:
                archive_file.unlink()
                deleted.append(archive_file.name)
            else:
                retained.append(archive_file.name)
        except (ValueError, OSError) as e:
            # Keep files we can't parse
            retained.append(f"{archive_file.name} (parse error: {e})")

    return {
        "agent_id": agent_id,
        "deleted": deleted,
        "retained": retained,
        "cutoff_date": cutoff.isoformat(),
        "retention_days": retention_days,
    }


def append_to_memory(agent_id: str, content: str) -> dict:
    """
    Append content to memory and check if rotation is needed.

    Returns dict with append status and any rotation performed.
    """
    memory_path = get_memory_path(agent_id)

    # Ensure agent directory exists
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    # Append content with timestamp
    timestamp = datetime.now(timezone.utc).isoformat()
    formatted_content = f"\n<!-- Entry: {timestamp} -->\n{content}\n"

    with open(memory_path, "a", encoding="utf-8") as f:
        f.write(formatted_content)

    # Check if rotation needed
    line_count = count_lines(memory_path)
    rotation_result = None

    if line_count > MAX_LINES:
        rotation_result = rotate_memory(agent_id)

    return {
        "appended": True,
        "agent_id": agent_id,
        "lines_after_append": line_count if not rotation_result else KEEP_RECENT,
        "rotation": rotation_result,
    }


def get_status(agent_id: str) -> dict:
    """Get memory status for an agent."""
    memory_path = get_memory_path(agent_id)
    archive_dir = get_archive_dir(agent_id)

    line_count = count_lines(memory_path) if memory_path.exists() else 0
    archives = list(archive_dir.glob("memory_*.md")) if archive_dir.exists() else []

    return {
        "agent_id": agent_id,
        "memory_exists": memory_path.exists(),
        "memory_path": str(memory_path),
        "current_lines": line_count,
        "max_lines": MAX_LINES,
        "rotation_threshold": MAX_LINES,
        "keep_recent": KEEP_RECENT,
        "needs_rotation": line_count > MAX_LINES,
        "archive_count": len(archives),
        "archive_dir": str(archive_dir),
        "archives": [a.name for a in sorted(archives)],
        "retention_days": ARCHIVE_RETENTION_DAYS,
    }


def print_help():
    """Print usage help."""
    help_text = """
Agent Memory Archival System

Commands:
    append <agent_id> <content>  - Append to memory and auto-rotate
    rotate <agent_id>            - Force rotation (archive + keep recent)
    cleanup <agent_id>           - Delete archives older than retention
    status <agent_id>            - Show memory status

Configuration:
    MAX_LINES = 1000             - Trigger rotation at this line count
    KEEP_RECENT = 200            - Lines to keep after rotation
    ARCHIVE_RETENTION_DAYS = 30  - Days to keep archives

Archive path: .company/agents/{agent_id}/archive/memory_{timestamp}.md
"""
    print(help_text)


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    if command == "help" or command == "--help" or command == "-h":
        print_help()
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Error: agent_id required")
        print_help()
        sys.exit(1)

    agent_id = sys.argv[2]

    if command == "append":
        if len(sys.argv) < 4:
            # Read content from stdin if not provided as argument
            content = sys.stdin.read().strip()
        else:
            content = " ".join(sys.argv[3:])

        if not content:
            print("Error: content required for append")
            sys.exit(1)

        result = append_to_memory(agent_id, content)
        print(json.dumps(result, indent=2))

    elif command == "rotate":
        result = rotate_memory(agent_id)
        print(json.dumps(result, indent=2))

    elif command == "cleanup":
        # Optional custom retention days
        retention = ARCHIVE_RETENTION_DAYS
        if len(sys.argv) >= 4:
            try:
                retention = int(sys.argv[3])
            except ValueError:
                print(
                    f"Warning: Invalid retention days '{sys.argv[3]}', using default {ARCHIVE_RETENTION_DAYS}"
                )

        result = cleanup_old_archives(agent_id, retention)
        print(json.dumps(result, indent=2))

    elif command == "status":
        result = get_status(agent_id)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
