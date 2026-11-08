#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The one rule for which config file governs a setting.

A project has up to two config files: the root ``gatesmith.json`` the
operator owns, and ``.claude/gatesmith.json``, the template ``install.sh``
writes. A fresh install has only the template, and ``/company-init`` writes a
root holding little more than ``modelProfile``. Readers that opened only the
root saw ``{}`` there and fell back to defaults, so controls failed open and
features did nothing (found 2026-10-30; patched one reader at a time in
#772, #778 and #780 before this module).

The rule, per setting (a dotted path such as ``autonomy.autoMerge``):

1. The root governs when it declares the path.
2. Otherwise the template governs when it declares the path.
3. Otherwise the setting is absent and the caller's default applies.

A file that exists but cannot be read, is not a JSON object, or holds a
non-object where the path needs one is an error, never "absent": callers of
a safety control must refuse on it. An unreadable root is an error even when
the template would declare the path, because the root may be what the
operator meant to say.

CLI (used by command docs such as ``/autonomy``)::

    uv run .claude/hooks/company/governing_config.py get autonomy.autoMerge
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_CONFIG = "gatesmith.json"
TEMPLATE_CONFIG = ".claude/gatesmith.json"

# A reader maps a repo-relative path to its text, None when the file does not
# exist, or raises OSError when it exists but cannot be read.
Reader = Callable[[str], str | None]


@dataclass(frozen=True)
class Governed:
    """One resolved setting.

    ``source`` is the file that governs (``ROOT_CONFIG`` or
    ``TEMPLATE_CONFIG``), or None when neither declares the path. ``error``
    is set when the governing answer cannot be trusted; ``value`` is then
    None and the caller must fail closed.
    """

    value: Any = None
    source: str | None = None
    error: str | None = None

    @property
    def declared(self) -> bool:
        return self.source is not None and self.error is None


def disk_reader(project_root: Path) -> Reader:
    root = Path(project_root)

    def read(rel: str) -> str | None:
        path = root / rel
        # A dangling symlink is present, so it fails closed instead of
        # reading as "no file".
        if not (path.is_symlink() or path.exists()):
            return None
        return path.read_text(encoding="utf-8")

    return read


def git_reader(repo: Path, ref: str = "HEAD") -> Reader:
    """Reads the committed files at ``ref``, never the working tree: a worker
    that rewrites the config cannot make a check read its own change."""

    def read(rel: str) -> str | None:
        try:
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{ref}:{rel}"],
                cwd=repo,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError(f"git unavailable: {exc}") from exc
        if probe.returncode != 0:
            return None
        try:
            shown = subprocess.run(
                ["git", "show", f"{ref}:{rel}"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError(f"git unavailable: {exc}") from exc
        if shown.returncode != 0:
            raise OSError(f"git show {ref}:{rel} failed")
        return shown.stdout

    return read


_ABSENT = object()


def _load(read: Reader, rel: str) -> dict | None | str:
    """The file as a dict, None when absent, or an error string."""
    try:
        text = read(rel)
    except OSError as exc:
        return f"{rel} unreadable: {exc}"
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return f"{rel} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return f"{rel} is not a JSON object"
    return data


def _walk(data: dict, keys: list[str], rel: str) -> Any:
    """The value at ``keys``, ``_ABSENT`` when undeclared, or raises
    ValueError when a non-object sits where the path needs one."""
    node: Any = data
    for i, key in enumerate(keys):
        if not isinstance(node, dict):
            where = ".".join(keys[:i]) or "<root>"
            raise ValueError(f"{rel}: {where} is not an object")
        if key not in node:
            return _ABSENT
        node = node[key]
    return node


def resolve(
    path: str,
    project_root: Path | None = None,
    *,
    read: Reader | None = None,
) -> Governed:
    """Which file governs ``path`` and what it says (see the module doc)."""
    if read is None:
        read = disk_reader(Path(project_root) if project_root else Path.cwd())
    keys = [k for k in path.split(".") if k]
    if not keys:
        raise ValueError("empty config path")
    for rel in (ROOT_CONFIG, TEMPLATE_CONFIG):
        data = _load(read, rel)
        if isinstance(data, str):
            return Governed(error=data)
        if data is None:
            continue
        try:
            value = _walk(data, keys, rel)
        except ValueError as exc:
            return Governed(error=str(exc))
        if value is not _ABSENT:
            return Governed(value=value, source=rel)
    return Governed()


def section(
    path: str,
    project_root: Path | None = None,
    *,
    read: Reader | None = None,
) -> tuple[dict, str | None]:
    """An object-valued setting as ``(dict, error)``: ``({}, None)`` when
    absent, ``({}, reason)`` when untrusted or not an object."""
    got = resolve(path, project_root, read=read)
    if got.error:
        return {}, got.error
    if got.value is None and got.source is None:
        return {}, None
    if not isinstance(got.value, dict):
        return {}, f"{got.source}: {path} is not an object"
    return got.value, None


def view(
    paths: list[str],
    project_root: Path | None = None,
    *,
    read: Reader | None = None,
) -> dict | None:
    """A config-shaped dict holding each of ``paths`` from its governing
    file, for code that reads a whole config dict. Undeclared paths are left
    out; None when any of them cannot be trusted (callers fail closed)."""
    out: dict = {}
    for path in paths:
        got = resolve(path, project_root, read=read)
        if got.error:
            return None
        if got.source is None:
            continue
        *parents, leaf = [k for k in path.split(".") if k]
        node = out
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = got.value
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "get":
        print("usage: governing_config.py get <dotted.path>", file=sys.stderr)
        return 2
    got = resolve(argv[2])
    print(
        json.dumps(
            {
                "path": argv[2],
                "source": got.source,
                "error": got.error,
                "value": got.value,
            },
            indent=2,
        )
    )
    return 1 if got.error else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
