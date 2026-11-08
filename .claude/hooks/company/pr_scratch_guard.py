"""Scratch-file guard for daemon pull requests (PR-SCRATCH-GUARD).

Worker PRs kept shipping scratch files after the per-task scratch directory
(#647) and a prompt instruction asking workers to use it: #661 added
``probe_linear_shapes.py`` at the repository root, #677 ``_gg_block.py`` and
``_gg_patch.py``; earlier PRs shipped a probe directory, a ``_dbg_`` test file
and commit-message drafts (#630, #639). A prompt instruction is not a control,
so this module checks the files a daemon PR would ADD before the PR is opened.

Two classes of added file are flagged:

* scratch-shaped paths (``is_scratch_shaped``):

  - at the root: file or directory names starting with ``probe``, or whose
    stem ends in ``_probe``; file names starting with ``_`` (``_probe*``,
    ``_dbg_*``, ``_gg_*``), starting with ``scratch`` or ``pr_body`` /
    ``pr-body``, containing ``commit-msg`` / ``commit_message`` (``-``, ``_``
    and ``.`` ignored, so ``.commitmsg.tmp`` counts), ``nohup.out``, and
    ``.log`` / ``.bak`` / ``.patch`` / ``.diff`` / ``.tmp`` / ``.swp`` files;
    the directories ``tmp/``, ``scratch/`` and ``.scratch/``;
  - anywhere: names starting with ``_dbg_``, ``.orig`` / ``.rej`` files,
    ``.DS_Store``, and directories starting with ``.probe``.

  ``probe_*`` and ``debug_*`` names in subdirectories are NOT flagged: a
  ``tests/probe_x.py`` can be a real test, and the guard cannot tell.
* unbriefed root files: a new file directly at the repository root whose name
  does not appear in the task title or description.

A path the task title or description names (``names_path``: whole token,
case-insensitive; a root file is not named by a longer path ending in its
name, e.g. ``docs/README.md`` does not name root ``README.md``) is never
flagged.

What happens to a flagged file depends on where it is:

* untracked and in the stage list the caller is about to commit: a
  scratch-shaped file is dropped from the stage list and reported in
  ``stripped``; it stays on disk — nothing is deleted. Exceptions that are
  refused instead of dropped: an unbriefed root file (it may be the
  deliverable), and anything under a hooks directory (``hooks``, ``*hooks``,
  ``.husky``), where git hook names like ``commit-msg`` live.
* already committed on the branch, or already staged in the index: refused.
  Removing a committed file would take a new deleting commit, which this
  guard never makes, and the callers commit index-wide, so leaving a staged
  file out of the stage list would not keep it out of the commit.
  (An intent-to-add entry, ``git add -N``, carries no content yet and is
  treated as untracked.)

The asymmetry is deliberate. A refusal loses the attempt's work (the worktree
is deleted after the attempt), so a probe script next to a finished change
should not cost the change; the scratch shapes are narrow and a named path is
never flagged, so dropping one from a commit is a low-cost mistake (the PR
then lacks a file and CI or the judge says so).

Only ADDED files are examined; modifications and deletions are not. The
committed half compares against ``origin/main`` (three-dot); without a merge
base (an orphan branch) it compares trees; without ``origin/main`` it falls
back to ``main``, ``origin/master``, ``master``. When no base resolves or git
cannot answer, that half is skipped with a ``WARNING`` on stderr and in
``unchecked``: a git failure never refuses a PR.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

Runner = Callable[..., subprocess.CompletedProcess]

REFUSAL_PREFIX = "Refusing to create PR: adds scratch or unbriefed root file(s): "
DEFAULT_BASE = "origin/main"
FALLBACK_BASES = ("main", "origin/master", "master")

_ROOT_SCRATCH_SUFFIXES = (".log", ".bak", ".patch", ".diff", ".tmp", ".swp")
_ANY_SCRATCH_SUFFIXES = (".orig", ".rej")
_ROOT_SCRATCH_PREFIXES = ("_", "scratch", "pr_body", "pr-body")
_ROOT_SCRATCH_DIRS = ("tmp", "scratch", ".scratch")


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_root_file(path: str) -> bool:
    """True for a file directly at the repository root (``foo.py``, ``.env``)."""
    p = _norm(path)
    return bool(p) and "/" not in p


def _squash(name: str) -> str:
    return re.sub(r"[-_.]", "", name.lower())


def _probe_named(name: str) -> bool:
    """probe*, _probe*, or a stem ending in _probe (``x_probe.py``)."""
    return (
        name.startswith("probe")
        or name.startswith("_probe")
        or Path(name).stem.endswith("_probe")
    )


def under_hooks_dir(path: str) -> bool:
    """True when a directory component is ``hooks``, ``*hooks`` or ``.husky``."""
    parts = [c.lower() for c in _norm(path).rstrip("/").split("/") if c]
    dirs = parts if _norm(path).endswith("/") else parts[:-1]
    return any(d.endswith("hooks") or d == ".husky" for d in dirs)


def is_scratch_shaped(path: str) -> bool:
    """True when *path* matches one of the scratch shapes in the module doc.

    A path given as a directory entry (``x/``, as ``git status`` prints an
    untracked directory) is judged as a directory.
    """
    raw = _norm(path)
    is_dir_entry = raw.endswith("/")
    parts = [c.lower() for c in raw.rstrip("/").split("/") if c]
    if not parts:
        return False
    dirs = parts if is_dir_entry else parts[:-1]
    name = None if is_dir_entry else parts[-1]
    root_file = name is not None and len(parts) == 1

    # Root level, file or directory.
    if _probe_named(parts[0]):
        return True
    if dirs and dirs[0] in _ROOT_SCRATCH_DIRS:
        return True
    # Root level, file only.
    if root_file:
        if name.startswith(_ROOT_SCRATCH_PREFIXES) or name == "nohup.out":
            return True
        if name.endswith(_ROOT_SCRATCH_SUFFIXES):
            return True
        if "commitmsg" in _squash(name) or "commitmessage" in _squash(name):
            return True
    # Anywhere.
    if any(c.startswith("_dbg_") for c in parts):
        return True
    if name is not None and (
        name.endswith(_ANY_SCRATCH_SUFFIXES) or name == ".ds_store"
    ):
        return True
    return any(d.startswith(".probe") for d in dirs)


def _token_in(token: str, text: str) -> bool:
    pattern = r"(?:(?<![\w./-])|(?<=\./))" + re.escape(token) + r"(?![\w-]|\.\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def names_path(path: str, text: str) -> bool:
    """True when *text* names *path* as a whole token, ignoring case.

    Root ``foo.toml`` is named by "add `foo.toml`", "./foo.toml" or
    "FOO.TOML", not by "barfoo.toml", "foo.toml.bak" or "config/foo.toml" (a
    different file). A path in a subdirectory is named by its full path or by
    its file name.
    """
    p = _norm(path).rstrip("/")
    if not p or not text:
        return False
    if _token_in(p, text):
        return True
    return "/" in p and _token_in(p.rsplit("/", 1)[1], text)


def classify(
    added: Iterable[str], title: str = "", description: str = ""
) -> tuple[list[str], list[str]]:
    """Split *added* into (scratch_shaped, unbriefed_root). Order preserved.

    A path the title or description names is in neither list.
    """
    text = f"{title or ''}\n{description or ''}"
    scratch: list[str] = []
    unbriefed: list[str] = []
    for f in added:
        if names_path(f, text):
            continue
        if is_scratch_shaped(f):
            scratch.append(f)
        elif is_root_file(f):
            unbriefed.append(f)
    return scratch, unbriefed


def refusal_reason(paths: Iterable[str]) -> str:
    """The refusal text; file names first so a 200-char log snippet keeps them."""
    names = ", ".join(sorted(set(paths)))
    return (
        REFUSAL_PREFIX + names + " — remove these (scratch belongs in "
        "$FORGE_SCRATCH_DIR, outside the repository); a new root-level file "
        "must be named in the task title or description"
    )


# ---------------------------------------------------------------------------
# Git queries
# ---------------------------------------------------------------------------


def _git(
    run: Runner | None, cwd: str | Path | None, args: list[str]
) -> subprocess.CompletedProcess | None:
    if run is None:
        run = subprocess.run  # resolved at call time so test patches apply
    cmd = ["git"] + (["-C", str(cwd)] if cwd is not None else []) + args
    try:
        res = run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 — any failure means "git could not answer"
        return None
    if not isinstance(getattr(res, "returncode", None), int):
        return None  # a blanket test mock, not a git answer
    if res.returncode != 0 or not isinstance(res.stdout, str):
        return None
    return res


def _resolves(run: Runner | None, cwd: str | Path | None, ref: str) -> bool:
    return (
        _git(run, cwd, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        is not None
    )


def _added_between(
    run: Runner | None, cwd: str | Path | None, spec: list[str]
) -> list[str] | None:
    res = _git(
        run,
        cwd,
        ["diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", *spec],
    )
    if res is None:
        return None
    return [p for p in res.stdout.split("\0") if p]


def branch_added_files_with_note(
    cwd: str | Path | None,
    base: str = DEFAULT_BASE,
    head: str = "HEAD",
    run: Runner | None = None,
) -> tuple[list[str] | None, str]:
    """Files *head* adds relative to *base*, and a note on how they were found.

    Tries *base* then FALLBACK_BASES. Against each base that resolves: the
    three-dot diff (vs the merge base); without a merge base (an orphan
    branch), a tree-to-tree diff. Returns (None, why) when nothing resolves.
    """
    candidates = [base] + [b for b in FALLBACK_BASES if b != base]
    for ref in candidates:
        if not _resolves(run, cwd, ref):
            continue
        how = "" if ref == base else f"{base} missing, compared with {ref}"
        files = _added_between(run, cwd, [f"{ref}...{head}"])
        if files is not None:
            return files, how
        files = _added_between(run, cwd, [ref, head])
        if files is not None:
            note = f"no merge base with {ref}, compared trees"
            return files, f"{how}; {note}" if how else note
    return None, (f"no base ref resolves ({', '.join(candidates)}) or git unavailable")


def branch_added_files(
    cwd: str | Path | None,
    base: str = DEFAULT_BASE,
    head: str = "HEAD",
    run: Runner | None = None,
) -> list[str] | None:
    """Files *head* adds relative to *base* (with fallbacks); None if unknown."""
    return branch_added_files_with_note(cwd, base, head, run)[0]


def worktree_new_files(
    cwd: str | Path | None, run: Runner | None = None
) -> dict[str, bool] | None:
    """New files in the working tree, one per file, mapped to "already staged".

    Untracked (not ignored) and intent-to-add (``git add -N``, status `` A``)
    files map to False, index-added files to True. None when git cannot answer.
    """
    res = _git(run, cwd, ["status", "--porcelain", "-uall", "--no-renames", "-z"])
    if res is None:
        return None
    out: dict[str, bool] = {}
    for entry in res.stdout.split("\0"):
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy == "??" or xy[1] == "A":
            out[path] = False
        elif xy[0] == "A":
            out[path] = True
    return out


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass
class ScratchGuardResult:
    refuse: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    stage: list[str] | None = None
    unchecked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    only_scratch: bool = False

    @property
    def refused(self) -> bool:
        return bool(self.refuse)

    @property
    def reason(self) -> str | None:
        if not self.refuse:
            return None
        text = refusal_reason(self.refuse)
        if self.only_scratch:
            text += " (every new file in this change was scratch-shaped)"
        return text


def check(
    cwd: str | Path | None,
    title: str = "",
    description: str = "",
    *,
    stage_list: list[str] | None = None,
    base: str = DEFAULT_BASE,
    head: str = "HEAD",
    run: Runner | None = None,
) -> ScratchGuardResult:
    """Examine what a daemon PR would add. See the module doc for the rules.

    *stage_list* is the list of paths the caller is about to ``git add`` (as
    ``git status --porcelain`` printed them, directory entries included); pass
    None when everything the PR ships is already committed. ``result.stage``
    is the stage list with scratch-shaped new files removed (directory entries
    expanded to their files when one of them was removed).
    """
    result = ScratchGuardResult()
    refuse: list[str] = []

    committed, how = branch_added_files_with_note(cwd, base, head, run)
    if committed is None:
        result.unchecked.append(f"committed files NOT checked: {how}")
    else:
        if how:
            result.notes.append(how)
        scratch, unbriefed = classify(committed, title, description)
        refuse.extend(scratch + unbriefed)

    if stage_list is not None:
        new_files = worktree_new_files(cwd, run)
        if new_files is None:
            result.unchecked.append("uncommitted files NOT checked: git status failed")
            result.stage = list(stage_list)
        else:
            # Callers commit index-wide, so a flagged file that is already
            # staged ships whatever the stage list says: refuse it.
            staged = [f for f, is_staged in new_files.items() if is_staged]
            scratch, unbriefed = classify(staged, title, description)
            refuse.extend(scratch + unbriefed)
            new_set = {f for f, is_staged in new_files.items() if not is_staged}
            stage: list[str] = []
            for entry in stage_list:
                if entry.endswith("/"):
                    members = sorted(f for f in new_set if f.startswith(entry))
                else:
                    members = [entry] if entry in new_set else []
                if not members:
                    # A modification or deletion (not examined), a staged new
                    # file (examined above), or a directory git did not list.
                    stage.append(entry)
                    continue
                scratch, unbriefed = classify(members, title, description)
                in_hooks = [f for f in scratch if under_hooks_dir(f)]
                drop = [f for f in scratch if f not in in_hooks]
                refuse.extend(unbriefed + in_hooks)
                if drop:
                    result.stripped.extend(drop)
                    stage.extend(f for f in members if f not in drop)
                else:
                    stage.append(entry)
            result.stage = stage
            if stage_list and not stage and result.stripped:
                # Nothing left to commit: a PR made only of scratch is refused.
                result.only_scratch = True
                refuse.extend(result.stripped)

    # de-duplicate, keep order
    seen: set[str] = set()
    result.refuse = [p for p in refuse if not (p in seen or seen.add(p))]
    for msg in result.unchecked:
        print(f"[scratch-guard] WARNING: {msg}", file=sys.stderr)
    for msg in result.notes:
        print(f"[scratch-guard] {msg}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# One-call entry points for call sites in protected files
# ---------------------------------------------------------------------------


def enforce_on_stage_list(
    cwd: str | Path | None,
    title: str,
    description: str,
    stage_list: list[str],
    *,
    base: str = DEFAULT_BASE,
    run: Runner | None = None,
) -> str | None:
    """Filter *stage_list* IN PLACE and return a refusal reason or None.

    Built for a one-line call in employee_activator._capture_code_changes:
    ``if (err := pr_scratch_guard.enforce_on_stage_list(project_root,
    task.get("title", ""), task.get("description", ""), code_changes)):``.
    On a refusal the list is left unchanged. Removed paths are printed to
    stderr with the ``[scratch-guard]`` tag.
    """
    res = check(cwd, title, description, stage_list=stage_list, base=base, run=run)
    if res.refused:
        print(f"[scratch-guard] REFUSED: {res.reason}", file=sys.stderr)
        return res.reason
    if res.stripped and res.stage is not None:
        stage_list[:] = res.stage
        print(
            "[scratch-guard] left out of the commit (untracked on disk, not "
            "deleted): " + ", ".join(res.stripped),
            file=sys.stderr,
        )
    return None


def _task_from_queue(task_id: str, queue_loader: Callable[[], dict] | None) -> dict:
    """The queue entry for *task_id* in any lane, or {} (never raises)."""
    try:
        if queue_loader is None:
            try:
                from . import work_allocator as wa
            except ImportError:
                import work_allocator as wa  # type: ignore[no-redef]
            queue_loader = wa.load_queue
        queue = queue_loader() or {}
        for lane in queue.values():
            if not isinstance(lane, list):
                continue
            for task in lane:
                if isinstance(task, dict) and task.get("task_id") == task_id:
                    return task
    except Exception as exc:  # noqa: BLE001 — a lookup failure is not a refusal
        print(
            f"[scratch-guard] task lookup for {task_id} failed: {exc}", file=sys.stderr
        )
    return {}


def raise_if_refused(
    cwd: str | Path | None,
    title: str = "",
    description: str = "",
    *,
    task_id: str | None = None,
    queue_loader: Callable[[], dict] | None = None,
    head: str = "HEAD",
    base: str = DEFAULT_BASE,
    run: Runner | None = None,
) -> None:
    """Raise RuntimeError(reason) when committed work adds a flagged file.

    Built for a one-line call in forge_daemon's stranded-work harvester, whose
    work is all committed (nothing can be left out) and whose per-entry
    ``except`` records the error in its audit log:
    ``pr_scratch_guard.raise_if_refused(entry, task_id=task_id)``. With
    *task_id*, a missing title/description is read from the work queue.
    """
    if task_id and not (title and description):
        task = _task_from_queue(task_id, queue_loader)
        title = title or str(task.get("title") or "")
        description = description or str(task.get("description") or "")
    res = check(cwd, title, description, base=base, head=head, run=run)
    if res.refused:
        raise RuntimeError(res.reason)
