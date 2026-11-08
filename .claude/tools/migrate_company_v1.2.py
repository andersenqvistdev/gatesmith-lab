#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
Migration Script: v1.1 to v1.2 Company Structure

Upgrades a single-project company installation (v1.1) to the multi-project
structure (v1.2). This enables company-wide employee management and
cross-project coordination.

Changes made during migration:
1. Creates .forge-company-root marker file (enables multi-project detection)
2. Moves .company/agents/ to .company/employees/ (new naming convention)
3. Creates .company/assignments/ directory (project assignment tracking)
4. Updates org.json with version, mode, and employees fields
5. Creates _index.json for assignments tracking

Usage:
    python migrate_company_v1.2.py [options]

Options:
    --dry-run           Show what would change without making changes
    --backup-dir PATH   Directory for backup (default: ./.company-backup-{timestamp})
    --force             Skip confirmation prompt
    --help, -h          Show this help message

Examples:
    # Preview migration without changes
    python migrate_company_v1.2.py --dry-run

    # Run migration with custom backup location
    python migrate_company_v1.2.py --backup-dir /tmp/company-backup

    # Run migration without confirmation
    python migrate_company_v1.2.py --force

Rollback:
    To rollback, restore from the backup directory:
    1. Remove .forge-company-root
    2. Remove .company/assignments/
    3. Restore .company/ from backup
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Exit codes
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_ALREADY_MIGRATED = 2
EXIT_NOT_V11 = 3
EXIT_USER_CANCELLED = 4

# File and directory names
COMPANY_DIR = ".company"
COMPANY_ROOT_MARKER = ".forge-company-root"
AGENTS_DIR = "agents"
EMPLOYEES_DIR = "employees"
ASSIGNMENTS_DIR = "assignments"
ORG_JSON = "org.json"
CONFIG_JSON = "config.json"


# Colors for terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"

    @classmethod
    def disable(cls):
        """Disable colors for non-terminal output."""
        cls.HEADER = ""
        cls.BLUE = ""
        cls.CYAN = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.RED = ""
        cls.ENDC = ""
        cls.BOLD = ""


def log_info(msg: str) -> None:
    """Print info message."""
    print(f"{Colors.CYAN}[INFO]{Colors.ENDC} {msg}")


def log_success(msg: str) -> None:
    """Print success message."""
    print(f"{Colors.GREEN}[OK]{Colors.ENDC} {msg}")


def log_warning(msg: str) -> None:
    """Print warning message."""
    print(f"{Colors.YELLOW}[WARN]{Colors.ENDC} {msg}")


def log_error(msg: str) -> None:
    """Print error message."""
    print(f"{Colors.RED}[ERROR]{Colors.ENDC} {msg}", file=sys.stderr)


def log_dry_run(msg: str) -> None:
    """Print dry-run message."""
    print(f"{Colors.BLUE}[DRY-RUN]{Colors.ENDC} Would: {msg}")


def detect_v11_installation(base_path: Path) -> tuple[bool, str]:
    """
    Detect if the current directory has a v1.1 company installation.

    Returns:
        Tuple of (is_v11, reason_message)
    """
    company_dir = base_path / COMPANY_DIR
    marker_file = base_path / COMPANY_ROOT_MARKER

    # Check if .company directory exists
    if not company_dir.exists():
        return False, f"No {COMPANY_DIR}/ directory found"

    if not company_dir.is_dir():
        return False, f"{COMPANY_DIR} exists but is not a directory"

    # Check if already migrated (has marker file)
    if marker_file.exists():
        return False, f"Already migrated: {COMPANY_ROOT_MARKER} marker exists"

    # Check for org.json (required for v1.1)
    org_json = company_dir / ORG_JSON
    if not org_json.exists():
        return False, f"No {ORG_JSON} found in {COMPANY_DIR}/"

    # Check if org.json already has v1.2 markers
    try:
        with open(org_json, "r", encoding="utf-8") as f:
            org_data = json.load(f)

        # Check version field - if it's 2.0+, already migrated
        version = org_data.get("version", "1.0")
        if version.startswith("2."):
            return False, f"org.json version is {version} (already v1.2+)"

        # Check mode field - if multi-project, already migrated
        mode = org_data.get("mode")
        if mode == "multi-project":
            return False, "org.json has mode: 'multi-project' (already v1.2)"

    except json.JSONDecodeError as e:
        return False, f"Invalid JSON in {ORG_JSON}: {e}"
    except OSError as e:
        return False, f"Cannot read {ORG_JSON}: {e}"

    return True, "v1.1 installation detected"


