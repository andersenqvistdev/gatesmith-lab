#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
External Connectors — secure bidirectional communication with external services.

This module provides secure integration with external services (GitHub, Slack, Discord,
webhooks) while maintaining Gatesmith's security-first philosophy.

Features:
- Secure credential management from ~/.config/forge/secrets.json (NEVER from repo)
- Rate limiting per service with configurable buckets
- Comprehensive audit logging (excluding secrets)
- Cryptographic webhook signature verification
- Replay protection with timestamp validation and nonce tracking
- Outgoing connectors for GitHub, Slack, Discord, and generic webhooks

Security Model:
- Credentials loaded from ~/.config/forge/secrets.json only
- Credentials loaded fresh on each request (never cached beyond request)
- All external calls logged with request metadata (secrets redacted)
- HMAC signature verification with constant-time comparison
- Timestamp validation rejects stale webhooks (>5 min)
- Nonce tracking prevents replay attacks (1-hour window)

Outgoing Connector Usage:
    # Load credentials for a service
    from external_connectors import load_credentials
    creds = load_credentials("slack")  # From ~/.config/forge/secrets.json

    # Validate service configuration
    from external_connectors import validate_service_config
    valid, errors = validate_service_config("github", config)

    # Use a connector
    from external_connectors import get_connector
    connector = get_connector("slack")
    result = connector.send({"text": "Hello from Gatesmith!"})

    # Rate limiting
    from external_connectors import get_rate_limiter
    limiter = get_rate_limiter()
    if limiter.can_proceed("slack"):
        connector.send(payload)

