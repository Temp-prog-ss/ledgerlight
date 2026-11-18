"""Plans and what each one unlocks. Prices match the websites (annual = 10 x monthly)."""
import os

LEVELS = {1: "Simple", 2: "Standard", 3: "Advanced", 4: "Full record"}

PLANS = {
    "solo": dict(name="Solo", monthly_gbp=29, level=1, regions=1, watchlist_limit=25, briefs_per_month=50,
                 seats=1, rows_per_export=500, export_rows_month=2000, channels={"email"}, instant=False, file_drops=False, crm_sync=False, wider_risk_alerts=False),
    "team": dict(name="Team", monthly_gbp=99, level=2, regions=None, watchlist_limit=250, briefs_per_month=500,
                 seats=3, rows_per_export=5000, export_rows_month=25000, channels={"email", "slack", "teams"}, instant=True, file_drops=False, crm_sync=False, wider_risk_alerts=False),
    "pro": dict(name="Pro", monthly_gbp=299, level=3, regions=None, watchlist_limit=2000, briefs_per_month=None,
                seats=10, rows_per_export=25000, export_rows_month=250000, channels={"email", "slack", "teams", "sms", "sftp", "hubspot"},
                instant=True, file_drops=True, crm_sync=True, wider_risk_alerts=True),
    "enterprise": dict(name="Enterprise", monthly_gbp=None, level=4, regions=None, watchlist_limit=None, briefs_per_month=None,
                       seats=None, rows_per_export=100000, export_rows_month=None, channels={"email", "slack", "teams", "sms", "sftp", "hubspot"},
                       instant=True, file_drops=True, crm_sync=True, wider_risk_alerts=True),
}
TRIAL_DAYS = 14
TRIAL_PLAN = "team"  # trials get Team features so customers see instant alerts


def annual_gbp(plan_id):
    m = PLANS[plan_id]["monthly_gbp"]
    return None if m is None else m * 10


def stripe_price_id(plan_id, cycle):
    """Stripe price ids are configured per plan and cycle, e.g. STRIPE_PRICE_TEAM_ANNUAL."""
    return os.environ.get(f"STRIPE_PRICE_{plan_id.upper()}_{cycle.upper()}", "")


def plan_for_price(price_id):
    for pid in PLANS:
        for cycle in ("monthly", "annual"):
            if price_id and stripe_price_id(pid, cycle) == price_id:
                return pid, cycle
    return None, None


def entitlements(org):
    """What an org can use right now, given plan and billing status."""
    plan_id = org["plan"]
    if org["status"] == "trialing":
        plan_id = TRIAL_PLAN  # every trial gets exactly Team features, whatever plan was picked
    if org["status"] in ("canceled", "unpaid"):
        return dict(PLANS["solo"], active=False, plan_id=org["plan"])
    return dict(PLANS[plan_id], active=True, plan_id=plan_id)


def plan_for_level(level):
    """Cheapest plan that unlocks a field level (for "upgrade to ..." labels)."""
    for pid in ("solo", "team", "pro", "enterprise"):
        if PLANS[pid]["level"] >= level:
            return pid
    return "enterprise"
