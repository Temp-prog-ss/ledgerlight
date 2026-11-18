"""Risk scoring (explainable) and the director network / phoenix checks."""
from datetime import date, timedelta

from app import brands, db

BAD_STATUSES = ("liquidation", "administration", "receivership", "insolvency-proceedings", "voluntary-arrangement")


def network(company_number):
    """Connected companies via shared directors, prior insolvencies and phoenix pattern."""
    keys = [r["officer_key"] for r in db.all(
        "SELECT DISTINCT officer_key FROM officers WHERE company_number=? AND officer_key IS NOT NULL", (company_number,))]
    if not keys:
        return {"connected_count": 0, "prior_insolvencies": 0, "phoenix_flag": False, "connected": []}
    marks = ",".join("?" * len(keys))
    rows = db.all(f"SELECT DISTINCT c.number, c.name, c.status, c.sic_codes, c.incorporated_on FROM officers o "
                  f"JOIN companies c ON c.number=o.company_number WHERE o.officer_key IN ({marks}) AND c.number<>?",
                  (*keys, company_number))
    me = db.one("SELECT sic_codes, incorporated_on, status FROM companies WHERE number=?", (company_number,)) or {}
    my_sic = {s.strip()[:2] for s in (me.get("sic_codes") or "").split(",") if s.strip()}
    prior = sum(1 for r in rows if (r["status"] or "") in BAD_STATUSES or (r["status"] == "dissolved"))
    phoenix = False
    if (me.get("status") or "") in BAD_STATUSES or me.get("status") == "dissolved":
        cutoff = (date.today() - timedelta(days=730)).isoformat()
        for r in rows:
            sic = {s.strip()[:2] for s in (r["sic_codes"] or "").split(",") if s.strip()}
            if r["status"] == "active" and (r["incorporated_on"] or "") >= cutoff and sic & my_sic:
                phoenix = True
    return {"connected_count": len(rows), "prior_insolvencies": prior, "phoenix_flag": phoenix,
            "connected": [dict(r) for r in rows[:25]]}


def score(event, company, net):
    """0-100 distress score plus the human-readable reasons behind it."""
    c = company or {}
    data = db.loads(event.get("data"), {}) if isinstance(event.get("data"), str) else (event.get("data") or {})
    s, reasons = brands._base(event["event_type"]), [f"{event['event_type'].replace('_', ' ')} (base {brands._base(event['event_type'])})"]

    def add(points, why):
        nonlocal s
        s += points
        reasons.append(f"{why} (+{points})")

    if c.get("accounts_overdue"):
        add(10, "accounts overdue")
    if c.get("cs_overdue"):
        add(5, "confirmation statement overdue")
    if c.get("ccj_count_12m"):
        add(min(15, 4 * int(c["ccj_count_12m"])), f"{c['ccj_count_12m']} CCJ(s) in 12 months")
    if (c.get("latest_charge_date") or "") >= (date.today() - timedelta(days=180)).isoformat():
        add(8, "new lender charge in last 6 months")
    if int(c.get("recent_resignations") or 0) >= 2:
        add(8, f"{c['recent_resignations']} director resignations in 12 months")
    if c.get("net_assets") is not None and c["net_assets"] < 0:
        add(10, "negative net assets")
    elif c.get("net_assets") and c.get("net_assets_prev") and c["net_assets"] < 0.7 * c["net_assets_prev"]:
        add(8, "net assets fell by over 30%")
    if net.get("prior_insolvencies"):
        add(min(15, 5 * net["prior_insolvencies"]), f"directors linked to {net['prior_insolvencies']} failed companies")
    if net.get("phoenix_flag"):
        add(10, "phoenix pattern: directors run a new firm in the same trade")
    if (data.get("petitioner") or "").upper().startswith("HMRC") or "REVENUE" in (data.get("petitioner") or "").upper():
        add(5, "HMRC is the petitioner")
    return max(0, min(100, s)), reasons
