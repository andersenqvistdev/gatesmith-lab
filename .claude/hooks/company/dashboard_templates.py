"""
Dashboard Templates — HTML template generation for the Gatesmith dashboard.

Task P38.8: Per-subsystem health indicators for the dashboard.

Provides functions to generate HTML sections for the dashboard, starting
with the Subsystem Health section that displays color-coded health status
for each daemon subsystem.

Usage:
    from dashboard_templates import generate_subsystem_health_section

    # Generate static HTML for subsystem health cards
    html = generate_subsystem_health_section()

    # Generate with initial data (from API response)
    html = generate_subsystem_health_section(data)
"""

from __future__ import annotations

from typing import Any

# Human-readable names for each subsystem key.
SUBSYSTEM_DISPLAY_NAMES: dict[str, str] = {
    "task_execution": "Task Execution",
    "proactive": "Proactive Initiative",
    "cross_project": "Cross-Project",
    "strategic_planning": "Strategic Planning",
    "roadmap_scheduling": "Roadmap Scheduling",
    "p25_autonomy": "Full Autonomy (P25)",
    "executive_loop": "Executive Loop",
    "improvement_cycle": "Self-Improvement",
    "employee_ideation": "Employee Ideation",
    "auto_merge": "Auto-Merge",
    "scheduling_efficiency": "Scheduling Efficiency",
    "circuit_breaker": "Circuit Breaker",
    "document_approvals": "Document Approvals",
}

# Ordered list of subsystem keys for consistent rendering.
SUBSYSTEM_ORDER: list[str] = list(SUBSYSTEM_DISPLAY_NAMES.keys())


def _status_color_class(status: str) -> str:
    """Return CSS modifier class for a health status value."""
    return {
        "healthy": "subsystem-card__dot--healthy",
        "degraded": "subsystem-card__dot--degraded",
        "unhealthy": "subsystem-card__dot--unhealthy",
    }.get(status, "subsystem-card__dot--unknown")


def _status_badge_class(status: str) -> str:
    """Return badge CSS class for a health status value."""
    return {
        "healthy": "badge--success",
        "degraded": "badge--warning",
        "unhealthy": "badge--error",
    }.get(status, "badge--muted")


def _format_last_active(last_active: str | None) -> str:
    """Format a last-active ISO timestamp for display."""
    if not last_active:
        return "Never"
    # Show the timestamp as-is (ISO format); JS will make it relative.
    return last_active


def _generate_card_html(key: str, info: dict[str, Any]) -> str:
    """Generate a single subsystem health card HTML string."""
    display_name = SUBSYSTEM_DISPLAY_NAMES.get(key, key)
    status = info.get("status", "unknown")
    message = info.get("message", "")
    last_active = info.get("last_active")
    dot_class = _status_color_class(status)
    badge_class = _status_badge_class(status)
    last_active_display = _format_last_active(last_active)

    from html import escape as _esc

    return f"""\
      <div class="subsystem-card" data-subsystem="{_esc(key)}">
        <div class="subsystem-card__header">
          <span class="subsystem-card__dot {dot_class}"></span>
          <span class="subsystem-card__name">{_esc(display_name)}</span>
        </div>
        <div class="subsystem-card__body">
          <span class="badge {badge_class} subsystem-card__badge">{_esc(status)}</span>
          <p class="subsystem-card__message">{_esc(message)}</p>
        </div>
        <div class="subsystem-card__footer">
          <span class="subsystem-card__last-active">Last active: {_esc(last_active_display)}</span>
        </div>
      </div>"""


