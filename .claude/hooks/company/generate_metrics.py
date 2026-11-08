#!/usr/bin/env python3
"""
Generate real Gatesmith Labs metrics component.

Reads from .company/org.json and work_queue.json to produce an embeddable
metrics widget showing actual company statistics.

Usage:
    python generate_metrics.py [--output path/to/file.html]
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def load_org_data() -> Dict[str, Any]:
    """Load org.json and extract metrics."""
    # Navigate from .claude/hooks/company/generate_metrics.py to project root
    # Parent dirs: company -> hooks -> .claude -> tasks (project root)
    org_path = Path(__file__).parent.parent.parent.parent / ".company" / "org.json"
    with open(org_path) as f:
        return json.load(f)


def count_work_queue_items() -> int:
    """Count pending items in work_queue.json."""
    queue_path = (
        Path(__file__).parent.parent.parent.parent
        / ".company"
        / "state/work_queue.json"
    )
    try:
        with open(queue_path) as f:
            data = json.load(f)
            return len(data.get("pending", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def calculate_metrics() -> Dict[str, Any]:
    """Calculate metrics from org data."""
    org = load_org_data()
    employees = org.get("employees", [])

    # Count employees
    total_employees = len(employees)
    active_employees = sum(1 for e in employees if e.get("status") == "available")

    # Count tasks
    total_tasks = sum(
        (e.get("efficiency") or {}).get("tasks_completed", 0) for e in employees
    )

    # Calculate success rate (first_pass_success_rate)
    success_rates = [
        (e.get("efficiency") or {}).get("first_pass_success_rate", 0.0)
        for e in employees
    ]
    avg_success_rate = sum(success_rates) / len(success_rates) if success_rates else 0.0

    # Company efficiency score
    company_score = (
        org.get("economics", {}).get("efficiency", {}).get("company_score", 0)
    )

    # Pending work
    pending_work = count_work_queue_items()

    return {
        "total_employees": total_employees,
        "active_employees": active_employees,
        "total_tasks_completed": total_tasks,
        "success_rate": avg_success_rate,
        "company_efficiency_score": company_score,
        "pending_work_items": pending_work,
        "generated_at": datetime.now().isoformat(),
    }


def generate_html_component(metrics: Dict[str, Any]) -> str:
    """Generate embeddable HTML component with metrics."""
    success_pct = metrics["success_rate"] * 100
    _efficiency_pct = min(
        metrics["company_efficiency_score"] * 10, 100
    )  # Scale to percentage

    html = f"""<!-- Gatesmith Labs Real Metrics Component -->
<!-- Generated: {metrics["generated_at"]} -->
<div class="metrics-widget">
    <div class="metrics-container">
        <div class="metrics-header">
            <h3>FORGE LABS OPERATIONS</h3>
            <span class="metrics-badge">LIVE DATA</span>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-value">{metrics["total_tasks_completed"]}+</div>
                <div class="metric-label">TASKS COMPLETED</div>
                <div class="metric-indicator positive"></div>
            </div>

            <div class="metric-card">
                <div class="metric-value">{metrics["total_employees"]}</div>
                <div class="metric-label">AI EMPLOYEES</div>
                <div class="metric-indicator"></div>
            </div>

            <div class="metric-card">
                <div class="metric-value">{success_pct:.0f}%</div>
                <div class="metric-label">SUCCESS RATE</div>
                <div class="metric-indicator positive"></div>
            </div>
        </div>

        <div class="metrics-footer">
            <div class="metric-stat">
                <span class="stat-label">Active Employees:</span>
                <span class="stat-value">{metrics["active_employees"]}/{metrics["total_employees"]}</span>
            </div>
            <div class="metric-stat">
                <span class="stat-label">Pending Work:</span>
                <span class="stat-value">{metrics["pending_work_items"]} items</span>
            </div>
            <div class="metric-stat">
                <span class="stat-label">Efficiency Score:</span>
                <span class="stat-value">{metrics["company_efficiency_score"]:.1f}/100</span>
            </div>
        </div>

        <div class="metrics-disclaimer">
            Real-time data from Gatesmith Labs autonomous operations.
            Updated automatically.
        </div>
    </div>
</div>

<style>
/* Metrics Widget Styles */
.metrics-widget {{
    margin: 48px 0;
    background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-tertiary) 100%);
    border: 1px solid var(--border);
    padding: 40px;
}}

.metrics-container {{
    max-width: 100%;
}}

.metrics-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 32px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
}}

.metrics-header h3 {{
    font-family: var(--font-heading);
    font-size: 20px;
    letter-spacing: 2px;
    margin: 0;
}}

.metrics-badge {{
    background: var(--success);
    color: var(--bg-primary);
    padding: 4px 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    border-radius: 2px;
}}

.metrics-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 24px;
    margin-bottom: 32px;
}}

.metric-card {{
    background: var(--bg-primary);
    border: 1px solid var(--border);
    padding: 24px;
    position: relative;
    padding-bottom: 32px;
}}

.metric-value {{
    font-family: var(--font-heading);
    font-size: 36px;
    letter-spacing: 1px;
    color: var(--accent);
    margin-bottom: 8px;
}}

.metric-label {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-secondary);
}}

.metric-indicator {{
    position: absolute;
    bottom: 16px;
    right: 24px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--warning);
}}

.metric-indicator.positive {{
    background: var(--success);
    animation: pulse 2s infinite;
}}

.metric-indicator.neutral {{
    background: var(--accent);
}}

@keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.7; }}
}}

.metrics-footer {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 16px;
    padding: 24px 0;
    border-top: 1px solid var(--border);
    margin-bottom: 16px;
}}

.metric-stat {{
    display: flex;
    justify-content: space-between;
    align-items: center;
}}

.stat-label {{
    color: var(--text-secondary);
    font-size: 13px;
}}

.stat-value {{
    color: var(--accent);
    font-weight: 600;
    font-family: var(--font-heading);
}}

.metrics-disclaimer {{
    font-size: 12px;
    color: var(--text-secondary);
    text-align: center;
    padding-top: 16px;
}}

@media (max-width: 768px) {{
    .metrics-grid {{
        grid-template-columns: repeat(2, 1fr);
    }}

    .metric-value {{
        font-size: 28px;
    }}

    .metrics-footer {{
        grid-template-columns: 1fr;
        gap: 12px;
    }}
}}
</style>
"""
    return html


def main():
    """Generate metrics and output."""
    try:
        metrics = calculate_metrics()
        html = generate_html_component(metrics)

        # Determine output path
        output_path = None
        if "--output" in sys.argv:
            idx = sys.argv.index("--output")
            if idx + 1 < len(sys.argv):
                output_path = sys.argv[idx + 1]

        if output_path:
            Path(output_path).write_text(html)
            print(f"✓ Metrics component generated: {output_path}")
        else:
            print(html)

        # Always print metrics summary
        print("\n=== Gatesmith Labs Metrics Summary ===", file=sys.stderr)
        print(f"Total Employees: {metrics['total_employees']}", file=sys.stderr)
        print(f"Active Employees: {metrics['active_employees']}", file=sys.stderr)
        print(f"Tasks Completed: {metrics['total_tasks_completed']}", file=sys.stderr)
        print(f"Success Rate: {metrics['success_rate'] * 100:.1f}%", file=sys.stderr)
        print(
            f"Efficiency Score: {metrics['company_efficiency_score']:.2f}",
            file=sys.stderr,
        )
        print(f"Pending Work: {metrics['pending_work_items']} items", file=sys.stderr)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