Incoming Webhook Usage:
    # Verify a webhook signature
    from external_connectors import verify_webhook_signature
    is_valid = verify_webhook_signature(payload, signature, secret)

    # Parse and verify GitHub webhook
    from external_connectors import WebhookReceiver
    receiver = WebhookReceiver(github_secret="...", slack_secret="...")
    valid, data = receiver.parse_github_webhook(headers, body)

    # With replay protection
    receiver = WebhookReceiver(
        github_secret="...",
        enable_replay_protection=True,
        max_timestamp_age_seconds=300
    )
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import company resolver for multi-project support
# Lazy import to handle both package and direct execution
_company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules, supporting both package and script modes."""
    global _company_resolver
    if _company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        _company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        _company_resolver = cr


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Supported HMAC algorithms for webhook verification
SUPPORTED_ALGORITHMS = {"sha256", "sha1"}

# Default maximum age for webhook timestamps (5 minutes)
DEFAULT_MAX_TIMESTAMP_AGE_SECONDS = 300

# Default nonce expiry window (1 hour)
DEFAULT_NONCE_TTL_SECONDS = 3600

# GitHub webhook signature header prefix
GITHUB_SIGNATURE_PREFIX = "sha256="
GITHUB_SIGNATURE_LEGACY_PREFIX = "sha1="

# Slack request timestamp header
SLACK_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SLACK_SIGNATURE_HEADER = "X-Slack-Signature"
SLACK_SIGNATURE_VERSION = "v0"

# -----------------------------------------------------------------------------
# Outgoing Connector Configuration (P19 External Service Integration)
# -----------------------------------------------------------------------------

# Path to credentials file (NEVER in repository)
SECRETS_PATH = Path.home() / ".config" / "forge" / "secrets.json"

# Rate limit state file
RATE_LIMIT_FILE = "external_rate_limits.json"

# Audit log file
AUDIT_LOG_FILE = "external_audit.log"

# Default rate limits (requests per minute)
DEFAULT_WEBHOOK_RATE_LIMIT = 60
DEFAULT_NOTIFICATION_RATE_LIMIT = 30

# Supported services
SUPPORTED_SERVICES = ["github", "slack", "discord", "generic_webhook"]

# Service registry with configuration
SERVICE_REGISTRY: dict[str, dict[str, Any]] = {
    "github": {
        "auth_type": "bearer_token",
        "required_fields": ["token"],
        "optional_fields": ["base_url"],
        "rate_limit": 30,  # requests per minute
        "description": "GitHub API integration",
        "url_pattern": None,
    },
    "slack": {
        "auth_type": "url_token",
        "required_fields": ["webhook_url"],
        "optional_fields": ["channel", "username", "icon_emoji"],
        "rate_limit": 60,
        "description": "Slack incoming webhook",
        "url_pattern": r"^https://hooks\.slack\.com/",
    },
    "discord": {
        "auth_type": "url_token",
        "required_fields": ["webhook_url"],
        "optional_fields": ["username", "avatar_url"],
        "rate_limit": 30,
        "description": "Discord webhook",
        "url_pattern": r"^https://discord(app)?\.com/api/webhooks/",
    },
    "generic_webhook": {
        "auth_type": "bearer_token",
        "required_fields": ["webhook_url"],
        "optional_fields": ["token", "headers"],
        "rate_limit": 60,
        "description": "Generic HTTP webhook with optional auth",
        "url_pattern": None,
    },
}


# -----------------------------------------------------------------------------
# Nonce Storage (Thread-Safe In-Memory with TTL)
# -----------------------------------------------------------------------------


@dataclass
class NonceEntry:
    """A nonce entry with expiration timestamp."""

    nonce: str
    expires_at: float


class NonceTracker:
    """
    Thread-safe in-memory nonce tracker with automatic TTL cleanup.

    Tracks seen nonces to prevent replay attacks. Nonces expire after
    a configurable TTL (default: 1 hour).

    Thread-safe: Uses a lock for all operations.

    Attributes:
        ttl_seconds: Time-to-live for nonces in seconds.
        _nonces: Dict mapping nonce -> expiration timestamp.
        _lock: Threading lock for thread safety.
        _last_cleanup: Timestamp of last cleanup run.

    Example:
        >>> tracker = NonceTracker(ttl_seconds=3600)
        >>> tracker.check_and_add("nonce-123")  # First time
        True
        >>> tracker.check_and_add("nonce-123")  # Replay attempt
        False
    """

    def __init__(self, ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS):
        """
        Initialize the nonce tracker.

        Args:
            ttl_seconds: How long to remember nonces (default: 3600 = 1 hour).
        """
        self.ttl_seconds = ttl_seconds
        self._nonces: dict[str, float] = {}  # nonce -> expires_at
        self._lock = threading.Lock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # Run cleanup every 60 seconds max

    def _cleanup_expired(self) -> None:
        """Remove expired nonces. Must be called with lock held."""
        now = time.time()

        # Only run cleanup periodically to avoid overhead
        if now - self._last_cleanup < self._cleanup_interval:
            return

        self._last_cleanup = now
        expired = [
            nonce for nonce, expires_at in self._nonces.items() if expires_at <= now
        ]
        for nonce in expired:
            del self._nonces[nonce]

    def check_and_add(self, nonce: str) -> bool:
        """
        Check if a nonce is new and add it if so.

        Args:
            nonce: The nonce value to check.

        Returns:
            True if the nonce is new (not a replay), False if already seen.

        Thread Safety:
            This method is thread-safe.
        """
        with self._lock:
            self._cleanup_expired()
            now = time.time()

            # Check if nonce exists and is not expired
            if nonce in self._nonces:
                if self._nonces[nonce] > now:
                    return False  # Replay detected
                # Nonce expired, allow reuse
                del self._nonces[nonce]

            # Add new nonce
            self._nonces[nonce] = now + self.ttl_seconds
            return True

    def is_replay(self, nonce: str) -> bool:
        """
        Check if a nonce would be a replay.

        Args:
            nonce: The nonce value to check.

        Returns:
            True if this nonce has been seen recently, False otherwise.

        Note:
            Unlike check_and_add(), this does NOT add the nonce.
        """
        with self._lock:
            self._cleanup_expired()
            now = time.time()

            if nonce in self._nonces and self._nonces[nonce] > now:
                return True
            return False

    def clear(self) -> None:
        """Clear all tracked nonces."""
        with self._lock:
            self._nonces.clear()

    def size(self) -> int:
        """Return the current number of tracked nonces."""
        with self._lock:
            self._cleanup_expired()
            return len(self._nonces)


# -----------------------------------------------------------------------------
# Webhook Signature Verification
# -----------------------------------------------------------------------------


def verify_webhook_signature(
    payload: bytes, signature: str, secret: str, algorithm: str = "sha256"
) -> bool:
    """
    Verify a webhook signature using HMAC.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        payload: The raw request body as bytes.
        signature: The signature to verify (hex-encoded).
        secret: The shared secret key.
        algorithm: Hash algorithm - "sha256" (default) or "sha1" (legacy).

    Returns:
        True if signature is valid, False otherwise.

    Raises:
        ValueError: If algorithm is not supported.

    Security:
        - Uses hmac.compare_digest() for constant-time comparison
        - Prevents timing attacks that could reveal signature bits

    Example:
        >>> payload = b'{"event": "push"}'
        >>> secret = "my-webhook-secret"
        >>> # Compute expected signature
        >>> import hmac, hashlib
        >>> sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        >>> verify_webhook_signature(payload, sig, secret)
        True
    """
    if not signature:
        return False

    if not secret:
        return False

    algorithm = algorithm.lower()
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ValueError(
            f"Unsupported algorithm: {algorithm}. "
            f"Supported: {', '.join(sorted(SUPPORTED_ALGORITHMS))}"
        )

    # Select hash function
    if algorithm == "sha256":
        hash_func = hashlib.sha256
    else:  # sha1
        hash_func = hashlib.sha1

    # Compute expected signature
    expected = hmac.new(secret.encode("utf-8"), payload, hash_func).hexdigest()

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected.lower(), signature.lower())


def validate_timestamp(
    timestamp: int | float | str,
    max_age_seconds: int = DEFAULT_MAX_TIMESTAMP_AGE_SECONDS,
) -> tuple[bool, str]:
    """
    Validate that a webhook timestamp is within acceptable range.

    Rejects requests that are too old (replay) or too far in the future
    (clock skew attack).

    Args:
        timestamp: Unix timestamp (seconds since epoch) as int, float, or string.
        max_age_seconds: Maximum allowed age in seconds (default: 300 = 5 min).

    Returns:
        Tuple of (is_valid, error_message).
        If valid, error_message is empty string.

    Example:
        >>> import time
        >>> ts = int(time.time())
        >>> valid, error = validate_timestamp(ts)
        >>> valid
        True
        >>> # Old timestamp
        >>> valid, error = validate_timestamp(ts - 600)  # 10 min ago
        >>> valid
        False
    """
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False, f"Invalid timestamp format: {timestamp}"

    now = time.time()
    age = now - ts

    # Reject timestamps from the future (with 60s tolerance for clock skew)
    if age < -60:
        return False, f"Timestamp is {abs(age):.0f}s in the future"

    # Reject timestamps that are too old
    if age > max_age_seconds:
        return False, f"Timestamp is {age:.0f}s old (max: {max_age_seconds}s)"

    return True, ""


# -----------------------------------------------------------------------------
# Webhook Receiver Class
# -----------------------------------------------------------------------------


@dataclass
class WebhookReceiver:
    """
    Multi-service webhook receiver with signature verification.

    Handles incoming webhooks from GitHub, Slack, and other services.
    Validates signatures before parsing payloads.

    Attributes:
        github_secret: Shared secret for GitHub webhook verification.
        slack_secret: Slack signing secret for Slack webhook verification.
        enable_replay_protection: Whether to track nonces and timestamps.
        max_timestamp_age_seconds: Maximum age for webhook timestamps.
        nonce_tracker: Internal nonce tracker for replay protection.

    Example:
        >>> receiver = WebhookReceiver(
        ...     github_secret="whsec_...",
        ...     enable_replay_protection=True
        ... )
        >>> valid, data = receiver.parse_github_webhook(headers, body)
        >>> if valid:
        ...     print(f"Event: {data['event']}")
    """

    github_secret: str | None = None
    slack_secret: str | None = None
    enable_replay_protection: bool = False
    max_timestamp_age_seconds: int = DEFAULT_MAX_TIMESTAMP_AGE_SECONDS
    nonce_tracker: NonceTracker = field(default_factory=NonceTracker)

    def __post_init__(self):
        """Initialize nonce tracker if replay protection is enabled."""
        if self.enable_replay_protection and self.nonce_tracker is None:
            self.nonce_tracker = NonceTracker()

    def parse_github_webhook(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[bool, dict[str, Any]]:
        """
        Parse and verify a GitHub webhook.

        Validates the X-Hub-Signature-256 header using HMAC-SHA256.
        Falls back to X-Hub-Signature (SHA1) for legacy compatibility.

        Args:
            headers: Request headers (case-insensitive keys expected).
            body: Raw request body as bytes.

        Returns:
            Tuple of (is_valid, parsed_data).

            If valid:
                - is_valid: True
                - parsed_data: Dict with 'event', 'delivery', 'payload'

            If invalid:
                - is_valid: False
                - parsed_data: Dict with 'error' describing the failure

        Headers Used:
            - X-Hub-Signature-256: HMAC-SHA256 signature (preferred)
            - X-Hub-Signature: HMAC-SHA1 signature (legacy fallback)
            - X-GitHub-Event: Event type (e.g., "push", "pull_request")
            - X-GitHub-Delivery: Unique delivery ID (used for replay protection)

        Example:
            >>> headers = {
            ...     "X-Hub-Signature-256": "sha256=...",
            ...     "X-GitHub-Event": "push",
            ...     "X-GitHub-Delivery": "uuid-here"
            ... }
            >>> body = b'{"ref": "refs/heads/main", ...}'
            >>> valid, data = receiver.parse_github_webhook(headers, body)
        """
        # Normalize headers to lowercase
        headers_lower = {k.lower(): v for k, v in headers.items()}

        # Check for required secret
        if not self.github_secret:
            return False, {"error": "GitHub secret not configured"}

        # Get signature (prefer SHA256, fallback to SHA1)
        signature = None
        algorithm = "sha256"

        sig_256 = headers_lower.get("x-hub-signature-256", "")
        sig_1 = headers_lower.get("x-hub-signature", "")

        if sig_256.startswith(GITHUB_SIGNATURE_PREFIX):
            signature = sig_256[len(GITHUB_SIGNATURE_PREFIX) :]
            algorithm = "sha256"
        elif sig_1.startswith(GITHUB_SIGNATURE_LEGACY_PREFIX):
            signature = sig_1[len(GITHUB_SIGNATURE_LEGACY_PREFIX) :]
            algorithm = "sha1"

        if not signature:
            return False, {"error": "Missing or invalid signature header"}

        # Verify signature
        if not verify_webhook_signature(body, signature, self.github_secret, algorithm):
            return False, {"error": "Invalid signature"}

        # Get event metadata
        event = headers_lower.get("x-github-event", "unknown")
        delivery_id = headers_lower.get("x-github-delivery", "")

        # Replay protection using delivery ID as nonce
        if self.enable_replay_protection and delivery_id:
            if not self.nonce_tracker.check_and_add(f"github:{delivery_id}"):
                return False, {"error": "Replay detected: duplicate delivery ID"}

        # Parse payload
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return False, {"error": f"Failed to parse payload: {e}"}

        return True, {
            "event": event,
            "delivery": delivery_id,
            "payload": payload,
            "algorithm": algorithm,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }

    def parse_slack_webhook(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[bool, dict[str, Any]]:
        """
        Parse and verify a Slack webhook (Events API or Slash Commands).

        Validates using Slack's signature scheme:
        - Computes: HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
        - Compares with X-Slack-Signature header

        Args:
            headers: Request headers (case-insensitive keys expected).
            body: Raw request body as bytes.

        Returns:
            Tuple of (is_valid, parsed_data).

            If valid:
                - is_valid: True
                - parsed_data: Dict with 'payload', 'timestamp'

            If invalid:
                - is_valid: False
                - parsed_data: Dict with 'error' describing the failure

        Headers Used:
            - X-Slack-Request-Timestamp: Unix timestamp of request
            - X-Slack-Signature: Format "v0=<hex-signature>"

        Example:
            >>> headers = {
            ...     "X-Slack-Request-Timestamp": "1531420618",
            ...     "X-Slack-Signature": "v0=..."
            ... }
            >>> body = b'token=...'
            >>> valid, data = receiver.parse_slack_webhook(headers, body)
        """
        # Normalize headers to lowercase
        headers_lower = {k.lower(): v for k, v in headers.items()}

        # Check for required secret
        if not self.slack_secret:
            return False, {"error": "Slack secret not configured"}

        # Get timestamp
        timestamp = headers_lower.get("x-slack-request-timestamp", "")
        if not timestamp:
            return False, {"error": "Missing X-Slack-Request-Timestamp header"}

        # Validate timestamp (replay protection)
        if self.enable_replay_protection:
            valid, error = validate_timestamp(timestamp, self.max_timestamp_age_seconds)
            if not valid:
                return False, {"error": f"Timestamp validation failed: {error}"}

        # Get signature
        signature_header = headers_lower.get("x-slack-signature", "")
        if not signature_header.startswith(f"{SLACK_SIGNATURE_VERSION}="):
            return False, {"error": "Missing or invalid X-Slack-Signature header"}

        provided_signature = signature_header[len(f"{SLACK_SIGNATURE_VERSION}=") :]

        # Compute expected signature
        # Slack format: v0:{timestamp}:{body}
        sig_basestring = f"{SLACK_SIGNATURE_VERSION}:{timestamp}:{body.decode('utf-8')}"
        expected_signature = hmac.new(
            self.slack_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # Constant-time comparison
        if not hmac.compare_digest(expected_signature, provided_signature):
            return False, {"error": "Invalid signature"}

        # Nonce tracking using timestamp (Slack doesn't have delivery IDs)
        # Use timestamp + first 32 chars of body hash as nonce
        if self.enable_replay_protection:
            body_hash = hashlib.sha256(body).hexdigest()[:32]
            nonce = f"slack:{timestamp}:{body_hash}"
            if not self.nonce_tracker.check_and_add(nonce):
                return False, {"error": "Replay detected: duplicate request"}

        # Parse payload
        # Slack sends either JSON or form-urlencoded depending on event type
        body_str = body.decode("utf-8")
        payload: dict[str, Any] = {}

        try:
            # Try JSON first
            payload = json.loads(body_str)
        except json.JSONDecodeError:
            # Fall back to form-urlencoded
            from urllib.parse import parse_qs

            parsed = parse_qs(body_str)
            # parse_qs returns lists, flatten single values
            payload = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

        return True, {
            "payload": payload,
            "timestamp": timestamp,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }

    def verify_generic_webhook(
        self,
        payload: bytes,
        signature: str,
        secret: str,
        algorithm: str = "sha256",
        timestamp: int | float | str | None = None,
        nonce: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Verify a generic webhook with optional replay protection.

        Use this for custom webhook sources that don't match GitHub/Slack patterns.

        Args:
            payload: Raw request body as bytes.
            signature: The signature to verify (hex-encoded).
            secret: The shared secret key.
            algorithm: Hash algorithm ("sha256" or "sha1").
            timestamp: Optional Unix timestamp for age validation.
            nonce: Optional unique identifier for deduplication.

        Returns:
            Tuple of (is_valid, result_data).

        Example:
            >>> valid, data = receiver.verify_generic_webhook(
            ...     payload=body,
            ...     signature=headers["X-Signature"],
            ...     secret=webhook_secret,
            ...     timestamp=headers.get("X-Timestamp"),
            ...     nonce=headers.get("X-Request-ID")
            ... )
        """
        # Verify signature
        try:
            if not verify_webhook_signature(payload, signature, secret, algorithm):
                return False, {"error": "Invalid signature"}
        except ValueError as e:
            return False, {"error": str(e)}

        # Validate timestamp if provided
        if self.enable_replay_protection and timestamp is not None:
            valid, error = validate_timestamp(timestamp, self.max_timestamp_age_seconds)
            if not valid:
                return False, {"error": f"Timestamp validation failed: {error}"}

        # Check nonce if provided
        if self.enable_replay_protection and nonce:
            if not self.nonce_tracker.check_and_add(f"generic:{nonce}"):
                return False, {"error": "Replay detected: duplicate nonce"}

        return True, {
            "verified": True,
            "algorithm": algorithm,
            "timestamp_validated": timestamp is not None,
            "nonce_tracked": nonce is not None,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }


