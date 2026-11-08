#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""model_profile_switcher.py — Model profile switching logic with worker-count sync.

Invoked by bin/gatesmith-model-profile and testable as a module.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_CONFIG_PATH = Path("gatesmith.json")


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def default_config_path(project_root: Path | None = None) -> Path:
    """The file that governs ``modelProfile`` (governing_config.py): the root
    config when it declares the setting, else the install template, else the
    file that holds the profiles. A fresh install has only the template."""
    root = Path(project_root) if project_root else Path.cwd()
    try:
        import governing_config as gc
    except ImportError:
        return root / DEFAULT_CONFIG_PATH
    for key in ("modelProfile", "modelProfiles"):
        got = gc.resolve(key, root)
        if got.declared:
            return root / got.source
    return root / DEFAULT_CONFIG_PATH


class EditRefused(Exception):
    """The change cannot be made as a one-value text edit."""


def _replace_one(text: str, pattern: str, repl: str, what: str) -> str:
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise EditRefused(f"{what}: found {len(matches)} matches, expected 1")
    m = matches[0]
    return text[: m.start(2)] + repl + text[m.end(2) :]


def _top_level_key_span(text: str, key: str) -> tuple[int, int]:
    """Start and end offsets of the value of top-level ``key`` in ``text``."""
    decoder = json.JSONDecoder()
    depth = 0
    i = 0
    in_str = False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            if depth == 1:
                name, end = decoder.raw_decode(text, i)
                rest = text[end:].lstrip()
                if name == key and rest.startswith(":"):
                    colon = text.index(":", end)
                    j = colon + 1
                    while text[j] in " \t\r\n":
                        j += 1
                    _value, value_end = decoder.raw_decode(text, j)
                    return j, value_end
                i = end
                continue
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        i += 1
    raise EditRefused(f"top-level {key} not found")


def edit_config_text(text: str, profile: str, workers: Optional[int]) -> str:
    """Change only the ``modelProfile`` value (and ``maxParallelWorkers`` when
    ``workers`` is given) in the file's own text, keeping every other byte.

    The result must parse to exactly the expected config, or EditRefused is
    raised and nothing should be written: config files are human-owned and a
    rewrite through json.dump would reformat the whole file.
    """
    expected = json.loads(text)
    if not isinstance(expected, dict):
        raise EditRefused("config is not a JSON object")
    new = text
    if "modelProfile" in expected:
        new = _replace_one(
            new,
            r'("modelProfile"\s*:\s*)("(?:[^"\\]|\\.)*")',
            json.dumps(profile),
            "modelProfile",
        )
    else:
        brace = new.index("{")
        new = (
            new[: brace + 1]
            + f'\n  "modelProfile": {json.dumps(profile)},'
            + new[brace + 1 :]
        )
    expected["modelProfile"] = profile
    if workers is not None:
        sched = expected.get("adaptiveScheduler")
        if sched is None:
            # The install template keeps its scheduler block under
            # fullAutonomy, which the scheduler does not read; the top-level
            # block it reads is added with the one value.
            brace = new.index("{")
            new = (
                new[: brace + 1]
                + f'\n  "adaptiveScheduler": {{"maxParallelWorkers": {workers}}},'
                + new[brace + 1 :]
            )
            expected["adaptiveScheduler"] = {"maxParallelWorkers": workers}
        elif not isinstance(sched, dict) or "maxParallelWorkers" not in sched:
            raise EditRefused(
                "adaptiveScheduler.maxParallelWorkers is not in this file; "
                "add it by hand"
            )
        else:
            start = _top_level_key_span(new, "adaptiveScheduler")
            block = new[start[0] : start[1]]
            block = _replace_one(
                block,
                r'("maxParallelWorkers"\s*:\s*)(-?\d+)',
                str(workers),
                "adaptiveScheduler.maxParallelWorkers",
            )
            new = new[: start[0]] + block + new[start[1] :]
            sched["maxParallelWorkers"] = workers
    if json.loads(new) != expected:
        raise EditRefused("the edit would change more than the requested values")
    return new


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def get_profiles(config: dict) -> dict:
    return config.get("modelProfiles", {}).get("profiles", {})


def get_current_profile(config: dict) -> str:
    return config.get("modelProfile", "")


def get_max_parallel_workers(config: dict) -> Optional[int]:
    return config.get("adaptiveScheduler", {}).get("maxParallelWorkers")


# ---------------------------------------------------------------------------
# Mismatch detection and sync
# ---------------------------------------------------------------------------


def check_worker_mismatch(
    config: dict, profile_name: str
) -> Tuple[bool, Optional[int], Optional[int]]:
    """Return (is_mismatch, profile_workers, current_max_workers).

    is_mismatch is False when the profile has no 'workers' field (not applicable).
    """
    profile = get_profiles(config).get(profile_name, {})
    profile_workers: Optional[int] = profile.get("workers")
    if profile_workers is None:
        return False, None, None
    current_workers = get_max_parallel_workers(config)
    return current_workers != profile_workers, profile_workers, current_workers


def sync_workers(config: dict, profile_name: str) -> Optional[int]:
    """Set adaptiveScheduler.maxParallelWorkers from profile's workers field.

    Returns the new value, or None if the profile has no workers field.
    """
    profile = get_profiles(config).get(profile_name, {})
    profile_workers: Optional[int] = profile.get("workers")
    if profile_workers is None:
        return None
    if "adaptiveScheduler" not in config:
        config["adaptiveScheduler"] = {}
    config["adaptiveScheduler"]["maxParallelWorkers"] = profile_workers
    return profile_workers


