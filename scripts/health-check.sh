#!/bin/sh
# Health check: prints OK to stdout and exits 0.
# Exits 1 (with a message on stderr) if OK cannot be written, so a broken
# stdout is never reported as healthy.
set -eu

if ! printf 'OK\n'; then
    printf 'health-check: failed to write OK to stdout\n' >&2
    exit 1
fi
exit 0