# -----------------------------------------------------------------------------
# Factory Function
# -----------------------------------------------------------------------------


def create_webhook_receiver(
    github_secret: str | None = None,
    slack_secret: str | None = None,
    enable_replay_protection: bool = True,
    max_timestamp_age_seconds: int = DEFAULT_MAX_TIMESTAMP_AGE_SECONDS,
) -> WebhookReceiver:
    """
    Create a WebhookReceiver with the specified configuration.

    This is the recommended way to create a receiver, as it provides
    sensible defaults for production use.

    Args:
        github_secret: GitHub webhook secret (from Settings > Webhooks).
        slack_secret: Slack signing secret (from App Settings > Basic Info).
        enable_replay_protection: Enable timestamp/nonce validation (default: True).
        max_timestamp_age_seconds: Max age for timestamps (default: 300 = 5 min).

    Returns:
        Configured WebhookReceiver instance.

    Example:
        >>> import os
        >>> receiver = create_webhook_receiver(
        ...     github_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        ...     slack_secret=os.environ.get("SLACK_SIGNING_SECRET"),
        ... )
    """
    return WebhookReceiver(
        github_secret=github_secret,
        slack_secret=slack_secret,
        enable_replay_protection=enable_replay_protection,
        max_timestamp_age_seconds=max_timestamp_age_seconds,
        nonce_tracker=NonceTracker(ttl_seconds=DEFAULT_NONCE_TTL_SECONDS),
    )


# -----------------------------------------------------------------------------
# Outgoing Connector Data Classes
# -----------------------------------------------------------------------------


@dataclass
class ConnectorResult:
    """Result from a connector operation."""

    success: bool
    service: str
    operation: str
    message: str
    status_code: int | None = None
    response_data: dict | None = None
    error: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    rate_limited: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        from dataclasses import asdict

        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class RateLimitBucket:
    """Token bucket for rate limiting."""

    service: str
    tokens: float
    max_tokens: float
    refill_rate: float  # tokens per second
    last_refill: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RateLimitBucket":
        """Create from dictionary."""
        return cls(
            service=data.get("service", "unknown"),
            tokens=data.get("tokens", 0),
            max_tokens=data.get("max_tokens", 60),
            refill_rate=data.get("refill_rate", 1.0),
            last_refill=data.get("last_refill", datetime.now(timezone.utc).isoformat()),
        )


# -----------------------------------------------------------------------------
# Path Utilities for Outgoing Connectors
# -----------------------------------------------------------------------------


def get_rate_limit_path() -> Path:
    """Get the rate limit state file path."""
    _ensure_imports()
    return _company_resolver.get_company_dir() / RATE_LIMIT_FILE


def get_audit_log_path() -> Path:
    """Get the audit log file path."""
    _ensure_imports()
    return _company_resolver.get_company_dir() / AUDIT_LOG_FILE


def ensure_company_dir():
    """Ensure the .company directory exists."""
    _ensure_imports()
    company_dir = _company_resolver.get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Credential Management
# -----------------------------------------------------------------------------


def load_credentials(service: str) -> dict | None:
    """
    Load credentials for a service from the secure secrets file.

    Credentials are loaded from ~/.config/forge/secrets.json and are
    NEVER cached beyond the function call. The file is read fresh each time.

    Args:
        service: The service name (github, slack, discord, generic_webhook)

    Returns:
        Dictionary with service credentials, or None if not found.

    Raises:
        No exceptions - returns None on any error.

    Security:
        - Logs access attempt (without logging secrets)
        - Never caches credentials
        - File must be user-readable only (mode 0600 recommended)
    """
    # Log access attempt (audit trail)
    _log_audit_event(
        event_type="credential_access",
        service=service,
        details={"action": "load_credentials", "secrets_path": str(SECRETS_PATH)},
    )

    if not SECRETS_PATH.exists():
        _log_audit_event(
            event_type="credential_access",
            service=service,
            details={"action": "load_credentials", "result": "file_not_found"},
        )
        return None

    try:
        # Check file permissions (warn if too permissive)
        mode = SECRETS_PATH.stat().st_mode
        if mode & 0o077:  # Group or other has any permission
            _log_audit_event(
                event_type="security_warning",
                service=service,
                details={
                    "warning": "secrets file has permissive permissions",
                    "mode": oct(mode),
                },
            )

        with open(SECRETS_PATH, encoding="utf-8") as f:
            all_secrets = json.load(f)

        service_creds = all_secrets.get("services", {}).get(service)

        if service_creds is None:
            _log_audit_event(
                event_type="credential_access",
                service=service,
                details={"action": "load_credentials", "result": "service_not_found"},
            )
            return None

        # Validate credential structure
        is_valid, errors = validate_service_config(service, service_creds)
        if not is_valid:
            _log_audit_event(
                event_type="credential_validation",
                service=service,
                details={
                    "action": "load_credentials",
                    "result": "validation_failed",
                    "error_count": len(errors),
                },
            )
            return None

        _log_audit_event(
            event_type="credential_access",
            service=service,
            details={"action": "load_credentials", "result": "success"},
        )

        return service_creds

    except json.JSONDecodeError as e:
        _log_audit_event(
            event_type="credential_access",
            service=service,
            details={
                "action": "load_credentials",
                "result": "json_error",
                "error_type": type(e).__name__,
            },
        )
        return None
    except (OSError, PermissionError) as e:
        _log_audit_event(
            event_type="credential_access",
            service=service,
            details={
                "action": "load_credentials",
                "result": "file_error",
                "error_type": type(e).__name__,
            },
        )
        return None


