#!/bin/sh
# Health check: prints OK to stdout and exits 0.
# Exits non-zero if OK cannot be written, so a broken stdout is not reported as healthy.
set -eu

if ! printf 'OK\n'; then
    echo "health-check: failed to write status to stdout" >&2
    exit 1
fi
exit 0