def get_migration_plan(base_path: Path) -> dict:
    """
    Generate a detailed migration plan.

    Returns:
        Dictionary describing all migration actions
    """
    company_dir = base_path / COMPANY_DIR
    agents_dir = company_dir / AGENTS_DIR
    employees_dir = company_dir / EMPLOYEES_DIR
    assignments_dir = company_dir / ASSIGNMENTS_DIR

    plan = {
        "base_path": str(base_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actions": [],
        "files_to_move": [],
        "files_to_create": [],
        "files_to_modify": [],
    }

    # Action 1: Create .forge-company-root marker
    marker_path = base_path / COMPANY_ROOT_MARKER
    plan["actions"].append(
        {
            "type": "create_marker",
            "path": str(marker_path),
            "description": "Create .forge-company-root marker file",
        }
    )
    plan["files_to_create"].append(str(marker_path))

    # Action 2: Move agents/ to employees/ (if agents/ exists)
    if agents_dir.exists() and agents_dir.is_dir():
        # Get list of files to move
        agent_files = list(agents_dir.rglob("*"))
        agent_files = [f for f in agent_files if f.is_file()]

        if agent_files:
            plan["actions"].append(
                {
                    "type": "move_directory",
                    "source": str(agents_dir),
                    "destination": str(employees_dir),
                    "file_count": len(agent_files),
                    "description": f"Move {AGENTS_DIR}/ to {EMPLOYEES_DIR}/ ({len(agent_files)} files)",
                }
            )
            plan["files_to_move"].extend([str(f) for f in agent_files])
        else:
            plan["actions"].append(
                {
                    "type": "rename_directory",
                    "source": str(agents_dir),
                    "destination": str(employees_dir),
                    "description": f"Rename empty {AGENTS_DIR}/ to {EMPLOYEES_DIR}/",
                }
            )
    elif not employees_dir.exists():
        # Create employees/ if neither exists
        plan["actions"].append(
            {
                "type": "create_directory",
                "path": str(employees_dir),
                "description": f"Create {EMPLOYEES_DIR}/ directory",
            }
        )
        plan["files_to_create"].append(str(employees_dir))

    # Action 3: Create assignments/ directory
    if not assignments_dir.exists():
        plan["actions"].append(
            {
                "type": "create_directory",
                "path": str(assignments_dir),
                "description": f"Create {ASSIGNMENTS_DIR}/ directory",
            }
        )
        plan["files_to_create"].append(str(assignments_dir))

        # Create _index.json
        index_path = assignments_dir / "_index.json"
        plan["actions"].append(
            {
                "type": "create_file",
                "path": str(index_path),
                "description": "Create assignments/_index.json",
            }
        )
        plan["files_to_create"].append(str(index_path))

        # Create README.md
        readme_path = assignments_dir / "README.md"
        if not readme_path.exists():
            plan["actions"].append(
                {
                    "type": "create_file",
                    "path": str(readme_path),
                    "description": "Create assignments/README.md",
                }
            )
            plan["files_to_create"].append(str(readme_path))

    # Action 4: Update org.json
    org_json_path = company_dir / ORG_JSON
    plan["actions"].append(
        {
            "type": "modify_file",
            "path": str(org_json_path),
            "description": "Update org.json with version, mode, and employees fields",
            "changes": [
                "Add version: '2.0'",
                "Add mode: 'single-project' (safe default)",
                "Rename 'agents' to 'employees' if present",
                "Add migration metadata",
            ],
        }
    )
    plan["files_to_modify"].append(str(org_json_path))

    return plan


def print_migration_plan(plan: dict) -> None:
    """Print the migration plan in a human-readable format."""
    print(f"\n{Colors.HEADER}=== Migration Plan ==={Colors.ENDC}")
    print(f"Base Path: {plan['base_path']}")
    print(f"Timestamp: {plan['timestamp']}")
    print()

    print(f"{Colors.BOLD}Actions to perform:{Colors.ENDC}")
    for i, action in enumerate(plan["actions"], 1):
        print(f"  {i}. {action['description']}")
        if action["type"] == "move_directory":
            print(f"     Source: {action['source']}")
            print(f"     Destination: {action['destination']}")
            print(f"     Files: {action['file_count']}")
        elif action["type"] == "modify_file":
            for change in action.get("changes", []):
                print(f"     - {change}")

    print()
    print(f"{Colors.BOLD}Summary:{Colors.ENDC}")
    print(f"  Files to create: {len(plan['files_to_create'])}")
    print(f"  Files to move: {len(plan['files_to_move'])}")
    print(f"  Files to modify: {len(plan['files_to_modify'])}")
    print()


def create_backup(base_path: Path, backup_dir: Path) -> bool:
    """
    Create a backup of the .company directory.

    Returns:
        True if backup successful, False otherwise
    """
    company_dir = base_path / COMPANY_DIR

    try:
        # Create backup directory
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Copy .company directory
        backup_company = backup_dir / COMPANY_DIR
        shutil.copytree(company_dir, backup_company)

        # Create backup manifest
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(base_path),
            "backup_path": str(backup_dir),
            "version": "v1.1",
            "migration_target": "v1.2",
        }

        manifest_path = backup_dir / "backup_manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return True

    except OSError as e:
        log_error(f"Failed to create backup: {e}")
        return False