def validate_service_config(service: str, config: dict) -> tuple[bool, list[str]]:
    """
    Validate that a service configuration has all required fields.

    Args:
        service: The service name
        config: The configuration dictionary to validate

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    import re

    errors: list[str] = []

    if service not in SERVICE_REGISTRY:
        errors.append(f"Unknown service: {service}")
        return False, errors

    service_spec = SERVICE_REGISTRY[service]
    required_fields = service_spec.get("required_fields", [])

    for field_name in required_fields:
        if field_name not in config:
            errors.append(f"Missing required field: {field_name}")
        elif not config[field_name]:
            errors.append(f"Empty required field: {field_name}")

    # Validate URL patterns if specified
    url_pattern = service_spec.get("url_pattern")
    if url_pattern and "webhook_url" in config:
        if not re.match(url_pattern, config["webhook_url"]):
            errors.append(f"Invalid URL format for {service}")

    return len(errors) == 0, errors


# -----------------------------------------------------------------------------
# Audit Logging
# -----------------------------------------------------------------------------


def _log_audit_event(
    event_type: str,
    service: str,
    details: dict,
):
    """
    Log an audit event for external connector operations.

    All external calls are logged (excluding secrets).

    Args:
        event_type: Type of event (credential_access, api_call, rate_limit, etc.)
        service: The service name
        details: Event details (MUST NOT contain secrets)
    """
    ensure_company_dir()
    audit_path = get_audit_log_path()

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "service": service,
        "details": details,
    }

    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except (OSError, PermissionError):
        pass  # Silently fail audit logging to avoid breaking operations


# -----------------------------------------------------------------------------
# Rate Limiting
# -----------------------------------------------------------------------------


class RateLimiter:
    """
    Token bucket rate limiter with per-service buckets.

    Implements the token bucket algorithm for rate limiting:
    - Each service has a bucket with max_tokens capacity
    - Tokens are consumed on each request
    - Tokens refill over time at refill_rate
    """

    def __init__(self):
        """Initialize the rate limiter."""
        self.buckets: dict[str, RateLimitBucket] = {}
        self._load_state()

    def _load_state(self):
        """Load rate limit state from file."""
        rate_path = get_rate_limit_path()

        if not rate_path.exists():
            return

        try:
            with open(rate_path, encoding="utf-8") as f:
                data = json.load(f)
                for service, bucket_data in data.get("buckets", {}).items():
                    self.buckets[service] = RateLimitBucket.from_dict(bucket_data)
        except (json.JSONDecodeError, OSError):
            pass

    def _save_state(self):
        """Save rate limit state to file."""
        ensure_company_dir()
        rate_path = get_rate_limit_path()

        data = {
            "buckets": {s: b.to_dict() for s, b in self.buckets.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(rate_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _get_or_create_bucket(self, service: str) -> RateLimitBucket:
        """Get or create a bucket for a service."""
        if service not in self.buckets:
            # Get rate limit from registry or use default
            rate_limit = SERVICE_REGISTRY.get(service, {}).get(
                "rate_limit", DEFAULT_WEBHOOK_RATE_LIMIT
            )

            self.buckets[service] = RateLimitBucket(
                service=service,
                tokens=float(rate_limit),  # Start full
                max_tokens=float(rate_limit),
                refill_rate=rate_limit / 60.0,  # tokens per second
                last_refill=datetime.now(timezone.utc).isoformat(),
            )

        return self.buckets[service]

    def _refill_bucket(self, bucket: RateLimitBucket):
        """Refill tokens based on elapsed time."""
        now = datetime.now(timezone.utc)

        try:
            last_refill = datetime.fromisoformat(
                bucket.last_refill.replace("Z", "+00:00")
            )
            elapsed = (now - last_refill).total_seconds()
        except (ValueError, TypeError):
            elapsed = 0
            bucket.last_refill = now.isoformat()

        # Add tokens based on elapsed time
        new_tokens = bucket.tokens + (elapsed * bucket.refill_rate)
        bucket.tokens = min(new_tokens, bucket.max_tokens)
        bucket.last_refill = now.isoformat()

    def can_proceed(self, service: str) -> bool:
        """
        Check if a request can proceed for a service.

        Does not consume a token - use acquire() for that.

        Args:
            service: The service name

        Returns:
            True if the request can proceed
        """
        bucket = self._get_or_create_bucket(service)
        self._refill_bucket(bucket)
        return bucket.tokens >= 1.0

    def acquire(self, service: str) -> bool:
        """
        Attempt to acquire a token for a service.

        Consumes a token if available, returns False if rate limited.

        Args:
            service: The service name

        Returns:
            True if token acquired, False if rate limited
        """
        bucket = self._get_or_create_bucket(service)
        self._refill_bucket(bucket)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            self._save_state()

            _log_audit_event(
                event_type="rate_limit",
                service=service,
                details={
                    "action": "acquire",
                    "result": "success",
                    "tokens_remaining": round(bucket.tokens, 2),
                },
            )
            return True
        else:
            _log_audit_event(
                event_type="rate_limit",
                service=service,
                details={
                    "action": "acquire",
                    "result": "rate_limited",
                    "tokens_remaining": round(bucket.tokens, 2),
                },
            )
            return False

    def get_status(self, service: str) -> dict:
        """
        Get rate limit status for a service.

        Returns:
            Dictionary with rate limit status
        """
        bucket = self._get_or_create_bucket(service)
        self._refill_bucket(bucket)

        time_until_next = (
            0.0
            if bucket.tokens >= 1.0
            else round((1.0 - bucket.tokens) / bucket.refill_rate, 2)
        )

        return {
            "service": service,
            "tokens_available": round(bucket.tokens, 2),
            "max_tokens": bucket.max_tokens,
            "refill_rate_per_second": round(bucket.refill_rate, 4),
            "can_proceed": bucket.tokens >= 1.0,
            "time_until_next_token": time_until_next,
        }

    def reset(self, service: str):
        """Reset rate limit for a service (restore to full)."""
        bucket = self._get_or_create_bucket(service)
        bucket.tokens = bucket.max_tokens
        bucket.last_refill = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def configure(self, service: str, rate_per_minute: int):
        """
        Configure rate limit for a service.

        Args:
            service: The service name
            rate_per_minute: Requests allowed per minute
        """
        bucket = self._get_or_create_bucket(service)
        bucket.max_tokens = float(rate_per_minute)
        bucket.refill_rate = rate_per_minute / 60.0
        bucket.tokens = min(bucket.tokens, bucket.max_tokens)
        self._save_state()


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


# -----------------------------------------------------------------------------
# Base Connector Class
# -----------------------------------------------------------------------------


class ExternalConnector:
    """
    Base class for external service connectors.

    Provides common functionality for credential loading, rate limiting,
    and audit logging. Subclasses implement the service-specific send() method.
    """

    def __init__(self, service: str):
        """
        Initialize the connector.

        Args:
            service: The service name (must be in SUPPORTED_SERVICES)

        Raises:
            ValueError: If service is not supported
        """
        if service not in SUPPORTED_SERVICES:
            raise ValueError(
                f"Unsupported service: {service}. Supported: {SUPPORTED_SERVICES}"
            )

        self.service = service
        self.service_spec = SERVICE_REGISTRY[service]
        self._credentials: dict | None = None

    def _get_credentials(self) -> dict | None:
        """
        Get credentials for this service.

        Loads fresh credentials on each call (never cached).

        Returns:
            Credentials dictionary or None
        """
        # Always load fresh - never cache credentials
        return load_credentials(self.service)

    def validate(self) -> bool:
        """
        Validate that this connector can operate.

        Checks:
        - Credentials are available and valid
        - Rate limit allows operation

        Returns:
            True if connector is ready to use
        """
        creds = self._get_credentials()
        if creds is None:
            return False

        is_valid, _ = validate_service_config(self.service, creds)
        return is_valid

    def get_validation_details(self) -> dict:
        """
        Get detailed validation status.

        Returns:
            Dictionary with validation details
        """
        creds = self._get_credentials()
        has_credentials = creds is not None

        is_valid = False
        errors: list[str] = []

        if has_credentials:
            is_valid, errors = validate_service_config(self.service, creds)

        rate_limiter = get_rate_limiter()
        rate_status = rate_limiter.get_status(self.service)

        return {
            "service": self.service,
            "has_credentials": has_credentials,
            "credentials_valid": is_valid,
            "validation_errors": errors,
            "rate_limit_ok": rate_status["can_proceed"],
            "rate_status": rate_status,
            "ready": has_credentials and is_valid and rate_status["can_proceed"],
        }

    def send(self, payload: dict) -> ConnectorResult:
        """
        Send a payload to the external service.

        Implementations must:
        - Load credentials fresh (not cached)
        - Check rate limits
        - Log the operation (without secrets)
        - Return a ConnectorResult

        Args:
            payload: The payload to send

        Returns:
            ConnectorResult with operation outcome
        """
        raise NotImplementedError("Subclasses must implement send()")

    def _check_rate_limit(self) -> tuple[bool, ConnectorResult | None]:
        """
        Check and acquire rate limit token.

        Returns:
            Tuple of (can_proceed, error_result_if_blocked)
        """
        rate_limiter = get_rate_limiter()

        if not rate_limiter.acquire(self.service):
            return False, ConnectorResult(
                success=False,
                service=self.service,
                operation="rate_limit_check",
                message="Rate limit exceeded",
                rate_limited=True,
            )

        return True, None


# -----------------------------------------------------------------------------
# Concrete Connector Implementations
# -----------------------------------------------------------------------------


class GitHubConnector(ExternalConnector):
    """
    Connector for GitHub API with PR comment and status check support.

    Provides methods for:
    - Generic GitHub API requests (send)
    - Posting PR comments (post_pr_comment)
    - Setting commit statuses (set_commit_status)
    - Getting PR information (get_pr_info)

    Falls back to gh CLI when API token is unavailable.
    """

    def __init__(self):
        super().__init__("github")

    def _has_gh_cli(self) -> bool:
        """Check if gh CLI is available and authenticated."""
        import subprocess

        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _run_gh_command(
        self, args: list[str], timeout: int = 30
    ) -> tuple[bool, str, str]:
        """
        Run a gh CLI command.

        Args:
            args: Command arguments (without 'gh' prefix)
            timeout: Command timeout in seconds

        Returns:
            Tuple of (success, stdout, stderr)
        """
        import subprocess

        try:
            result = subprocess.run(
                ["gh"] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            return False, "", str(e)

    def send(self, payload: dict) -> ConnectorResult:
        """
        Send a request to GitHub API.

        Payload format:
            {
                "method": "GET" | "POST" | "PUT" | "DELETE",
                "endpoint": "/repos/owner/repo/issues",
                "data": {...}  # optional, for POST/PUT
            }
        """
        import urllib.error
        import urllib.request

        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation (without secrets)
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send",
                "method": payload.get("method", "GET"),
                "endpoint": payload.get("endpoint", "/"),
            },
        )

        # Make the request
        try:
            base_url = creds.get("base_url", "https://api.github.com")
            endpoint = payload.get("endpoint", "/")
            method = payload.get("method", "GET")
            url = f"{base_url}{endpoint}"

            request_data = None
            if method in ("POST", "PUT", "PATCH") and "data" in payload:
                request_data = json.dumps(payload["data"]).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=request_data,
                method=method,
                headers={
                    "Authorization": f"Bearer {creds['token']}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "forge-external-connector/1.0",
                },
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                return ConnectorResult(
                    success=True,
                    service=self.service,
                    operation="send",
                    message="Request successful",
                    status_code=response.status,
                    response_data=response_data,
                )

        except urllib.error.HTTPError as e:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message=f"HTTP error: {e.code}",
                status_code=e.code,
                error=str(e),
            )
        except Exception as e:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="Request failed",
                error=str(e),
            )

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> ConnectorResult:
        """
        Post a comment to a GitHub PR.

        Uses GitHub API with bearer token auth. Falls back to gh CLI if token unavailable.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number
            body: Comment body (markdown supported)

        Returns:
            ConnectorResult with operation outcome
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "post_pr_comment",
                "repo": repo,
                "pr_number": pr_number,
                "body_length": len(body),
            },
        )

        # Try API first
        creds = self._get_credentials()
        if creds is not None:
            result = self.send(
                {
                    "method": "POST",
                    "endpoint": f"/repos/{repo}/issues/{pr_number}/comments",
                    "data": {"body": body},
                }
            )
            if result.success:
                return ConnectorResult(
                    success=True,
                    service=self.service,
                    operation="post_pr_comment",
                    message="Comment posted via API",
                    status_code=result.status_code,
                    response_data=result.response_data,
                )

        # Fall back to gh CLI
        if self._has_gh_cli():
            success, stdout, stderr = self._run_gh_command(
                [
                    "pr",
                    "comment",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--body",
                    body,
                ]
            )
            if success:
                return ConnectorResult(
                    success=True,
                    service=self.service,
                    operation="post_pr_comment",
                    message="Comment posted via gh CLI",
                    response_data={"output": stdout},
                )
            else:
                return ConnectorResult(
                    success=False,
                    service=self.service,
                    operation="post_pr_comment",
                    message="gh CLI comment failed",
                    error=stderr,
                )

        return ConnectorResult(
            success=False,
            service=self.service,
            operation="post_pr_comment",
            message="No GitHub credentials or gh CLI available",
            error="Configure GitHub token in ~/.config/forge/secrets.json or authenticate gh CLI",
        )

    def set_commit_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str = "",
        target_url: str | None = None,
    ) -> ConnectorResult:
        """
        Set commit status (for CI integration).

        Args:
            repo: Repository in "owner/repo" format
            sha: Full commit SHA
            state: Status state - "pending", "success", "failure", or "error"
            context: Status context (e.g., "forge/security-validation")
            description: Short description (max 140 chars)
            target_url: Optional URL for details

        Returns:
            ConnectorResult with operation outcome
        """
        # Validate state
        valid_states = {"pending", "success", "failure", "error"}
        if state not in valid_states:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="set_commit_status",
                message=f"Invalid state: {state}",
                error=f"State must be one of: {', '.join(valid_states)}",
            )

        # Truncate description to GitHub's limit
        if len(description) > 140:
            description = description[:137] + "..."

        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "set_commit_status",
                "repo": repo,
                "sha": sha[:8],
                "state": state,
                "context": context,
            },
        )

        # Build payload
        data: dict[str, Any] = {
            "state": state,
            "context": context,
        }
        if description:
            data["description"] = description
        if target_url:
            data["target_url"] = target_url

        # Try API
        creds = self._get_credentials()
        if creds is not None:
            result = self.send(
                {
                    "method": "POST",
                    "endpoint": f"/repos/{repo}/statuses/{sha}",
                    "data": data,
                }
            )
            if result.success:
                return ConnectorResult(
                    success=True,
                    service=self.service,
                    operation="set_commit_status",
                    message=f"Status set to {state}",
                    status_code=result.status_code,
                    response_data=result.response_data,
                )
            else:
                return ConnectorResult(
                    success=False,
                    service=self.service,
                    operation="set_commit_status",
                    message="Failed to set status via API",
                    error=result.error,
                )

        # gh CLI doesn't have a direct status command, so we fail gracefully
        return ConnectorResult(
            success=False,
            service=self.service,
            operation="set_commit_status",
            message="GitHub token required for commit status",
            error="Configure GitHub token in ~/.config/forge/secrets.json",
        )

    def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any]:
        """
        Get PR details including title, author, status, and checks.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number

        Returns:
            Dict with PR information:
            {
                "success": bool,
                "title": str,
                "author": str,
                "state": str,
                "head_sha": str,
                "base_branch": str,
                "head_branch": str,
                "draft": bool,
                "mergeable": bool | None,
                "checks": [{"name": str, "status": str, "conclusion": str}],
                "error": str | None
            }
        """
        # Check rate limit (but don't block for read operations)
        rate_limiter = get_rate_limiter()
        rate_limiter.acquire(self.service)

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "get_pr_info",
                "repo": repo,
                "pr_number": pr_number,
            },
        )

        # Try API first
        creds = self._get_credentials()
        if creds is not None:
            result = self.send(
                {
                    "method": "GET",
                    "endpoint": f"/repos/{repo}/pulls/{pr_number}",
                }
            )
            if result.success and result.response_data:
                pr_data = result.response_data
                head_sha = pr_data.get("head", {}).get("sha", "")

                # Get check runs for the head commit
                checks = []
                if head_sha:
                    check_result = self.send(
                        {
                            "method": "GET",
                            "endpoint": f"/repos/{repo}/commits/{head_sha}/check-runs",
                        }
                    )
                    if check_result.success and check_result.response_data:
                        for check in check_result.response_data.get("check_runs", []):
                            checks.append(
                                {
                                    "name": check.get("name", "unknown"),
                                    "status": check.get("status", "unknown"),
                                    "conclusion": check.get("conclusion"),
                                }
                            )

                return {
                    "success": True,
                    "title": pr_data.get("title", ""),
                    "author": pr_data.get("user", {}).get("login", "unknown"),
                    "state": pr_data.get("state", "unknown"),
                    "head_sha": head_sha,
                    "base_branch": pr_data.get("base", {}).get("ref", "main"),
                    "head_branch": pr_data.get("head", {}).get("ref", ""),
                    "draft": pr_data.get("draft", False),
                    "mergeable": pr_data.get("mergeable"),
                    "checks": checks,
                    "error": None,
                }

        # Fall back to gh CLI
        if self._has_gh_cli():
            success, stdout, stderr = self._run_gh_command(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "title,author,state,headRefOid,baseRefName,headRefName,isDraft,mergeable,statusCheckRollup",
                ]
            )
            if success:
                try:
                    data = json.loads(stdout)
                    checks = []
                    for check in data.get("statusCheckRollup", []):
                        checks.append(
                            {
                                "name": check.get(
                                    "name", check.get("context", "unknown")
                                ),
                                "status": check.get(
                                    "status", check.get("state", "unknown")
                                ),
                                "conclusion": check.get("conclusion"),
                            }
                        )

                    return {
                        "success": True,
                        "title": data.get("title", ""),
                        "author": data.get("author", {}).get("login", "unknown"),
                        "state": data.get("state", "unknown"),
                        "head_sha": data.get("headRefOid", ""),
                        "base_branch": data.get("baseRefName", "main"),
                        "head_branch": data.get("headRefName", ""),
                        "draft": data.get("isDraft", False),
                        "mergeable": data.get("mergeable"),
                        "checks": checks,
                        "error": None,
                    }
                except json.JSONDecodeError:
                    pass

        return {
            "success": False,
            "title": "",
            "author": "",
            "state": "",
            "head_sha": "",
            "base_branch": "",
            "head_branch": "",
            "draft": False,
            "mergeable": None,
            "checks": [],
            "error": "No GitHub credentials or gh CLI available",
        }