def switch_profile(config: dict, profile_name: str) -> None:
    config["modelProfile"] = profile_name


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _short_model(model: str) -> str:
    # Checked before the family names below so a future "claude-fable-*" that
    # also carries a family word still renders as Fable in the profile table.
    if "fable" in model:
        return "Fable"
    if "opus" in model:
        return "Opus"
    if "sonnet" in model:
        return "Sonnet"
    if "haiku" in model:
        return "Haiku"
    return model


_SEP = "=" * 65


def display_profiles(config: dict) -> None:
    current = get_current_profile(config)
    profiles = get_profiles(config)
    current_workers = get_max_parallel_workers(config)

    print(_SEP)
    print(" MODEL PROFILES                                    [gatesmith-config]")
    print(_SEP)
    print(f" Active: {current}")
    if current_workers is not None:
        print(f" adaptiveScheduler.maxParallelWorkers: {current_workers}")
    print(_SEP)
    print()
    print(
        f"{'':1}{'Profile':<13} {'Workers':>7}  {'Trivial':<7} {'Standard':<8}"
        f" {'Complex':<7} {'Epic':<7} {'Executive':<9}"
    )
    print("-" * 90)
    for name, prof in profiles.items():
        marker = ">" if name == current else " "
        workers = str(prof.get("workers", "—"))
        trivial = _short_model(prof.get("trivial", ""))
        standard = _short_model(prof.get("standard", ""))
        complex_ = _short_model(prof.get("complex", ""))
        epic = _short_model(prof.get("epic", ""))
        executive = _short_model(prof.get("executive", ""))
        print(
            f"{marker} {name:<13} {workers:>7}  {trivial:<7} {standard:<8}"
            f" {complex_:<7} {epic:<7} {executive:<9}"
        )
    print()
    print("To switch:             /model-profile <name>")
    print("To switch+sync workers: /model-profile <name> --sync-workers")
    print(_SEP)


def display_switch_result(
    old_profile: str,
    new_profile: str,
    profiles: dict,
    mismatch: bool,
    profile_workers: Optional[int],
    current_workers: Optional[int],
    synced_workers: Optional[int],
) -> None:
    print(_SEP)
    print(" MODEL PROFILE SWITCHED                              [success]")
    print(_SEP)
    print(f" Previous: {old_profile}")
    print(f" Active:   {new_profile}")
    prof = profiles.get(new_profile, {})
    print(f" Description: {prof.get('description', '')}")
    if synced_workers is not None:
        print(f" adaptiveScheduler.maxParallelWorkers: {synced_workers} (synced)")
    elif mismatch and profile_workers is not None:
        print(
            f" adaptiveScheduler.maxParallelWorkers: {current_workers} "
            f"(unchanged — profile wants {profile_workers}, use --sync-workers)"
        )
    print(_SEP)
    print()
    print("| Complexity | Model    |")
    print("|------------|----------|")
    for tier in ("trivial", "standard", "complex", "epic", "executive"):
        model_str = _short_model(prof.get(tier, ""))
        print(f"| {tier:<10} | {model_str:<8} |")
    print()
    print("The model choice takes effect on the next daemon task.")
    if synced_workers is not None:
        print("The worker limit is read when the daemon starts: restart it.")
    print(_SEP)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gatesmith-model-profile",
        description="Switch the active Gatesmith model profile.",
    )
    parser.add_argument("profile", nargs="?", help="Profile name to switch to")
    parser.add_argument("--list", action="store_true", help="List all profiles")
    parser.add_argument(
        "--sync-workers",
        action="store_true",
        help=(
            "Set adaptiveScheduler.maxParallelWorkers to the selected profile's "
            "'workers' field. Without this flag, a mismatch is warned but not fixed."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "Config file to read and edit (default: the file that governs "
            "modelProfile: gatesmith.json, else .claude/gatesmith.json)"
        ),
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config) if args.config else default_config_path()

    if not config_path.exists():
        print(f"Error: config not found: {config_path}", file=sys.stderr)
        return 1

    config = load_config(config_path)
    if args.config is None and not get_profiles(config):
        # /company-init writes a root holding little more than modelProfile;
        # the profiles themselves then come from the template.
        try:
            import governing_config as gc

            profiles, _err = gc.section("modelProfiles", Path.cwd())
        except ImportError:
            profiles = {}
        if profiles:
            config = {**config, "modelProfiles": profiles}

    # Display-only mode
    if args.list or args.profile is None:
        print(f"Config: {config_path.resolve()}")
        display_profiles(config)
        return 0

    profile_name: str = args.profile
    profiles = get_profiles(config)

    if profile_name not in profiles:
        available = ", ".join(profiles.keys())
        print(
            f'Profile "{profile_name}" not found.\n\nAvailable profiles: {available}',
            file=sys.stderr,
        )
        return 1

    old_profile = get_current_profile(config)
    mismatch, profile_workers, current_workers = check_worker_mismatch(
        config, profile_name
    )

    # Warn on mismatch when --sync-workers is not given
    if mismatch and not args.sync_workers:
        print(
            f"Warning: profile '{profile_name}' specifies workers={profile_workers} but "
            f"adaptiveScheduler.maxParallelWorkers={current_workers}. "
            f"Use --sync-workers to align them.",
            file=sys.stderr,
        )

    switch_profile(config, profile_name)

    synced_workers: Optional[int] = None
    if args.sync_workers:
        synced_workers = sync_workers(config, profile_name)

    try:
        new_text = edit_config_text(
            config_path.read_text(encoding="utf-8"), profile_name, synced_workers
        )
    except EditRefused as exc:
        print(f"Error: {config_path}: {exc}. Nothing was written.", file=sys.stderr)
        return 1
    config_path.write_text(new_text, encoding="utf-8")

    print(f"Config: {config_path.resolve()}")
    display_switch_result(
        old_profile,
        profile_name,
        profiles,
        mismatch,
        profile_workers,
        current_workers,
        synced_workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
