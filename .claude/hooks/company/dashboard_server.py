#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["watchdog>=3.0.0"]
# ///
"""
Dashboard Server — lightweight HTTP server for the Gatesmith dashboard.

Task 25.1: Create dashboard server with data endpoints.

Provides REST endpoints for dashboard data and serves static files.
Uses http.server for minimal dependencies.

Endpoints:
    GET /api/health      - Health data (aggregate_health)
    GET /api/progress    - Task progress (aggregate_progress)
    GET /api/workforce   - Employee data (aggregate_workforce)
    GET /api/risks       - Risk assessment (aggregate_risks)
    GET /api/efficiency  - Efficiency metrics (aggregate_efficiency)
    GET /api/org         - Raw org.json
    GET /api/queue       - Raw work_queue.json
    GET /api/full        - Complete dashboard data (get_dashboard_data)
    GET /api/unified     - Multi-project unified view
    GET /api/compare     - Project comparison
    GET /api/subsystem-health - Per-subsystem health status
    GET /api/stream      - Server-Sent Events for real-time updates
    GET /*               - Static files from dashboard/ subdirectory

Usage:
    # Start server on default port 8080
    python dashboard_server.py

    # Start on custom port
    python dashboard_server.py --port 3000

    # Specify data directory
    python dashboard_server.py --data-dir /path/to/.company

    # Verbose mode
    python dashboard_server.py --verbose

Exit codes:
    0 = Success (normal shutdown)
    1 = Error
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import mimetypes
import os
import signal
import sys
import urllib.parse
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

# Try to import watchdog for file watching
WATCHDOG_AVAILABLE = False
try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    # Watchdog not available - SSE will work without file watching
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DEFAULT_PORT = 8080
DEFAULT_HOST = "localhost"
SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_DATA_DIR = SCRIPT_DIR.parent.parent.parent / ".company"
STATIC_DIR = SCRIPT_DIR / "dashboard"


def get_project_port(project_root: Path | None = None) -> int:
    """Get a deterministic dashboard port for this project.

    Derives port from project path hash (range 8100-8999) to avoid
    collisions when running multiple companies on the same machine.
    Stores the port in .company/dashboard.port for consistency.

    Falls back to DEFAULT_PORT (8080) if no .company/ directory exists.

    Args:
        project_root: Project root path. Defaults to 4 levels up from this file.

    Returns:
        Port number (8100-8999 for company mode, 8080 otherwise).
    """
    if project_root is None:
        project_root = SCRIPT_DIR.parent.parent.parent

    company_dir = project_root / ".company"
    port_file = company_dir / "dashboard.port"

    # If port already assigned, reuse it
    if port_file.exists():
        try:
            stored_port = int(port_file.read_text().strip())
            if 1024 <= stored_port <= 65535:
                return stored_port
        except (ValueError, OSError):
            pass

    # No company directory = standalone mode, use default
    if not company_dir.exists():
        return DEFAULT_PORT

    # Derive port deterministically from project path
    import hashlib

    path_hash = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()
    port = 8100 + (int(path_hash[:8], 16) % 900)

    # Store for consistency across restarts
    try:
        port_file.write_text(str(port))
    except OSError:
        pass

    return port


# CORS headers - restricted to localhost by default, configurable via env var
# Set FORGE_DASHBOARD_CORS_ORIGIN for production (e.g., "https://gatesmith.dev")
_cors_origin = os.environ.get("FORGE_DASHBOARD_CORS_ORIGIN", "http://localhost:8080")
CORS_HEADERS = {
    "Access-Control-Allow-Origin": _cors_origin,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization",
    "Access-Control-Max-Age": "86400",
}

# -----------------------------------------------------------------------------
# Authentication & Rate Limiting
# -----------------------------------------------------------------------------

# Bearer token auth: only enforced when env var is set
_DASHBOARD_TOKEN: str | None = os.environ.get("FORGE_DASHBOARD_TOKEN") or None

# Rate limiting: max 60 requests per 60 seconds per IP
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW = 60.0  # seconds
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_lock = None  # initialized lazily after threading import


def _check_rate_limit(client_ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    global _rate_limit_lock
    import threading as _threading
    import time as _time

    if _rate_limit_lock is None:
        _rate_limit_lock = _threading.Lock()

    now = _time.time()
    cutoff = now - _RATE_LIMIT_WINDOW

    with _rate_limit_lock:
        timestamps = _rate_limit_store.get(client_ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            _rate_limit_store[client_ip] = timestamps
            return False
        timestamps.append(now)
        _rate_limit_store[client_ip] = timestamps

        # Periodic cleanup: if store has > 1000 IPs, drop stale ones
        if len(_rate_limit_store) > 1000:
            stale_ips = [
                ip for ip, ts in _rate_limit_store.items() if not ts or ts[-1] < cutoff
            ]
            for ip in stale_ips:
                del _rate_limit_store[ip]

    return True


# Gatesmith version for X-Forge-Version header
_FORGE_VERSION: str = "unknown"
_version_file = SCRIPT_DIR.parent.parent.parent / ".gatesmith-version"
if _version_file.exists():
    try:
        _FORGE_VERSION = _version_file.read_text().strip()
    except OSError:
        pass

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data Access Functions
# -----------------------------------------------------------------------------


def _load_json_file(file_path: Path) -> dict[str, Any]:
    """Load a JSON file, returning empty dict on error."""
    try:
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load {file_path}: {e}")
    return {}


def _safe_import_aggregator():
    """Safely import dashboard_aggregator functions."""
    try:
        # Add script directory to path for imports
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))

        from dashboard_aggregator import (
            aggregate_efficiency,
            aggregate_health,
            aggregate_progress,
            aggregate_risks,
            aggregate_workforce,
            get_dashboard_data,
            get_project_comparison,
            get_unified_dashboard,
        )

        return {
            "aggregate_health": aggregate_health,
            "aggregate_progress": aggregate_progress,
            "aggregate_workforce": aggregate_workforce,
            "aggregate_risks": aggregate_risks,
            "aggregate_efficiency": aggregate_efficiency,
            "get_dashboard_data": get_dashboard_data,
            "get_unified_dashboard": get_unified_dashboard,
            "get_project_comparison": get_project_comparison,
        }
    except ImportError as e:
        logger.warning(f"Failed to import dashboard_aggregator: {e}")
        return None


# Global aggregator functions (lazy-loaded)
_aggregator_funcs: dict[str, Any] | None = None


def _get_aggregator():
    """Get aggregator functions, loading if necessary."""
    global _aggregator_funcs
    if _aggregator_funcs is None:
        _aggregator_funcs = _safe_import_aggregator()
    return _aggregator_funcs


# -----------------------------------------------------------------------------
# SSE (Server-Sent Events) Support
# -----------------------------------------------------------------------------

# Import threading modules at the top level for SSE support
import queue
import threading
import time


class SSEManager:
    """Thread-safe manager for Server-Sent Events connections."""

    # Heartbeat interval in seconds
    HEARTBEAT_INTERVAL = 15.0
    # Debounce interval in seconds (100ms)
    DEBOUNCE_INTERVAL = 0.1

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()
        self._running = False
        self._heartbeat_thread: threading.Thread | None = None
        self._observer = None
        self._last_event_time: float = 0
        self._pending_changes: set[str] = set()
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()

    def start(self):
        """Start the SSE manager (heartbeat thread and file watcher)."""
        if self._running:
            return

        self._running = True

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="SSE-Heartbeat",
        )
        self._heartbeat_thread.start()

        # Start file watcher if watchdog is available
        if WATCHDOG_AVAILABLE:
            self._start_file_watcher()
        else:
            logger.warning(
                "watchdog not available - SSE will work without file change notifications"
            )

        logger.info("SSE manager started")

    def stop(self):
        """Stop the SSE manager."""
        self._running = False

        # Stop file watcher
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

        # Cancel any pending debounce timer
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

        # Clear all clients
        with self._clients_lock:
            self._clients.clear()

        logger.info("SSE manager stopped")

    def add_client(self) -> queue.Queue:
        """Add a new SSE client and return its message queue."""
        client_queue: queue.Queue = queue.Queue()
        with self._clients_lock:
            self._clients.append(client_queue)
        logger.debug(f"SSE client connected. Total clients: {len(self._clients)}")
        return client_queue

    def remove_client(self, client_queue: queue.Queue):
        """Remove an SSE client."""
        with self._clients_lock:
            if client_queue in self._clients:
                self._clients.remove(client_queue)
        logger.debug(f"SSE client disconnected. Total clients: {len(self._clients)}")

    def broadcast(self, event_type: str, data: dict):
        """Broadcast an event to all connected clients."""
        message = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        event = self._format_sse(event_type, message)

        with self._clients_lock:
            dead_clients = []
            for client_queue in self._clients:
                try:
                    client_queue.put_nowait(event)
                except queue.Full:
                    dead_clients.append(client_queue)

            # Remove dead clients
            for client in dead_clients:
                self._clients.remove(client)

    def get_full_state(self) -> dict:
        """Get the full dashboard state for initial SSE connection."""
        agg = _get_aggregator()
        if agg and "get_dashboard_data" in agg:
            return agg["get_dashboard_data"](None)
        return {"success": False, "error": "Aggregator not available"}

    def _format_sse(self, event_type: str, data: dict) -> str:
        """Format data as an SSE message."""
        json_data = json.dumps(data, default=str)
        return f"event: {event_type}\ndata: {json_data}\n\n"

    def _heartbeat_loop(self):
        """Send periodic heartbeat events to keep connections alive."""
        while self._running:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if self._running:
                self.broadcast("heartbeat", {"status": "alive"})

    def _start_file_watcher(self):
        """Start watching files for changes."""
        if not WATCHDOG_AVAILABLE or Observer is None:
            return

        # Determine paths to watch
        watch_paths = []

        # Watch .company/*.json files
        if self.data_dir.exists():
            watch_paths.append(self.data_dir)

        # Watch .company/escalations/ directory
        escalations_dir = self.data_dir / "escalations"
        if escalations_dir.exists():
            watch_paths.append(escalations_dir)

        # Watch .planning/ directory
        planning_dir = self.data_dir.parent / ".planning"
        if planning_dir.exists():
            watch_paths.append(planning_dir)

        # Watch .company/logs/ directory for daemon.log changes
        logs_dir = self.data_dir / "logs"
        if logs_dir.exists():
            watch_paths.append(logs_dir)

        if not watch_paths:
            logger.warning("No watch paths found for file watcher")
            return

        # Create observer and handler
        self._observer = Observer()
        handler = _FileChangeHandler(self)

        for watch_path in watch_paths:
            try:
                self._observer.schedule(handler, str(watch_path), recursive=False)
                logger.debug(f"Watching: {watch_path}")
            except Exception as e:
                logger.warning(f"Failed to watch {watch_path}: {e}")

        self._observer.start()
        logger.info(f"File watcher started on {len(watch_paths)} paths")

    def on_file_change(self, file_path: str):
        """Handle a file change event with debouncing."""
        # Only care about specific files
        path = Path(file_path)
        if not self._is_watched_file(path):
            return

        with self._debounce_lock:
            self._pending_changes.add(file_path)

            # Cancel existing timer if any
            if self._debounce_timer:
                self._debounce_timer.cancel()

            # Start new debounce timer
            self._debounce_timer = threading.Timer(
                self.DEBOUNCE_INTERVAL,
                self._flush_changes,
            )
            self._debounce_timer.start()

    def _is_watched_file(self, path: Path) -> bool:
        """Check if a file should trigger an update."""
        # Watch .company/*.json
        if path.parent == self.data_dir and path.suffix == ".json":
            return True

        # Watch .company/escalations/*
        escalations_dir = self.data_dir / "escalations"
        if path.parent == escalations_dir:
            return True

        # Watch .planning/ROADMAP.md and STATE.md
        planning_dir = self.data_dir.parent / ".planning"
        if path.parent == planning_dir and path.name in ("ROADMAP.md", "STATE.md"):
            return True

        # Watch .company/logs/daemon.log
        logs_dir = self.data_dir / "logs"
        if path.parent == logs_dir and path.name == "daemon.log":
            return True

        return False

    def _flush_changes(self):
        """Flush pending changes and broadcast update."""
        with self._debounce_lock:
            changed_files = list(self._pending_changes)
            self._pending_changes.clear()
            self._debounce_timer = None

        if changed_files:
            logger.debug(f"Broadcasting update for: {changed_files}")
            # Get fresh data and broadcast
            update_data = self.get_full_state()
            update_data["changed_files"] = changed_files
            self.broadcast("update", update_data)


class _FileChangeHandler(FileSystemEventHandler):
    """Watchdog event handler for file changes."""

    def __init__(self, sse_manager: SSEManager):
        super().__init__()
        self.sse_manager = sse_manager

    def on_modified(self, event):
        """Handle file modification."""
        if not event.is_directory:
            self.sse_manager.on_file_change(event.src_path)

    def on_created(self, event):
        """Handle file creation."""
        if not event.is_directory:
            self.sse_manager.on_file_change(event.src_path)

    def on_deleted(self, event):
        """Handle file deletion."""
        if not event.is_directory:
            self.sse_manager.on_file_change(event.src_path)


# Global SSE manager instance
_sse_manager: SSEManager | None = None


def _get_sse_manager(data_dir: Path) -> SSEManager:
    """Get or create the SSE manager."""
    global _sse_manager
    if _sse_manager is None:
        _sse_manager = SSEManager(data_dir)
        _sse_manager.start()
    return _sse_manager


def _stop_sse_manager():
    """Stop the SSE manager if running."""
    global _sse_manager
    if _sse_manager is not None:
        _sse_manager.stop()
        _sse_manager = None


# -----------------------------------------------------------------------------
# Request Handler
# -----------------------------------------------------------------------------


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for dashboard API and static files."""

    def __init__(
        self,
        *args,
        data_dir: Path = DEFAULT_DATA_DIR,
        static_dir: Path = STATIC_DIR,
        verbose: bool = False,
        **kwargs,
    ):
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.verbose = verbose
        # Call parent init last (it immediately handles the request)
        super().__init__(*args, directory=str(static_dir), **kwargs)

    def log_message(self, format: str, *args):
        """Override logging to use our logger."""
        if self.verbose:
            logger.info("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args):
        """Override error logging to use our logger."""
        logger.error("%s - %s", self.address_string(), format % args)

    def end_headers(self):
        """Add CORS headers to all responses."""
        for header, value in CORS_HEADERS.items():
            self.send_header(header, value)
        super().end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _check_rate_limit(self) -> bool:
        """Check per-IP rate limit. Returns True if allowed."""
        client_ip = self.client_address[0] if self.client_address else "unknown"
        if not _check_rate_limit(client_ip):
            self._send_json(
                {
                    "success": False,
                    "error": "Rate limit exceeded. Max 60 requests per minute.",
                },
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )
            return False
        return True

    def _check_auth(self) -> bool:
        """Check bearer token auth for /api/* endpoints.

        Returns True if authorized (or auth not configured).
        Sends 401 and returns False if unauthorized.
        """
        if _DASHBOARD_TOKEN is None:
            return True  # No auth configured — backward compatible

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_json(
                {"success": False, "error": "Authorization required"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False

        provided_token = auth_header[7:]  # Strip "Bearer " prefix
        if not hmac.compare_digest(provided_token, _DASHBOARD_TOKEN):
            self._send_json(
                {"success": False, "error": "Invalid token"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False

        return True

    def do_GET(self):
        """Handle GET requests."""
        # Rate limiting applies to all requests
        if not self._check_rate_limit():
            return

        # Parse URL
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Extract project_id from query string if present
        project_id = query.get("project", [None])[0]

        # Route API requests
        if path.startswith("/api/"):
            # Bearer token auth required for /api/* endpoints
            if not self._check_auth():
                return
            self._handle_api(path, project_id)
        else:
            # Serve static files (no auth required)
            self._handle_static(path)

    def _handle_api(self, path: str, project_id: str | None):
        """Handle API endpoint requests."""
        endpoint = path[5:]  # Remove "/api/" prefix

        # SSE endpoint requires special handling
        if endpoint == "stream":
            self._handle_sse_stream()
            return

        # Map endpoints to handlers
        handlers = {
            "health": self._api_health,
            "progress": self._api_progress,
            "workforce": self._api_workforce,
            "risks": self._api_risks,
            "efficiency": self._api_efficiency,
            "org": self._api_org,
            "queue": self._api_queue,
            "full": self._api_full,
            "unified": self._api_unified,
            "compare": self._api_compare,
            "status": self._api_status,
            "current": self._api_current,
            "activity": self._api_activity,
            "daemon": self._api_daemon,
            "subsystem-health": self._api_subsystem_health,
            "goals": self._api_goals,
            "roadmap": self._api_roadmap,
            "results": self._api_results,
            "g7-progress": self._api_g7_progress,
            "autonomy-widget": self._api_autonomy_widget,
        }

        # Find handler
        handler = handlers.get(endpoint)
        if handler:
            try:
                data = handler(project_id)
                extra = None
                if endpoint == "health":
                    extra = {"X-Forge-Version": _FORGE_VERSION}
                self._send_json(data, extra_headers=extra)
            except Exception as e:
                logger.exception(f"API error on {endpoint}")
                self._send_json(
                    {
                        "success": False,
                        "error": str(e),
                        "endpoint": endpoint,
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
        else:
            self._send_json(
                {
                    "success": False,
                    "error": f"Unknown endpoint: {endpoint}",
                    "available_endpoints": list(handlers.keys()) + ["stream"],
                },
                status=HTTPStatus.NOT_FOUND,
            )

    def _handle_static(self, path: str):
        """Handle static file requests."""
        # Normalize path
        if path == "/" or path == "":
            path = "/index.html"

        # Security: prevent directory traversal
        try:
            file_path = (self.static_dir / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(self.static_dir)):
                self._send_json(
                    {"success": False, "error": "Access denied"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
        except (ValueError, OSError):
            self._send_json(
                {"success": False, "error": "Invalid path"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        # Check if file exists
        if not file_path.exists() or not file_path.is_file():
            # For SPA support: serve index.html for unknown paths
            index_path = self.static_dir / "index.html"
            if index_path.exists():
                file_path = index_path
            else:
                self._send_json(
                    {"success": False, "error": "Not found"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return

        # Serve the file
        try:
            content_type, _ = mimetypes.guess_type(str(file_path))
            if content_type is None:
                content_type = "application/octet-stream"

            with open(file_path, "rb") as f:
                content = f.read()

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(content)
        except IOError as e:
            logger.error(f"Error serving {file_path}: {e}")
            self._send_json(
                {"success": False, "error": "Error reading file"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_sse_stream(self):
        """Handle SSE stream connection at /api/stream."""
        # Get or create SSE manager
        sse_manager = _get_sse_manager(self.data_dir)

        # Send SSE headers
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # Disable nginx buffering
        # Add CORS headers
        for header, value in CORS_HEADERS.items():
            self.send_header(header, value)
        self.end_headers()

        # Register this client
        client_queue = sse_manager.add_client()

        try:
            # Send initial full state
            full_state = sse_manager.get_full_state()
            initial_event = sse_manager._format_sse(
                "full",
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": full_state,
                },
            )
            self.wfile.write(initial_event.encode("utf-8"))
            self.wfile.flush()

            # Stream events to client
            while True:
                try:
                    # Wait for next event with timeout
                    event = client_queue.get(timeout=30.0)
                    self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # No event received - connection still alive, continue waiting
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    # Client disconnected
                    break
        except Exception as e:
            logger.debug(f"SSE stream ended: {e}")
        finally:
            # Unregister client
            sse_manager.remove_client(client_queue)

    def _send_json(
        self,
        data: dict | list,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ):
        """Send JSON response."""
        content = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        if extra_headers:
            for header, value in extra_headers.items():
                self.send_header(header, value)
        self.end_headers()
        self.wfile.write(content)

    # -------------------------------------------------------------------------
    # API Handlers
    # -------------------------------------------------------------------------

    def _api_health(self, project_id: str | None) -> dict:
        """GET /api/health - Health data."""
        agg = _get_aggregator()
        if agg and "aggregate_health" in agg:
            return agg["aggregate_health"](project_id)
        return self._fallback_health()

    def _api_progress(self, project_id: str | None) -> dict:
        """GET /api/progress - Task progress."""
        agg = _get_aggregator()
        if agg and "aggregate_progress" in agg:
            return agg["aggregate_progress"](project_id)
        return self._fallback_progress()

    def _api_workforce(self, project_id: str | None) -> dict:
        """GET /api/workforce - Employee data."""
        agg = _get_aggregator()
        if agg and "aggregate_workforce" in agg:
            return agg["aggregate_workforce"](project_id)
        return self._fallback_workforce()

    def _api_risks(self, project_id: str | None) -> dict:
        """GET /api/risks - Risk assessment."""
        agg = _get_aggregator()
        if agg and "aggregate_risks" in agg:
            risks = agg["aggregate_risks"]()
            return {
                "success": True,
                "risk_count": len(risks),
                "critical_count": sum(
                    1 for r in risks if r.get("severity") == "CRITICAL"
                ),
                "warning_count": sum(
                    1 for r in risks if r.get("severity") == "WARNING"
                ),
                "risks": risks,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        return {"success": False, "risks": [], "error": "Aggregator not available"}

    def _api_efficiency(self, project_id: str | None) -> dict:
        """GET /api/efficiency - Efficiency metrics."""
        agg = _get_aggregator()
        if agg and "aggregate_efficiency" in agg:
            return agg["aggregate_efficiency"]()
        return {"success": False, "error": "Aggregator not available"}

    def _api_org(self, project_id: str | None) -> dict:
        """GET /api/org - Raw org.json."""
        org_path = self.data_dir / "org.json"
        data = _load_json_file(org_path)
        if data:
            return {"success": True, "data": data}
        return {"success": False, "error": f"Could not load {org_path}"}

    def _api_queue(self, project_id: str | None) -> dict:
        """GET /api/queue - Raw work_queue.json."""
        queue_path = self.data_dir / "state/work_queue.json"
        data = _load_json_file(queue_path)
        if data:
            return {"success": True, "data": data}
        return {"success": False, "error": f"Could not load {queue_path}"}

    def _api_full(self, project_id: str | None) -> dict:
        """GET /api/full - Complete dashboard data."""
        agg = _get_aggregator()
        if agg and "get_dashboard_data" in agg:
            return agg["get_dashboard_data"](project_id)
        # Fallback: combine manual data
        return {
            "success": True,
            "health": self._api_health(project_id),
            "progress": self._api_progress(project_id),
            "workforce": self._api_workforce(project_id),
            "risks": self._api_risks(project_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _api_unified(self, project_id: str | None) -> dict:
        """GET /api/unified - Multi-project unified view."""
        agg = _get_aggregator()
        if agg and "get_unified_dashboard" in agg:
            return agg["get_unified_dashboard"]()
        return {"success": False, "error": "Unified dashboard not available"}

    def _api_compare(self, project_id: str | None) -> dict:
        """GET /api/compare - Project comparison."""
        agg = _get_aggregator()
        if agg and "get_project_comparison" in agg:
            return agg["get_project_comparison"]()
        return {"success": False, "error": "Project comparison not available"}

    def _api_status(self, project_id: str | None) -> dict:
        """GET /api/status - Server status."""
        return {
            "success": True,
            "status": "running",
            "server_time": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(self.data_dir),
            "static_dir": str(self.static_dir),
            "aggregator_available": _get_aggregator() is not None,
        }

    def _api_current(self, project_id: str | None) -> dict:
        """GET /api/current - Current work context with real data."""
        queue = _load_json_file(self.data_dir / "state/work_queue.json")
        session = _load_json_file(self.data_dir / "state/session_state.json")
        loop_monitor = _load_json_file(self.data_dir / "loop_monitor.json")

        # Get active task from work queue
        active_task = None
        in_progress = queue.get("in_progress", [])
        if in_progress:
            task = in_progress[0]
            active_task = {
                "id": task.get("task_id", "unknown"),
                "title": task.get("title", "No title"),
                "description": task.get("description", ""),
                "assignee": task.get("assignee", "unassigned"),
                "status": "in_progress",
                "started_at": task.get("started_at", ""),
            }

        # Get current phase from session or loop monitor
        current_phase = None
        if session:
            current_phase = {
                "id": session.get("current_phase", ""),
                "name": session.get("phase_name", ""),
                "status": session.get("status", "active"),
            }

        # Get daemon status from loop monitor
        daemon_status = None
        if loop_monitor:
            daemon_status = {
                "state": loop_monitor.get("circuit_state", "unknown"),
                "consecutive_failures": loop_monitor.get("consecutive_failures", 0),
                "tasks_this_hour": loop_monitor.get("tasks_this_hour", 0),
                "last_success": loop_monitor.get("last_success_time", ""),
            }

        # Get blockers from escalations
        blockers = []
        escalations_dir = self.data_dir / "escalations"
        if escalations_dir.exists():
            for esc_file in escalations_dir.glob("*.json"):
                try:
                    esc = json.loads(esc_file.read_text())
                    if esc.get("status") == "open":
                        blockers.append(
                            {
                                "id": esc.get("id", esc_file.stem),
                                "type": esc.get("type", "unknown"),
                                "summary": esc.get("summary", ""),
                            }
                        )
                except Exception:
                    pass

        return {
            "success": True,
            "active_task": active_task,
            "current_phase": current_phase,
            "daemon_status": daemon_status,
            "blockers": blockers,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _api_activity(self, project_id: str | None) -> dict:
        """GET /api/activity - Recent activity feed from daemon logs."""
        activities = []

        # Read from daemon log
        log_file = self.data_dir / "logs" / "daemon.log"
        if log_file.exists():
            try:
                lines = log_file.read_text().strip().split("\n")[-50:]  # Last 50 lines
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                        msg = entry.get("message", "")
                        ts = entry.get("timestamp", "")
                        level = entry.get("level", "INFO")

                        # Parse time for display
                        time_str = ""
                        if ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                time_str = dt.strftime("%H:%M")
                            except Exception:
                                time_str = ts[:5] if len(ts) >= 5 else ts

                        # Determine activity type
                        activity_type = "info"
                        if "COMPLETED" in msg or "✓" in msg:
                            activity_type = "success"
                        elif "FAILED" in msg or "ERROR" in msg:
                            activity_type = "error"
                        elif "WARNING" in msg:
                            activity_type = "warning"

                        activities.append(
                            {
                                "time": time_str,
                                "text": msg,
                                "type": activity_type,
                                "level": level,
                            }
                        )

                        if len(activities) >= 20:
                            break
                    except json.JSONDecodeError:
                        # Plain text line
                        activities.append(
                            {
                                "time": "",
                                "text": line[:200],
                                "type": "info",
                                "level": "INFO",
                            }
                        )
            except Exception as e:
                logger.warning(f"Failed to read daemon log: {e}")

        return {
            "success": True,
            "activities": activities,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _api_daemon(self, project_id: str | None) -> dict:
        """GET /api/daemon - Daemon status, lifecycle stats, and activity timeline."""
        import re

        data_dir = self.data_dir

        # --- Daemon status from PID file ---
        pid_file = data_dir / "runtime/daemon.pid"
        daemon_pid = None
        daemon_running = False
        daemon_start_time = None
        if pid_file.exists():
            try:
                pid_text = pid_file.read_text().strip()
                # PID file may be JSON (with "pid" key) or plain integer
                try:
                    pid_data = json.loads(pid_text)
                    daemon_pid = pid_data.get("pid")
                    started_at = pid_data.get("started_at")
                    if started_at:
                        daemon_start_time = datetime.fromisoformat(
                            started_at.replace("Z", "+00:00")
                        )
                except (json.JSONDecodeError, AttributeError):
                    daemon_pid = int(pid_text)
                if daemon_pid:
                    # Check if process is alive
                    os.kill(daemon_pid, 0)
                    daemon_running = True
                    if not daemon_start_time:
                        daemon_start_time = datetime.fromtimestamp(
                            pid_file.stat().st_mtime, tz=timezone.utc
                        )
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                daemon_running = False

        # --- Heartbeat ---
        heartbeat_file = data_dir / "runtime/daemon.heartbeat"
        heartbeat_age_seconds = None
        if heartbeat_file.exists():
            try:
                hb_mtime = heartbeat_file.stat().st_mtime
                heartbeat_age_seconds = round(time.time() - hb_mtime)
            except OSError:
                pass

        # --- Parse daemon log ---
        log_file = data_dir / "logs" / "daemon.log"
        events = []
        lifetime_completed = 0
        lifetime_failed = 0
        current_cycle = 0
        cycles_today = 0
        last_strategic = None

        if log_file.exists():
            try:
                raw = log_file.read_text(encoding="utf-8")
                lines = raw.strip().split("\n")
                # Parse from the end for efficiency (last 500 lines)
                recent_lines = lines[-500:] if len(lines) > 500 else lines
                today_str = datetime.now().strftime("%Y-%m-%d")

                cycle_re = re.compile(
                    r"--- Cycle (\d+) \| queue: (\d+) pending, (\d+) active, (\d+) blocked, (\d+) done ---"
                )
                completed_re = re.compile(
                    r"\[4/4 Execute\] ✓ COMPLETED: (.+?) \| Employee: (\S+) \| Task ID: (\S+)"
                )
                failed_re = re.compile(
                    r"\[4/4 Execute\] ✗ FAILED: (.+?) \| Employee: (\S+) \| Error: (.+?) \| Task ID: (\S+)"
                )
                strategic_start_re = re.compile(
                    r"\[1/4 Discovery\] Strategic planning cycle\.\.\."
                )
                strategic_end_re = re.compile(
                    r"Strategic planning: (\d+) initiatives proposed, (\d+) auto-approved, (\d+) tasks queued"
                )
                exec_loop_re = re.compile(
                    r"Executive loop: (\d+) executives invoked, (\d+) decisions, (\d+) work items"
                )
                proactive_re = re.compile(
                    r"Proactive scan complete: (\d+) approved, (\d+) pending"
                )
                cycle_done_re = re.compile(
                    r"--- Cycle \d+ done \((\d+)s\) \| lifetime: (\d+) completed, (\d+) failed ---"
                )
                roadmap_re = re.compile(
                    r"Roadmap scheduling: scheduled (\d+) tasks from (\d+) found \(wave (\d+)\)"
                )

                strategic_start_ts = None

                for line in recent_lines:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = entry.get("timestamp", "")
                    msg = entry.get("message", "")
                    entry.get("level", "INFO")
                    is_today = ts.startswith(today_str)

                    # Cycle start
                    m = cycle_re.search(msg)
                    if m:
                        c = int(m.group(1))
                        if c > current_cycle:
                            current_cycle = c
                        if is_today:
                            cycles_today = max(cycles_today, c)
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "cycle_start",
                                "cycle": c,
                                "title": f"Cycle {c}",
                                "detail": f"Queue: {m.group(2)} pending, {m.group(3)} active, {m.group(4)} blocked, {m.group(5)} done",
                            }
                        )
                        continue

                    # Task completed
                    m = completed_re.search(msg)
                    if m:
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "task_completed",
                                "title": m.group(1)[:80],
                                "employee": m.group(2),
                                "task_id": m.group(3),
                            }
                        )
                        continue

                    # Task failed
                    m = failed_re.search(msg)
                    if m:
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "task_failed",
                                "title": m.group(1)[:80],
                                "employee": m.group(2),
                                "error": m.group(3)[:120],
                                "task_id": m.group(4),
                            }
                        )
                        continue

                    # Cycle done (lifetime stats)
                    m = cycle_done_re.search(msg)
                    if m:
                        lifetime_completed = int(m.group(2))
                        lifetime_failed = int(m.group(3))
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "cycle_done",
                                "title": f"Cycle complete ({m.group(1)}s)",
                                "detail": f"Lifetime: {m.group(2)} completed, {m.group(3)} failed",
                            }
                        )
                        continue

                    # Strategic planning start
                    if strategic_start_re.search(msg):
                        strategic_start_ts = ts
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "strategic_planning",
                                "title": "Strategic planning started",
                            }
                        )
                        continue

                    # Strategic planning end
                    m = strategic_end_re.search(msg)
                    if m:
                        duration_s = None
                        if strategic_start_ts:
                            try:
                                t0 = datetime.fromisoformat(strategic_start_ts)
                                t1 = datetime.fromisoformat(ts)
                                duration_s = int((t1 - t0).total_seconds())
                            except Exception:
                                pass
                        last_strategic = {
                            "last_run": ts,
                            "duration_seconds": duration_s,
                            "initiatives_proposed": int(m.group(1)),
                            "auto_approved": int(m.group(2)),
                            "tasks_queued": int(m.group(3)),
                        }
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "strategic_result",
                                "title": f"Strategic planning: {m.group(1)} initiatives, {m.group(3)} tasks",
                                "detail": f"Duration: {duration_s}s"
                                if duration_s
                                else "",
                            }
                        )
                        strategic_start_ts = None
                        continue

                    # Executive loop
                    m = exec_loop_re.search(msg)
                    if m:
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "executive_loop",
                                "title": f"Executive loop: {m.group(1)} execs, {m.group(2)} decisions",
                                "detail": f"{m.group(3)} work items submitted",
                            }
                        )
                        continue

                    # Proactive scan
                    m = proactive_re.search(msg)
                    if m:
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "proactive_scan",
                                "title": f"Proactive scan: {m.group(1)} approved, {m.group(2)} pending",
                            }
                        )
                        continue

                    # Roadmap scheduling
                    m = roadmap_re.search(msg)
                    if m:
                        events.append(
                            {
                                "timestamp": ts,
                                "type": "roadmap_schedule",
                                "title": f"Roadmap: {m.group(1)} tasks scheduled (wave {m.group(3)})",
                                "detail": f"From {m.group(2)} found",
                            }
                        )
                        continue

            except Exception as e:
                logger.warning(f"Failed to parse daemon log: {e}")

        # Keep only last 50 events for the response
        events = events[-50:]
        events.reverse()  # Most recent first

        # Calculate success rate
        total = lifetime_completed + lifetime_failed
        success_rate = round(lifetime_completed / total, 2) if total > 0 else 0.0

        # Calculate uptime
        uptime_seconds = None
        if daemon_running and daemon_start_time:
            uptime_seconds = int(
                (datetime.now(timezone.utc) - daemon_start_time).total_seconds()
            )

        return {
            "success": True,
            "status": "running" if daemon_running else "stopped",
            "pid": daemon_pid,
            "uptime_seconds": uptime_seconds,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "lifetime": {
                "completed": lifetime_completed,
                "failed": lifetime_failed,
                "success_rate": success_rate,
            },
            "current_cycle": current_cycle,
            "cycles_today": cycles_today,
            "recent_events": events,
            "strategic_planning": last_strategic,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _api_subsystem_health(self, project_id: str | None) -> dict:
        """GET /api/subsystem-health - Per-subsystem health status from heartbeat."""
        heartbeat_file = self.data_dir / "runtime/daemon.heartbeat"

        # If heartbeat file doesn't exist, daemon is not running
        if not heartbeat_file.exists():
            not_running_sub = {
                "status": "unknown",
                "message": "Daemon not running",
                "last_active": None,
            }
            return {
                "success": True,
                "subsystems": {
                    "task_execution": not_running_sub,
                    "proactive": not_running_sub,
                    "cross_project": not_running_sub,
                    "strategic_planning": not_running_sub,
                    "roadmap_scheduling": not_running_sub,
                    "p25_autonomy": not_running_sub,
                    "executive_loop": not_running_sub,
                    "improvement_cycle": not_running_sub,
                    "employee_ideation": not_running_sub,
                    "auto_merge": not_running_sub,
                    "scheduling_efficiency": not_running_sub,
                    "circuit_breaker": not_running_sub,
                    "document_approvals": not_running_sub,
                },
                "overall": "unknown",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Read heartbeat JSON
        try:
            with open(heartbeat_file, "r", encoding="utf-8") as f:
                hb = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to parse heartbeat file: {e}")
            return {
                "success": False,
                "error": f"Invalid heartbeat file: {e}",
            }

        subsystems: dict[str, dict[str, Any]] = {}

        # --- task_execution: healthy if tasks_failed < tasks_completed ---
        completed = hb.get("tasks_completed_this_session", 0)
        failed = hb.get("tasks_failed_this_session", 0)
        if completed > 0 and failed < completed:
            te_status = "healthy"
            te_msg = f"{completed} completed, {failed} failed"
        elif completed == 0 and failed == 0:
            te_status = "healthy"
            te_msg = "No tasks processed yet"
        else:
            te_status = "degraded"
            te_msg = f"{completed} completed, {failed} failed"
        subsystems["task_execution"] = {
            "status": te_status,
            "message": te_msg,
            "last_active": None,
        }

        # --- Helper for timestamp-based subsystems ---
        def _ts_health(key: str, name: str) -> dict[str, Any]:
            val = hb.get(key)
            if val:
                return {
                    "status": "healthy",
                    "message": "Active",
                    "last_active": val,
                }
            return {
                "status": "unknown",
                "message": "Not run yet",
                "last_active": None,
            }

        # --- Timestamp-based subsystems ---
        subsystems["proactive"] = _ts_health("last_proactive_scan", "proactive")
        subsystems["cross_project"] = _ts_health(
            "last_rebalance_check", "cross_project"
        )
        subsystems["strategic_planning"] = _ts_health(
            "last_strategic_planning", "strategic_planning"
        )
        subsystems["roadmap_scheduling"] = _ts_health(
            "last_roadmap_scan", "roadmap_scheduling"
        )
        subsystems["p25_autonomy"] = _ts_health("last_session_snapshot", "p25_autonomy")
        subsystems["executive_loop"] = _ts_health(
            "last_executive_loop", "executive_loop"
        )
        subsystems["improvement_cycle"] = _ts_health(
            "last_improvement_cycle", "improvement_cycle"
        )
        subsystems["employee_ideation"] = _ts_health(
            "last_employee_ideation", "employee_ideation"
        )
        subsystems["auto_merge"] = _ts_health("last_auto_merge_check", "auto_merge")

        # --- scheduling_efficiency: always healthy ---
        subsystems["scheduling_efficiency"] = {
            "status": "healthy",
            "message": "Always active",
            "last_active": None,
        }

        # --- circuit_breaker: healthy if state == "closed" ---
        cb_state = hb.get("circuit_breaker_state", "unknown")
        if cb_state == "closed":
            cb_status = "healthy"
            cb_msg = "Closed"
        elif cb_state == "half_open":
            cb_status = "degraded"
            cb_msg = "Half-open (testing recovery)"
        elif cb_state == "open":
            cb_status = "unhealthy"
            cb_msg = "Open (tripped)"
        else:
            cb_status = "unknown"
            cb_msg = f"State: {cb_state}"
        subsystems["circuit_breaker"] = {
            "status": cb_status,
            "message": cb_msg,
            "last_active": None,
        }

        # --- document_approvals: not tracked in heartbeat ---
        subsystems["document_approvals"] = {
            "status": "unknown",
            "message": "Not tracked in heartbeat",
            "last_active": None,
        }

        # --- Compute overall health (worst status wins) ---
        # Priority order: unhealthy > degraded > unknown > healthy
        status_priority = {
            "unhealthy": 0,
            "degraded": 1,
            "unknown": 2,
            "healthy": 3,
        }
        worst = "healthy"
        for sub in subsystems.values():
            s = sub["status"]
            if status_priority.get(s, 2) < status_priority.get(worst, 3):
                worst = s

        return {
            "success": True,
            "subsystems": subsystems,
            "overall": worst,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _api_goals(self, project_id: str | None) -> dict:
        """GET /api/goals - Strategic goal progress from goal_tracker."""
        try:
            hooks_dir = Path(__file__).parent
            if str(hooks_dir) not in sys.path:
                sys.path.insert(0, str(hooks_dir))
            from goal_tracker import (
                assess_goal,
                get_active_period,
                parse_goals_from_vision,
            )

            vision_path = self.data_dir / "vision.md"
            if not vision_path.exists():
                return {"success": True, "goals": [], "period": "unknown"}

            active_period = get_active_period(vision_path)
            goals = parse_goals_from_vision(vision_path, period="active")
            all_goals = parse_goals_from_vision(vision_path, period="all")

            goal_list = []
            for g in goals:
                progress = 0
                status = "not_started"
                try:
                    result = assess_goal(g, self.data_dir, lightweight=True)
                    progress = result.progress_percent
                    status = (
                        result.status.value
                        if hasattr(result.status, "value")
                        else str(result.status)
                    )
                except Exception:
                    pass

                goal_list.append(
                    {
                        "id": g.id,
                        "name": g.name,
                        "description": g.description,
                        "metric": g.success_metric,
                        "owner": g.owner,
                        "progress": progress,
                        "status": status,
                        "period": getattr(g, "period", active_period),
                    }
                )

            # Completed periods summary
            completed_goals = [
                g for g in all_goals if getattr(g, "period_status", "") == "complete"
            ]

            return {
                "success": True,
                "period": active_period,
                "goals": goal_list,
                "completed_count": len(completed_goals),
                "total_count": len(all_goals),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.exception("Failed to load goals")
            return {"success": False, "error": str(e), "goals": []}

    def _api_roadmap(self, project_id: str | None) -> dict:
        """GET /api/roadmap - Phase progress from STATE.md."""
        try:
            # Try planning dir relative to data_dir parent
            planning_dir = self.data_dir.parent / ".planning"
            state_path = planning_dir / "STATE.md"

            if not state_path.exists():
                return {"success": True, "phases": []}

            with open(state_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Parse the progress table from STATE.md
            phases = []
            in_table = False
            for line in content.split("\n"):
                if "| Phase |" in line or "|----" in line:
                    in_table = True
                    continue
                if in_table and line.startswith("|"):
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cols) >= 4:
                        name = cols[0]
                        total = int(cols[1]) if cols[1].isdigit() else 0
                        done = int(cols[2]) if cols[2].isdigit() else 0
                        status = cols[3]
                        pct = int(done / total * 100) if total > 0 else 0
                        phases.append(
                            {
                                "name": name,
                                "total_tasks": total,
                                "completed_tasks": done,
                                "status": status,
                                "progress": pct,
                            }
                        )
                elif in_table and not line.startswith("|"):
                    in_table = False

            # Parse current session info
            session_info = {}
            for line in content.split("\n"):
                if line.startswith("- **Phase:**"):
                    session_info["current_phase"] = line.split("**Phase:**")[1].strip()
                elif line.startswith("- **Last Completed Task:**"):
                    session_info["last_task"] = line.split("**Last Completed Task:**")[
                        1
                    ].strip()
                elif line.startswith("- **Next Task:**"):
                    session_info["next_task"] = line.split("**Next Task:**")[1].strip()

            return {
                "success": True,
                "phases": phases,
                "session": session_info,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.exception("Failed to load roadmap")
            return {"success": False, "error": str(e), "phases": []}

    def _api_results(self, project_id: str | None) -> dict:
        """GET /api/results - Task outcomes from .company/results/."""
        try:
            results_dir = self.data_dir / "results"
            if not results_dir.is_dir():
                return {"success": True, "results": [], "total": 0}

            results = []
            for fp in sorted(results_dir.glob("*.json"), reverse=True):
                try:
                    data = _load_json_file(fp)
                    if data:
                        results.append(data)
                except Exception:
                    continue

            # Sort by timestamp descending
            results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

            return {
                "success": True,
                "results": results,
                "total": len(results),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.exception("Failed to load results")
            return {"success": False, "error": str(e), "results": []}

    def _api_g7_progress(self, project_id: str | None) -> dict:
        """GET /api/g7-progress - G7 sustained autonomy goal progress.

        Returns current uptime hours, longest streak, 7-day success rate,
        and target (168 hours = 7 days) for dashboard visibility.
        """
        G7_TARGET_HOURS = 168  # 7 days in hours
        now = datetime.now(timezone.utc)

        # --- Current uptime from daemon PID/heartbeat ---
        current_uptime_hours: float = 0.0
        daemon_running = False

        pid_file = self.data_dir / "runtime/daemon.pid"
        heartbeat_file = self.data_dir / "runtime/daemon.heartbeat"

        daemon_start_time: datetime | None = None
        if pid_file.exists():
            try:
                pid_text = pid_file.read_text().strip()
                try:
                    pid_data = json.loads(pid_text)
                    daemon_pid = pid_data.get("pid")
                    started_at = pid_data.get("started_at")
                    if started_at:
                        daemon_start_time = datetime.fromisoformat(
                            started_at.replace("Z", "+00:00")
                        )
                except (json.JSONDecodeError, AttributeError):
                    daemon_pid = int(pid_text)
                    daemon_start_time = None
                if daemon_pid:
                    os.kill(daemon_pid, 0)
                    daemon_running = True
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                daemon_running = False

        if daemon_running and daemon_start_time:
            delta = (now - daemon_start_time).total_seconds()
            current_uptime_hours = round(delta / 3600, 2)
        elif daemon_running and heartbeat_file.exists():
            try:
                hb_data_text = heartbeat_file.read_text()
                hb_data = json.loads(hb_data_text)
                started_at_str = hb_data.get("started_at")
                if started_at_str:
                    started_at_dt = datetime.fromisoformat(
                        started_at_str.replace("Z", "+00:00")
                    )
                    delta = (now - started_at_dt).total_seconds()
                    current_uptime_hours = round(delta / 3600, 2)
            except (OSError, json.JSONDecodeError, ValueError, KeyError):
                pass

        # --- Longest streak from daemon_metrics.json ---
        metrics_path = self.data_dir / "state/daemon_metrics.json"
        metrics = _load_json_file(metrics_path)

        longest_streak_hours: float = 0.0
        uptime_windows: list = metrics.get("uptime_windows", [])

        for window in uptime_windows:
            duration = window.get("duration_seconds")
            if duration and isinstance(duration, (int, float)) and duration > 0:
                window_hours = duration / 3600
                if window_hours > longest_streak_hours:
                    longest_streak_hours = window_hours

        if daemon_running and current_uptime_hours > longest_streak_hours:
            longest_streak_hours = current_uptime_hours

        longest_streak_hours = round(longest_streak_hours, 2)

        # --- 7-day success rate from daemon log ---
        log_file = self.data_dir / "logs/daemon.log"
        completed_7d = 0
        failed_7d = 0
        cutoff = now.timestamp() - 7 * 24 * 3600

        if log_file.exists():
            try:
                import re as _re

                completed_re = _re.compile(r"\[4/4 Execute\] ✓ COMPLETED:")
                failed_re = _re.compile(r"\[4/4 Execute\] ✗ FAILED:")
                raw = log_file.read_text(encoding="utf-8", errors="replace")
                lines = raw.strip().split("\n")
                recent = lines[-2000:] if len(lines) > 2000 else lines
                for line in recent:
                    try:
                        entry = json.loads(line)
                        ts_str = entry.get("time") or entry.get("timestamp", "")
                        if ts_str:
                            ts_dt = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            )
                            if ts_dt.timestamp() < cutoff:
                                continue
                        msg = entry.get("message", "") or entry.get("msg", "")
                        if completed_re.search(msg):
                            completed_7d += 1
                        elif failed_re.search(msg):
                            failed_7d += 1
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue
            except OSError:
                pass

        total_7d = completed_7d + failed_7d
        success_rate_7d: float | None = None
        if total_7d > 0:
            success_rate_7d = round(completed_7d / total_7d * 100, 1)

        # --- Health cache for supplemental data ---
        health_cache_path = self.data_dir / "health_cache.json"
        health_cache = _load_json_file(health_cache_path)
        health_score = health_cache.get("health_score") if health_cache else None

        progress_pct = round(min(current_uptime_hours / G7_TARGET_HOURS * 100, 100), 1)

        return {
            "success": True,
            "current_uptime_hours": current_uptime_hours,
            "longest_streak_hours": longest_streak_hours,
            "g7_target": G7_TARGET_HOURS,
            "progress_percent": progress_pct,
            "daemon_running": daemon_running,
            "success_rate_7d": success_rate_7d,
            "tasks_completed_7d": completed_7d,
            "tasks_failed_7d": failed_7d,
            "health_score": health_score,
            "generated_at": now.isoformat(),
        }

    def _api_autonomy_widget(self, project_id: str | None) -> dict:
        """GET /api/autonomy-widget - Autonomy metrics for the dashboard widget."""
        try:
            from dashboard_aggregator import aggregate_autonomy_widget_metrics

            return aggregate_autonomy_widget_metrics()
        except Exception as e:
            logger.exception("Failed to aggregate autonomy widget metrics")
            return {"success": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # Fallback Data (when aggregator is not available)
    # -------------------------------------------------------------------------

    def _fallback_health(self) -> dict:
        """Fallback health data from raw files."""
        org = _load_json_file(self.data_dir / "org.json")
        queue = _load_json_file(self.data_dir / "state/work_queue.json")

        # Calculate basic health metrics
        employees = org.get("employees", [])
        len(employees)
        sum(1 for e in employees if e.get("status") == "active")

        pending_tasks = len(queue.get("pending", []))
        in_progress = len(queue.get("in_progress", []))
        blocked = len(queue.get("blocked", []))

        # Simple health score calculation
        if pending_tasks + in_progress + blocked > 0:
            blocked_ratio = blocked / (pending_tasks + in_progress + blocked) * 100
        else:
            blocked_ratio = 0

        if blocked_ratio > 40:
            health_status = "critical"
            health_score = 40
        elif blocked_ratio > 20:
            health_status = "warning"
            health_score = 60
        else:
            health_status = "healthy"
            health_score = 80

        return {
            "success": True,
            "health_score": health_score,
            "health_status": health_status,
            "factors": [
                {
                    "name": "blocked_ratio",
                    "value": blocked_ratio,
                    "status": health_status,
                }
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }

    def _fallback_progress(self) -> dict:
        """Fallback progress data from raw files."""
        queue = _load_json_file(self.data_dir / "state/work_queue.json")

        pending = len(queue.get("pending", []))
        in_progress = len(queue.get("in_progress", []))
        blocked = len(queue.get("blocked", []))
        completed = len(queue.get("completed", []))
        total = pending + in_progress + blocked + completed

        return {
            "success": True,
            "completion": {
                "total": total,
                "completed": completed,
                "in_progress": in_progress,
                "blocked": blocked,
                "pending": pending,
                "completion_percentage": (completed / total * 100) if total > 0 else 0,
            },
            "velocity": {"daily_average": 0, "completed_today": 0},
            "forecast": {"can_estimate": False, "reason": "Fallback mode"},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }

    def _fallback_workforce(self) -> dict:
        """Fallback workforce data from raw files."""
        org = _load_json_file(self.data_dir / "org.json")

        employees = org.get("employees", [])
        total = len(employees)
        active = sum(1 for e in employees if e.get("status") == "active")
        idle = sum(1 for e in employees if e.get("status") == "available")
        blocked = sum(1 for e in employees if e.get("status") == "blocked")

        return {
            "success": True,
            "agents": {
                "total": total,
                "active": active,
                "idle": idle,
                "blocked": blocked,
            },
            "utilization": {
                "percentage": (active / total * 100) if total > 0 else 0,
                "status": "optimal"
                if 60 <= (active / total * 100 if total > 0 else 0) <= 80
                else "low",
            },
            "company_name": org.get("company", {}).get("name", "Unknown"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "fallback",
        }


# -----------------------------------------------------------------------------
# Server Management
# -----------------------------------------------------------------------------


class DashboardServer:
    """Dashboard HTTP server with graceful shutdown support."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        data_dir: Path = DEFAULT_DATA_DIR,
        static_dir: Path = STATIC_DIR,
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir).resolve()
        self.static_dir = Path(static_dir).resolve()
        self.verbose = verbose
        self.server: HTTPServer | None = None
        self._shutdown_requested = False

    def _make_handler(self):
        """Create request handler with configuration."""
        return partial(
            DashboardRequestHandler,
            data_dir=self.data_dir,
            static_dir=self.static_dir,
            verbose=self.verbose,
        )

    def start(self):
        """Start the HTTP server."""
        # Ensure static directory exists
        if not self.static_dir.exists():
            logger.warning(f"Static directory does not exist: {self.static_dir}")
            logger.info("Creating static directory...")
            self.static_dir.mkdir(parents=True, exist_ok=True)

        # Ensure data directory exists
        if not self.data_dir.exists():
            logger.warning(f"Data directory does not exist: {self.data_dir}")

        # Create server
        handler = self._make_handler()
        self.server = HTTPServer((self.host, self.port), handler)

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Initialize SSE manager (starts file watcher and heartbeat thread)
        _get_sse_manager(self.data_dir)
        logger.info(f"SSE manager started (watchdog available: {WATCHDOG_AVAILABLE})")

        # Log startup info
        logger.info(f"Dashboard server starting on http://{self.host}:{self.port}")
        logger.info(f"Data directory: {self.data_dir}")
        logger.info(f"Static directory: {self.static_dir}")
        logger.info(f"Aggregator available: {_get_aggregator() is not None}")
        logger.info("Press Ctrl+C to stop...")

        # Serve requests
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        """Stop the HTTP server."""
        # Stop SSE manager first
        _stop_sse_manager()

        if self.server:
            logger.info("Shutting down server...")
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            logger.info("Server stopped.")

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        if not self._shutdown_requested:
            self._shutdown_requested = True
            logger.info(f"Received signal {signum}, shutting down...")
            self.stop()


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Dashboard server for Gatesmith company data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Start on default port 8080
    python dashboard_server.py

    # Start on custom port
    python dashboard_server.py --port 3000

    # Specify data directory
    python dashboard_server.py --data-dir /path/to/.company

Endpoints:
    GET /api/health      Health data
    GET /api/progress    Task progress
    GET /api/workforce   Employee data
    GET /api/risks       Risk assessment
    GET /api/org         Raw org.json
    GET /api/queue       Raw work_queue.json
    GET /api/full        Complete dashboard data
    GET /api/unified     Multi-project unified view
    GET /api/compare     Project comparison
    GET /api/status      Server status
    GET /api/stream      SSE stream (real-time updates)
    GET /*               Static files
        """,
    )

    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST})",
    )

    auto_port = get_project_port()
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=auto_port,
        help=f"Port to listen on (default: {auto_port}, auto-allocated for this project)",
    )

    parser.add_argument(
        "-d",
        "--data-dir",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"Data directory containing org.json and work_queue.json (default: {DEFAULT_DATA_DIR})",
    )

    parser.add_argument(
        "-s",
        "--static-dir",
        type=str,
        default=str(STATIC_DIR),
        help=f"Static files directory (default: {STATIC_DIR})",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Create and start server
    server = DashboardServer(
        host=args.host,
        port=args.port,
        data_dir=Path(args.data_dir),
        static_dir=Path(args.static_dir),
        verbose=args.verbose,
    )

    try:
        server.start()
    except OSError as e:
        if "Address already in use" in str(e):
            logger.error(
                f"Port {args.port} is already in use. Try a different port with --port"
            )
            sys.exit(1)
        raise
    except Exception as e:
        logger.exception(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
