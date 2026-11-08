#!/bin/sh
# Prints the project version read from the VERSION file at the repo root, verbatim, and exits 0.
# Exits 1 with a message on stderr if VERSION cannot be read.

ROOT=$(cd "$(dirname "$0")/.." && pwd) || exit 1
VERSION_FILE="$ROOT/VERSION"

if [ ! -r "$VERSION_FILE" ]; then
    echo "error: cannot read $VERSION_FILE" >&2
    exit 1
fi

cat "$VERSION_FILE"
exit 0
