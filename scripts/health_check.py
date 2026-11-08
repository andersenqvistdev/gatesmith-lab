#!/usr/bin/env python3
"""Health check: prints OK to stdout and exits 0."""

import sys


def main() -> int:
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
