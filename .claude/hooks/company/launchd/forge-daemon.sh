#!/bin/bash
# Gatesmith Daemon wrapper script for launchd supervision.
# Resolves project root from its own location and launches the daemon in foreground mode.

set -euo pipefail

# Resolve the directory this script lives in (follow symlinks)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Project root is 4 levels up: launchd/ -> company/ -> hooks/ -> .claude/ -> PROJECT_ROOT
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# Critical: unset CLAUDECODE to prevent nested session failures.
# See MEMORY.md — when launched from inside a Claude Code session the env var
# causes every child invocation to fail immediately.
unset CLAUDECODE

# Ensure PATH includes Homebrew and system binaries
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

# Create logs directory if missing
mkdir -p "${PROJECT_ROOT}/.company/logs"

cd "${PROJECT_ROOT}"

exec python3 .claude/hooks/company/forge_daemon.py start --foreground
