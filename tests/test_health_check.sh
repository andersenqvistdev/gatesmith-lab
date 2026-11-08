#!/bin/sh
# Tests scripts/health-check.sh: stdout must be exactly "OK\n" and exit code must be 0.
# Dependency-free POSIX sh. Exits 0 on pass, 1 on fail.

ROOT=$(cd "$(dirname "$0")/.." && pwd) || exit 1
SCRIPT="$ROOT/scripts/health-check.sh"

OUT_FILE=$(mktemp) || exit 1
EXPECTED_FILE=$(mktemp) || { rm -f "$OUT_FILE"; exit 1; }
trap 'rm -f "$OUT_FILE" "$EXPECTED_FILE"' EXIT INT TERM

printf 'OK\n' > "$EXPECTED_FILE"

"$SCRIPT" > "$OUT_FILE"
status=$?

failed=0

if [ "$status" -ne 0 ]; then
    echo "FAIL: exit code is $status, expected 0"
    failed=1
fi

if ! cmp -s "$OUT_FILE" "$EXPECTED_FILE"; then
    echo "FAIL: stdout is not exactly 'OK' followed by a newline"
    echo "  got: $(od -An -c "$OUT_FILE" | tr -s ' ')"
    failed=1
fi

if [ "$failed" -ne 0 ]; then
    exit 1
fi

echo "PASS: health-check prints OK and exits 0"
exit 0
