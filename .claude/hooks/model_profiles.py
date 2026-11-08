# /// script
# requires-python = ">=3.10"
# ///
"""
Model Profiles — cost control from GSD.

Three profiles that determine which models to use for which roles:
- quality: Best models everywhere (expensive, for critical work)
- balanced: Smart mix (default)
- budget: Cost-efficient (for exploration, prototyping)

Usage: python model_profiles.py <profile> <role>
Returns the recommended model for that role in that profile.

Roles: planner, implementer, reviewer, checker
"""

import json
import sys

PROFILES = {
    "quality": {
        "planner": "claude-opus-5-5",
        "implementer": "claude-opus-5-5",
        "reviewer": "claude-sonnet-5-5",
        "checker": "claude-opus-5-5",
        "description": "Maximum quality. Use for production releases, security-critical code.",
    },
    "balanced": {
        "planner": "claude-opus-5-5",
        "implementer": "claude-sonnet-5-5",
        "reviewer": "claude-sonnet-5-5",
        "checker": "claude-sonnet-5-5",
        "description": "Smart mix. Default profile for most work.",
    },
    "budget": {
        "planner": "claude-sonnet-5-5",
        "implementer": "claude-sonnet-5-5",
        "reviewer": "claude-haiku-5-5",
        "checker": "claude-sonnet-5-5",
        "description": "Cost-efficient. Use for exploration, prototyping, simple changes.",
    },
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps(PROFILES, indent=2))
        sys.exit(0)

    profile = sys.argv[1]
    role = sys.argv[2] if len(sys.argv) > 2 else None

    if profile not in PROFILES:
        print(
            f"Unknown profile: {profile}. Use: quality, balanced, budget",
            file=sys.stderr,
        )
        sys.exit(1)

    if role:
        if role not in PROFILES[profile]:
            print(
                f"Unknown role: {role}. Use: planner, implementer, reviewer, checker",
                file=sys.stderr,
            )
            sys.exit(1)
        print(PROFILES[profile][role])
    else:
        print(json.dumps(PROFILES[profile], indent=2))
