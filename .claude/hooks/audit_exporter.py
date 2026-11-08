# /// script
# requires-python = ">=3.10"
# ///
"""
Audit Exporter: Generate compliance-ready audit packages.

Aggregates:
- Activity logs (tool invocations)
- Session logs (session metadata)
- Prompt logs (prompt guard triggers)
- Gate logs (human approvals)
- Security configuration
- SBOM (optional)

Outputs JSON and/or CSV formats with manifest for integrity verification.
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def find_project_root() -> Path:
    """Find project root by looking for .claude directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".claude").is_dir():
            return parent
    return cwd


PROJECT_ROOT = find_project_root()
LOGS_DIR = PROJECT_ROOT / "logs"
CLAUDE_DIR = PROJECT_ROOT / ".claude"


def parse_date(date_str: str) -> datetime:
    """Parse YYYY-MM-DD date string to datetime."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def get_default_dates() -> tuple[str, str]:
    """Get default date range (30 days ago to today)."""
    today = datetime.now(timezone.utc)
    thirty_days_ago = today - timedelta(days=30)
    return thirty_days_ago.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def load_jsonl(file_path: Path, start_date: datetime, end_date: datetime) -> list[dict]:
    """Load JSONL file and filter by date range."""
    if not file_path.exists():
        return []

    records = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                # Try different timestamp fields
                ts_field = None
                for field in ["timestamp", "session_start", "created_at", "time"]:
                    if field in record:
                        ts_field = field
                        break

                if ts_field:
                    ts_str = record[ts_field]
                    # Parse ISO format timestamp
                    if "T" in ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if start_date <= ts <= end_date + timedelta(days=1):
                            records.append(record)
                else:
                    # No timestamp, include it
                    records.append(record)
            except (json.JSONDecodeError, ValueError):
                continue

    return records


def redact_paths(data: Any, project_root: str) -> Any:
    """Recursively redact absolute paths in data structure."""
    if isinstance(data, dict):
        return {k: redact_paths(v, project_root) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_paths(item, project_root) for item in data]
    elif isinstance(data, str):
        # Replace absolute project path with relative
        if project_root in data:
            return data.replace(project_root, ".")
        # Redact other absolute paths
        if data.startswith("/Users/") or data.startswith("/home/"):
            return re.sub(r"^(/Users/|/home/)[^/]+/", "~/", data)
        return data
    return data


def sha256_file(file_path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def load_dangerous_patterns(hook_path: Path) -> list[str]:
    """Return DANGEROUS_PATTERNS from block_dangerous.py by loading the module.

    The module is executed under a private name and not registered in
    sys.modules, so it neither shadows nor is shadowed by a real import; its
    main() sits behind ``if __name__ == "__main__"`` and does not run.
    """
    spec = importlib.util.spec_from_file_location("_forge_block_dangerous", hook_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [str(getattr(p, "pattern", p)) for p in module.DANGEROUS_PATTERNS]


def get_security_config() -> dict:
    """Extract security configuration from Gatesmith setup."""
    config: dict[str, Any] = {
        "hooks": {"total": 0, "by_event": {}},
        "trust_tier": "unknown",
        "autonomy_level": "unknown",
        "flow_mode_enabled": False,
        "protected_branches": [],
        "forbidden_patterns": [],
    }

    # Count hooks from settings.json
    settings_file = CLAUDE_DIR / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                settings = json.load(f)
                hooks = settings.get("hooks", {})
                for event, hook_list in hooks.items():
                    if isinstance(hook_list, list):
                        config["hooks"]["by_event"][event] = len(hook_list)
                        config["hooks"]["total"] += len(hook_list)
        except (json.JSONDecodeError, KeyError):
            pass

    # Get forge config
    forge_config_file = CLAUDE_DIR / "gatesmith.json"
    if forge_config_file.exists():
        try:
            with open(forge_config_file) as f:
                forge_config = json.load(f)
                autonomy = forge_config.get("autonomy", {})
                config["autonomy_level"] = autonomy.get("level", "unknown")
                flow_mode = autonomy.get("flowMode", {})
                config["flow_mode_enabled"] = flow_mode.get("enabled", False)

                security = forge_config.get("security", {})
                config["trust_tier"] = security.get("trustTier", "unknown")
                config["protected_branches"] = security.get("protectedBranches", [])
        except (json.JSONDecodeError, KeyError):
            pass

    # Get forbidden patterns from block_dangerous.py. Load the module and read
    # DANGEROUS_PATTERNS directly: the entries are composed from fragment
    # constants at import time, so a regex over the source text yields
    # fragments (and the former 2000-character slice yielded nothing at all).
    block_dangerous = CLAUDE_DIR / "hooks" / "block_dangerous.py"
    if block_dangerous.exists():
        try:
            patterns = load_dangerous_patterns(block_dangerous)
            config["forbidden_patterns"] = patterns[:10]  # First 10 patterns
        except Exception:
            pass

    return config


def generate_sbom(output_dir: Path) -> dict | None:
    """Generate SBOM using syft if available."""
    sbom_generator = CLAUDE_DIR / "hooks" / "sbom_generator.py"
    if not sbom_generator.exists():
        return None

    sbom_output = output_dir / "sbom.json"
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                str(sbom_generator),
                "--format",
                "cyclonedx",
                "--output",
                str(sbom_output),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and sbom_output.exists():
            with open(sbom_output) as f:
                sbom_data = json.load(f)
                components = sbom_data.get("components", [])
                return {
                    "generated": True,
                    "file": "sbom.json",
                    "components": len(components),
                    "format": "CycloneDX",
                }
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    return None


def records_to_csv(records: list[dict], output_file: Path) -> None:
    """Convert records to CSV format."""
    if not records:
        return

    # Get all unique keys across all records
    all_keys: set[str] = set()
    for record in records:
        all_keys.update(record.keys())
    fieldnames = sorted(all_keys)

    with open(output_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            # Flatten nested structures for CSV
            flat_record = {}
            for k, v in record.items():
                if isinstance(v, (dict, list)):
                    flat_record[k] = json.dumps(v)
                else:
                    flat_record[k] = v
            writer.writerow(flat_record)


def export_audit(
    start_date: str,
    end_date: str,
    output_format: str,
    output_dir: Path,
    include_sbom: bool,
    redact: bool,
) -> dict:
    """Main export function."""
    start_dt = parse_date(start_date)
    end_dt = parse_date(end_date)
    project_root_str = str(PROJECT_ROOT)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "forge-audit-export",
        "period": {"start": start_date, "end": end_date},
        "files": [],
        "summary": {
            "total_events": 0,
            "sessions": 0,
            "blocked_actions": 0,
            "gate_approvals": 0,
            "unique_tools": set(),
        },
    }

    # Load and export activity logs
    activity_records = load_jsonl(LOGS_DIR / "activity.jsonl", start_dt, end_dt)
    if redact:
        activity_records = redact_paths(activity_records, project_root_str)

    manifest["summary"]["total_events"] += len(activity_records)
    for record in activity_records:
        if "tool" in record:
            manifest["summary"]["unique_tools"].add(record["tool"])

    if output_format in ("json", "both"):
        activity_file = output_dir / "activity-log.json"
        with open(activity_file, "w") as f:
            json.dump(activity_records, f, indent=2, default=str)
        manifest["files"].append(
            {
                "name": "activity-log.json",
                "sha256": sha256_file(activity_file),
                "records": len(activity_records),
            }
        )

    if output_format in ("csv", "both"):
        activity_csv = output_dir / "activity-log.csv"
        records_to_csv(activity_records, activity_csv)
        manifest["files"].append(
            {
                "name": "activity-log.csv",
                "sha256": sha256_file(activity_csv),
                "records": len(activity_records),
            }
        )

    # Load and export session logs
    session_records = load_jsonl(LOGS_DIR / "sessions.jsonl", start_dt, end_dt)
    if redact:
        session_records = redact_paths(session_records, project_root_str)

    manifest["summary"]["sessions"] = len(session_records)

    if output_format in ("json", "both"):
        sessions_file = output_dir / "sessions.json"
        with open(sessions_file, "w") as f:
            json.dump(session_records, f, indent=2, default=str)
        manifest["files"].append(
            {
                "name": "sessions.json",
                "sha256": sha256_file(sessions_file),
                "records": len(session_records),
            }
        )

    if output_format in ("csv", "both"):
        sessions_csv = output_dir / "sessions.csv"
        records_to_csv(session_records, sessions_csv)
        manifest["files"].append(
            {
                "name": "sessions.csv",
                "sha256": sha256_file(sessions_csv),
                "records": len(session_records),
            }
        )

    # Load and export prompt logs
    prompt_records = load_jsonl(LOGS_DIR / "prompts.jsonl", start_dt, end_dt)
    if redact:
        prompt_records = redact_paths(prompt_records, project_root_str)

    manifest["summary"]["blocked_actions"] += sum(
        1 for r in prompt_records if r.get("blocked", False) or r.get("exit_code") == 2
    )

    if prompt_records:
        if output_format in ("json", "both"):
            prompts_file = output_dir / "prompts.json"
            with open(prompts_file, "w") as f:
                json.dump(prompt_records, f, indent=2, default=str)
            manifest["files"].append(
                {
                    "name": "prompts.json",
                    "sha256": sha256_file(prompts_file),
                    "records": len(prompt_records),
                }
            )

    # Load and export gate logs
    gate_records = load_jsonl(LOGS_DIR / "gates.jsonl", start_dt, end_dt)
    if redact:
        gate_records = redact_paths(gate_records, project_root_str)

    manifest["summary"]["gate_approvals"] = sum(
        1
        for r in gate_records
        if r.get("approved", False) or r.get("decision") == "approved"
    )
    # Automated hook denials share gates.jsonl with human /gate approvals and are
    # told apart by `source`. Counted separately so the approval figure keeps its
    # meaning — and so a reviewer can see at a glance that anything was blocked
    # at all. Until hooks began recording, every denial vanished with the session.
    manifest["summary"]["hook_blocks"] = sum(
        1
        for r in gate_records
        if r.get("source") == "hook" and r.get("decision") == "block"
    )
    manifest["summary"]["hook_warnings"] = sum(
        1
        for r in gate_records
        if r.get("source") == "hook" and r.get("decision") == "warn"
    )
    # An "ask" is neither: the egress guard handed the call to a human for an
    # inline approval. Counted on its own so the prompts are visible too.
    manifest["summary"]["hook_asks"] = sum(
        1
        for r in gate_records
        if r.get("source") == "hook" and r.get("decision") == "ask"
    )

    if gate_records:
        if output_format in ("json", "both"):
            gates_file = output_dir / "gates.json"
            with open(gates_file, "w") as f:
                json.dump(gate_records, f, indent=2, default=str)
            manifest["files"].append(
                {
                    "name": "gates.json",
                    "sha256": sha256_file(gates_file),
                    "records": len(gate_records),
                }
            )

    # Export security configuration
    security_config = get_security_config()
    security_file = output_dir / "security-config.json"
    with open(security_file, "w") as f:
        json.dump(security_config, f, indent=2)
    manifest["files"].append(
        {
            "name": "security-config.json",
            "sha256": sha256_file(security_file),
            "records": 1,
        }
    )

    # Generate SBOM if requested
    if include_sbom:
        sbom_result = generate_sbom(output_dir)
        if sbom_result:
            sbom_file = output_dir / "sbom.json"
            manifest["files"].append(
                {
                    "name": "sbom.json",
                    "sha256": sha256_file(sbom_file),
                    "records": sbom_result["components"],
                    "format": sbom_result["format"],
                }
            )

    # Convert set to list for JSON serialization
    manifest["summary"]["unique_tools"] = sorted(manifest["summary"]["unique_tools"])

    # Write manifest
    manifest_file = output_dir / "audit-manifest.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest


def verify_export(output_dir: Path) -> bool:
    """Verify export integrity using manifest checksums."""
    manifest_file = output_dir / "audit-manifest.json"
    if not manifest_file.exists():
        print("Error: audit-manifest.json not found", file=sys.stderr)
        return False

    with open(manifest_file) as f:
        manifest = json.load(f)

    all_valid = True
    for file_info in manifest.get("files", []):
        file_path = output_dir / file_info["name"]
        if not file_path.exists():
            print(f"Missing: {file_info['name']}", file=sys.stderr)
            all_valid = False
            continue

        actual_hash = sha256_file(file_path)
        expected_hash = file_info.get("sha256", "")
        if actual_hash != expected_hash:
            print(f"Checksum mismatch: {file_info['name']}", file=sys.stderr)
            all_valid = False
        else:
            print(f"Verified: {file_info['name']}")

    return all_valid


def main() -> None:
    parser = argparse.ArgumentParser(description="Export audit trail for compliance")
    parser.add_argument("--since", "-s", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", "-u", help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--format", "-f", choices=["json", "csv", "both"], default="json"
    )
    parser.add_argument("--output", "-o", default="./audit-export")
    parser.add_argument("--include-sbom", action="store_true")
    parser.add_argument("--redact-paths", action="store_true")
    parser.add_argument("--verify", metavar="DIR", help="Verify existing export")

    args = parser.parse_args()

    # Handle verify mode
    if args.verify:
        output_dir = Path(args.verify)
        if verify_export(output_dir):
            print("\nIntegrity Check: All files verified")
            sys.exit(0)
        else:
            print("\nIntegrity Check: FAILED", file=sys.stderr)
            sys.exit(1)

    # Get default dates if not provided
    default_start, default_end = get_default_dates()
    start_date = args.since or default_start
    end_date = args.until or default_end

    output_dir = Path(args.output)

    try:
        manifest = export_audit(
            start_date=start_date,
            end_date=end_date,
            output_format=args.format,
            output_dir=output_dir,
            include_sbom=args.include_sbom,
            redact=args.redact_paths,
        )

        # Output result as JSON for command to parse
        print(
            json.dumps(
                {
                    "success": True,
                    "output_dir": str(output_dir),
                    "manifest": manifest,
                }
            )
        )

    except Exception as e:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": str(e),
                }
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