class SlackConnector(ExternalConnector):
    """Connector for Slack webhooks with rich formatting and alert support."""

    # Severity color mapping (Slack attachment colors)
    SEVERITY_COLORS = {
        "critical": "danger",  # Red
        "warning": "warning",  # Yellow
        "info": "good",  # Green (default)
    }

    # Maximum retries for delivery (99% delivery target)
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 0.5

    def __init__(self):
        super().__init__("slack")

    def _make_request(
        self, webhook_url: str, message: dict, operation: str
    ) -> ConnectorResult:
        """
        Make HTTP request with retry logic for reliability.

        Implements 3-retry policy to achieve 99% delivery rate.
        Measures latency to ensure <2s delivery time.
        """
        import urllib.error
        import urllib.request

        start_time = time.time()
        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=json.dumps(message).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )

                with urllib.request.urlopen(req, timeout=10) as response:
                    response_text = response.read().decode("utf-8")
                    latency_ms = (time.time() - start_time) * 1000

                    return ConnectorResult(
                        success=True,
                        service=self.service,
                        operation=operation,
                        message="Message sent",
                        status_code=response.status,
                        response_data={
                            "response": response_text,
                            "latency_ms": round(latency_ms, 2),
                            "attempts": attempt + 1,
                        },
                    )

            except urllib.error.HTTPError as e:
                last_error = e
                # Don't retry on 4xx errors (client errors)
                if 400 <= e.code < 500:
                    return ConnectorResult(
                        success=False,
                        service=self.service,
                        operation=operation,
                        message=f"HTTP error: {e.code}",
                        status_code=e.code,
                        error=str(e),
                    )
                # Retry on 5xx errors
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY_SECONDS * (attempt + 1))

            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY_SECONDS * (attempt + 1))

        latency_ms = (time.time() - start_time) * 1000
        return ConnectorResult(
            success=False,
            service=self.service,
            operation=operation,
            message=f"Request failed after {self.MAX_RETRIES} attempts",
            error=str(last_error),
            response_data={
                "latency_ms": round(latency_ms, 2),
                "attempts": self.MAX_RETRIES,
            },
        )

    def send(self, payload: dict) -> ConnectorResult:
        """
        Send a message to Slack.

        Payload format:
            {
                "text": "Message text",
                "blocks": [...]  # optional, Slack block kit
            }
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation (without secrets)
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send",
                "has_text": "text" in payload,
                "has_blocks": "blocks" in payload,
            },
        )

        # Build message
        message: dict[str, Any] = {"text": payload.get("text", "")}

        if "blocks" in payload:
            message["blocks"] = payload["blocks"]
        if "channel" in creds:
            message["channel"] = creds["channel"]
        if "username" in creds:
            message["username"] = creds["username"]
        if "icon_emoji" in creds:
            message["icon_emoji"] = creds["icon_emoji"]

        return self._make_request(creds["webhook_url"], message, "send")

    def send_message(
        self, channel: str, text: str, blocks: list | None = None
    ) -> ConnectorResult:
        """
        Post message with optional Slack blocks for rich formatting.

        Args:
            channel: Channel name or ID (e.g., "#general" or "C1234567890")
            text: Fallback text for notifications and clients that don't support blocks
            blocks: Optional list of Slack Block Kit blocks for rich formatting

        Returns:
            ConnectorResult with operation outcome including latency metrics

        Example:
            >>> connector = SlackConnector()
            >>> result = connector.send_message(
            ...     channel="#alerts",
            ...     text="New deployment ready",
            ...     blocks=[
            ...         {"type": "section", "text": {"type": "mrkdwn", "text": "*Deploy*"}}
            ...     ]
            ... )
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send_message",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send_message",
                "channel": channel,
                "has_blocks": blocks is not None,
                "block_count": len(blocks) if blocks else 0,
            },
        )

        # Build message
        message: dict[str, Any] = {
            "text": text,
            "channel": channel,
        }

        if blocks:
            message["blocks"] = blocks

        if "username" in creds:
            message["username"] = creds["username"]
        if "icon_emoji" in creds:
            message["icon_emoji"] = creds["icon_emoji"]

        return self._make_request(creds["webhook_url"], message, "send_message")

    def send_alert(self, alert: dict) -> ConnectorResult:
        """
        Format and send alerts with severity-appropriate styling.

        Args:
            alert: Alert dictionary containing:
                - severity: "critical", "warning", or "info"
                - message: Alert message text
                - rule_id: Optional identifier for the alert rule
                - task_id: Optional task context
                - task_name: Optional task name
                - details: Optional additional details dict

        Returns:
            ConnectorResult with operation outcome

        Styling:
            - Critical: Red attachment, @channel mention
            - Warning: Yellow attachment
            - Info: Default styling (green)

        Example:
            >>> connector = SlackConnector()
            >>> result = connector.send_alert({
            ...     "severity": "critical",
            ...     "message": "Circuit breaker tripped",
            ...     "task_id": "task-123",
            ...     "details": {"consecutive_failures": 3}
            ... })
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send_alert",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        severity = alert.get("severity", "info").lower()
        message_text = alert.get("message", "Alert triggered")
        rule_id = alert.get("rule_id", "unknown")
        task_id = alert.get("task_id")
        task_name = alert.get("task_name")
        details = alert.get("details", {})

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send_alert",
                "severity": severity,
                "rule_id": rule_id,
                "has_task_context": task_id is not None,
            },
        )

        # Build attachment based on severity
        color = self.SEVERITY_COLORS.get(severity, "good")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build attachment fields
        fields: list[dict[str, Any]] = []
        if task_id:
            fields.append({"title": "Task ID", "value": task_id, "short": True})
        if task_name:
            fields.append({"title": "Task", "value": task_name, "short": True})
        if rule_id and rule_id != "unknown":
            fields.append({"title": "Rule", "value": rule_id, "short": True})

        # Add any additional details
        for key, value in details.items():
            fields.append(
                {
                    "title": key.replace("_", " ").title(),
                    "value": str(value),
                    "short": True,
                }
            )

        # Build the attachment
        attachment: dict[str, Any] = {
            "color": color,
            "text": message_text,
            "footer": f"Gatesmith Alert | {timestamp}",
            "mrkdwn_in": ["text"],
        }

        if fields:
            attachment["fields"] = fields

        # Build main text (with @channel for critical)
        if severity == "critical":
            main_text = "<!channel> :rotating_light: *CRITICAL ALERT*"
            attachment["title"] = "Critical Alert"
        elif severity == "warning":
            main_text = ":warning: *Warning Alert*"
            attachment["title"] = "Warning"
        else:
            main_text = ":information_source: *Info*"
            attachment["title"] = "Information"

        # Build full message
        slack_message: dict[str, Any] = {
            "text": main_text,
            "attachments": [attachment],
        }

        if "channel" in creds:
            slack_message["channel"] = creds["channel"]
        if "username" in creds:
            slack_message["username"] = creds["username"]
        if "icon_emoji" in creds:
            slack_message["icon_emoji"] = creds["icon_emoji"]

        return self._make_request(creds["webhook_url"], slack_message, "send_alert")


class DiscordConnector(ExternalConnector):
    """Connector for Discord webhooks with embed and alert support."""

    # Severity color mapping (Discord embed colors in decimal)
    SEVERITY_COLORS = {
        "critical": 15158332,  # Red (#E74C3C)
        "warning": 15105570,  # Yellow (#E67E22)
        "info": 3447003,  # Blue (#3498DB)
    }

    # Maximum retries for delivery (99% delivery target)
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 0.5

    def __init__(self):
        super().__init__("discord")

    def _make_request(
        self, webhook_url: str, message: dict, operation: str
    ) -> ConnectorResult:
        """
        Make HTTP request with retry logic for reliability.

        Implements 3-retry policy to achieve 99% delivery rate.
        Measures latency to ensure <2s delivery time.
        """
        import urllib.error
        import urllib.request

        start_time = time.time()
        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=json.dumps(message).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )

                with urllib.request.urlopen(req, timeout=10) as response:
                    # Discord returns 204 No Content on success
                    resp_text = (
                        response.read().decode("utf-8") if response.length else ""
                    )
                    latency_ms = (time.time() - start_time) * 1000

                    return ConnectorResult(
                        success=True,
                        service=self.service,
                        operation=operation,
                        message="Message sent",
                        status_code=response.status,
                        response_data={
                            "response": resp_text,
                            "latency_ms": round(latency_ms, 2),
                            "attempts": attempt + 1,
                        }
                        if resp_text
                        else {
                            "latency_ms": round(latency_ms, 2),
                            "attempts": attempt + 1,
                        },
                    )

            except urllib.error.HTTPError as e:
                last_error = e
                # Don't retry on 4xx errors (client errors)
                if 400 <= e.code < 500:
                    return ConnectorResult(
                        success=False,
                        service=self.service,
                        operation=operation,
                        message=f"HTTP error: {e.code}",
                        status_code=e.code,
                        error=str(e),
                    )
                # Retry on 5xx errors
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY_SECONDS * (attempt + 1))

            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY_SECONDS * (attempt + 1))

        latency_ms = (time.time() - start_time) * 1000
        return ConnectorResult(
            success=False,
            service=self.service,
            operation=operation,
            message=f"Request failed after {self.MAX_RETRIES} attempts",
            error=str(last_error),
            response_data={
                "latency_ms": round(latency_ms, 2),
                "attempts": self.MAX_RETRIES,
            },
        )

    def send(self, payload: dict) -> ConnectorResult:
        """
        Send a message to Discord.

        Payload format:
            {
                "content": "Message text",
                "embeds": [...]  # optional
            }
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation (without secrets)
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send",
                "has_content": "content" in payload,
                "has_embeds": "embeds" in payload,
            },
        )

        # Build message
        message: dict[str, Any] = {}
        if "content" in payload:
            message["content"] = payload["content"]
        if "embeds" in payload:
            message["embeds"] = payload["embeds"]
        if "username" in creds:
            message["username"] = creds["username"]
        if "avatar_url" in creds:
            message["avatar_url"] = creds["avatar_url"]

        return self._make_request(creds["webhook_url"], message, "send")

    def send_webhook(self, content: str, embeds: list | None = None) -> ConnectorResult:
        """
        Send webhook with optional Discord embeds for rich formatting.

        Args:
            content: Message content text
            embeds: Optional list of Discord embed objects for rich formatting

        Returns:
            ConnectorResult with operation outcome including latency metrics

        Example:
            >>> connector = DiscordConnector()
            >>> result = connector.send_webhook(
            ...     content="Alert notification",
            ...     embeds=[{
            ...         "title": "Task Complete",
            ...         "description": "Build finished successfully",
            ...         "color": 3066993  # Green
            ...     }]
            ... )
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send_webhook",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send_webhook",
                "has_embeds": embeds is not None,
                "embed_count": len(embeds) if embeds else 0,
            },
        )

        # Build message
        message: dict[str, Any] = {"content": content}

        if embeds:
            message["embeds"] = embeds

        if "username" in creds:
            message["username"] = creds["username"]
        if "avatar_url" in creds:
            message["avatar_url"] = creds["avatar_url"]

        return self._make_request(creds["webhook_url"], message, "send_webhook")

    def send_alert(self, alert: dict) -> ConnectorResult:
        """
        Format and send alerts with severity-appropriate color-coded embeds.

        Args:
            alert: Alert dictionary containing:
                - severity: "critical", "warning", or "info"
                - message: Alert message text
                - rule_id: Optional identifier for the alert rule
                - task_id: Optional task context
                - task_name: Optional task name
                - details: Optional additional details dict

        Returns:
            ConnectorResult with operation outcome

        Styling (color-coded embeds):
            - Critical: Red embed
            - Warning: Orange/yellow embed
            - Info: Blue embed (default)

        Example:
            >>> connector = DiscordConnector()
            >>> result = connector.send_alert({
            ...     "severity": "critical",
            ...     "message": "Circuit breaker tripped",
            ...     "task_id": "task-123",
            ...     "details": {"consecutive_failures": 3}
            ... })
        """
        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send_alert",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        severity = alert.get("severity", "info").lower()
        message_text = alert.get("message", "Alert triggered")
        rule_id = alert.get("rule_id", "unknown")
        task_id = alert.get("task_id")
        task_name = alert.get("task_name")
        details = alert.get("details", {})

        # Log the operation
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send_alert",
                "severity": severity,
                "rule_id": rule_id,
                "has_task_context": task_id is not None,
            },
        )

        # Build embed based on severity
        color = self.SEVERITY_COLORS.get(severity, self.SEVERITY_COLORS["info"])
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build embed fields
        fields: list[dict[str, Any]] = []
        if task_id:
            fields.append({"name": "Task ID", "value": task_id, "inline": True})
        if task_name:
            fields.append({"name": "Task", "value": task_name, "inline": True})
        if rule_id and rule_id != "unknown":
            fields.append({"name": "Rule", "value": rule_id, "inline": True})

        # Add any additional details
        for key, value in details.items():
            fields.append(
                {
                    "name": key.replace("_", " ").title(),
                    "value": str(value),
                    "inline": True,
                }
            )

        # Build title based on severity
        if severity == "critical":
            title = "CRITICAL ALERT"
            content = "@everyone **CRITICAL ALERT**"
        elif severity == "warning":
            title = "Warning Alert"
            content = "**Warning Alert**"
        else:
            title = "Information"
            content = "**Info**"

        # Build the embed
        embed: dict[str, Any] = {
            "title": title,
            "description": message_text,
            "color": color,
            "timestamp": timestamp,
            "footer": {"text": "Gatesmith Alert System"},
        }

        if fields:
            embed["fields"] = fields

        # Build full message
        discord_message: dict[str, Any] = {
            "content": content,
            "embeds": [embed],
        }

        if "username" in creds:
            discord_message["username"] = creds["username"]
        if "avatar_url" in creds:
            discord_message["avatar_url"] = creds["avatar_url"]

        return self._make_request(creds["webhook_url"], discord_message, "send_alert")


class GenericWebhookConnector(ExternalConnector):
    """Connector for generic HTTP webhooks."""

    def __init__(self):
        super().__init__("generic_webhook")

    def send(self, payload: dict) -> ConnectorResult:
        """
        Send a payload to a generic webhook.

        Payload format:
            {
                "method": "POST",  # optional, defaults to POST
                "data": {...}  # the actual payload to send
            }
        """
        import urllib.error
        import urllib.request

        # Check rate limit
        can_proceed, error_result = self._check_rate_limit()
        if not can_proceed:
            return error_result  # type: ignore

        # Load credentials
        creds = self._get_credentials()
        if creds is None:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="No credentials available",
                error="Credentials not found in ~/.config/forge/secrets.json",
            )

        # Log the operation (without secrets)
        _log_audit_event(
            event_type="api_call",
            service=self.service,
            details={
                "action": "send",
                "method": payload.get("method", "POST"),
            },
        )

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "forge-external-connector/1.0",
        }

        # Add custom headers from config
        if "headers" in creds:
            headers.update(creds["headers"])

        # Add bearer token if configured
        if "token" in creds:
            headers["Authorization"] = f"Bearer {creds['token']}"

        # Make the request
        try:
            method = payload.get("method", "POST")
            data = payload.get("data", {})

            req = urllib.request.Request(
                creds["webhook_url"],
                data=json.dumps(data).encode("utf-8") if data else None,
                method=method,
                headers=headers,
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                response_text = response.read().decode("utf-8")
                try:
                    response_data = json.loads(response_text) if response_text else None
                except json.JSONDecodeError:
                    response_data = {"response": response_text}

                return ConnectorResult(
                    success=True,
                    service=self.service,
                    operation="send",
                    message="Request successful",
                    status_code=response.status,
                    response_data=response_data,
                )

        except urllib.error.HTTPError as e:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message=f"HTTP error: {e.code}",
                status_code=e.code,
                error=str(e),
            )
        except Exception as e:
            return ConnectorResult(
                success=False,
                service=self.service,
                operation="send",
                message="Request failed",
                error=str(e),
            )


# -----------------------------------------------------------------------------
# Connector Factory
# -----------------------------------------------------------------------------


def get_connector(service: str) -> ExternalConnector:
    """
    Get a connector instance for a service.

    Args:
        service: The service name

    Returns:
        ExternalConnector instance

    Raises:
        ValueError: If service is not supported
    """
    connectors = {
        "github": GitHubConnector,
        "slack": SlackConnector,
        "discord": DiscordConnector,
        "generic_webhook": GenericWebhookConnector,
    }

    if service not in connectors:
        raise ValueError(
            f"Unsupported service: {service}. Supported: {list(connectors.keys())}"
        )

    return connectors[service]()


# -----------------------------------------------------------------------------
# Alert Dispatch Integration (notify.py compatibility)
# -----------------------------------------------------------------------------


def dispatch_to_external_services(
    alert: dict, services: list[str]
) -> list[ConnectorResult]:
    """
    Dispatch an alert to multiple external services.

    Takes an alert in the format used by notify.py and dispatches it to
    the specified external notification services (Slack, Discord, etc.).

    Args:
        alert: Alert dictionary containing:
            - severity: "critical", "warning", or "info"
            - message: Alert message text
            - rule_id: Optional identifier for the alert rule
            - task_id: Optional task context
            - task_name: Optional task name
            - details: Optional additional details dict
        services: List of service names to dispatch to (e.g., ["slack", "discord"])

    Returns:
        List of ConnectorResult objects, one per service attempted

    Example:
        >>> from external_connectors import dispatch_to_external_services
        >>> results = dispatch_to_external_services(
        ...     alert={
        ...         "severity": "critical",
        ...         "message": "Circuit breaker tripped",
        ...         "rule_id": "circuit_breaker",
        ...         "task_id": "task-123",
        ...         "details": {"consecutive_failures": 3}
        ...     },
        ...     services=["slack", "discord"]
        ... )
        >>> for r in results:
        ...     print(f"{r.service}: {'OK' if r.success else 'FAILED'}")

    Integration with notify.py:
        This function is designed to complement the desktop notification
        system in notify.py. Call it after send_alert() to also notify
        external services:

        >>> from notify import send_alert
        >>> from external_connectors import dispatch_to_external_services
        >>>
        >>> alert = {"severity": "critical", "message": "...", "rule_id": "..."}
        >>> if send_alert(alert):  # Desktop notification sent
        ...     dispatch_to_external_services(alert, ["slack", "discord"])
    """
    results: list[ConnectorResult] = []

    # Log the dispatch attempt
    _log_audit_event(
        event_type="alert_dispatch",
        service="dispatcher",
        details={
            "action": "dispatch_to_external_services",
            "services": services,
            "severity": alert.get("severity", "info"),
            "rule_id": alert.get("rule_id", "unknown"),
        },
    )

    for service_name in services:
        # Validate service name
        if service_name not in ["slack", "discord"]:
            results.append(
                ConnectorResult(
                    success=False,
                    service=service_name,
                    operation="dispatch_alert",
                    message=f"Service '{service_name}' does not support alerts",
                    error="Only slack and discord support send_alert()",
                )
            )
            continue

        try:
            connector = get_connector(service_name)

            # Both SlackConnector and DiscordConnector have send_alert()
            if hasattr(connector, "send_alert"):
                result = connector.send_alert(alert)
                results.append(result)
            else:
                results.append(
                    ConnectorResult(
                        success=False,
                        service=service_name,
                        operation="dispatch_alert",
                        message=f"Connector for '{service_name}' has no send_alert method",
                        error="Method not implemented",
                    )
                )

        except ValueError as e:
            # Service not supported
            results.append(
                ConnectorResult(
                    success=False,
                    service=service_name,
                    operation="dispatch_alert",
                    message="Service not supported",
                    error=str(e),
                )
            )
        except Exception as e:
            # Unexpected error
            results.append(
                ConnectorResult(
                    success=False,
                    service=service_name,
                    operation="dispatch_alert",
                    message="Dispatch failed",
                    error=str(e),
                )
            )

    # Log dispatch results
    successful = sum(1 for r in results if r.success)
    _log_audit_event(
        event_type="alert_dispatch",
        service="dispatcher",
        details={
            "action": "dispatch_complete",
            "services_attempted": len(services),
            "services_successful": successful,
            "services_failed": len(results) - successful,
        },
    )

    return results


# -----------------------------------------------------------------------------
# PR Integration Functions
# -----------------------------------------------------------------------------


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """
    Parse a GitHub PR URL to extract repo and PR number.

    Args:
        pr_url: GitHub PR URL (e.g., "https://github.com/owner/repo/pull/123")

    Returns:
        Tuple of (repo, pr_number) or None if URL is invalid
    """
    import re

    # Match GitHub PR URLs
    patterns = [
        r"github\.com/([^/]+/[^/]+)/pull/(\d+)",
        r"github\.com/([^/]+/[^/]+)/pulls/(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, pr_url)
        if match:
            return match.group(1), int(match.group(2))

    return None


def post_validation_comment(
    pr_url: str,
    validation_result: dict,
    set_status: bool = True,
) -> dict:
    """
    Post a security validation report as a PR comment and set commit status.

    This function is designed to integrate with pr_output_manager.py's
    ValidationResult dataclass.

    Args:
        pr_url: Full GitHub PR URL
        validation_result: Dictionary with validation results containing:
            - passed: bool
            - tests_passed: bool | None
            - secrets_clean: bool | None
            - lint_passed: bool | None
            - dependency_issues: list[str] | None
            - errors: list[str] | None
            - warnings: list[str] | None
            - report_markdown: str (the validation report table)
        set_status: Whether to also set commit status (default True)

    Returns:
        Dictionary with operation results:
        {
            "success": bool,
            "comment_posted": bool,
            "status_set": bool,
            "pr_url": str,
            "errors": list[str]
        }
    """
    errors: list[str] = []
    comment_posted = False
    status_set = False

    # Parse PR URL
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return {
            "success": False,
            "comment_posted": False,
            "status_set": False,
            "pr_url": pr_url,
            "errors": [f"Invalid PR URL: {pr_url}"],
        }

    repo, pr_number = parsed

    # Get GitHub connector
    connector = GitHubConnector()

    # Build comment body
    passed = validation_result.get("passed", False)
    report_md = validation_result.get("report_markdown", "")

    status_emoji = ":white_check_mark:" if passed else ":x:"
    status_text = "PASSED" if passed else "FAILED"

    comment_body = f"""## Gatesmith Security Validation {status_emoji}

