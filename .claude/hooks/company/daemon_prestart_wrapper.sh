#!/bin/bash
# CI-health invariant for the Gatesmith daemon (Issue #955).
#
# Refuses to start the daemon when main branch CI is red. Fail-open:
# if the check itself errors (gh unavailable, network blip, rate-limited),
# the daemon is allowed to start — the invariant exists to stop the
# daemon from churning out PRs that cannot merge, not to block work
# whenever external tooling is flaky.
#
# Wired in via ~/Library/LaunchAgents/dev.gatesmith.daemon.tasks-*.plist
# as the first entry in ProgramArguments; the real daemon command is
# passed as "$@" and exec'd on success.
#
# To skip the check (manual debug): FORGE_SKIP_CI_CHECK=1 <command>

set -u

LOG_TAG="forgedaemon.prestart"

log() {
    # Emit to stderr for LaunchAgent StandardErrorPath + also to syslog
    # so it shows in `log stream --predicate 'subsystem contains "forgedaemon"'`
    echo "[prestart] $*" >&2
    if command -v logger >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "$*"
    fi
}

if [ "${FORGE_SKIP_CI_CHECK:-}" = "1" ]; then
    log "FORGE_SKIP_CI_CHECK=1 — bypassing main-CI health check"
    exec "$@"
fi

if ! command -v gh >/dev/null 2>&1; then
    log "gh CLI not on PATH — fail-open, allowing daemon to start"
    exec "$@"
fi

# Query latest workflow run on main. Timeout quickly so a hung gh call
# doesn't block daemon startup indefinitely.
#
# Fields:
#   status:     queued | in_progress | completed
#   conclusion: success | failure | cancelled | "" (empty while in_progress)
RUN_JSON=$(timeout 20 gh run list --branch main --limit 1 --json conclusion,status 2>/dev/null || echo "")

if [ -z "$RUN_JSON" ] || [ "$RUN_JSON" = "[]" ]; then
    log "no main CI runs visible — fail-open, allowing daemon to start"
    exec "$@"
fi

# Parse with python (ubiquitous on macOS, jq-free)
STATUS=$(printf '%s' "$RUN_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0].get("status","") if d else "")' 2>/dev/null || echo "")
CONCLUSION=$(printf '%s' "$RUN_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0].get("conclusion","") if d else "")' 2>/dev/null || echo "")

if [ -z "$STATUS" ]; then
    log "could not parse CI status — fail-open, allowing daemon to start"
    exec "$@"
fi

if [ "$STATUS" != "completed" ]; then
    log "main CI still $STATUS (conclusion=$CONCLUSION) — allowing daemon to start"
    exec "$@"
fi

# status=completed: now conclusion is authoritative
case "$CONCLUSION" in
    success)
        log "main CI green — proceeding with daemon start"
        exec "$@"
        ;;
    failure|cancelled|timed_out|action_required|startup_failure)
        log "main CI $CONCLUSION — REFUSING to start daemon (Issue #955 invariant)"
        log "resume: fix main CI, verify with 'gh run list --branch main --limit 1', then the next LaunchAgent cycle will pass"
        exit 1
        ;;
    *)
        log "unknown main CI conclusion '$CONCLUSION' — fail-open, allowing daemon to start"
        exec "$@"
        ;;
esac
