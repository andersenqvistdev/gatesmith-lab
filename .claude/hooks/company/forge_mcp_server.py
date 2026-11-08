#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
Gatesmith MCP Task Queue Server — Phase 1 (read-only)

Standalone MCP server exposing read-only tools that wrap work_allocator.py's
existing queue API, plus a lightweight daemon status check. See
docs/mcp-taskqueue-assessment.md for the design rationale.

Deliberate architectural decision: the `mcp` PyPI package is NOT a dependency
of this repo (confirmed absent from pyproject.toml). Rather than add an
uninstalled/unstable SDK dependency ahead of the 2026-07-28 MCP spec
finalization, this module hand-rolls the wire protocol directly:

    - JSON-RPC 2.0 messages
    - Newline-delimited (one JSON object per line)
    - Over stdin/stdout

This keeps the server dependency-free and fully testable in CI without a
network-fetched SDK. See the assessment doc's "Effort Estimate > Phase 1"
and "Appendix: Security Implementation Requirements" sections for the exact
tool surface and the task_id validation requirement this module implements.

Security:
    - `task_id` parameters are validated against TASK_ID_PATTERN before ever
      being passed to work_allocator.get_task() or near a file path. This is
      defense-in-depth against path traversal, per the assessment doc.
    - Read-only: no queue-mutating tool is exposed in Phase 1.

Usage:
    # Run as an MCP server (stdio transport), typically spawned by Claude
    # Code via the `mcpServers` config in .claude/settings.json:
    uv run forge_mcp_server.py

Registered in `.claude/settings.json` `mcpServers` (see docs/mcp-server-setup.md
for the exact config block — that file is humanProtected and is NOT edited by
this task).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Resolve sibling-module imports the same way other hooks in this directory do.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from . import work_allocator as wa
except ImportError:
    import work_allocator as wa  # type: ignore[no-redef]

try:
    from . import company_paths
except ImportError:
    import company_paths  # type: ignore[no-redef]

# Logging MUST go to stderr, never stdout — stdout is the JSON-RPC channel.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("forge_mcp_server")

# -----------------------------------------------------------------------------
# Protocol constants
# -----------------------------------------------------------------------------

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "gatesmith-queue", "version": "1.0.0"}

# Protocol versions this server actually implements. A client's requested
# version is only ever echoed back if it's in this collection — otherwise
# the server responds with the newest version it supports (date-formatted
# strings sort correctly with max()) and lets the client decide whether to
# continue, per the MCP handshake contract. Add new versions here as this
# server gains support for them.
SUPPORTED_PROTOCOL_VERSIONS = (DEFAULT_PROTOCOL_VERSION,)

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Security: task_id must match this exact shape before it goes anywhere near
# work_allocator.get_task() or a file path. Defense-in-depth against path
# traversal (see docs/mcp-taskqueue-assessment.md, Appendix: Security
# Implementation Requirements).
TASK_ID_PATTERN = re.compile(r"^task-\d{14}-[0-9a-f]{6}(-[0-9a-f]+)?$")