**Status**: {status_text}

### Validation Report

{report_md}
"""

    # Add errors if any
    if validation_result.get("errors"):
        comment_body += "\n### Errors\n\n"
        for error in validation_result["errors"][:10]:  # Limit to 10 errors
            comment_body += f"- {error}\n"

    # Add warnings if any
    if validation_result.get("warnings"):
        comment_body += "\n### Warnings\n\n"
        for warning in validation_result["warnings"][:10]:  # Limit to 10 warnings
            comment_body += f"- {warning}\n"

    comment_body += """
---
*Generated by Gatesmith Security Validation Gate*
"""

    # Post comment
    comment_result = connector.post_pr_comment(repo, pr_number, comment_body)
    if comment_result.success:
        comment_posted = True
    else:
        errors.append(f"Failed to post comment: {comment_result.error}")

    # Set commit status if requested
    if set_status:
        # Get PR info to find head SHA
        pr_info = connector.get_pr_info(repo, pr_number)
        if pr_info.get("success") and pr_info.get("head_sha"):
            head_sha = pr_info["head_sha"]
            state = "success" if passed else "failure"
            description = (
                "All security checks passed" if passed else "Security validation failed"
            )

            status_result = connector.set_commit_status(
                repo=repo,
                sha=head_sha,
                state=state,
                context="forge/security-validation",
                description=description,
                target_url=None,
            )

            if status_result.success:
                status_set = True
            else:
                errors.append(f"Failed to set status: {status_result.error}")
        else:
            errors.append(
                f"Failed to get PR info: {pr_info.get('error', 'unknown error')}"
            )

    return {
        "success": comment_posted or status_set,
        "comment_posted": comment_posted,
        "status_set": status_set,
        "pr_url": pr_url,
        "errors": errors if errors else None,
    }


# -----------------------------------------------------------------------------
# CLI Interface (for testing)
# -----------------------------------------------------------------------------


def _parse_cli_args(args: list[str]) -> dict[str, Any]:
    """Parse command line arguments."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            key = arg[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                result[key] = args[i + 1]
                i += 2
            else:
                result[key] = True
                i += 1
        else:
            i += 1
    return result


