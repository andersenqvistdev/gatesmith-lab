#!/usr/bin/env python3
"""
Metrics API endpoint for Gatesmith Labs.

Provides a simple HTTP endpoint that serves real-time metrics data.
Can be integrated into any web server or used as a standalone service.

Example usage with Flask:
    from metrics_api import MetricsAPI
    app = Flask(__name__)
    api = MetricsAPI()

    @app.route('/api/metrics')
    def get_metrics():
        return api.get_metrics_json()
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


class MetricsAPI:
    """Metrics API for Gatesmith Labs."""

    def __init__(self, project_root: Path = None):
        """Initialize metrics API.

        Args:
            project_root: Path to project root. Defaults to script location parent.
        """
        if project_root is None:
            # Navigate from .claude/hooks/company/metrics_api.py to project root
            project_root = Path(__file__).parent.parent.parent.parent
        self.project_root = project_root

    def load_org_data(self) -> Dict[str, Any]:
        """Load org.json and extract metrics."""
        org_path = self.project_root / ".company" / "org.json"
        with open(org_path) as f:
            return json.load(f)

    def count_work_queue_items(self) -> int:
        """Count pending items in work_queue.json."""
        queue_path = self.project_root / ".company" / "state/work_queue.json"
        try:
            with open(queue_path) as f:
                data = json.load(f)
                return len(data.get("pending", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return 0

    def get_metrics(self) -> Dict[str, Any]:
        """Calculate and return metrics."""
        org = self.load_org_data()
        employees = org.get("employees", [])

        # Count employees
        total_employees = len(employees)
        active_employees = sum(1 for e in employees if e.get("status") == "available")

        # Count tasks (missing efficiency block contributes 0)
        total_tasks = sum(
            e.get("efficiency", {}).get("tasks_completed", 0) for e in employees
        )

        # Calculate success rate (exclude employees without the field)
        success_rates = [
            e["efficiency"]["first_pass_success_rate"]
            for e in employees
            if e.get("efficiency", {}).get("first_pass_success_rate") is not None
        ]
        avg_success_rate = (
            sum(success_rates) / len(success_rates) if success_rates else 0.0
        )

        # Company efficiency score
        company_score = (
            org.get("economics", {}).get("efficiency", {}).get("company_score", 0)
        )

        # Pending work
        pending_work = self.count_work_queue_items()

        return {
            "total_employees": total_employees,
            "active_employees": active_employees,
            "total_tasks_completed": total_tasks,
            "success_rate": avg_success_rate,
            "company_efficiency_score": company_score,
            "pending_work_items": pending_work,
            "uptime_days": 29,  # From autonomy milestone
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def get_metrics_json(self) -> str:
        """Get metrics as JSON string."""
        metrics = self.get_metrics()
        return json.dumps(metrics, indent=2)

    def get_metrics_dict(self) -> Dict[str, Any]:
        """Get metrics as dictionary."""
        return self.get_metrics()


def main():
    """CLI interface for metrics API."""
    import argparse

    parser = argparse.ArgumentParser(description="Gatesmith Labs Metrics API")
    parser.add_argument(
        "--format",
        choices=["json", "table"],
        default="json",
        help="Output format",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Project root directory",
    )

    args = parser.parse_args()

    api = MetricsAPI(project_root=args.project_root)

    if args.format == "json":
        print(api.get_metrics_json())
    else:
        metrics = api.get_metrics()
        print("=== Gatesmith Labs Metrics ===")
        print(f"Total Employees: {metrics['total_employees']}")
        print(f"Active Employees: {metrics['active_employees']}")
        print(f"Tasks Completed: {metrics['total_tasks_completed']}")
        print(f"Success Rate: {metrics['success_rate'] * 100:.1f}%")
        print(f"Efficiency Score: {metrics['company_efficiency_score']:.2f}")
        print(f"Pending Work: {metrics['pending_work_items']} items")
        print(f"Uptime: {metrics['uptime_days']} days")
        print(f"Updated: {metrics['timestamp']}")


if __name__ == "__main__":
    main()