def create_marker_file(base_path: Path, dry_run: bool = False) -> bool:
    """Create the .forge-company-root marker file."""
    marker_path = base_path / COMPANY_ROOT_MARKER

    marker_content = {
        "version": "1.0",
        "company_name": None,  # Will be populated from org.json if available
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "work_queue_mode": "company-level",
            "strict_mode": False,
        },
        "_migration": {
            "migrated_from": "v1.1",
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "script_version": "1.0.0",
        },
    }

    # Try to get company name from org.json
    org_json_path = base_path / COMPANY_DIR / ORG_JSON
    if org_json_path.exists():
        try:
            with open(org_json_path, "r", encoding="utf-8") as f:
                org_data = json.load(f)
            company_info = org_data.get("company", {})
            if isinstance(company_info, dict):
                marker_content["company_name"] = company_info.get("name")
        except (json.JSONDecodeError, OSError):
            pass  # Keep company_name as None

    if dry_run:
        log_dry_run(f"Create {COMPANY_ROOT_MARKER} with content:")
        print(json.dumps(marker_content, indent=2))
        return True

    try:
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(marker_content, f, indent=2)
        log_success(f"Created {COMPANY_ROOT_MARKER}")
        return True
    except OSError as e:
        log_error(f"Failed to create marker file: {e}")
        return False


def move_agents_to_employees(base_path: Path, dry_run: bool = False) -> bool:
    """Move .company/agents/ to .company/employees/."""
    company_dir = base_path / COMPANY_DIR
    agents_dir = company_dir / AGENTS_DIR
    employees_dir = company_dir / EMPLOYEES_DIR

    if not agents_dir.exists():
        if dry_run:
            log_dry_run(f"No {AGENTS_DIR}/ directory to move")
        else:
            log_info(f"No {AGENTS_DIR}/ directory to move")
        return True

    if employees_dir.exists():
        if dry_run:
            log_dry_run(f"{EMPLOYEES_DIR}/ already exists, merging contents")
        else:
            log_warning(f"{EMPLOYEES_DIR}/ already exists, merging contents")

            # Merge: copy contents from agents/ to employees/
            for item in agents_dir.iterdir():
                dest = employees_dir / item.name
                if item.is_dir():
                    if dest.exists():
                        # Recursively merge
                        for sub_item in item.rglob("*"):
                            if sub_item.is_file():
                                rel_path = sub_item.relative_to(item)
                                sub_dest = dest / rel_path
                                sub_dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(sub_item, sub_dest)
                    else:
                        shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            # Remove agents/ after successful merge
            shutil.rmtree(agents_dir)
            log_success(f"Merged {AGENTS_DIR}/ into {EMPLOYEES_DIR}/")
            return True

    if dry_run:
        log_dry_run(f"Move {AGENTS_DIR}/ to {EMPLOYEES_DIR}/")
        return True

    try:
        agents_dir.rename(employees_dir)
        log_success(f"Moved {AGENTS_DIR}/ to {EMPLOYEES_DIR}/")
        return True
    except OSError as e:
        log_error(f"Failed to move {AGENTS_DIR}/ to {EMPLOYEES_DIR}/: {e}")
        return False