def _print_help():
    """Print usage help."""
    print("""
External Connectors — secure bidirectional communication with external services

OUTGOING CONNECTOR COMMANDS:
    validate        Validate service configuration
    rate-status     Check rate limit status for a service
    test            Send a test payload (use --dry-run for safety)
    services        List supported services
    audit           Show recent audit log entries
    configure       Configure rate limits

GITHUB PR COMMANDS:
    pr-comment      Post a comment to a GitHub PR
    pr-status       Set commit status for a PR
    pr-info         Get PR details (title, author, checks)
    post-validation Post security validation report to PR

INCOMING WEBHOOK COMMANDS:
    verify          Verify a webhook signature
    test-nonce      Test the nonce tracker

Validate options:
    --service NAME     Service to validate (required)

Rate-status options:
    --service NAME     Service to check (required)

Test options:
    --service NAME     Service to test (required)
    --dry-run          Don't actually send, just validate (recommended)
    --payload JSON     JSON payload to send (optional)

Configure options:
    --service NAME         Service to configure (required)
    --rate-per-minute N    Requests per minute limit

Audit options:
    --lines N          Number of lines to show (default: 20)

PR comment options:
    --repo OWNER/REPO    Repository (required)
    --pr NUMBER          PR number (required)
    --body TEXT          Comment body (required)

PR status options:
    --repo OWNER/REPO    Repository (required)
    --sha COMMIT_SHA     Full commit SHA (required)
    --state STATE        pending|success|failure|error (required)
    --context NAME       Status context (default: forge/validation)
    --description TEXT   Short description (optional)

PR info options:
    --repo OWNER/REPO    Repository (required)
    --pr NUMBER          PR number (required)

Post validation options:
    --pr-url URL         Full PR URL (required)
    --passed             Validation passed (default: false)
    --report TEXT        Markdown report table (optional)

Verify options (for incoming webhooks):
    python external_connectors.py verify <payload> <signature> <secret> [algorithm]

Supported services:
    github           GitHub API with bearer token auth (PR comments, status)
    slack            Slack incoming webhooks
    discord          Discord webhooks
    generic_webhook  Custom HTTP webhooks

Credentials file: ~/.config/forge/secrets.json
Example format:
    {
      "services": {
        "github": {
          "token": "ghp_xxxxxxxxxxxx"
        },
        "slack": {
          "webhook_url": "https://hooks.slack.com/services/..."
        },
        "discord": {
          "webhook_url": "https://discord.com/api/webhooks/..."
        }
      }
    }

Security:
    - Credentials are NEVER stored in the repository
    - Credentials are loaded fresh on each request (never cached)
    - All operations are logged (without secrets)
    - Rate limiting is enforced per-service (60/min for GitHub)

Examples:
    # Validate GitHub credentials
    python external_connectors.py validate --service github

    # Check Slack rate limits
    python external_connectors.py rate-status --service slack

    # Test Discord webhook (dry-run)
    python external_connectors.py test --service discord --dry-run

    # List services
    python external_connectors.py services

    # Verify incoming webhook signature
    python external_connectors.py verify '{"event":"push"}' abc123 mysecret sha256
""")


