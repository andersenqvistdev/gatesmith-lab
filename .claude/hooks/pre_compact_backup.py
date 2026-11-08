# /// script
# requires-python = ">=3.10"
# ///
"""
PreCompact Hook: Back up conversation transcript before compression.
Preserves full context history for debugging and analysis.

The PreCompact payload carries ``transcript_path`` (a path to the JSONL
transcript), never an inlined ``transcript`` list. Reading the latter returned
[] on every compaction, so this hook silently backed up nothing from the day it
was written. Verified against the CLI's own hook-input schema:
``hook_event_name:N("PreCompact"), trigger, custom_instructions`` plus the base
fields ``session_id``/``transcript_path``/``cwd``.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    if not isinstance(input_data, dict):
        # The schema always sends an object. Anything else (null, a list, a
        # bare string) must not become a traceback on the operator's screen.
        sys.exit(0)

    transcript_path = input_data.get("transcript_path")
    if not isinstance(transcript_path, str):
        transcript_path = ""
    session_id = input_data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown"
    trigger = input_data.get("trigger")
    if not isinstance(trigger, str):
        trigger = "unknown"

    if not transcript_path or not os.path.isfile(transcript_path):
        # Nothing to preserve. Say so on stderr rather than exiting silently —
        # a backup hook that quietly does nothing is the failure this fixes.
        print(
            f"PreCompact: no transcript to back up (path={transcript_path!r})",
            file=sys.stderr,
        )
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    backup_dir = os.path.join(project_dir, "logs", "transcripts")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = f"transcript_{session_id}_{timestamp}"

    try:
        os.makedirs(backup_dir, exist_ok=True)
        destination = os.path.join(backup_dir, f"{stem}.jsonl")
        shutil.copyfile(transcript_path, destination)

        line_count = 0
        with open(destination, encoding="utf-8", errors="replace") as handle:
            for _ in handle:
                line_count += 1

        meta = {
            "session_id": session_id,
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "source": transcript_path,
            "line_count": line_count,
        }
        with open(os.path.join(backup_dir, f"{stem}.meta.json"), "w") as handle:
            json.dump(meta, handle, indent=2)
    except OSError as exc:
        # Never block a compaction because a backup failed.
        print(f"PreCompact: backup failed ({exc})", file=sys.stderr)
        sys.exit(0)

    _prune_old_backups(backup_dir)

    print(f"Transcript backed up: {stem}.jsonl ({line_count} lines)", file=sys.stderr)
    sys.exit(0)


# A transcript is a verbatim record of everything the session saw, including any
# credential that passed through it. Keeping every one forever is both unbounded
# disk growth and an ever-growing pile of plaintext. This hook backed nothing up
# at all before it was fixed, so a retention limit is part of turning it on
# rather than a change to established behaviour.
MAX_BACKUPS = 20


def _prune_old_backups(backup_dir: str) -> None:
    """Keep only the most recent MAX_BACKUPS transcript backups."""
    try:
        entries = [
            name
            for name in os.listdir(backup_dir)
            if name.startswith("transcript_") and name.endswith(".jsonl")
        ]
        # The timestamp is in the name, so lexical order is chronological.
        for name in sorted(entries)[:-MAX_BACKUPS]:
            stem = name[: -len(".jsonl")]
            for path in (
                os.path.join(backup_dir, name),
                os.path.join(backup_dir, f"{stem}.meta.json"),
            ):
                try:
                    os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


if __name__ == "__main__":
    main()
