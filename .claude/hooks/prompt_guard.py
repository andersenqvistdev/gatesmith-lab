# /// script
# requires-python = ">=3.10"
# ///
"""
UserPromptSubmit Hook: Validate and enrich prompts before processing.
Catches vague prompts, injects project context, logs prompt history.

Security Profile Aware:
- strict: Blocks on destructive keywords
- standard: Warns on destructive keywords
- minimal: Disabled

Automated context (a daemon worker or the deliverable judge — FORGE_WORKER_CONTEXT
/ FORGE_DAEMON / FORGE_EMPLOYEE_ID in the environment): never blocks, only warns,
whatever the profile. Those prompts are machine-built and embed git history, file
contents and PR diffs; blocking one does not stop a destructive action, it
produces a silent phantom — the worker gets no prompt and exits 0 (2026-08-17),
or the judge fails closed on a diff that merely contains the word "wipe"
(2026-08-28: four PRs on one file, each held "no parseable verdict"). Real
commands stay guarded by block_dangerous.py.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

# Import hook_config for profile-aware behavior
try:
    from hook_config import get_exit_code, is_enabled
except ImportError:
    # Fallback if hook_config not available
    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "prompt_guard"

# Set by agent_providers.prepare_environment for every worker and judge spawn;
# the marker propagates to hooks (same triad network_egress_guard uses).
_AUTOMATED_ENV_MARKERS = ("FORGE_WORKER_CONTEXT", "FORGE_DAEMON", "FORGE_EMPLOYEE_ID")


def is_automated_context(environ=None) -> bool:
    """True when the prompt was built by a machine, not typed by a human."""
    env = os.environ if environ is None else environ
    return any(env.get(marker) for marker in _AUTOMATED_ENV_MARKERS)


def caution_exit_code(automated: bool | None = None) -> int:
    """Exit code for a caution-keyword match.

    Interactive: the profile decides (strict -> 2 block, standard -> 1 warn;
    minimal disables the hook before we get here). Automated: 1 (warn), never
    block — see the module docstring for why a block there is a phantom, not
    a safety gain.
    """
    if automated is None:
        automated = is_automated_context()
    if automated:
        return 1
    return get_exit_code(HOOK_NAME)


LOG_DIR = os.path.join(os.getcwd(), "logs")
PROMPT_LOG = os.path.join(LOG_DIR, "prompts.jsonl")

# Prompts that are too short or vague to be actionable
MIN_PROMPT_LENGTH = 5

# Keywords that suggest the user wants destructive operations.
# NOTE: entries are deliberately destructive PHRASES, not bare common words.
# "truncate" was removed — it is an everyday word (truncate a string/output/log
# preview) and hard-blocked every prompt containing it. Genuinely destructive
# truncation is still guarded elsewhere (block_dangerous for rm, plan_checker for
# "TRUNCATE TABLE").
CAUTION_KEYWORDS = [
    "delete everything",
    "remove all",
    "wipe",
    "nuke",
    "destroy",
    "drop database",
]

# Match keywords on WORD BOUNDARIES, not as bare substrings. The old
# ``keyword in prompt`` test fired on incidental substrings — e.g. a git-log
# line "prevent queue wipes", or "swipe"/"wipeout"/"destroyed"/"destructor".
# That matters because this hook also runs on *automated worker* prompts, which
# inject git history, file contents, and agent memory: an incidental match
# blocked the UserPromptSubmit, the worker received no prompt, produced no
# files, and exited 0 — a silent phantom. Word boundaries keep genuine
# destructive instructions blocked ("wipe the disk" still matches) while
# letting everyday text through. Genuinely destructive shell commands remain
# guarded by block_dangerous.py regardless.
_CAUTION_PATTERNS = [
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    for kw in CAUTION_KEYWORDS
]


def main():
    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    prompt = input_data.get("prompt", "")
    session_id = input_data.get("session_id", "unknown")

    # Log the prompt
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "prompt_length": len(prompt),
        "prompt_preview": prompt[:100],
    }
    with open(PROMPT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    # Check for caution keywords (word-boundary matched — see _CAUTION_PATTERNS).
    # The exit code follows the profile (hook_config: strict block, standard
    # warn) and is never a block for a machine-built prompt.
    for keyword, pattern in _CAUTION_PATTERNS:
        if pattern.search(prompt):
            code = caution_exit_code()
            if code == 2:
                print(
                    f"CAUTION: Prompt contains '{keyword}' — blocked (strict profile). "
                    "Rephrase, or confirm the destructive intent explicitly.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"CAUTION: Prompt contains '{keyword}' — flagged, not blocked. "
                    "Destructive shell commands remain guarded by block_dangerous.",
                    file=sys.stderr,
                )
            sys.exit(code)

    sys.exit(0)


if __name__ == "__main__":
    main()