def main():
    """CLI entry point for external connectors."""
    import sys

    if len(sys.argv) < 2:
        _print_help()
        sys.exit(0)

    command = sys.argv[1].lower()
    args = _parse_cli_args(sys.argv[2:])

    if command in ("help", "--help", "-h"):
        _print_help()
        sys.exit(0)

    try:
        # =====================================================================
        # OUTGOING CONNECTOR COMMANDS
        # =====================================================================

        if command == "validate":
            if "service" not in args:
                print("Error: --service required")
                sys.exit(1)

            service = args["service"]
            if service not in SUPPORTED_SERVICES:
                print(f"Error: Unknown service '{service}'")
                print(f"Supported: {SUPPORTED_SERVICES}")
                sys.exit(1)

            connector = get_connector(service)
            details = connector.get_validation_details()

            result = {
                "success": True,
                "command": "validate",
                "validation": details,
            }
            print(json.dumps(result, indent=2))

        elif command == "rate-status":
            if "service" not in args:
                print("Error: --service required")
                sys.exit(1)

            service = args["service"]
            rate_limiter = get_rate_limiter()
            status = rate_limiter.get_status(service)

            result = {
                "success": True,
                "command": "rate-status",
                "status": status,
            }
            print(json.dumps(result, indent=2))

        elif command == "test":
            if "service" not in args:
                print("Error: --service required")
                sys.exit(1)

            service = args["service"]
            dry_run = args.get("dry_run", False)

            connector = get_connector(service)

            # Validate first
            details = connector.get_validation_details()

            if not details["ready"]:
                result = {
                    "success": False,
                    "command": "test",
                    "error": "Connector not ready",
                    "validation": details,
                }
                print(json.dumps(result, indent=2))
                sys.exit(1)

            if dry_run:
                result = {
                    "success": True,
                    "command": "test",
                    "dry_run": True,
                    "message": "Validation passed, would send payload",
                    "validation": details,
                }
                print(json.dumps(result, indent=2))
            else:
                # Parse payload if provided
                payload: dict[str, Any] = {}
                if "payload" in args:
                    try:
                        payload = json.loads(args["payload"])
                    except json.JSONDecodeError as e:
                        print(f"Error: Invalid JSON in --payload: {e}")
                        sys.exit(1)

                # Use test payload if none provided
                if not payload:
                    ts = datetime.now(timezone.utc).isoformat()
                    payload = {"text": f"Test message from Gatesmith at {ts}"}

                send_result = connector.send(payload)
                result = {
                    "success": send_result.success,
                    "command": "test",
                    "result": send_result.to_dict(),
                }
                print(json.dumps(result, indent=2))

        elif command == "services":
            services = []
            for name, spec in SERVICE_REGISTRY.items():
                services.append(
                    {
                        "name": name,
                        "description": spec["description"],
                        "auth_type": spec["auth_type"],
                        "required_fields": spec["required_fields"],
                        "rate_limit": spec["rate_limit"],
                    }
                )

            result = {
                "success": True,
                "command": "services",
                "services": services,
                "count": len(services),
            }
            print(json.dumps(result, indent=2))

        elif command == "audit":
            lines = int(args.get("lines", 20))
            audit_path = get_audit_log_path()

            if not audit_path.exists():
                result = {
                    "success": True,
                    "command": "audit",
                    "entries": [],
                    "message": "No audit log found",
                }
            else:
                entries = []
                with open(audit_path, encoding="utf-8") as f:
                    all_lines = f.readlines()
                    for line in all_lines[-lines:]:
                        try:
                            entries.append(json.loads(line.strip()))
                        except json.JSONDecodeError:
                            pass

                result = {
                    "success": True,
                    "command": "audit",
                    "entries": entries,
                    "count": len(entries),
                    "total_in_log": len(all_lines),
                }

            print(json.dumps(result, indent=2))

        elif command == "configure":
            if "service" not in args:
                print("Error: --service required")
                sys.exit(1)

            service = args["service"]
            rate_limiter = get_rate_limiter()

            if "rate_per_minute" in args:
                try:
                    rate = int(args["rate_per_minute"])
                except ValueError:
                    print("Error: --rate-per-minute must be a number")
                    sys.exit(1)

                rate_limiter.configure(service, rate)

                result = {
                    "success": True,
                    "command": "configure",
                    "service": service,
                    "rate_per_minute": rate,
                    "message": f"Rate limit updated to {rate}/minute",
                }
                print(json.dumps(result, indent=2))
            else:
                print("Error: No configuration option provided")
                print("Available: --rate-per-minute N")
                sys.exit(1)

        # =====================================================================
        # GITHUB PR COMMANDS
        # =====================================================================

        elif command == "pr-comment":
            repo = args.get("repo")
            pr = args.get("pr")
            body = args.get("body")

            if not repo or not pr or not body:
                print("Error: --repo, --pr, and --body required")
                sys.exit(1)

            try:
                pr_number = int(pr)
            except ValueError:
                print("Error: --pr must be a number")
                sys.exit(1)

            connector = GitHubConnector()
            result = connector.post_pr_comment(repo, pr_number, body)
            print(json.dumps(result.to_dict(), indent=2))

        elif command == "pr-status":
            repo = args.get("repo")
            sha = args.get("sha")
            state = args.get("state")
            context = args.get("context", "forge/validation")
            description = args.get("description", "")

            if not repo or not sha or not state:
                print("Error: --repo, --sha, and --state required")
                sys.exit(1)

            connector = GitHubConnector()
            result = connector.set_commit_status(
                repo=repo,
                sha=sha,
                state=state,
                context=context,
                description=description,
            )
            print(json.dumps(result.to_dict(), indent=2))

        elif command == "pr-info":
            repo = args.get("repo")
            pr = args.get("pr")

            if not repo or not pr:
                print("Error: --repo and --pr required")
                sys.exit(1)

            try:
                pr_number = int(pr)
            except ValueError:
                print("Error: --pr must be a number")
                sys.exit(1)

            connector = GitHubConnector()
            result = connector.get_pr_info(repo, pr_number)
            print(json.dumps(result, indent=2))

        elif command == "post-validation":
            pr_url = args.get("pr_url")

            if not pr_url:
                print("Error: --pr-url required")
                sys.exit(1)

            # Build validation result from args
            passed = args.get("passed", False)
            if isinstance(passed, str):
                passed = passed.lower() in ("true", "1", "yes")

            report_md = args.get("report", "| Check | Result |\n|-------|--------|\n")

            validation_result = {
                "passed": passed,
                "report_markdown": report_md,
                "errors": None,
                "warnings": None,
            }

            result = post_validation_comment(pr_url, validation_result)
            print(json.dumps(result, indent=2))

        # =====================================================================
        # INCOMING WEBHOOK COMMANDS
        # =====================================================================

        elif command == "verify":
            if len(sys.argv) < 5:
                print(
                    "Error: verify requires <payload> <signature> <secret> [algorithm]"
                )
                sys.exit(1)

            payload = sys.argv[2].encode("utf-8")
            signature = sys.argv[3]
            secret = sys.argv[4]
            algorithm = sys.argv[5] if len(sys.argv) > 5 else "sha256"

            try:
                result_val = verify_webhook_signature(
                    payload, signature, secret, algorithm
                )
                print(
                    json.dumps(
                        {
                            "valid": result_val,
                            "algorithm": algorithm,
                            "payload_length": len(payload),
                        },
                        indent=2,
                    )
                )
            except ValueError as e:
                print(json.dumps({"error": str(e)}, indent=2))
                sys.exit(1)

        elif command == "test-nonce":
            tracker = NonceTracker(ttl_seconds=5)

            # Test basic functionality
            print("Testing nonce tracker...")

            nonce1 = "test-nonce-1"
            result1 = tracker.check_and_add(nonce1)
            print(f"  First add of '{nonce1}': {result1} (expected: True)")

            result2 = tracker.check_and_add(nonce1)
            print(f"  Second add of '{nonce1}': {result2} (expected: False)")

            nonce2 = "test-nonce-2"
            result3 = tracker.check_and_add(nonce2)
            print(f"  First add of '{nonce2}': {result3} (expected: True)")

            print(f"  Tracker size: {tracker.size()} (expected: 2)")

            print(
                "\nAll tests passed!"
                if (result1 and not result2 and result3)
                else "\nSome tests failed!"
            )

        else:
            print(f"Unknown command: {command}")
            _print_help()
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