# The real bucket names used by work_allocator's queue file (see
# work_allocator.get_task()'s all_statuses list — the authoritative set of
# queue buckets, which includes pr_open in addition to the 6 statuses named
# in list_tasks()'s docstring).
VALID_STATUSES = [
    "proposed",
    "pending",
    "in_progress",
    "blocked",
    "review",
    "pr_open",
    "completed",
]

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "forge_list_tasks",
        "description": (
            "List tasks in the Gatesmith work queue, optionally filtered by "
            "status and/or project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": VALID_STATUSES,
                    "description": "Filter tasks by status.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Filter tasks by project id.",
                },
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, list tasks across all projects.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "forge_get_task",
        "description": "Get a single task by its task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": ("The task id, e.g. task-20260722181555-7dbc86."),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "forge_daemon_status",
        "description": (
            "Report Gatesmith daemon heartbeat age, PID liveness, and active worker count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# -----------------------------------------------------------------------------
# Tool implementations (independently testable — not just reachable via the
# stdio loop)
# -----------------------------------------------------------------------------


def tool_forge_list_tasks(arguments: dict) -> dict:
    """Wraps work_allocator.list_tasks() with status validation."""
    arguments = arguments or {}
    status = arguments.get("status")
    project_id = arguments.get("project_id")
    all_projects = bool(arguments.get("all_projects", False))

    if status is not None:
        if not isinstance(status, str) or status not in VALID_STATUSES:
            return {
                "success": False,
                "error": (
                    f"Invalid status {status!r}. Must be one of: "
                    f"{', '.join(VALID_STATUSES)}"
                ),
            }

    return wa.list_tasks(
        status=status, project_id=project_id, all_projects=all_projects
    )


def tool_forge_get_task(arguments: dict) -> dict:
    """Wraps work_allocator.get_task() with task_id validation.

    The task_id is validated against TASK_ID_PATTERN BEFORE it is passed
    anywhere near work_allocator.get_task() — path traversal defense in
    depth (the queue is file-based).
    """
    arguments = arguments or {}
    task_id = arguments.get("task_id")

    if not isinstance(task_id, str) or not TASK_ID_PATTERN.match(task_id):
        return {
            "success": False,
            "error": (
                f"Invalid task_id {task_id!r}. Must match pattern "
                f"{TASK_ID_PATTERN.pattern!r}"
            ),
        }

    return wa.get_task(task_id)


def _load_json_safe(path: Path) -> dict:
    """Load a JSON file, returning {} on any error. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def tool_forge_daemon_status(
    arguments: dict | None = None, company_dir: Path | None = None
) -> dict:
    """Reads daemon.heartbeat and daemon.pid directly (no forge_daemon import).

    company_dir defaults to company_paths.COMPANY_ROOT ONLY when the
    parameter is None, so tests can override it wholesale via a single
    tmp_path fixture (see CLAUDE.md's path-isolation rule for test
    hermeticity — never let a path field default to something cwd-relative
    that tests have to override piecemeal).
    """
    if company_dir is None:
        company_dir = company_paths.COMPANY_ROOT
    else:
        company_dir = Path(company_dir)

    heartbeat_path = company_dir / "runtime" / "daemon.heartbeat"
    pid_path = company_dir / "runtime" / "daemon.pid"

    heartbeat = _load_json_safe(heartbeat_path)
    pid_data = _load_json_safe(pid_path)

    now = datetime.now(timezone.utc)

    heartbeat_age_seconds: float | None = None
    last_heartbeat = heartbeat.get("last_heartbeat")
    if last_heartbeat:
        try:
            hb_dt = datetime.fromisoformat(str(last_heartbeat).replace("Z", "+00:00"))
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            heartbeat_age_seconds = (now - hb_dt).total_seconds()
        except (ValueError, TypeError):
            heartbeat_age_seconds = None

    pid = pid_data.get("pid")
    pid_alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            pid_alive = True
        except ProcessLookupError:
            pid_alive = False
        except PermissionError:
            pid_alive = True  # process exists, just owned by someone else
        except OSError:
            pid_alive = False

    return {
        "success": True,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "pid": pid,
        "pid_alive": pid_alive,
        "active_workers": heartbeat.get("active_workers", 0),
        "generated_at": now.isoformat(),
    }


_TOOL_DISPATCH: dict[str, Callable[..., dict]] = {
    "forge_list_tasks": tool_forge_list_tasks,
    "forge_get_task": tool_forge_get_task,
    "forge_daemon_status": tool_forge_daemon_status,
}


def dispatch_tool(name: str, arguments: dict, company_dir: Path | None = None) -> dict:
    """Route a tool call by name to its implementation.

    Unknown tool names return an error result rather than raising.
    """
    handler = _TOOL_DISPATCH.get(name)
    if handler is None:
        return {
            "success": False,
            "error": f"Unknown tool: {name!r}",
        }

    if name == "forge_daemon_status":
        return handler(arguments, company_dir=company_dir)
    return handler(arguments)


# -----------------------------------------------------------------------------
# JSON-RPC 2.0 framing
# -----------------------------------------------------------------------------


def _make_result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error(request_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_initialize(request: dict) -> dict:
    params = request.get("params") or {}
    requested_version = params.get("protocolVersion")
    if not requested_version:
        protocol_version = DEFAULT_PROTOCOL_VERSION
    elif requested_version in SUPPORTED_PROTOCOL_VERSIONS:
        protocol_version = requested_version
    else:
        protocol_version = max(SUPPORTED_PROTOCOL_VERSIONS)
    return _make_result(
        request.get("id"),
        {
            "protocolVersion": protocol_version,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        },
    )


def _handle_tools_list(request: dict) -> dict:
    return _make_result(request.get("id"), {"tools": TOOL_DEFINITIONS})


def _handle_tools_call(request: dict) -> dict:
    request_id = request.get("id")
    params = request.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if not isinstance(name, str):
        return _make_error(request_id, INVALID_PARAMS, "Missing tool name")

    try:
        result = dispatch_tool(name, arguments)
    except Exception:  # noqa: BLE001 - tool failures become isError results
        # Full exception detail goes to stderr only; the client only ever
        # sees a generic message (avoid leaking paths/secrets via str(exc)).
        logger.exception("Tool %s raised an exception", name)
        return _make_result(
            request_id,
            {
                "content": [
                    {"type": "text", "text": f"Tool {name} failed unexpectedly"}
                ],
                "isError": True,
            },
        )

    is_error = not bool(result.get("success", False))
    return _make_result(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": is_error,
        },
    )


# Methods that produce a response (id is present on the request).
_METHOD_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}

# Notifications: processed but never answered (no "id" on the request, and
# we must not write a response even if one is present).
_NOTIFICATION_METHODS = {"notifications/initialized"}


def handle_request(request: dict) -> dict | None:
    """Handle a single decoded JSON-RPC request/notification.

    Returns a response dict to write, or None if no response should be sent
    (notifications, or malformed requests with no attributable id).
    """
    method = request.get("method")
    has_id = "id" in request

    if method in _NOTIFICATION_METHODS:
        # Notification: process (no-op) and never respond.
        return None

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        if not has_id:
            # Can't respond to a notification-shaped unknown method either.
            return None
        return _make_error(
            request.get("id"), METHOD_NOT_FOUND, f"Method not found: {method}"
        )

    response = handler(request)
    if not has_id:
        # Client sent this as a notification; MCP/JSON-RPC says don't reply.
        return None
    return response


def serve_stdio(
    in_stream=None, out_stream=None
) -> None:  # pragma: no cover - exercised via handle_request in tests
    """Main stdio loop: read newline-delimited JSON-RPC requests, write
    newline-delimited JSON-RPC responses, flush after every write.
    """
    in_stream = in_stream or sys.stdin
    out_stream = out_stream or sys.stdout

    for line in in_stream:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # Can't attribute this to a request id — skip silently per spec
            # note (no id known, so no way to address a parse-error response).
            logger.warning("Skipping malformed JSON line on stdin")
            continue

        if not isinstance(request, dict):
            logger.warning("Skipping non-object JSON-RPC message")
            continue

        try:
            response = handle_request(request)
        except Exception as exc:  # noqa: BLE001 - never crash the stdio loop
            logger.exception("Unhandled error processing request")
            if "id" in request:
                response = _make_error(
                    request.get("id"), INTERNAL_ERROR, f"Internal error: {exc}"
                )
            else:
                response = None

        if response is not None:
            out_stream.write(json.dumps(response) + "\n")
            out_stream.flush()


if __name__ == "__main__":
    logger.info("Gatesmith MCP task queue server starting (stdio transport)")
    serve_stdio()