def generate_subsystem_health_section(
    data: dict[str, Any] | None = None,
) -> str:
    """Generate the full Subsystem Health HTML section.

    Parameters
    ----------
    data:
        Optional API response dict from ``/api/subsystem-health``.
        Expected shape::

            {
                "subsystems": {
                    "<key>": {"status": "...", "message": "...", "last_active": "..."},
                    ...
                },
                "overall": "healthy" | "degraded" | "unhealthy" | "unknown"
            }

        If *None*, placeholder cards with ``unknown`` status are rendered
        and the JS fetch will populate them on page load.

    Returns
    -------
    str
        An HTML string containing the section element, inline CSS, and
        inline JavaScript for live updates.
    """
    subsystems = {}
    if data and isinstance(data.get("subsystems"), dict):
        subsystems = data["subsystems"]

    # Build card HTML for each subsystem in consistent order.
    cards_html_parts: list[str] = []
    for key in SUBSYSTEM_ORDER:
        info = subsystems.get(
            key, {"status": "unknown", "message": "Loading...", "last_active": None}
        )
        cards_html_parts.append(_generate_card_html(key, info))

    cards_html = "\n".join(cards_html_parts)

    overall = "unknown"
    if data and "overall" in data:
        overall = data["overall"]

    overall_dot_class = _status_color_class(overall)
    overall_badge_class = _status_badge_class(overall)

    return f"""\
<!-- ===== Subsystem Health Section (P38.8) ===== -->
<style>
  /* ----- Subsystem Health Grid ----- */
  .subsystem-health {{
    margin-top: var(--spacing-lg);
  }}

  .subsystem-health__header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-md);
  }}

  .subsystem-health__title {{
    font-size: var(--font-xl);
    font-weight: 600;
    color: var(--text-primary);
  }}

  .subsystem-health__overall {{
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-sm);
    color: var(--text-secondary);
  }}

  .subsystem-health__grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: var(--spacing-md);
  }}

  /* ----- Subsystem Card ----- */
  .subsystem-card {{
    background-color: var(--bg-card, #1e293b);
    border-radius: var(--radius-lg, 0.75rem);
    border: 1px solid var(--border-color, #334155);
    padding: var(--spacing-md, 1rem);
    transition: box-shadow 150ms ease, border-color 150ms ease;
  }}

  .subsystem-card:hover {{
    box-shadow: var(--shadow-md, 0 4px 6px rgba(0,0,0,0.4));
    border-color: var(--border-light, #475569);
  }}

  .subsystem-card__header {{
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 0.5rem);
    margin-bottom: var(--spacing-sm, 0.5rem);
  }}

  .subsystem-card__dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }}

  .subsystem-card__dot--healthy {{
    background-color: var(--status-success, #22c55e);
    box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
  }}

  .subsystem-card__dot--degraded {{
    background-color: var(--status-warning, #eab308);
    box-shadow: 0 0 6px rgba(234, 179, 8, 0.4);
  }}

  .subsystem-card__dot--unhealthy {{
    background-color: var(--status-error, #ef4444);
    box-shadow: 0 0 6px rgba(239, 68, 68, 0.4);
    animation: pulse 2s infinite;
  }}

  .subsystem-card__dot--unknown {{
    background-color: var(--text-muted, #64748b);
  }}

  .subsystem-card__name {{
    font-size: var(--font-sm, 0.875rem);
    font-weight: 600;
    color: var(--text-primary, #f8fafc);
  }}

  .subsystem-card__body {{
    margin-bottom: var(--spacing-sm, 0.5rem);
  }}

  .subsystem-card__badge {{
    display: inline-block;
    margin-bottom: var(--spacing-xs, 0.25rem);
    text-transform: capitalize;
  }}

  .badge--muted {{
    background-color: rgba(100, 116, 139, 0.2);
    color: var(--text-muted, #64748b);
  }}

  .subsystem-card__message {{
    font-size: var(--font-xs, 0.75rem);
    color: var(--text-secondary, #94a3b8);
    margin: 0;
    line-height: 1.4;
  }}

  .subsystem-card__footer {{
    border-top: 1px solid var(--border-color, #334155);
    padding-top: var(--spacing-xs, 0.25rem);
    margin-top: var(--spacing-xs, 0.25rem);
  }}

  .subsystem-card__last-active {{
    font-size: var(--font-xs, 0.75rem);
    color: var(--text-muted, #64748b);
  }}

  @media (max-width: 768px) {{
    .subsystem-health__grid {{
      grid-template-columns: 1fr;
    }}
  }}
</style>

<div class="subsystem-health" id="subsystem-health-section">
  <div class="subsystem-health__header">
    <h3 class="subsystem-health__title">Subsystem Health</h3>
    <div class="subsystem-health__overall" id="subsystem-overall">
      <span class="subsystem-card__dot {overall_dot_class}" id="subsystem-overall-dot"></span>
      <span>Overall: <strong id="subsystem-overall-text">{overall}</strong></span>
      <span class="badge {overall_badge_class}" id="subsystem-overall-badge">{overall}</span>
    </div>
  </div>
  <div class="subsystem-health__grid" id="subsystem-health-grid">
{cards_html}
  </div>
</div>

<script>
(function() {{
  'use strict';

  // Subsystem display name mapping
  var SUBSYSTEM_NAMES = {{
    'task_execution': 'Task Execution',
    'proactive': 'Proactive Initiative',
    'cross_project': 'Cross-Project',
    'strategic_planning': 'Strategic Planning',
    'roadmap_scheduling': 'Roadmap Scheduling',
    'p25_autonomy': 'Full Autonomy (P25)',
    'executive_loop': 'Executive Loop',
    'improvement_cycle': 'Self-Improvement',
    'employee_ideation': 'Employee Ideation',
    'auto_merge': 'Auto-Merge',
    'scheduling_efficiency': 'Scheduling Efficiency',
    'circuit_breaker': 'Circuit Breaker',
    'document_approvals': 'Document Approvals'
  }};

  // Status to CSS class mappings
  function dotClass(status) {{
    return {{
      'healthy': 'subsystem-card__dot--healthy',
      'degraded': 'subsystem-card__dot--degraded',
      'unhealthy': 'subsystem-card__dot--unhealthy'
    }}[status] || 'subsystem-card__dot--unknown';
  }}

  function badgeClass(status) {{
    return {{
      'healthy': 'badge--success',
      'degraded': 'badge--warning',
      'unhealthy': 'badge--error'
    }}[status] || 'badge--muted';
  }}

  // Format ISO timestamp to a human-readable relative string
  function formatLastActive(ts) {{
    if (!ts) return 'Never';
    try {{
      var d = new Date(ts);
      if (isNaN(d.getTime())) return ts;
      var now = new Date();
      var diffMs = now - d;
      var diffS = Math.floor(diffMs / 1000);
      if (diffS < 60) return diffS + 's ago';
      var diffM = Math.floor(diffS / 60);
      if (diffM < 60) return diffM + 'm ago';
      var diffH = Math.floor(diffM / 60);
      if (diffH < 24) return diffH + 'h ' + (diffM % 60) + 'm ago';
      var diffD = Math.floor(diffH / 24);
      return diffD + 'd ago';
    }} catch (e) {{
      return ts;
    }}
  }}

  // Update a single card DOM element with new data
  function updateCard(card, info) {{
    var status = info.status || 'unknown';
    var message = info.message || '';
    var lastActive = info.last_active || null;

    // Update dot
    var dot = card.querySelector('.subsystem-card__dot');
    if (dot) {{
      dot.className = 'subsystem-card__dot ' + dotClass(status);
    }}

    // Update badge
    var badge = card.querySelector('.subsystem-card__badge');
    if (badge) {{
      badge.className = 'badge ' + badgeClass(status) + ' subsystem-card__badge';
      badge.textContent = status;
    }}

    // Update message
    var msg = card.querySelector('.subsystem-card__message');
    if (msg) {{
      msg.textContent = message;
    }}

    // Update last active
    var la = card.querySelector('.subsystem-card__last-active');
    if (la) {{
      la.textContent = 'Last active: ' + formatLastActive(lastActive);
    }}
  }}

  // Update overall status indicator
  function updateOverall(overall) {{
    var dot = document.getElementById('subsystem-overall-dot');
    var text = document.getElementById('subsystem-overall-text');
    var badge = document.getElementById('subsystem-overall-badge');
    if (dot) {{
      dot.className = 'subsystem-card__dot ' + dotClass(overall);
    }}
    if (text) {{
      text.textContent = overall;
    }}
    if (badge) {{
      badge.className = 'badge ' + badgeClass(overall) + ' subsystem-card__badge';
      badge.textContent = overall;
    }}
  }}

  // Fetch subsystem health data and update the UI
  function refreshSubsystemHealth() {{
    fetch('/api/subsystem-health')
      .then(function(response) {{
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      }})
      .then(function(data) {{
        if (!data.success && !data.subsystems) return;
        var subsystems = data.subsystems || {{}};
        var grid = document.getElementById('subsystem-health-grid');
        if (!grid) return;

        // Update each card
        var cards = grid.querySelectorAll('.subsystem-card');
        cards.forEach(function(card) {{
          var key = card.getAttribute('data-subsystem');
          if (key && subsystems[key]) {{
            updateCard(card, subsystems[key]);
          }}
        }});

        // Update overall
        if (data.overall) {{
          updateOverall(data.overall);
        }}
      }})
      .catch(function(err) {{
        // On error, show all as unknown (daemon likely not running)
        var grid = document.getElementById('subsystem-health-grid');
        if (!grid) return;
        var cards = grid.querySelectorAll('.subsystem-card');
        cards.forEach(function(card) {{
          updateCard(card, {{
            status: 'unknown',
            message: 'Unable to reach API',
            last_active: null
          }});
        }});
        updateOverall('unknown');
      }});
  }}

  // Run on page load
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', refreshSubsystemHealth);
  }} else {{
    refreshSubsystemHealth();
  }}

  // Refresh every 10 seconds
  setInterval(refreshSubsystemHealth, 10000);
}})();
</script>
<!-- ===== End Subsystem Health Section ===== -->
"""


