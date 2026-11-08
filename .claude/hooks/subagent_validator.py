# /// script
# requires-python = ">=3.10"
# ///
"""
SubagentStop Hook: Validate sub-agent output quality.
Ensures sub-agents deliver complete, structured results.
Also handles auto-consultant lifecycle updates.
"""

import json
import os
import sys

# Import consultant_lifecycle functions with graceful fallback
_consultant_lifecycle_available = False
try:
    # Add the company hooks directory to the path for imports
    _hooks_dir = os.path.dirname(os.path.abspath(__file__))
    _company_hooks_dir = os.path.join(_hooks_dir, "company")
    if _company_hooks_dir not in sys.path:
        sys.path.insert(0, _company_hooks_dir)

    from consultant_lifecycle import update_consultant_status

    _consultant_lifecycle_available = True
except ImportError:
    # Fallback: consultant_lifecycle not available
    def update_consultant_status(consultant_id, status, context=None, company_dir=None):
        return {
            "success": False,
            "reason": "import_error",
            "message": "consultant_lifecycle module not available",
        }


def _extract_learnings_from_output(output: str) -> dict | None:
    """
    Extract learnings/insights from subagent output.

    Looks for patterns like:
    - "Learned: ..."
    - "Key insight: ..."
    - "Pattern discovered: ..."
    - "## Learnings" sections

    Returns dict with extracted learnings or None if none found.
    """
    if not output:
        return None

    learnings = {
        "patterns": [],
        "insights": [],
        "decisions": [],
    }

    _ = output.lower()  # Reserved for case-insensitive matching

    # Look for explicit learning markers
    learning_markers = [
        ("learned:", "insights"),
        ("key insight:", "insights"),
        ("pattern discovered:", "patterns"),
        ("pattern identified:", "patterns"),
        ("decision:", "decisions"),
        ("decided to:", "decisions"),
    ]

    lines = output.split("\n")
    for line in lines:
        line_lower = line.lower().strip()
        for marker, category in learning_markers:
            if marker in line_lower:
                # Extract the content after the marker
                idx = line_lower.find(marker)
                content = line[idx + len(marker) :].strip()
                if content:
                    learnings[category].append(content)

    # Check if any learnings were found
    total_learnings = sum(len(v) for v in learnings.values())
    if total_learnings == 0:
        return None

    return learnings


def _handle_auto_consultant(input_data: dict) -> None:
    """
    Handle auto-consultant lifecycle updates after subagent completion.

    If the subagent was an auto-consultant:
    - Updates status to "available"
    - Captures any learnings from the output
    - Updates lastActive timestamp (handled by update_consultant_status)
    """
    if not _consultant_lifecycle_available:
        return

    # Check if this subagent is an auto-consultant
    # Check both top-level and metadata sub-object for backward compatibility
    metadata = input_data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    is_auto_consultant = input_data.get("is_auto_consultant", False) or metadata.get(
        "is_auto_consultant", False
    )
    if not is_auto_consultant:
        return

    consultant_id = input_data.get("consultant_id") or metadata.get("consultant_id")
    if not consultant_id:
        return

    output = input_data.get("last_assistant_message")
    if not isinstance(output, str):
        output = ""

    # Extract learnings from the output
    learnings = _extract_learnings_from_output(output)

    # Build context string for the status update
    context_parts = ["Subagent completed"]
    if learnings:
        learning_count = sum(len(v) for v in learnings.values())
        context_parts.append(f"{learning_count} learnings captured")

    context = ". ".join(context_parts)

    # Update consultant status to available
    try:
        result = update_consultant_status(
            consultant_id=consultant_id,
            status="available",
            context=context,
        )
        # Log result for debugging (non-blocking)
        if not result.get("success"):
            # Could log to stderr or audit log, but don't fail the hook
            pass
    except Exception:
        # Gracefully handle any errors - don't fail the hook
        pass


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    if not isinstance(input_data, dict):
        # The schema always sends an object. Anything else (null, a list, a
        # bare string) must not become a traceback on the operator's screen.
        sys.exit(0)

    # SubagentStop sends `agent_type` and `last_assistant_message`. The old code
    # read `subagent_type` and `output` — `subagent_type` is the Agent TOOL's
    # input parameter, not a field of this payload, and `output` does not exist
    # at all. Both gates below therefore saw an empty string for every subagent
    # that has ever run, and neither has fired once.
    agent_type = input_data.get("agent_type")
    if not isinstance(agent_type, str):
        agent_type = ""
    output = input_data.get("last_assistant_message")
    if not isinstance(output, str):
        output = ""
    already_nudged = input_data.get("stop_hook_active") is not False

    # Handle auto-consultant lifecycle updates (non-blocking)
    _handle_auto_consultant(input_data)

    # Never nudge twice: stop_hook_active is true when a previous run already
    # forced a continuation.
    if already_nudged:
        sys.exit(0)

    # A subagent that returned nothing is the one unambiguous failure, and the
    # only one worth forcing a retry over.
    #
    # The previous gates enforced taste, not correctness: the architect gate
    # demanded the literal substrings "step" and "file", so a complete numbered
    # plan naming .py paths failed it, and the reviewer gate forced continuation
    # on any output under 50 characters, so "No issues found." — a valid review
    # result — was rejected. Now that the payload keys are right, those gates
    # would start firing on good work. Whether a plan is well structured or a
    # review is thorough enough is a judgement, and judgements belong in review,
    # not in a hook that can silently restart an agent. Thin output is reported
    # on stderr instead, where a human can see it without the agent being forced.
    if not output.strip():
        # Claude Code's rule (from the CLI's own hook dispatcher, verified on
        # 2.1.260): a SubagentStop hook blocks the stop with `decision: "block"`
        # or `continue: false`. `continue: true` is the default and the output
        # normaliser DROPS the key, so the previous `{"continue": true, ...}`
        # never re-ran anything.
        result = {
            "decision": "block",
            "reason": (
                f"The {agent_type or 'subagent'} returned no output. "
                "Re-run it and return the result."
            ),
        }
        print(json.dumps(result))
        sys.exit(0)

    role = agent_type.lower()
    if (agent_type == "Plan" or "architect" in role) and len(output.strip()) < 200:
        print(
            f"NOTE: {agent_type} returned {len(output.strip())} characters — "
            "unusually short for a plan.",
            file=sys.stderr,
        )
    elif "review" in role and len(output.strip()) < 50:
        print(
            f"NOTE: {agent_type} returned {len(output.strip())} characters. "
            "If that is a clean result, it is fine.",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