def create_assignments_directory(base_path: Path, dry_run: bool = False) -> bool:
    """Create .company/assignments/ directory with initial files."""
    assignments_dir = base_path / COMPANY_DIR / ASSIGNMENTS_DIR

    if assignments_dir.exists():
        if dry_run:
            log_dry_run(f"{ASSIGNMENTS_DIR}/ already exists")
        else:
            log_info(f"{ASSIGNMENTS_DIR}/ already exists")
        return True

    if dry_run:
        log_dry_run(f"Create {ASSIGNMENTS_DIR}/ directory")
        log_dry_run(f"Create {ASSIGNMENTS_DIR}/_index.json")
        return True

    try:
        assignments_dir.mkdir(parents=True, exist_ok=True)

        # Create _index.json
        index_content = {
            "projects": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        index_path = assignments_dir / "_index.json"
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_content, f, indent=2)

        log_success(f"Created {ASSIGNMENTS_DIR}/ directory")
        log_success(f"Created {ASSIGNMENTS_DIR}/_index.json")
        return True

    except OSError as e:
        log_error(f"Failed to create assignments directory: {e}")
        return False


def update_org_json(base_path: Path, dry_run: bool = False) -> bool:
    """Update org.json with v1.2 schema fields."""
    org_json_path = base_path / COMPANY_DIR / ORG_JSON

    try:
        with open(org_json_path, "r", encoding="utf-8") as f:
            org_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"Failed to read {ORG_JSON}: {e}")
        return False

    # Make updates
    changes_made = []

    # Add version if not present or update to 2.0
    if org_data.get("version") != "2.0":
        org_data["version"] = "2.0"
        changes_made.append("Set version to '2.0'")

    # Add mode if not present (default to single-project for safe migration)
    if "mode" not in org_data:
        org_data["mode"] = "single-project"
        changes_made.append("Added mode: 'single-project'")

    # Rename 'agents' to 'employees' if 'agents' exists
    if "agents" in org_data and "employees" not in org_data:
        org_data["employees"] = org_data.pop("agents")
        changes_made.append("Renamed 'agents' to 'employees'")
    elif "employees" not in org_data:
        org_data["employees"] = []
        changes_made.append("Added empty 'employees' array")

    # Add projects array if not present
    if "projects" not in org_data:
        org_data["projects"] = []
        changes_made.append("Added empty 'projects' array")

    # Add migration metadata in definitions
    if "definitions" not in org_data:
        org_data["definitions"] = {}

    if "_migrationHistory" not in org_data["definitions"]:
        org_data["definitions"]["_migrationHistory"] = []

    org_data["definitions"]["_migrationHistory"].append(
        {
            "from_version": "1.x",
            "to_version": "2.0",
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "script": "migrate_company_v1.2.py",
        }
    )
    changes_made.append("Added migration history record")

    if dry_run:
        log_dry_run(f"Update {ORG_JSON}:")
        for change in changes_made:
            print(f"  - {change}")
        return True

    try:
        with open(org_json_path, "w", encoding="utf-8") as f:
            json.dump(org_data, f, indent=2)
        log_success(f"Updated {ORG_JSON}")
        for change in changes_made:
            print(f"  - {change}")
        return True
    except OSError as e:
        log_error(f"Failed to write {ORG_JSON}: {e}")
        return False


