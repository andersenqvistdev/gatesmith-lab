#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["flask>=3.0.0"]
# ///
"""
WS-067-001: Webhook Receiver

Standalone HTTP server to receive webhooks from Stripe, Sentry, and GitHub.
Verifies signatures and writes signals to .company/signals/ for processing.

Security:
- Verifies webhook signatures using HMAC-SHA256
- Rate limited per source
- Logs all webhook attempts

Usage:
    # Start server (default port 5100)
    python webhook_receiver.py

    # Custom port
    python webhook_receiver.py --port 8080

    # Test mode (no signature verification)
    python webhook_receiver.py --test

Environment variables:
    STRIPE_WEBHOOK_SECRET: Stripe webhook signing secret
    SENTRY_WEBHOOK_SECRET: Sentry webhook signing secret (optional)
    GITHUB_WEBHOOK_SECRET: GitHub webhook secret
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from flask import Flask, jsonify, request
except ImportError:
    print("Flask not installed. Run: uv pip install flask")
    raise

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("webhook_receiver")

app = Flask(__name__)

# Rate limiting state
_rate_limits: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 30  # max requests per window per source

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
COMPANY_DIR = PROJECT_ROOT / ".company"
SIGNALS_DIR = COMPANY_DIR / "signals"


def _check_rate_limit(source: str) -> bool:
    """Check if source is within rate limit."""
    now = time.time()
    if source not in _rate_limits:
        _rate_limits[source] = []

    # Remove old entries
    _rate_limits[source] = [
        t for t in _rate_limits[source] if now - t < RATE_LIMIT_WINDOW
    ]

    if len(_rate_limits[source]) >= RATE_LIMIT_MAX:
        return False

    _rate_limits[source].append(now)
    return True


def _verify_stripe_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Stripe webhook signature."""
    try:
        # Stripe signature format: t=timestamp,v1=signature
        parts = dict(p.split("=", 1) for p in signature.split(","))
        timestamp = parts.get("t", "")
        expected_sig = parts.get("v1", "")

        if not timestamp or not expected_sig:
            return False

        # Compute expected signature
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        computed_sig = hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_sig, expected_sig)
    except Exception as e:
        logger.debug(f"Stripe signature verification failed: {e}")
        return False


def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    try:
        # GitHub signature format: sha256=signature
        if not signature.startswith("sha256="):
            return False

        expected_sig = signature[7:]
        computed_sig = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_sig, expected_sig)
    except Exception as e:
        logger.debug(f"GitHub signature verification failed: {e}")
        return False


def _verify_sentry_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Sentry webhook signature (if configured)."""
    # Sentry signature verification varies by setup
    # For now, accept if secret matches header directly
    if not secret:
        return True  # No secret configured, accept all
    return hmac.compare_digest(signature, secret)


def _write_signal(source: str, event_type: str, data: dict[str, Any]) -> bool:
    """Write signal to JSONL file."""
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    signal_file = SIGNALS_DIR / f"{source}.jsonl"

    entry = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }

    try:
        with open(signal_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info(f"Signal written: {source}/{event_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to write signal: {e}")
        return False


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify(
        {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
    )


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events."""
    if not _check_rate_limit("stripe"):
        return jsonify({"error": "rate limited"}), 429

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")

    # Verify signature in production
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if secret and not app.config.get("TESTING"):
        if not _verify_stripe_signature(payload, signature, secret):
            logger.warning("Stripe webhook signature verification failed")
            return jsonify({"error": "invalid signature"}), 401

    try:
        event = json.loads(payload)
        event_type = event.get("type", "unknown")

        # Extract relevant data
        data = {
            "stripe_event_id": event.get("id"),
            "event_type": event_type,
        }

        # Add event-specific data
        obj = event.get("data", {}).get("object", {})
        if event_type.startswith("charge."):
            data["amount"] = obj.get("amount", 0)
            data["currency"] = obj.get("currency", "usd")
            data["customer"] = obj.get("customer")
            data["failure_message"] = obj.get("failure_message")
        elif event_type.startswith("subscription."):
            data["customer"] = obj.get("customer")
            data["status"] = obj.get("status")
        elif event_type.startswith("invoice."):
            data["amount"] = obj.get("amount_due", 0)
            data["customer"] = obj.get("customer")

        _write_signal("stripe", event_type, data)
        return jsonify({"received": True})

    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400


