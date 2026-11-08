#!/usr/bin/env python3
"""Execution-provider adapters for Gatesmith employee workers.

The daemon owns task orchestration, worktree isolation, timeouts, and
deliverable verification.  Providers only translate a neutral worker request
to a concrete coding-agent CLI and normalize that CLI's output.

Provider selection lives in gatesmith.json:

    "execution": {
      "defaultProvider": "claude-subscription",
      "providers": {
        "claude-subscription": {"type": "claude", "auth": "subscription",
                                "models": {"trivial": "...", "epic": "..."}},
        "codex-subscription":  {"type": "codex", "auth": "subscription",
                                "sandbox": "workspace-write",
                                "reasoningEffort": "low", "models": {...}},
        "claude-ollama":       {"type": "claude", "auth": "ollama",
                                "models": {"standard": "<ollama-model>"}}
      }
    }

Projects without an ``execution`` section keep the historical Claude
behavior (claude + subscription), making the migration backward compatible.

Codex has no equivalent of --allowedTools/--disallowedTools — its sandbox
(workspace-write / read-only) is the control surface, and the system
reinforcement travels inside the prompt via prepare_prompt() instead of a
system-prompt flag.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import spawn_guard as _spawn_guard
except ImportError:  # direct script execution
    import spawn_guard as _spawn_guard

assert_spawn_allowed = _spawn_guard.assert_spawn_allowed

SUPPORTED_PROVIDER_TYPES = {"claude", "codex"}

# Mirrors the historical effort mapping in employee_activator (WS-121).
EFFORT_BY_COMPLEXITY = {
    "trivial": "high",
    "standard": "high",
    "complex": "high",
    "epic": "max",
}


@dataclass(frozen=True)
class ProviderSpec:
    """Resolved provider configuration for one worker invocation."""

    name: str
    provider_type: str
    auth: str
    model: str | None
    sandbox: str = "workspace-write"
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider preflight check."""

    healthy: bool
    message: str


def resolve_provider(
    config: dict[str, Any], complexity: str, fallback_model: str | None = None
) -> ProviderSpec:
    """Resolve the configured provider and complexity-specific model.

    Projects without the new ``execution`` section retain the historical
    Claude behavior, making the configuration migration backward compatible.
    """

    execution = config.get("execution", {}) or {}
    name = execution.get("defaultProvider", "claude-subscription")
    providers = execution.get("providers", {}) or {}
    provider = providers.get(name)

    if provider is None:
        if execution:
            raise ValueError(f"execution provider {name!r} is not configured")
        provider = {"type": "claude", "auth": "subscription"}

    provider_type = str(provider.get("type", "")).lower()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        raise ValueError(
            f"unsupported execution provider type {provider_type!r}; "
            f"expected one of {sorted(SUPPORTED_PROVIDER_TYPES)}"
        )

    models = provider.get("models", {}) or {}
    model = (
        models.get(complexity)
        or models.get("standard")
        or provider.get("model")
        or fallback_model
    )
    return ProviderSpec(
        name=name,
        provider_type=provider_type,
        auth=str(provider.get("auth", "subscription")),
        model=str(model) if model else None,
        sandbox=str(provider.get("sandbox", "workspace-write")),
        reasoning_effort=(
            str(provider["reasoningEffort"])
            if provider.get("reasoningEffort")
            else None
        ),
    )


# A slow `claude auth status`/`codex login status` (keychain contention, cold
# CLI start) should not burn an entire task retry cycle — that costs a fresh
# worktree and an LLM invocation for a problem no amount of task-simplifying
# fixes. Retry the preflight check itself first, cheaply, before failing it.
_PREFLIGHT_RETRY_ATTEMPTS = 2
_PREFLIGHT_RETRY_DELAY_SECONDS = 1.5


