#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
P18 Economic Maturity — Revenue tracking, customer acquisition, and financial operations.

This module provides economic maturity features for company operations:
1. Revenue tracking and forecasting
2. Customer acquisition (leads, trials, conversions)
3. Pricing optimization
4. Reinvestment decisions
5. Profit distribution rules

Data is stored in org.json under economics.revenue_data

Usage:
    # Record revenue
    python p18_features.py record-revenue --amount 1000 --source subscription --project forge

    # Track customer funnel
    python p18_features.py add-lead --source github --email test@example.com
    python p18_features.py convert-trial --lead-id lead-001
    python p18_features.py convert-customer --trial-id trial-001

    # Get revenue forecast
    python p18_features.py forecast --months 3

    # Calculate reinvestment allocation
    python p18_features.py reinvestment --profit 10000

    # Get profit distribution
    python p18_features.py distribute --amount 5000
"""

import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Lazy imports for sibling modules
company_resolver = None


def _ensure_imports():
    """Lazily import sibling modules."""
    global company_resolver
    if company_resolver is not None:
        return

    try:
        from . import company_resolver as cr

        company_resolver = cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]

        company_resolver = cr


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ORG_FILE = "org.json"


# Revenue source types
class RevenueSource(str, Enum):
    SUBSCRIPTION = "subscription"
    ONE_TIME = "one_time"
    CONSULTING = "consulting"
    LICENSING = "licensing"
    OTHER = "other"


# Customer funnel stages
class FunnelStage(str, Enum):
    LEAD = "lead"
    TRIAL = "trial"
    CUSTOMER = "customer"
    CHURNED = "churned"


# Lead sources
class LeadSource(str, Enum):
    GITHUB = "github"
    WEBSITE = "website"
    REFERRAL = "referral"
    CONTENT = "content"
    CONFERENCE = "conference"
    OTHER = "other"


# Pricing tiers
class PricingTier(str, Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"


# Distribution categories
class DistributionCategory(str, Enum):
    REINVESTMENT = "reinvestment"
    RESERVES = "reserves"
    DIVIDENDS = "dividends"
    BONUS_POOL = "bonus_pool"


# Default distribution rules (percentages)
DEFAULT_DISTRIBUTION_RULES = {
    DistributionCategory.REINVESTMENT.value: 50,  # 50% back into growth
    DistributionCategory.RESERVES.value: 20,  # 20% emergency reserves
    DistributionCategory.DIVIDENDS.value: 20,  # 20% to stakeholders
    DistributionCategory.BONUS_POOL.value: 10,  # 10% employee bonuses
}

# Reinvestment allocation defaults
DEFAULT_REINVESTMENT_RULES = {
    "engineering": 40,  # 40% to engineering
    "marketing": 25,  # 25% to marketing
    "operations": 20,  # 20% to operations
    "research": 15,  # 15% to R&D
}

# Pricing configuration
DEFAULT_PRICING = {
    PricingTier.FREE.value: {
        "price_monthly": 0,
        "price_yearly": 0,
        "features": ["basic"],
    },
    PricingTier.STARTER.value: {
        "price_monthly": 29,
        "price_yearly": 290,
        "features": ["basic", "support"],
    },
    PricingTier.PRO.value: {
        "price_monthly": 99,
        "price_yearly": 990,
        "features": ["basic", "support", "advanced", "api"],
    },
    PricingTier.ENTERPRISE.value: {
        "price_monthly": 499,
        "price_yearly": 4990,
        "features": ["basic", "support", "advanced", "api", "sla", "custom"],
    },
}


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class RevenueRecord:
    """A single revenue record."""

    id: str
    amount: float
    source: str  # RevenueSource value
    project_id: str | None
    timestamp: str
    description: str = ""
    recurring: bool = False
    currency: str = "USD"


@dataclass
class Lead:
    """A potential customer lead."""

    id: str
    source: str  # LeadSource value
    email: str
    created_at: str
    status: str = "active"  # active, converted, lost
    metadata: dict = field(default_factory=dict)


@dataclass
class Trial:
    """A trial customer."""

    id: str
    lead_id: str
    tier: str  # PricingTier value
    started_at: str
    expires_at: str
    status: str = "active"  # active, converted, expired
    metadata: dict = field(default_factory=dict)


@dataclass
class Customer:
    """A paying customer."""

    id: str
    trial_id: str | None
    lead_id: str | None
    tier: str  # PricingTier value
    started_at: str
    mrr: float  # Monthly recurring revenue
    status: str = "active"  # active, churned
    metadata: dict = field(default_factory=dict)


@dataclass
class RevenueForecast:
    """Revenue forecast for a period."""

    period: str  # YYYY-MM
    predicted_mrr: float
    predicted_arr: float
    confidence: float  # 0-1
    growth_rate: float  # percentage
    assumptions: list[str] = field(default_factory=list)


@dataclass
class ReinvestmentPlan:
    """A reinvestment allocation plan."""

    total_amount: float
    allocations: dict[str, float]  # department -> amount
    rationale: str
    created_at: str


@dataclass
class ProfitDistribution:
    """Profit distribution breakdown."""

    total_profit: float
    distributions: dict[str, float]  # category -> amount
    period: str
    created_at: str


# -----------------------------------------------------------------------------
# Path Utilities
# -----------------------------------------------------------------------------


def get_company_dir() -> Path:
    """Get the company directory path."""
    _ensure_imports()
    return company_resolver.get_company_dir()


def get_org_path() -> Path:
    """Get the org.json file path."""
    return get_company_dir() / ORG_FILE


def ensure_company_dir() -> Path:
    """Ensure company directory exists."""
    company_dir = get_company_dir()
    company_dir.mkdir(parents=True, exist_ok=True)
    return company_dir


# -----------------------------------------------------------------------------
# Data Access
# -----------------------------------------------------------------------------


def load_org() -> dict:
    """Load organization data from org.json."""
    path = get_org_path()

    if not path.exists():
        return {
            "company": {"name": "Unknown"},
            "employees": [],
            "economics": {},
        }

    try:
        with open(path, encoding="utf-8") as f:
            org = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "company": {"name": "Unknown"},
            "employees": [],
            "economics": {},
        }
    # Normalize bare-string employees to dict records (ProjectK root-cause fix).
    try:
        from . import company_resolver as cr
    except ImportError:
        import company_resolver as cr  # type: ignore[no-redef]
    return cr.normalize_org_employees(org, path.parent)


def save_org(org: dict):
    """Save organization data to org.json.

    Safety: Refuses to save if it would wipe existing employees.
    """
    import os
    import tempfile

    ensure_company_dir()
    path = get_org_path()

    # Safety check: Don't wipe employees if file already has them
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
            existing_employees = existing.get("employees", [])
            new_employees = org.get("employees", [])

            if len(existing_employees) > 0 and len(new_employees) == 0:
                print(
                    f"[SAFETY] Blocked save_org: Would wipe {len(existing_employees)} employees.",
                    file=sys.stderr,
                )
                return
        except (json.JSONDecodeError, OSError):
            if len(org.get("employees", [])) == 0:
                print(
                    "[SAFETY] Blocked save_org: Cannot read existing file and new data has no employees.",
                    file=sys.stderr,
                )
                return

    # Atomic write: write to temp file, then os.replace (prevents truncation race)
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix="org_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(org, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_revenue_data() -> dict:
    """Get revenue data from org.json."""
    org = load_org()
    economics = org.get("economics", {})
    return economics.get("revenue_data", get_empty_revenue_data())


def save_revenue_data(revenue_data: dict):
    """Save revenue data to org.json."""
    org = load_org()
    if "economics" not in org:
        org["economics"] = {}
    org["economics"]["revenue_data"] = revenue_data
    save_org(org)


def get_empty_revenue_data() -> dict:
    """Return empty revenue data structure."""
    return {
        "revenue_records": [],
        "leads": [],
        "trials": [],
        "customers": [],
        "forecasts": [],
        "reinvestment_plans": [],
        "distributions": [],
        "pricing_config": DEFAULT_PRICING.copy(),
        "distribution_rules": DEFAULT_DISTRIBUTION_RULES.copy(),
        "reinvestment_rules": DEFAULT_REINVESTMENT_RULES.copy(),
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "version": "1.0",
        },
    }


# -----------------------------------------------------------------------------
# Revenue Tracking
# -----------------------------------------------------------------------------


def record_revenue(
    amount: float,
    source: str,
    project_id: str | None = None,
    description: str = "",
    recurring: bool = False,
    currency: str = "USD",
) -> dict:
    """
    Record a revenue entry.

    Args:
        amount: Revenue amount
        source: Revenue source (subscription, one_time, etc.)
        project_id: Optional project attribution
        description: Optional description
        recurring: Whether this is recurring revenue
        currency: Currency code (default: USD)

    Returns:
        Dict with recording result
    """
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive"}

    # Validate source
    valid_sources = [s.value for s in RevenueSource]
    if source not in valid_sources:
        return {
            "success": False,
            "error": f"Invalid source. Must be one of: {valid_sources}",
        }

    data = get_revenue_data()
    now = datetime.now(timezone.utc).isoformat()

    record = RevenueRecord(
        id=f"rev-{uuid.uuid4().hex[:8]}",
        amount=amount,
        source=source,
        project_id=project_id,
        timestamp=now,
        description=description,
        recurring=recurring,
        currency=currency,
    )

    data["revenue_records"].append(asdict(record))
    data["metadata"]["last_updated"] = now
    save_revenue_data(data)

    return {
        "success": True,
        "record_id": record.id,
        "amount": amount,
        "source": source,
        "recurring": recurring,
    }


def get_revenue_summary(period: str | None = None) -> dict:
    """
    Get revenue summary for a period.

    Args:
        period: Optional period filter (YYYY-MM format)

    Returns:
        Dict with revenue summary
    """
    data = get_revenue_data()
    records = data.get("revenue_records", [])

    # Filter by period if specified
    if period:
        records = [r for r in records if r.get("timestamp", "").startswith(period)]

    total_revenue = sum(r.get("amount", 0) for r in records)
    recurring_revenue = sum(r.get("amount", 0) for r in records if r.get("recurring"))
    one_time_revenue = sum(
        r.get("amount", 0) for r in records if not r.get("recurring")
    )

    by_source: dict[str, float] = {}
    for r in records:
        source = r.get("source", "other")
        by_source[source] = by_source.get(source, 0) + r.get("amount", 0)

    by_project: dict[str, float] = {}
    for r in records:
        project = r.get("project_id") or "unattributed"
        by_project[project] = by_project.get(project, 0) + r.get("amount", 0)

    return {
        "success": True,
        "period": period or "all_time",
        "total_revenue": round(total_revenue, 2),
        "recurring_revenue": round(recurring_revenue, 2),
        "one_time_revenue": round(one_time_revenue, 2),
        "by_source": {k: round(v, 2) for k, v in by_source.items()},
        "by_project": {k: round(v, 2) for k, v in by_project.items()},
        "record_count": len(records),
    }


def forecast_revenue(months: int = 3) -> dict:
    """
    Generate revenue forecast.

    Args:
        months: Number of months to forecast

    Returns:
        Dict with revenue forecast
    """
    if months < 1 or months > 24:
        return {"success": False, "error": "Months must be between 1 and 24"}

    data = get_revenue_data()
    customers = [c for c in data.get("customers", []) if c.get("status") == "active"]

    # Calculate current MRR
    current_mrr = sum(c.get("mrr", 0) for c in customers)

    # Simple growth model based on recent data
    # Future: analyze historical records for growth rate calculation
    _ = data.get("revenue_records", [])  # Reserved for future growth rate analysis

    # Default growth rate (can be enhanced with historical analysis)
    growth_rate = 0.05  # 5% monthly growth assumption

    forecasts = []
    now = datetime.now(timezone.utc)

    for i in range(1, months + 1):
        forecast_month = (now.month + i - 1) % 12 + 1
        forecast_year = now.year + (now.month + i - 1) // 12
        period = f"{forecast_year:04d}-{forecast_month:02d}"

        projected_mrr = current_mrr * ((1 + growth_rate) ** i)
        projected_arr = projected_mrr * 12

        # Confidence decreases with time
        confidence = max(0.3, 1.0 - (i * 0.1))

        forecast = RevenueForecast(
            period=period,
            predicted_mrr=round(projected_mrr, 2),
            predicted_arr=round(projected_arr, 2),
            confidence=round(confidence, 2),
            growth_rate=round(growth_rate * 100, 1),
            assumptions=[
                f"Based on {len(customers)} active customers",
                f"Assumes {growth_rate * 100}% monthly growth rate",
                "No major churn events",
            ],
        )
        forecasts.append(asdict(forecast))

    return {
        "success": True,
        "current_mrr": round(current_mrr, 2),
        "current_arr": round(current_mrr * 12, 2),
        "active_customers": len(customers),
        "forecasts": forecasts,
    }


# -----------------------------------------------------------------------------
# Customer Acquisition
# -----------------------------------------------------------------------------


def add_lead(
    email: str,
    source: str,
    metadata: dict | None = None,
) -> dict:
    """
    Add a new lead to the funnel.

    Args:
        email: Lead email address
        source: Lead source (github, website, etc.)
        metadata: Optional additional metadata

    Returns:
        Dict with result
    """
    if not email or "@" not in email:
        return {"success": False, "error": "Invalid email address"}

    valid_sources = [s.value for s in LeadSource]
    if source not in valid_sources:
        return {
            "success": False,
            "error": f"Invalid source. Must be one of: {valid_sources}",
        }

    data = get_revenue_data()
    now = datetime.now(timezone.utc).isoformat()

    # Check for duplicate
    existing = [lead for lead in data.get("leads", []) if lead.get("email") == email]
    if existing:
        return {
            "success": False,
            "error": "Lead with this email already exists",
            "lead_id": existing[0].get("id"),
        }

    lead = Lead(
        id=f"lead-{uuid.uuid4().hex[:8]}",
        source=source,
        email=email,
        created_at=now,
        status="active",
        metadata=metadata or {},
    )

    data["leads"].append(asdict(lead))
    data["metadata"]["last_updated"] = now
    save_revenue_data(data)

    return {
        "success": True,
        "lead_id": lead.id,
        "email": email,
        "source": source,
    }


def convert_to_trial(
    lead_id: str,
    tier: str = "starter",
    trial_days: int = 14,
) -> dict:
    """
    Convert a lead to a trial.

    Args:
        lead_id: Lead ID to convert
        tier: Pricing tier for trial
        trial_days: Trial duration in days

    Returns:
        Dict with result
    """
    valid_tiers = [t.value for t in PricingTier]
    if tier not in valid_tiers:
        return {
            "success": False,
            "error": f"Invalid tier. Must be one of: {valid_tiers}",
        }

    data = get_revenue_data()

    # Find lead
    leads = data.get("leads", [])
    lead_entry = next((entry for entry in leads if entry.get("id") == lead_id), None)
    if not lead_entry:
        return {"success": False, "error": f"Lead not found: {lead_id}"}

    if lead_entry.get("status") != "active":
        return {
            "success": False,
            "error": f"Lead is not active: {lead_entry.get('status')}",
        }

    now = datetime.now(timezone.utc)
    expires = now.replace(hour=23, minute=59, second=59) + __import__(
        "datetime"
    ).timedelta(days=trial_days)

    trial = Trial(
        id=f"trial-{uuid.uuid4().hex[:8]}",
        lead_id=lead_id,
        tier=tier,
        started_at=now.isoformat(),
        expires_at=expires.isoformat(),
        status="active",
        metadata={"email": lead_entry.get("email")},
    )

    # Update lead status
    for entry in leads:
        if entry.get("id") == lead_id:
            entry["status"] = "converted"
            break

    data["trials"].append(asdict(trial))
    data["leads"] = leads
    data["metadata"]["last_updated"] = now.isoformat()
    save_revenue_data(data)

    return {
        "success": True,
        "trial_id": trial.id,
        "lead_id": lead_id,
        "tier": tier,
        "expires_at": expires.isoformat(),
    }


def convert_to_customer(
    trial_id: str,
    tier: str | None = None,
) -> dict:
    """
    Convert a trial to a paying customer.

    Args:
        trial_id: Trial ID to convert
        tier: Optional tier override (defaults to trial tier)

    Returns:
        Dict with result
    """
    data = get_revenue_data()
    pricing = data.get("pricing_config", DEFAULT_PRICING)

    # Find trial
    trials = data.get("trials", [])
    trial = next((t for t in trials if t.get("id") == trial_id), None)
    if not trial:
        return {"success": False, "error": f"Trial not found: {trial_id}"}

    if trial.get("status") != "active":
        return {
            "success": False,
            "error": f"Trial is not active: {trial.get('status')}",
        }

    # Determine tier
    customer_tier = tier or trial.get("tier", "starter")
    tier_pricing = pricing.get(customer_tier, pricing.get("starter", {}))
    mrr = tier_pricing.get("price_monthly", 0)

    now = datetime.now(timezone.utc)

    customer = Customer(
        id=f"cust-{uuid.uuid4().hex[:8]}",
        trial_id=trial_id,
        lead_id=trial.get("lead_id"),
        tier=customer_tier,
        started_at=now.isoformat(),
        mrr=mrr,
        status="active",
        metadata=trial.get("metadata", {}),
    )

    # Update trial status
    for t in trials:
        if t.get("id") == trial_id:
            t["status"] = "converted"
            break

    data["customers"].append(asdict(customer))
    data["trials"] = trials
    data["metadata"]["last_updated"] = now.isoformat()
    save_revenue_data(data)

    # Record recurring revenue
    record_revenue(
        amount=mrr,
        source=RevenueSource.SUBSCRIPTION.value,
        description=f"New customer: {customer.id}",
        recurring=True,
    )

    return {
        "success": True,
        "customer_id": customer.id,
        "trial_id": trial_id,
        "tier": customer_tier,
        "mrr": mrr,
    }


def get_funnel_metrics() -> dict:
    """
    Get customer acquisition funnel metrics.

    Returns:
        Dict with funnel metrics
    """
    data = get_revenue_data()

    leads = data.get("leads", [])
    trials = data.get("trials", [])
    customers = data.get("customers", [])

    active_leads = [ld for ld in leads if ld.get("status") == "active"]
    converted_leads = [ld for ld in leads if ld.get("status") == "converted"]
    active_trials = [tr for tr in trials if tr.get("status") == "active"]
    converted_trials = [tr for tr in trials if tr.get("status") == "converted"]
    active_customers = [cust for cust in customers if cust.get("status") == "active"]
    churned_customers = [cust for cust in customers if cust.get("status") == "churned"]

    # Conversion rates
    lead_to_trial = len(converted_leads) / len(leads) if leads else 0
    trial_to_customer = len(converted_trials) / len(trials) if trials else 0
    overall_conversion = len(active_customers) / len(leads) if leads else 0

    # Calculate MRR
    total_mrr = sum(cust.get("mrr", 0) for cust in active_customers)

    return {
        "success": True,
        "funnel": {
            "leads": {
                "total": len(leads),
                "active": len(active_leads),
                "converted": len(converted_leads),
            },
            "trials": {
                "total": len(trials),
                "active": len(active_trials),
                "converted": len(converted_trials),
            },
            "customers": {
                "total": len(customers),
                "active": len(active_customers),
                "churned": len(churned_customers),
            },
        },
        "conversion_rates": {
            "lead_to_trial": round(lead_to_trial * 100, 1),
            "trial_to_customer": round(trial_to_customer * 100, 1),
            "overall": round(overall_conversion * 100, 1),
        },
        "revenue": {
            "mrr": round(total_mrr, 2),
            "arr": round(total_mrr * 12, 2),
        },
    }


# -----------------------------------------------------------------------------
# Pricing Optimization
# -----------------------------------------------------------------------------


def get_pricing_config() -> dict:
    """Get current pricing configuration."""
    data = get_revenue_data()
    return {
        "success": True,
        "pricing": data.get("pricing_config", DEFAULT_PRICING),
    }


def update_pricing(
    tier: str,
    price_monthly: float | None = None,
    price_yearly: float | None = None,
    features: list[str] | None = None,
) -> dict:
    """
    Update pricing for a tier.

    Args:
        tier: Pricing tier to update
        price_monthly: New monthly price
        price_yearly: New yearly price
        features: Updated feature list

    Returns:
        Dict with result
    """
    valid_tiers = [t.value for t in PricingTier]
    if tier not in valid_tiers:
        return {
            "success": False,
            "error": f"Invalid tier. Must be one of: {valid_tiers}",
        }

    data = get_revenue_data()
    pricing = data.get("pricing_config", DEFAULT_PRICING.copy())

    if tier not in pricing:
        pricing[tier] = {"price_monthly": 0, "price_yearly": 0, "features": []}

    if price_monthly is not None:
        if price_monthly < 0:
            return {"success": False, "error": "Price cannot be negative"}
        pricing[tier]["price_monthly"] = price_monthly

    if price_yearly is not None:
        if price_yearly < 0:
            return {"success": False, "error": "Price cannot be negative"}
        pricing[tier]["price_yearly"] = price_yearly

    if features is not None:
        pricing[tier]["features"] = features

    data["pricing_config"] = pricing
    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_revenue_data(data)

    return {
        "success": True,
        "tier": tier,
        "pricing": pricing[tier],
    }


def analyze_pricing() -> dict:
    """
    Analyze pricing and suggest optimizations.

    Returns:
        Dict with pricing analysis and recommendations
    """
    data = get_revenue_data()
    pricing = data.get("pricing_config", DEFAULT_PRICING)
    customers = data.get("customers", [])

    # Distribution by tier
    tier_distribution: dict[str, int] = {}
    tier_revenue: dict[str, float] = {}

    for c in customers:
        if c.get("status") == "active":
            tier = c.get("tier", "unknown")
            tier_distribution[tier] = tier_distribution.get(tier, 0) + 1
            tier_revenue[tier] = tier_revenue.get(tier, 0) + c.get("mrr", 0)

    total_customers = sum(tier_distribution.values())

    recommendations = []

    # Check for tier imbalances
    if total_customers > 0:
        free_pct = tier_distribution.get("free", 0) / total_customers
        if free_pct > 0.7:
            recommendations.append(
                {
                    "type": "conversion",
                    "priority": "high",
                    "suggestion": "High percentage of free users. Consider improving trial conversion.",
                }
            )

        # Check yearly discount
        for tier_name, tier_data in pricing.items():
            monthly = tier_data.get("price_monthly", 0)
            yearly = tier_data.get("price_yearly", 0)
            if monthly > 0 and yearly > 0:
                yearly_monthly_equiv = yearly / 12
                discount = (monthly - yearly_monthly_equiv) / monthly
                if discount < 0.15:
                    recommendations.append(
                        {
                            "type": "pricing",
                            "priority": "medium",
                            "suggestion": f"Consider increasing yearly discount for {tier_name} (currently {discount * 100:.0f}%)",
                        }
                    )

    return {
        "success": True,
        "analysis": {
            "tier_distribution": tier_distribution,
            "tier_revenue": {k: round(v, 2) for k, v in tier_revenue.items()},
            "total_customers": total_customers,
        },
        "recommendations": recommendations,
    }


# -----------------------------------------------------------------------------
# Reinvestment Decisions
# -----------------------------------------------------------------------------


def get_reinvestment_rules() -> dict:
    """Get current reinvestment allocation rules."""
    data = get_revenue_data()
    return {
        "success": True,
        "rules": data.get("reinvestment_rules", DEFAULT_REINVESTMENT_RULES),
    }


def update_reinvestment_rules(rules: dict[str, float]) -> dict:
    """
    Update reinvestment allocation rules.

    Args:
        rules: Department -> percentage mapping

    Returns:
        Dict with result
    """
    # Validate percentages sum to 100
    total = sum(rules.values())
    if abs(total - 100) > 0.01:
        return {"success": False, "error": f"Percentages must sum to 100, got {total}"}

    # Validate no negative values
    for dept, pct in rules.items():
        if pct < 0:
            return {"success": False, "error": f"Negative percentage for {dept}"}

    data = get_revenue_data()
    data["reinvestment_rules"] = rules
    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_revenue_data(data)

    return {
        "success": True,
        "rules": rules,
    }


def calculate_reinvestment(profit: float, rationale: str = "") -> dict:
    """
    Calculate reinvestment allocation based on current rules.

    Args:
        profit: Total profit to reinvest
        rationale: Reason for reinvestment

    Returns:
        Dict with reinvestment plan
    """
    if profit <= 0:
        return {"success": False, "error": "Profit must be positive"}

    data = get_revenue_data()
    rules = data.get("reinvestment_rules", DEFAULT_REINVESTMENT_RULES)
    distribution_rules = data.get("distribution_rules", DEFAULT_DISTRIBUTION_RULES)

    # First determine how much goes to reinvestment
    reinvest_pct = distribution_rules.get(DistributionCategory.REINVESTMENT.value, 50)
    reinvestment_amount = profit * (reinvest_pct / 100)

    # Then allocate by department
    allocations = {}
    for dept, pct in rules.items():
        allocations[dept] = round(reinvestment_amount * (pct / 100), 2)

    now = datetime.now(timezone.utc).isoformat()
    plan = ReinvestmentPlan(
        total_amount=round(reinvestment_amount, 2),
        allocations=allocations,
        rationale=rationale or "Standard allocation per rules",
        created_at=now,
    )

    # Save plan
    data["reinvestment_plans"].append(asdict(plan))
    data["metadata"]["last_updated"] = now
    save_revenue_data(data)

    return {
        "success": True,
        "total_profit": round(profit, 2),
        "reinvestment_amount": round(reinvestment_amount, 2),
        "allocations": allocations,
        "plan_id": f"plan-{uuid.uuid4().hex[:8]}",
    }


# -----------------------------------------------------------------------------
# Profit Distribution
# -----------------------------------------------------------------------------


def get_distribution_rules() -> dict:
    """Get current profit distribution rules."""
    data = get_revenue_data()
    return {
        "success": True,
        "rules": data.get("distribution_rules", DEFAULT_DISTRIBUTION_RULES),
    }


def update_distribution_rules(rules: dict[str, float]) -> dict:
    """
    Update profit distribution rules.

    Args:
        rules: Category -> percentage mapping

    Returns:
        Dict with result
    """
    # Validate categories
    valid_categories = [c.value for c in DistributionCategory]
    for category in rules.keys():
        if category not in valid_categories:
            return {
                "success": False,
                "error": f"Invalid category: {category}. Must be one of: {valid_categories}",
            }

    # Validate percentages sum to 100
    total = sum(rules.values())
    if abs(total - 100) > 0.01:
        return {"success": False, "error": f"Percentages must sum to 100, got {total}"}

    # Validate no negative values
    for category, pct in rules.items():
        if pct < 0:
            return {"success": False, "error": f"Negative percentage for {category}"}

    data = get_revenue_data()
    data["distribution_rules"] = rules
    data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_revenue_data(data)

    return {
        "success": True,
        "rules": rules,
    }


def distribute_profit(amount: float, period: str | None = None) -> dict:
    """
    Calculate profit distribution.

    Args:
        amount: Total profit to distribute
        period: Optional period identifier (YYYY-MM)

    Returns:
        Dict with distribution breakdown
    """
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive"}

    data = get_revenue_data()
    rules = data.get("distribution_rules", DEFAULT_DISTRIBUTION_RULES)

    now = datetime.now(timezone.utc)
    period = period or now.strftime("%Y-%m")

    distributions = {}
    for category, pct in rules.items():
        distributions[category] = round(amount * (pct / 100), 2)

    distribution = ProfitDistribution(
        total_profit=round(amount, 2),
        distributions=distributions,
        period=period,
        created_at=now.isoformat(),
    )

    # Save distribution
    data["distributions"].append(asdict(distribution))
    data["metadata"]["last_updated"] = now.isoformat()
    save_revenue_data(data)

    return {
        "success": True,
        "total_profit": round(amount, 2),
        "period": period,
        "distributions": distributions,
    }


def get_distribution_history(periods: int = 6) -> dict:
    """
    Get profit distribution history.

    Args:
        periods: Number of periods to return

    Returns:
        Dict with distribution history
    """
    data = get_revenue_data()
    distributions = data.get("distributions", [])

    # Sort by period descending
    distributions.sort(key=lambda d: d.get("period", ""), reverse=True)

    # Limit to requested periods
    recent = distributions[:periods]

    return {
        "success": True,
        "history": recent,
        "total_distributed": sum(d.get("total_profit", 0) for d in recent),
    }


# -----------------------------------------------------------------------------
# CLI Interface
# -----------------------------------------------------------------------------


def print_help():
    """Print help information."""
    help_text = """
