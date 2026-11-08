"""The pending-verdict hold: a daemon PR is held until its CURRENT head is judged.

A deliverable verdict belongs to one commit. Before this module every daemon
PR opened unlabeled and stayed mergeable by ci.yml's Auto-Merge job — which
reads only hold labels — through three windows:

* judge vs CI: a docs-only PR finishes its required checks in ~61 s, the
  judge takes 13–43 s (timeout 240 s); nothing held the PR in between;
* head moved after a positive verdict: a CI-healer push, a conflict rebase
  (``gh pr update-branch --rebase``) or any later push left native auto-merge
  armed, and ci.yml re-armed on the new, unjudged head (#581: judged
  6ac686b, merged 6744b84);
* never judged: the stranded-work harvester's PRs (#498 merged via ci.yml
  80 s after creation, no verdict).

The rule, enforced at every lever:

1. every daemon-opened PR opens WITH ``verdict-pending`` (``create_args``);
2. every push that moves a daemon PR's head re-applies it and disarms native
   auto-merge (``hold_for_new_head``; ci.yml's verdict-hold job does the same
   for pushes the daemon did not make);
3. it is removed only when a POSITIVE verdict is recorded for the PR's
   current head (``release_if_positive_for_head``).

``needs-manual-review`` and ``awaiting-operator-review`` keep their meaning;
``verdict-pending`` means "not judged yet", not "rejected".

All gh calls are best-effort except where noted; ``run`` is injectable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

PENDING_LABEL = "verdict-pending"
PENDING_COLOR = "c5def5"
PENDING_DESCRIPTION = (
    "Daemon PR held until the deliverable judge passes its current head commit"
)

Runner = Callable[..., subprocess.CompletedProcess]


def _gh(run: Runner | None, args: list[str], cwd: str | None = None, timeout: int = 30):
    # Resolved at CALL time: a default bound at import would bypass every
    # test's patch of subprocess.run and reach the real gh.
    if run is None:
        run = subprocess.run
    try:
        return run(
            ["gh", *args], capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(["gh", *args], 1, "", str(exc))


def hold_enabled(config_path: Path | None = None) -> bool:
    """False only when ``autonomy.deliverableGate.enabled`` is explicitly false.

    With the gate off nothing ever judges a PR, so a pending label could never
    be released; the hold is skipped entirely, matching the daemon merge gate
    (which then accepts a missing verdict). Unreadable config => enabled.
    """
    candidates = (
        [config_path]
        if config_path
        else [
            Path.cwd() / "gatesmith.json",
            Path.cwd() / ".claude/gatesmith.json",
        ]
    )
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        autonomy = data.get("autonomy") if isinstance(data, dict) else None
        # A malformed autonomy (null, a list) used to raise here; the hold
        # stays on, as for an unreadable file (challenger on #778).
        gate = autonomy.get("deliverableGate", {}) if isinstance(autonomy, dict) else {}
        if isinstance(gate, dict) and gate.get("enabled") is False:
            return False
        return True
    return True


def ensure_pending_label(run: Runner | None = None, cwd: str | None = None) -> bool:
    """Create (or refresh) the label; ``gh pr create --label`` fails without it."""
    res = _gh(
        run,
        [
            "label",
            "create",
            PENDING_LABEL,
            "--color",
            PENDING_COLOR,
            "--description",
            PENDING_DESCRIPTION,
            "--force",
        ],
        cwd,
    )
    return res.returncode == 0


def create_args(run: Runner | None = None, cwd: str | None = None) -> list[str]:
    """Extra ``gh pr create`` arguments so the PR opens already held.

    Atomic with the create: the ``pull_request: opened`` CI run, and any
    merge lever that looks before the judge finishes, already sees the hold.
    Returns [] when the deliverable gate is disabled.
    """
    if not hold_enabled():
        return []
    ensure_pending_label(run, cwd)
    return ["--label", PENDING_LABEL]


def hold_for_new_head(
    pr: str | int, run: Runner | None = None, cwd: str | None = None
) -> bool:
    """The PR's head just moved (or it just opened): hold it for a new verdict.

    Disarms native auto-merge FIRST — GitHub's auto-merge ignores labels once
    armed, and an arm from the previous head would merge the new one — then
    (re)applies the pending label. Returns True when the label was applied.
    """
    if not hold_enabled():
        return False
    _gh(run, ["pr", "merge", str(pr), "--disable-auto"], cwd)
    ensure_pending_label(run, cwd)
    res = _gh(run, ["pr", "edit", str(pr), "--add-label", PENDING_LABEL], cwd)
    return res.returncode == 0


def current_head(
    pr: str | int, run: Runner | None = None, cwd: str | None = None
) -> str | None:
    res = _gh(
        run, ["pr", "view", str(pr), "--json", "headRefOid", "-q", ".headRefOid"], cwd
    )
    sha = (res.stdout or "").strip() if res.returncode == 0 else ""
    return sha or None


def is_positive(rec: dict | None) -> bool:
    """A verdict record (cache entry, gate-log row, or verdict dict) that passes."""
    return bool(rec) and rec.get("addresses_task") is True and not rec.get("blocked")


def release_if_positive_for_head(
    pr: str | int,
    verdict_sha: str | None,
    positive: bool,
    run: Runner | None = None,
    cwd: str | None = None,
) -> bool:
    """Remove the pending label iff the verdict is positive for the CURRENT head.

    ``verdict_sha`` is the commit the verdict judged. The head is re-read here:
    a push that landed after judging leaves the label on (and the new head
    gets its own verdict). Returns True when the label was removed.
    """
    if not positive or not verdict_sha:
        return False
    if current_head(pr, run, cwd) != verdict_sha:
        return False
    res = _gh(run, ["pr", "edit", str(pr), "--remove-label", PENDING_LABEL], cwd)
    return res.returncode == 0
