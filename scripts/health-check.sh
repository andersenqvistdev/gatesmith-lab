#!/bin/sh
# Health check: prints OK to stdout and exits 0.
# With --json, prints {"status":"ok"} instead.
if [ "$1" = "--json" ]; then
    printf '{"status":"ok"}\n'
    exit 0
fi
printf 'OK\n'
exit 0