P18 Economic Maturity — Revenue Tracking & Financial Operations

Commands:
    record-revenue        Record a revenue entry
    revenue-summary       Get revenue summary
    forecast              Generate revenue forecast

    add-lead              Add a new lead
    convert-trial         Convert lead to trial
    convert-customer      Convert trial to customer
    funnel                Get funnel metrics

    pricing               Get pricing configuration
    update-pricing        Update tier pricing
    analyze-pricing       Get pricing analysis

    reinvestment-rules    Get reinvestment rules
    reinvestment          Calculate reinvestment allocation

    distribution-rules    Get distribution rules
    distribute            Calculate profit distribution
    distribution-history  Get distribution history

    help                  Show this help

Examples:
    # Record revenue
    python p18_features.py record-revenue --amount 1000 --source subscription

    # Add and convert lead
    python p18_features.py add-lead --email test@example.com --source github
    python p18_features.py convert-trial --lead-id lead-abc123
    python p18_features.py convert-customer --trial-id trial-xyz789

    # Get forecast
    python p18_features.py forecast --months 3

    # Calculate reinvestment
    python p18_features.py reinvestment --profit 10000

    # Distribute profit
    python p18_features.py distribute --amount 5000
"""
    print(help_text)


def main():
    """Main CLI entry point."""
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print_help()
        return

    command = args[0]
    result: dict[str, Any] = {}

    try:
        if command == "record-revenue":
            amount = 0.0
            source = "other"
            project = None
            description = ""
            recurring = False

            for i, arg in enumerate(args[1:], 1):
                if arg == "--amount" and i < len(args):
                    amount = float(args[i + 1])
                elif arg == "--source" and i < len(args):
                    source = args[i + 1]
                elif arg == "--project" and i < len(args):
                    project = args[i + 1]
                elif arg == "--description" and i < len(args):
                    description = args[i + 1]
                elif arg == "--recurring":
                    recurring = True

            result = record_revenue(amount, source, project, description, recurring)

        elif command == "revenue-summary":
            period = None
            for i, arg in enumerate(args[1:], 1):
                if arg == "--period" and i < len(args):
                    period = args[i + 1]
            result = get_revenue_summary(period)

        elif command == "forecast":
            months = 3
            for i, arg in enumerate(args[1:], 1):
                if arg == "--months" and i < len(args):
                    months = int(args[i + 1])
            result = forecast_revenue(months)

        elif command == "add-lead":
            email = ""
            source = "other"
            for i, arg in enumerate(args[1:], 1):
                if arg == "--email" and i < len(args):
                    email = args[i + 1]
                elif arg == "--source" and i < len(args):
                    source = args[i + 1]
            result = add_lead(email, source)

        elif command == "convert-trial":
            lead_id = ""
            tier = "starter"
            for i, arg in enumerate(args[1:], 1):
                if arg == "--lead-id" and i < len(args):
                    lead_id = args[i + 1]
                elif arg == "--tier" and i < len(args):
                    tier = args[i + 1]
            result = convert_to_trial(lead_id, tier)

        elif command == "convert-customer":
            trial_id = ""
            tier = None
            for i, arg in enumerate(args[1:], 1):
                if arg == "--trial-id" and i < len(args):
                    trial_id = args[i + 1]
                elif arg == "--tier" and i < len(args):
                    tier = args[i + 1]
            result = convert_to_customer(trial_id, tier)

        elif command == "funnel":
            result = get_funnel_metrics()

        elif command == "pricing":
            result = get_pricing_config()

        elif command == "update-pricing":
            tier = ""
            monthly = None
            yearly = None
            for i, arg in enumerate(args[1:], 1):
                if arg == "--tier" and i < len(args):
                    tier = args[i + 1]
                elif arg == "--monthly" and i < len(args):
                    monthly = float(args[i + 1])
                elif arg == "--yearly" and i < len(args):
                    yearly = float(args[i + 1])
            result = update_pricing(tier, monthly, yearly)

        elif command == "analyze-pricing":
            result = analyze_pricing()

        elif command == "reinvestment-rules":
            result = get_reinvestment_rules()

        elif command == "reinvestment":
            profit = 0.0
            rationale = ""
            for i, arg in enumerate(args[1:], 1):
                if arg == "--profit" and i < len(args):
                    profit = float(args[i + 1])
                elif arg == "--rationale" and i < len(args):
                    rationale = args[i + 1]
            result = calculate_reinvestment(profit, rationale)

        elif command == "distribution-rules":
            result = get_distribution_rules()

        elif command == "distribute":
            amount = 0.0
            period = None
            for i, arg in enumerate(args[1:], 1):
                if arg == "--amount" and i < len(args):
                    amount = float(args[i + 1])
                elif arg == "--period" and i < len(args):
                    period = args[i + 1]
            result = distribute_profit(amount, period)

        elif command == "distribution-history":
            periods = 6
            for i, arg in enumerate(args[1:], 1):
                if arg == "--periods" and i < len(args):
                    periods = int(args[i + 1])
            result = get_distribution_history(periods)

        else:
            result = {"success": False, "error": f"Unknown command: {command}"}

    except Exception as e:
        result = {"success": False, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
