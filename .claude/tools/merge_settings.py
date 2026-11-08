# /// script
# requires-python = ">=3.10"
# ///
"""
Merge Gatesmith settings into an existing .claude/settings.json.

Behavior:
- Hooks: ADDS Gatesmith hooks to existing hooks (never removes yours)
- Permissions: ADDS Gatesmith deny/ask rules, preserves your allow rules
- Other settings: Only sets if not already present (never overwrites)

Usage:
    uv run merge_settings.py <existing_settings> <forge_settings> [--dry-run]

Outputs the merged JSON to stdout. Use --dry-run to preview without writing.
"""

import json
import sys


def merge_hook_list(existing: list, forge: list) -> list:
    """Add Gatesmith hooks to existing hooks without duplicating."""
    # Build set of existing hook commands for dedup
    existing_commands = set()
    for entry in existing:
        for hook in entry.get("hooks", []):
            existing_commands.add(hook.get("command", ""))

    merged = list(existing)  # Start with all existing

    for forge_entry in forge:
        for hook in forge_entry.get("hooks", []):
            if hook.get("command", "") not in existing_commands:
                # This Gatesmith hook doesn't exist yet — add the whole entry
                merged.append(forge_entry)
                existing_commands.add(hook.get("command", ""))
                break  # Only add the entry once

    return merged


def merge_hooks(existing: dict, forge: dict) -> dict:
    """Merge hook lifecycle events."""
    merged = dict(existing)

    for event, forge_hooks in forge.items():
        if event in merged:
            merged[event] = merge_hook_list(merged[event], forge_hooks)
        else:
            merged[event] = forge_hooks

    return merged


def merge_string_list(existing: list, forge: list) -> list:
    """Add items from forge list that aren't already in existing."""
    existing_set = set(existing)
    merged = list(existing)
    for item in forge:
        if item not in existing_set:
            merged.append(item)
    return merged


def merge_permissions(existing: dict, forge: dict) -> dict:
    """Merge permission rules. Gatesmith deny/ask rules are added. Allow is preserved."""
    merged = dict(existing)

    # Deny: add all Gatesmith deny rules (security critical — these should always be present)
    if "deny" in forge:
        merged["deny"] = merge_string_list(merged.get("deny", []), forge["deny"])

    # Ask: add Gatesmith ask rules that aren't already denied or asked
    if "ask" in forge:
        merged["ask"] = merge_string_list(merged.get("ask", []), forge["ask"])

    # Allow: keep existing, add Gatesmith allows that aren't already present
    if "allow" in forge:
        merged["allow"] = merge_string_list(merged.get("allow", []), forge["allow"])

    return merged


def merge_settings(existing: dict, forge: dict) -> dict:
    """Top-level merge. Existing values take precedence for scalar settings."""
    merged = dict(existing)

    # Model and thinking — only set if not present
    if "model" not in merged and "model" in forge:
        merged["model"] = forge["model"]

    if "alwaysThinkingEnabled" not in merged and "alwaysThinkingEnabled" in forge:
        merged["alwaysThinkingEnabled"] = forge["alwaysThinkingEnabled"]

    # Status line — only set if not present
    if "statusLine" not in merged and "statusLine" in forge:
        merged["statusLine"] = forge["statusLine"]

    # Permissions — merge with add-only strategy
    if "permissions" in forge:
        merged["permissions"] = merge_permissions(
            merged.get("permissions", {}), forge["permissions"]
        )

    # Hooks — merge with add-only strategy
    if "hooks" in forge:
        merged["hooks"] = merge_hooks(merged.get("hooks", {}), forge["hooks"])

    return merged


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: merge_settings.py <existing> <forge> [--dry-run]", file=sys.stderr
        )
        sys.exit(1)

    existing_path = sys.argv[1]
    forge_path = sys.argv[2]
    dry_run = "--dry-run" in sys.argv

    with open(existing_path) as f:
        existing = json.load(f)

    with open(forge_path) as f:
        forge = json.load(f)

    merged = merge_settings(existing, forge)

    output = json.dumps(merged, indent=2) + "\n"

    if dry_run:
        # Show what changed
        existing_hooks = sum(len(hooks) for hooks in existing.get("hooks", {}).values())
        merged_hooks = sum(len(hooks) for hooks in merged.get("hooks", {}).values())
        existing_deny = len(existing.get("permissions", {}).get("deny", []))
        merged_deny = len(merged.get("permissions", {}).get("deny", []))

        print(
            f"Hooks: {existing_hooks} → {merged_hooks} (+{merged_hooks - existing_hooks} from Gatesmith)",
            file=sys.stderr,
        )
        print(
            f"Deny rules: {existing_deny} → {merged_deny} (+{merged_deny - existing_deny} from Gatesmith)",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print("Merged output (preview):", file=sys.stderr)

    print(output)


if __name__ == "__main__":
    main()