def run_migration(
    base_path: Path,
    backup_dir: Path,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """
    Execute the full migration process.

    Returns:
        Exit code indicating success or failure
    """
    # Step 1: Detect v1.1 installation
    is_v11, reason = detect_v11_installation(base_path)

    if not is_v11:
        if "Already migrated" in reason or "already v1.2" in reason:
            log_info(reason)
            return EXIT_ALREADY_MIGRATED
        else:
            log_error(f"Not a v1.1 installation: {reason}")
            return EXIT_NOT_V11

    log_success(reason)

    # Step 2: Generate and display migration plan
    plan = get_migration_plan(base_path)
    print_migration_plan(plan)

    # Step 3: Confirm with user (unless --force or --dry-run)
    if not dry_run and not force:
        print(f"{Colors.YELLOW}This will modify your .company/ directory.{Colors.ENDC}")
        print(f"A backup will be created at: {backup_dir}")
        print()
        try:
            response = input("Proceed with migration? [y/N] ").strip().lower()
            if response not in ("y", "yes"):
                log_info("Migration cancelled by user")
                return EXIT_USER_CANCELLED
        except (EOFError, KeyboardInterrupt):
            print()
            log_info("Migration cancelled")
            return EXIT_USER_CANCELLED

    # Step 4: Create backup (unless --dry-run)
    if not dry_run:
        log_info(f"Creating backup at {backup_dir}...")
        if not create_backup(base_path, backup_dir):
            log_error("Backup failed, aborting migration")
            return EXIT_ERROR
        log_success(f"Backup created at {backup_dir}")
    else:
        log_dry_run(f"Create backup at {backup_dir}")

    print()
    if dry_run:
        print(f"{Colors.HEADER}=== Dry Run: No changes made ==={Colors.ENDC}")
    else:
        print(f"{Colors.HEADER}=== Executing Migration ==={Colors.ENDC}")

    # Step 5: Execute migration steps
    success = True

    # 5a: Create marker file
    if not create_marker_file(base_path, dry_run):
        success = False

    # 5b: Move agents/ to employees/
    if not move_agents_to_employees(base_path, dry_run):
        success = False

    # 5c: Create assignments/ directory
    if not create_assignments_directory(base_path, dry_run):
        success = False

    # 5d: Update org.json
    if not update_org_json(base_path, dry_run):
        success = False

    print()

    if success:
        if dry_run:
            print(f"{Colors.GREEN}Dry run complete. No changes were made.{Colors.ENDC}")
            print("Run without --dry-run to perform the migration.")
        else:
            print(f"{Colors.GREEN}Migration complete!{Colors.ENDC}")
            print()
            print("Next steps:")
            print("  1. Verify the migration by running: verify.sh")
            print("  2. If issues occur, restore from backup:")
            print(f"     rm -rf .company && cp -r {backup_dir}/.company .")
            print("     rm .forge-company-root")
            print("  3. Start using multi-project features with /company-add-project")
        return EXIT_SUCCESS
    else:
        if not dry_run:
            print(f"{Colors.RED}Migration had errors.{Colors.ENDC}")
            print(f"Consider restoring from backup at: {backup_dir}")
        return EXIT_ERROR


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate company structure from v1.1 to v1.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without making changes",
    )

    parser.add_argument(
        "--backup-dir",
        type=str,
        default=None,
        help="Directory for backup (default: ./.company-backup-{timestamp})",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    args = parser.parse_args()

    # Disable colors if requested or not a terminal
    if args.no_color or not sys.stdout.isatty():
        Colors.disable()

    # Determine paths
    base_path = Path.cwd()
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = base_path / f".company-backup-{timestamp}"

    print(f"{Colors.HEADER}Gatesmith Company Migration: v1.1 -> v1.2{Colors.ENDC}")
    print(f"Working directory: {base_path}")
    print()

    return run_migration(
        base_path=base_path,
        backup_dir=backup_dir,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