@app.route("/webhook/sentry", methods=["POST"])
def sentry_webhook():
    """Handle Sentry webhook events."""
    if not _check_rate_limit("sentry"):
        return jsonify({"error": "rate limited"}), 429

    payload = request.get_data()
    signature = request.headers.get("Sentry-Hook-Signature", "")

    # Verify signature if secret configured
    secret = os.environ.get("SENTRY_WEBHOOK_SECRET", "")
    if secret and not app.config.get("TESTING"):
        if not _verify_sentry_signature(payload, signature, secret):
            logger.warning("Sentry webhook signature verification failed")
            return jsonify({"error": "invalid signature"}), 401

    try:
        event = json.loads(payload)

        # Sentry sends different event structures
        action = event.get("action", "")
        data_obj = event.get("data", {})

        if action == "triggered" or "issue" in event:
            # Issue alert
            issue = data_obj.get("issue", event.get("issue", {}))
            data = {
                "event_type": "error",
                "title": issue.get("title", ""),
                "count": issue.get("count", 1),
                "culprit": issue.get("culprit", ""),
                "level": issue.get("level", "error"),
                "first_seen": issue.get("firstSeen"),
                "last_seen": issue.get("lastSeen"),
            }

            # Extract file info if available
            if issue.get("metadata", {}).get("filename"):
                data["file"] = issue["metadata"]["filename"]

            _write_signal("sentry", "error", data)

        elif action == "resolved":
            # Issue resolved - could decrease priority
            pass

        return jsonify({"received": True})

    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400


@app.route("/webhook/github", methods=["POST"])
def github_webhook():
    """Handle GitHub webhook events."""
    if not _check_rate_limit("github"):
        return jsonify({"error": "rate limited"}), 429

    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    # Verify signature
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret and not app.config.get("TESTING"):
        if not _verify_github_signature(payload, signature, secret):
            logger.warning("GitHub webhook signature verification failed")
            return jsonify({"error": "invalid signature"}), 401

    try:
        event = json.loads(payload)
        event_type = request.headers.get("X-GitHub-Event", "unknown")

        if event_type == "issues":
            action = event.get("action", "")
            issue = event.get("issue", {})

            if action in ("opened", "labeled"):
                data = {
                    "action": action,
                    "issue_number": issue.get("number"),
                    "title": issue.get("title", ""),
                    "body": issue.get("body", "")[:500],  # Truncate
                    "labels": issue.get("labels", []),
                    "user": issue.get("user", {}).get("login"),
                    "url": issue.get("html_url"),
                }
                _write_signal("github", f"issue.{action}", data)

        elif event_type == "issue_comment":
            action = event.get("action", "")
            if action == "created":
                issue = event.get("issue", {})
                comment = event.get("comment", {})
                data = {
                    "issue_number": issue.get("number"),
                    "issue_title": issue.get("title", ""),
                    "comment_body": comment.get("body", "")[:500],
                    "user": comment.get("user", {}).get("login"),
                }
                _write_signal("github", "issue.comment", data)

        elif event_type == "pull_request":
            action = event.get("action", "")
            pr = event.get("pull_request", {})

            if action in ("opened", "labeled", "review_requested"):
                data = {
                    "action": action,
                    "pr_number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "labels": pr.get("labels", []),
                    "user": pr.get("user", {}).get("login"),
                }
                _write_signal("github", f"pr.{action}", data)

        return jsonify({"received": True})

    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400


@app.route("/webhook/test", methods=["POST"])
def test_webhook():
    """Test endpoint for development."""
    if not app.config.get("TESTING"):
        return jsonify({"error": "test endpoint disabled"}), 403

    try:
        event = request.get_json()
        source = event.get("source", "test")
        event_type = event.get("event_type", "test_event")
        data = event.get("data", {})

        _write_signal(source, event_type, data)
        return jsonify({"received": True, "source": source, "event_type": event_type})

    except Exception as e:
        return jsonify({"error": str(e)}), 400


def create_app(testing: bool = False) -> Flask:
    """Create Flask app for testing or production."""
    app.config["TESTING"] = testing
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Webhook Receiver")
    parser.add_argument("--port", type=int, default=5100, help="Port to listen on")
    parser.add_argument("--test", action="store_true", help="Enable test mode")
    args = parser.parse_args()

    if args.test:
        app.config["TESTING"] = True
        logger.warning("Running in TEST MODE - signature verification disabled")

    # Ensure signals directory exists
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"Starting webhook receiver on port {args.port}")
    logger.info(f"Signals directory: {SIGNALS_DIR}")

    app.run(host="0.0.0.0", port=args.port, debug=False)