def generate_autonomy_widget_section(
    data: dict | None = None,
) -> str:
    """Generate the Autonomy Metrics widget HTML section.

    Parameters
    ----------
    data:
        Optional API response dict from ``/api/autonomy-widget``.
        Expected shape::

            {
                "autonomy_percent": float,
                "time_since_last_human_formatted": str,
                "active_recovery_attempts": int,
                "goal_progress_velocity": float,
            }

        If *None*, placeholder values are rendered and the JS fetch
        will populate them on page load.

    Returns
    -------
    str
        An HTML string with inline CSS and JS for the autonomy widget.
    """
    autonomy_pct = 0.0
    time_since = "N/A"
    recovery_count = 0
    velocity = 0.0

    if data:
        autonomy_pct = data.get("autonomy_percent", 0.0)
        time_since = data.get("time_since_last_human_formatted", "N/A")
        recovery_count = data.get("active_recovery_attempts", 0)
        velocity = data.get("goal_progress_velocity", 0.0)

    # Determine status color for autonomy %
    if autonomy_pct >= 90:
        pct_color = "var(--status-success, #22c55e)"
    elif autonomy_pct >= 70:
        pct_color = "var(--status-warning, #eab308)"
    else:
        pct_color = "var(--status-error, #ef4444)"

    return f"""\
<!-- ===== Autonomy Metrics Widget ===== -->
<style>
  .autonomy-widget {{
    margin-top: var(--spacing-lg);
  }}
  .autonomy-widget__title {{
    font-size: var(--font-xl);
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: var(--spacing-md);
  }}
  .autonomy-widget__grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: var(--spacing-md);
  }}
  .autonomy-metric-card {{
    background-color: var(--bg-card, #1e293b);
    border-radius: var(--radius-lg, 0.75rem);
    border: 1px solid var(--border-color, #334155);
    padding: var(--spacing-md, 1rem);
  }}
  .autonomy-metric-card__label {{
    font-size: var(--font-xs, 0.75rem);
    color: var(--text-muted, #64748b);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: var(--spacing-xs, 0.25rem);
  }}
  .autonomy-metric-card__value {{
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--text-primary, #f8fafc);
    line-height: 1;
  }}
  .autonomy-metric-card__sub {{
    font-size: var(--font-xs, 0.75rem);
    color: var(--text-secondary, #94a3b8);
    margin-top: var(--spacing-xs, 0.25rem);
  }}
  @media (max-width: 768px) {{
    .autonomy-widget__grid {{ grid-template-columns: 1fr 1fr; }}
  }}
</style>

<div class="autonomy-widget" id="autonomy-widget-section">
  <h3 class="autonomy-widget__title">Autonomy Metrics</h3>
  <div class="autonomy-widget__grid">
    <div class="autonomy-metric-card">
      <div class="autonomy-metric-card__label">Autonomy Rate</div>
      <div class="autonomy-metric-card__value" id="aw-autonomy-pct"
           style="color: {pct_color};">{autonomy_pct}%</div>
      <div class="autonomy-metric-card__sub">tasks resolved without human</div>
    </div>
    <div class="autonomy-metric-card">
      <div class="autonomy-metric-card__label">Since Last Human</div>
      <div class="autonomy-metric-card__value" id="aw-since-human">{time_since}</div>
      <div class="autonomy-metric-card__sub">since last escalation</div>
    </div>
    <div class="autonomy-metric-card">
      <div class="autonomy-metric-card__label">Active Recovery</div>
      <div class="autonomy-metric-card__value" id="aw-recovery-count">{recovery_count}</div>
      <div class="autonomy-metric-card__sub">tasks in autonomous recovery</div>
    </div>
    <div class="autonomy-metric-card">
      <div class="autonomy-metric-card__label">Goal Velocity</div>
      <div class="autonomy-metric-card__value" id="aw-velocity">{velocity}</div>
      <div class="autonomy-metric-card__sub">tasks completed per day</div>
    </div>
  </div>
</div>

<script>
(function() {{
  'use strict';

  function refreshAutonomyWidget() {{
    fetch('/api/autonomy-widget')
      .then(function(r) {{
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }})
      .then(function(data) {{
        if (!data.success && !data.autonomy_percent) return;

        var pctEl = document.getElementById('aw-autonomy-pct');
        if (pctEl) {{
          var pct = data.autonomy_percent || 0;
          pctEl.textContent = pct + '%';
          if (pct >= 90) pctEl.style.color = 'var(--status-success, #22c55e)';
          else if (pct >= 70) pctEl.style.color = 'var(--status-warning, #eab308)';
          else pctEl.style.color = 'var(--status-error, #ef4444)';
        }}

        var sinceEl = document.getElementById('aw-since-human');
        if (sinceEl) {{
          sinceEl.textContent = data.time_since_last_human_formatted || 'Never';
        }}

        var recovEl = document.getElementById('aw-recovery-count');
        if (recovEl) {{
          var cnt = data.active_recovery_attempts || 0;
          recovEl.textContent = cnt;
          recovEl.style.color = cnt > 0
            ? 'var(--status-warning, #eab308)'
            : 'var(--text-primary, #f8fafc)';
        }}

        var velEl = document.getElementById('aw-velocity');
        if (velEl) {{
          velEl.textContent = (data.goal_progress_velocity || 0);
        }}
      }})
      .catch(function() {{}});
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', refreshAutonomyWidget);
  }} else {{
    refreshAutonomyWidget();
  }}

  setInterval(refreshAutonomyWidget, 30000);
}})();
</script>
<!-- ===== End Autonomy Metrics Widget ===== -->
"""