def _run_preflight_command(
    command: list[str], *, timeout: int, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a preflight subprocess, retrying on a transient timeout."""

    last_exc: subprocess.TimeoutExpired | None = None
    for attempt in range(_PREFLIGHT_RETRY_ATTEMPTS):
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt + 1 < _PREFLIGHT_RETRY_ATTEMPTS:
                time.sleep(_PREFLIGHT_RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def check_provider_health(spec: ProviderSpec, timeout: int = 15) -> ProviderHealth:
    """Fail fast when a worker CLI or its subscription login is unavailable."""

    if spec.provider_type == "claude" and spec.auth == "ollama":
        if shutil.which("ollama") is None:
            return ProviderHealth(False, "Ollama CLI is not installed or not on PATH")
        if shutil.which("claude") is None:
            return ProviderHealth(False, "Claude CLI is not installed or not on PATH")
        assert_spawn_allowed("agent_providers.check_provider_health", subprocess.run)
        try:
            result = _run_preflight_command(
                ["ollama", "list"], timeout=timeout, env=prepare_environment(spec)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderHealth(False, f"Ollama preflight failed: {exc}")
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".strip()
            return ProviderHealth(False, detail or "Ollama server is not reachable")
        installed = {
            line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()
        }
        requested_names = {spec.model, f"{spec.model}:latest"} if spec.model else set()
        if spec.model and not requested_names.intersection(installed):
            return ProviderHealth(
                False,
                f"Ollama model {spec.model!r} is not installed",
            )
        return ProviderHealth(True, f"{spec.name} is ready")

    executable = "claude" if spec.provider_type == "claude" else "codex"
    if shutil.which(executable) is None:
        return ProviderHealth(
            False, f"{executable} CLI is not installed or not on PATH"
        )

    if spec.auth != "subscription":
        return ProviderHealth(
            False,
            f"provider {spec.name!r} uses unsupported auth mode {spec.auth!r}; "
            "first release supports subscription auth only",
        )

    command = (
        ["claude", "auth", "status"]
        if spec.provider_type == "claude"
        else ["codex", "login", "status"]
    )
    assert_spawn_allowed("agent_providers.check_provider_health", subprocess.run)
    try:
        result = _run_preflight_command(
            command, timeout=timeout, env=prepare_environment(spec)
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderHealth(False, f"{spec.name} preflight failed: {exc}")

    combined = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return ProviderHealth(False, combined or f"{spec.name} is not authenticated")
    if spec.provider_type == "claude":
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            status = {}
        if not status.get("loggedIn", False):
            return ProviderHealth(False, "Claude Code subscription is not logged in")
    return ProviderHealth(True, f"{spec.name} is ready")


# Worker subprocesses (claude/codex/ollama CLI, run from employee_activator.py
# immediately before every spawn) get an environment built ONLY from this
# allowlist, never a stripped copy of the daemon's own ambient environment.
# Workers execute inside isolated per-task git worktrees and may process
# untrusted, prompt-injected repository content (the threat model
# SCOUT-20260713-1 hardened reviewer.md/security-auditor.md against) — a
# daemon-ambient GitHub token, cloud credential, or DB URL has no business
# reaching that sandbox just because it happened to be sitting in the
# daemon's shell environment.
#
# GITHUB_TOKEN / GH_TOKEN are deliberately absent from both this allowlist
# and ALLOWED_ENV_PREFIXES — this was verified, not assumed. `gh` resolves
# auth from ~/.config/gh/hosts.yml (a `gh auth login` config file under
# HOME, which IS allowlisted below) before it ever consults an env var, so
# a worker's own `gh pr create` inside its worktree keeps working without
# the token in its process env. The daemon's post-execution auto-capture
# PR path (employee_activator.py's "Create draft PR" step) calls
# `gh pr create` directly from the daemon process itself, not through this
# function, so it is unaffected either way. Stripping these two removes a
# credential a prompt-injected worker could otherwise exfiltrate or use to
# push/comment as the daemon's own GitHub identity, for no loss of function.
ALLOWED_ENV_NAMES = {
    "PATH",  # resolve uv/ollama/claude/codex binaries
    "HOME",  # ~/.claude, ~/.codex, ~/.cache/uv, ~/.gitconfig config/creds lookup
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",  # CLI's Bash tool spawns a shell; needs the real one
    "TZ",
}
# CLI auth/config vars — the *_API_KEY / *_ACCESS_TOKEN members of these
# prefixes are conditionally stripped below under subscription auth (P71).
ALLOWED_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_")


def prepare_environment(
    spec: ProviderSpec, *, employee_id: str | None = None
) -> dict[str, str]:
    """Build a worker environment from ALLOWED_ENV_NAMES/PREFIXES, not a
    stripped copy of the daemon's own environment.

    P71 lesson (superseded): the old implementation started from the FULL
    environment and stripped a short denylist. That let anything else in the
    daemon's ambient environment — a GitHub token, cloud credentials, a DB
    URL — pass through unscoped into every worker. SCOUT-20260726-3 flips
    this to an allowlist: only the auth/PATH/locale essentials the
    claude/codex/ollama CLIs actually read make it into the child env. The
    API-key strip below remains a deliberate, auth-mode-conditional
    exception: under subscription auth a stray ANTHROPIC_API_KEY /
    OPENAI_API_KEY would silently override the OAuth login and bill the API
    instead.

    ``employee_id``, when provided, marks the environment as a real daemon
    worker session (FORGE_DAEMON=1, FORGE_EMPLOYEE_ID=<id>). lint_on_edit.py's
    humanProtected check is unconditional (blocks daemon and interactive
    sessions alike, closing a Kiro-class prompt-injection gap) — these vars no
    longer gate that enforcement, only attribution/telemetry that wants to
    know whether a Write/Edit came from a daemon worker. Preflight/health-check
    callers that have no employee context omit this and get an unmarked env.
    """

    # Defense in depth: never let these through even though some would match
    # an ALLOWED_ENV_PREFIXES entry — nested-session/package-manager markers,
    # not worker-facing CLI config, so they stay denied regardless of what the
    # allowlist above grows to include.
    deny_prefixes = ["UV_", "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_"]
    if spec.provider_type == "claude":
        if spec.auth == "subscription":
            deny_prefixes.extend(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    else:
        deny_prefixes.extend(["CODEX_THREAD_ID", "CODEX_INTERNAL_"])
        if spec.auth == "subscription":
            deny_prefixes.extend(["OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"])

    env = {
        key: value
        for key, value in os.environ.items()
        if key in ALLOWED_ENV_NAMES or key.startswith(ALLOWED_ENV_PREFIXES)
    }
    for key in list(env):
        if any(key.startswith(prefix) for prefix in deny_prefixes):
            del env[key]

    uv_prefix = str(Path.home() / ".cache" / "uv" / "environments")
    env["PATH"] = (
        os.pathsep.join(
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if part
            and not part.startswith(uv_prefix)
            and "virtualenv" not in part.lower()
        )
        or "/usr/bin:/bin"
    )
    env["TERM"] = "xterm-256color"
    env["LANG"] = "en_US.UTF-8"
    # Worker marker — forge_daemon's CLI refuses stop/start/restart when set
    # (prevents workers from unloading the LaunchAgent). Not stripped, so the
    # marker propagates to child subprocesses.
    env["FORGE_WORKER_CONTEXT"] = "1"
    if employee_id:
        env["FORGE_DAEMON"] = "1"
        env["FORGE_EMPLOYEE_ID"] = employee_id
    return {k: v for k, v in env.items() if v}


def prepare_prompt(
    spec: ProviderSpec, prompt: str, system_reinforcement: str | None
) -> str:
    """Fold the system reinforcement into the prompt for providers that lack
    a system-prompt flag (codex).  Claude gets it via --append-system-prompt
    in build_command, so the prompt passes through unchanged."""

    if spec.provider_type == "codex" and system_reinforcement:
        return f"{system_reinforcement}\n\n{prompt}"
    return prompt


def build_command(
    spec: ProviderSpec,
    *,
    project_root: Path,
    complexity: str,
    allowed_tools: list[str],
    disallowed_tools: list[str],
    system_reinforcement: str,
) -> list[str]:
    """Build a non-interactive command for the selected coding agent.

    The prompt is always delivered via stdin (codex takes ``-``; claude runs
    in full agent mode and reads stdin per WS-099-003)."""

    if spec.provider_type == "codex":
        command = ["codex"]
        if spec.reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{spec.reasoning_effort}"'])
        command.extend(
            [
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                spec.sandbox,
                "--cd",
                str(project_root),
            ]
        )
        if spec.model and spec.model != "default":
            command.extend(["--model", spec.model])
        command.append("-")
        return command

    effort = EFFORT_BY_COMPLEXITY.get(complexity, "high")
    claude_flags = [
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--effort",
        effort,
        "--append-system-prompt",
        system_reinforcement,
    ]
    if allowed_tools:
        claude_flags.extend(["--allowedTools", ",".join(allowed_tools)])
    if disallowed_tools:
        claude_flags.extend(["--disallowedTools", ",".join(disallowed_tools)])

    if spec.auth == "ollama":
        # ``ollama launch claude -- <flags>`` passes everything after ``--``
        # straight to Claude Code: flags only, never the binary name.  The
        # --model before ``--`` belongs to ollama (it serves the local model).
        return [
            "ollama",
            "launch",
            "claude",
            "--yes",
            "--model",
            spec.model or "",
            "--",
            *claude_flags,
        ]

    return [
        "uv",
        "run",
        "claude",
        "--model",
        spec.model or "claude-sonnet-5-5",
        *claude_flags,
    ]


def normalize_output(spec: ProviderSpec, stdout: str) -> str:
    """Convert provider-specific machine output to useful task output."""

    if spec.provider_type != "codex":
        return stdout

    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return "\n\n".join(messages) or stdout
