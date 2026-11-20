"""Shared read model: what one org may see, projected to its plan's level.

Used by the dashboard API, alerts, digests and every CSV export, so the same
rules (brand, vetting, cooling-off, plan level) apply everywhere.
"""
import csv
import io
from datetime import date

from app import brands, db, fields, plans
from app.pipeline import compliance, score


def org_level(org):
    return plans.entitlements(org)["level"]


def watch_items(org_id):
    rows = db.all("SELECT i.* FROM watchlist_items i JOIN watchlists w ON w.id=i.watchlist_id WHERE w.org_id=?", (org_id,))
    return {r["company_number"]: r for r in rows}


def ctx(event, company, watch=None, relevance=None, level=1):
    c = {"event": dict(event, data=db.loads(event.get("data"), {}) if isinstance(event.get("data"), str) else event.get("data")),
         "company": company, "watch": watch, "relevance": relevance}
    if level >= 4 and company:
        net = score.network(company["number"])
        c.update(connected_count=net["connected_count"], phoenix_flag=net["phoenix_flag"], prior_insolvencies=net["prior_insolvencies"])
    elif level >= 3 and company:
        c["prior_insolvencies"] = score.network(company["number"])["prior_insolvencies"]
    return c


def visible_events(org, filters=None, limit=100, offset=0, include_embargoed=False):
    """Events this org may see: its brand's feed plus anything on its watchlists."""
    f = filters or {}
    brand = org["brand"]
    b = brands.get(brand)
    if b.vetted_only and not org.get("vetted"):
        return []
    watch = watch_items(org["id"])
    watch_nums = list(watch)
    ent = plans.entitlements(org)
    params, where = [brand], ["e.status='processed'", "e.event_type NOT IN ('bankruptcy','iva','dro')"]
    if not b.allows_personal_data:
        where.append("COALESCE(e.personal_data,0)=0")
    if not include_embargoed:
        where.append("(e.restricted_until IS NULL OR e.restricted_until<=?)")
        params.append(date.today().isoformat())
    if f.get("type"):
        where.append("e.event_type=?")
        params.append(f["type"])
    if f.get("min_score") and ent["level"] >= 3:  # scores are an Advanced field
        where.append("COALESCE(e.risk_score,0)>=?")
        params.append(int(f["min_score"]))
    if f.get("since"):
        where.append("e.published_at>=?")
        params.append(f["since"])
    if f.get("company"):
        where.append("e.company_number=?")
        params.append(f["company"])
    if f.get("until"):
        where.append("SUBSTR(e.published_at,1,10)<=?")
        params.append(f["until"][:10])
    if f.get("region"):
        where.append("c.region=?")
        params.append(f["region"])
    if f.get("q"):
        where.append("(UPPER(c.name) LIKE ? OR UPPER(e.title) LIKE ? OR e.company_number=?)")
        q = f["q"].upper()
        params += [f"%{q}%", f"%{q}%", q]
    region_cap = ent.get("regions")
    if region_cap:  # e.g. Solo: own region(s) only, plus anything on a watchlist
        regions = (db.loads(org.get("regions"), []) or [])[:region_cap]
        if regions:
            where.append(f"(c.region IN ({','.join('?' * len(regions))})" + (f" OR e.company_number IN ({','.join('?' * len(watch_nums))}))" if watch_nums else ")"))
            params += regions + watch_nums
        else:
            f = dict(f, watchlist=True)
    base = (f"SELECT e.*, eb.relevance AS brand_relevance FROM events e LEFT JOIN event_brands eb ON eb.event_id=e.id AND eb.brand=? "
            f"LEFT JOIN companies c ON c.number=e.company_number WHERE {' AND '.join(where)} ")
    if f.get("watchlist"):
        if not watch_nums:
            return []
        base += f"AND e.company_number IN ({','.join('?' * len(watch_nums))}) "
        params += watch_nums
    elif watch_nums:
        base += f"AND (eb.brand IS NOT NULL OR e.company_number IN ({','.join('?' * len(watch_nums))})) "
        params += watch_nums
    else:
        base += "AND eb.brand IS NOT NULL "
    base += "ORDER BY e.published_at DESC, e.id LIMIT ? OFFSET ?"
    rows = db.all(base, (*params, int(limit), int(offset)))
    out = []
    for e in rows:
        if not compliance.can_route(brand, org, e):
            continue
        if not include_embargoed and compliance.embargoed(e):
            continue
        out.append(e)
    return out


def project_events(org, rows, level=None):
    """level: project at a simpler level than the plan allows (never higher; callers check)."""
    level = min(level or org_level(org), org_level(org))
    watch = watch_items(org["id"])
    out = []
    for e in rows:
        company = db.one("SELECT * FROM companies WHERE number=?", (e["company_number"],)) if e.get("company_number") else None
        w = watch.get(e.get("company_number"))
        rel = brands.relevance(org["brand"], e["event_type"], company, on_watchlist=bool(w))
        if rel is None:
            rel = e.get("brand_relevance")
        row = fields.project(ctx(e, company, w, rel, level), level, org["brand"])
        if row.get("business_phone") and compliance.suppressed("phone", row["business_phone"]):
            row["business_phone"] = None
        if row.get("ip_email") and compliance.suppressed("email", row["ip_email"]):
            row["ip_email"] = None
        out.append(row)
    return out


def to_csv(rows, columns=None):
    if not rows and not columns:
        return "﻿"
    cols = columns or list(rows[0].keys())
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return "﻿" + buf.getvalue()


def bump_usage(org_id, briefs=0, exports=0, rows=0, insight=0):
    month = date.today().strftime("%Y-%m")
    if not db.one("SELECT org_id FROM usage WHERE org_id=? AND month=?", (org_id, month)):
        db.insert("usage", {"org_id": org_id, "month": month, "briefs": 0, "exports": 0, "export_rows": 0, "insight_lookups": 0})
    db.execute("UPDATE usage SET briefs=briefs+?, exports=exports+?, export_rows=export_rows+?, "
               "insight_lookups=COALESCE(insight_lookups,0)+? WHERE org_id=? AND month=?",
               (briefs, exports, rows, insight, org_id, month))


def export_allowance(org):
    """Rows this org may still export this month, and the most per single file."""
    ent = plans.entitlements(org)
    month = date.today().strftime("%Y-%m")
    used = (db.one("SELECT export_rows FROM usage WHERE org_id=? AND month=?", (org["id"], month)) or {}).get("export_rows", 0) or 0
    cap = ent.get("export_rows_month")
    remaining = None if cap is None else max(0, cap - used)
    per_file = ent.get("rows_per_export") or 5000
    return {"rows_per_export": per_file, "rows_month": cap, "used_month": used, "remaining_month": remaining,
            "max_now": per_file if remaining is None else min(per_file, remaining)}
