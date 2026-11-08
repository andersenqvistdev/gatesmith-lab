# /// script
# requires-python = ">=3.10"
# ///
"""
Scale-Adaptive Intelligence — detects project complexity to adjust pipeline depth.
From BMAD: a bug fix shouldn't go through the same pipeline as a new product.

Called by commands to determine appropriate workflow depth.
Not a hook — a utility script used by other hooks/commands.

Complexity levels:
- trivial: typo fix, config change, small bug fix → skip planning, direct implement
- standard: single feature, moderate change → plan → build → review
- complex: multi-file feature, architectural change → discuss → plan(+checker) → build → gate → review
- epic: new product, major refactor → full pipeline with all phases
"""

import json
import os
import subprocess
import sys


def count_files_in_scope(description: str) -> int:
    """Estimate how many files will be touched based on keywords."""
    broad_keywords = [
        "refactor",
        "migrate",
        "redesign",
        "rewrite",
        "all",
        "every",
        "across",
    ]
    medium_keywords = ["feature", "add", "implement", "create", "integrate"]
    narrow_keywords = ["fix", "bug", "typo", "update", "change", "tweak", "config"]

    desc_lower = description.lower()

    if any(k in desc_lower for k in broad_keywords):
        return 20
    if any(k in desc_lower for k in medium_keywords):
        return 8
    if any(k in desc_lower for k in narrow_keywords):
        return 2
    return 5  # default


def detect_complexity(description: str) -> dict:
    """Detect project complexity from task description and project state."""

    estimated_files = count_files_in_scope(description)

    # Check project size
    total_src_files = 0
    try:
        result = subprocess.run(
            [
                "find",
                ".",
                "-name",
                "*.py",
                "-o",
                "-name",
                "*.ts",
                "-o",
                "-name",
                "*.js",
                "-o",
                "-name",
                "*.go",
                "-o",
                "-name",
                "*.rs",
                "-o",
                "-name",
                "*.java",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.getcwd(),
        )
        total_src_files = len(
            [line for line in result.stdout.strip().split("\n") if line]
        )
    except Exception:
        total_src_files = 0

    # Check for architectural keywords
    desc_lower = description.lower()
    is_architectural = any(
        k in desc_lower
        for k in [
            "architect",
            "database",
            "schema",
            "migration",
            "api design",
            "authentication",
            "authorization",
            "infrastructure",
            "deploy",
            "microservice",
            "monolith",
            "redesign",
            "new project",
        ]
    )

    is_security_sensitive = any(
        k in desc_lower
        for k in [
            "auth",
            "password",
            "token",
            "encrypt",
            "secret",
            "permission",
            "rbac",
            "oauth",
            "jwt",
            "payment",
            "credit card",
            "pii",
        ]
    )

    # Determine complexity level
    if estimated_files <= 2 and not is_architectural and not is_security_sensitive:
        level = "trivial"
    elif estimated_files <= 10 and not is_architectural:
        level = "standard"
    elif estimated_files <= 20 or is_architectural:
        level = "complex"
    else:
        level = "epic"

    # Adjust up if security-sensitive
    if is_security_sensitive and level in ("trivial", "standard"):
        level = "complex"

    # Recommended pipeline
    pipelines = {
        "trivial": ["implement", "review"],
        "standard": ["plan", "implement", "review", "test"],
        "complex": [
            "discuss",
            "plan",
            "check-plan",
            "implement",
            "gate",
            "review",
            "test",
        ],
        "epic": [
            "discuss",
            "plan",
            "check-plan",
            "implement",
            "gate",
            "review",
            "test",
            "security-audit",
        ],
    }

    return {
        "level": level,
        "estimated_files": estimated_files,
        "total_src_files": total_src_files,
        "is_architectural": is_architectural,
        "is_security_sensitive": is_security_sensitive,
        "pipeline": pipelines[level],
    }


if __name__ == "__main__":
    desc = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else os.environ.get("TASK_DESCRIPTION", "")
    )
    if not desc:
        # Read from stdin
        desc = sys.stdin.read().strip()

    result = detect_complexity(desc)
    print(json.dumps(result, indent=2))
