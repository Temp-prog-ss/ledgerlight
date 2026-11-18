"""Customer portal (sign in + manual data extraction) and the staff admin dashboard.

Pages:   /app    customers sign in, build an extract within their plan, download CSV
         /admin  staff only: platform health, customers, data, review queues, jobs, audit
Both pages call the in-house JSON API below with the signed-in user's token.
"""
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from app import brands, db, fields, plans, security, views
from app.sources.companies_house import REGIONS
from app.web import ApiError, _client_ip, _csv, auth_throttle, body, current, ok, require_active, require_staff

HERE = Path(__file__).parent / "pages"
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREVIEW_ROWS = 25
COUNT_CAP = 5000
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


def _page(name):
    return HTMLResponse((HERE / name).read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


async def app_page(request):
    return _page("portal.html")


async def admin_page(request):
    return _page("admin.html")


async def root(request):
    return RedirectResponse("/app", status_code=302)


def _label(t):
    return t.replace("_", " ").capitalize()


# ---------------- public ----------------

async def public_brands(request):
    return ok({"brands": [{"id": b.id, "name": b.name, "tagline": b.tagline, "accent": b.accent, "vetted_only": b.vetted_only}
                          for b in brands.BRANDS.values()], "regions": sorted(REGIONS)})


# ---------------- account ----------------

class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


async def change_password(request):
    if not auth_throttle.allow(_client_ip(request)):
        raise ApiError(429, "Too many attempts, try again in a minute")
    user, org = current(request)
    s = await body(request, PasswordIn)
    if not security.verify_password(s.current_password, user["password_hash"]):
        raise ApiError(401, "Your current password is wrong")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(s.new_password), user["id"]))
    db.audit(user["id"], "password_changed", {})
    return ok({"changed": True})


async def list_members(request):
    user, org = current(request)
    rows = db.all("SELECT id, email, name, role, created_at, last_login FROM users WHERE org_id=? ORDER BY created_at", (org["id"],))
    return ok({"members": rows, "seats": plans.entitlements(org)["seats"]})


# ---------------- extraction ----------------

def _extract_params(request, org):
    """Validate the extract builder's filters, level and columns against the org's plan."""
    q = request.query_params
    ent = require_active(org)
    b = brands.get(org["brand"])
    f = {"watchlist": q.get("watchlist") in ("1", "true")}
    t = q.get("type")
    if t:
        if t not in b.event_types:
            raise ApiError(422, "That event type isn't part of your product")
        f["type"] = t
    region = q.get("region")
    if region:
        if region not in REGIONS:
            raise ApiError(422, "Unknown region")
        f["region"] = region
    for k in ("since", "until"):
        v = q.get(k)
        if v:
            if not DATE_RX.match(v):
                raise ApiError(422, f"{k} must be a date like 2026-11-01")
            f[k] = v
    if q.get("q"):
        f["q"] = q["q"].strip()[:100]
    if q.get("min_score"):
        if ent["level"] < 3:
            raise ApiError(402, f"Filtering by risk score needs the {plans.PLANS[plans.plan_for_level(3)]['name']} plan")
        try:
            f["min_score"] = max(0, min(100, int(q["min_score"])))
        except ValueError:
            raise ApiError(422, "min_score must be a number from 0 to 100")
    try:
        level = int(q.get("level") or ent["level"])
    except ValueError:
        raise ApiError(422, "level must be 1 to 4")
    if level < 1 or level > 4:
        raise ApiError(422, "level must be 1 to 4")
    if level > ent["level"]:
        need = plans.plan_for_level(level)
        raise ApiError(402, f"{plans.LEVELS[level]} extraction needs the {plans.PLANS[need]['name']} plan")
    allowed = [c[0] for c in fields.columns(level, org["brand"])]
    wanted = [x for x in (q.get("fields") or "").split(",") if x]
    if wanted:
        everything = {c[0]: c for c in fields.catalogue(org["brand"])}
        bad = [x for x in wanted if x not in everything]
        if bad:
            raise ApiError(422, f"Unknown fields: {', '.join(bad)}")
        locked = [x for x in wanted if x not in allowed]
        if locked:
            raise ApiError(402, f"Not included at {plans.LEVELS[level]} level: {', '.join(locked)}")
        cols = [c for c in allowed if c in wanted]  # keep catalogue order
    else:
        cols = allowed
    return f, level, cols


def _project_at(org, rows, level):
    return views.project_events(org, rows, level)


async def extract_options(request):
    user, org = current(request)
    ent = plans.entitlements(org)
    b = brands.get(org["brand"])
    level = ent["level"]
    cat = fields.catalogue(org["brand"])
    counts = {r["event_type"]: r["n"] for r in db.all(
        "SELECT e.event_type, COUNT(*) AS n FROM events e JOIN event_brands eb ON eb.event_id=e.id AND eb.brand=? "
        "WHERE e.status='processed' GROUP BY e.event_type", (org["brand"],))}
    org_regions = db.loads(org["regions"], []) or []
    return ok({
        "brand": {"id": b.id, "name": b.name, "tagline": b.tagline, "accent": b.accent, "vetted_only": b.vetted_only},
        "vetted": bool(org["vetted"]), "active": ent["active"], "status": org["status"], "trial_ends_at": org["trial_ends_at"],
        "plan": {"id": org["plan"], "name": plans.PLANS[org["plan"]]["name"], "effective": ent["plan_id"],
                 "effective_name": plans.PLANS[ent["plan_id"]]["name"]},
        "level": level, "level_name": plans.LEVELS[level],
        "levels": [{"level": n, "name": nm, "unlocked": n <= level, "plan_needed": plans.PLANS[plans.plan_for_level(n)]["name"],
                    "field_count": len([c for c in cat if c[3] <= n])} for n, nm in plans.LEVELS.items()],
        "fields": [{"key": k, "label": lab, "description": d, "level": lv, "level_name": plans.LEVELS[lv], "unlocked": lv <= level,
                    "plan_needed": plans.PLANS[plans.plan_for_level(lv)]["name"]} for k, lab, d, lv, _b, _g in cat],
        "event_types": [{"id": t, "label": _label(t), "count": counts.get(t, 0)} for t in sorted(b.event_types)],
        "regions": sorted(REGIONS), "region_cap": ent.get("regions"), "org_regions": org_regions,
        "score_filter": level >= 3, "watchlist_count": db.one(
            "SELECT COUNT(*) AS n FROM watchlist_items i JOIN watchlists w ON w.id=i.watchlist_id WHERE w.org_id=?", (org["id"],))["n"],
        "allowance": views.export_allowance(org),
    })


async def extract_preview(request):
    user, org = current(request)
    f, level, cols = _extract_params(request, org)
    rows = views.visible_events(org, f, limit=COUNT_CAP + 1)
    labels = {c[0]: c[1] for c in fields.catalogue(org["brand"])}
    proj = _project_at(org, rows[:PREVIEW_ROWS], level)
    return ok({"total": min(len(rows), COUNT_CAP), "more": len(rows) > COUNT_CAP, "level": level, "level_name": plans.LEVELS[level],
               "columns": [{"key": c, "label": labels[c]} for c in cols],
               "rows": [{c: r.get(c) for c in cols} for r in proj], "allowance": views.export_allowance(org)})


async def extract_csv(request):
    user, org = current(request)
    f, level, cols = _extract_params(request, org)
    allow = views.export_allowance(org)
    if allow["max_now"] <= 0:
        raise ApiError(402, "You have used this month's export allowance. Upgrade your plan for more rows.")
    try:
        want = int(request.query_params.get("max_rows") or allow["max_now"])
    except ValueError:
        raise ApiError(422, "max_rows must be a number")
    limit = max(1, min(want, allow["max_now"]))
    rows = views.visible_events(org, f, limit=limit)
    proj = _project_at(org, rows, level)
    views.bump_usage(org["id"], exports=1, rows=len(proj))
    db.audit(user["id"], "extract_csv", {"org": org["id"], "rows": len(proj), "level": level, "filters": f, "columns": len(cols)})
    resp = _csv(views.to_csv(proj, cols), f"{org['brand']}_{plans.LEVELS[level].lower().replace(' ', '-')}_{date.today()}.csv")
    resp.headers["X-Rows"] = str(len(proj))
    return resp


# ---------------- admin ----------------

def _n(sql, p=()):
    return (db.one(sql, p) or {}).get("n", 0) or 0


def _job_health():
    from app import worker
    states = {r["key"][4:]: r for r in db.all("SELECT * FROM ingest_state WHERE key LIKE 'job:%'")}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for job, every in worker.SCHEDULE.items():
        st = states.get(job)
        value = (st or {}).get("value") or ""
        parts = value.split(" ", 2)
        when, status, result = (parts + ["", "", ""])[:3]
        stale = False
        try:
            last = datetime.fromisoformat(when.replace("Z", "")[:19])
            stale = (now - last).total_seconds() > max(every * 3, 900)
        except ValueError:
            last = None
        out.append({"job": job, "every_seconds": every, "last_run": when or None, "status": status or "never",
                    "result": result[:300], "stale": bool(stale) if last else False})
    return out


def _awaiting_vetting():
    vetted_brands = [b.id for b in brands.BRANDS.values() if b.vetted_only]
    if not vetted_brands:
        return 0
    return _n(f"SELECT COUNT(*) AS n FROM orgs WHERE vetted=0 AND brand IN ({','.join('?' * len(vetted_brands))})", tuple(vetted_brands))


async def admin_overview(request):
    require_staff(request)
    today = date.today()
    month = today.strftime("%Y-%m")
    week = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    per_day = {r["d"]: r["n"] for r in db.all(
        "SELECT SUBSTR(created_at,1,10) AS d, COUNT(*) AS n FROM events WHERE created_at>=? GROUP BY SUBSTR(created_at,1,10)", (week[0],))}
    active = db.all("SELECT plan, billing_cycle FROM orgs WHERE status='active'")
    mrr = sum((plans.PLANS[o["plan"]]["monthly_gbp"] or 0) for o in active if o["plan"] in plans.PLANS)
    try:
        from app.outreach import engine
        outreach = engine.stats()
    except Exception:  # pragma: no cover
        outreach = {}
    return ok({
        "kpis": {
            "companies": _n("SELECT COUNT(*) AS n FROM companies"),
            "events_total": _n("SELECT COUNT(*) AS n FROM events"),
            "events_today": _n("SELECT COUNT(*) AS n FROM events WHERE created_at>=?", (today.isoformat(),)),
            "events_waiting": _n("SELECT COUNT(*) AS n FROM events WHERE status='new'"),
            "customers": _n("SELECT COUNT(*) AS n FROM orgs o WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.org_id=o.id AND u.role='staff')"),
            "paying": _n("SELECT COUNT(*) AS n FROM orgs WHERE status='active'"),
            "trialing": _n("SELECT COUNT(*) AS n FROM orgs WHERE status='trialing'"),
            "trials_ending_3d": _n("SELECT COUNT(*) AS n FROM orgs WHERE status='trialing' AND trial_ends_at<=?", ((today + timedelta(days=3)).isoformat(),)),
            "awaiting_vetting": _awaiting_vetting(),
            "mrr_gbp": mrr,
            "export_rows_month": _n("SELECT COALESCE(SUM(export_rows),0) AS n FROM usage WHERE month=?", (month,)),
            "sponsors": _n("SELECT COUNT(*) AS n FROM sponsor_register WHERE removed_at IS NULL"),
            "unclaimed_estates": _n("SELECT COUNT(*) AS n FROM unclaimed_estates WHERE removed_at IS NULL"),
            "risk_review": _n("SELECT COUNT(*) AS n FROM risk_factors WHERE needs_review=1"),
            "suppressions": _n("SELECT COUNT(*) AS n FROM suppressions"),
        },
        "events_per_day": [{"date": d, "n": per_day.get(d, 0)} for d in week],
        "events_by_type": db.all("SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type ORDER BY n DESC LIMIT 20"),
        "events_by_brand": db.all("SELECT brand, COUNT(*) AS n FROM event_brands GROUP BY brand ORDER BY n DESC"),
        "customers_by_brand": db.all("SELECT brand, status, COUNT(*) AS n FROM orgs GROUP BY brand, status ORDER BY brand"),
        "deliveries": db.all("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"),
        "jobs": _job_health(),
        "streams": [r for r in db.all("SELECT * FROM ingest_state WHERE key NOT LIKE 'job:%' ORDER BY key")
                    if not r["key"].startswith(("stripe_last_event", "unclaimed_estates_headers"))][:40],
        "outreach": outreach,
    })


async def admin_customers(request):
    require_staff(request)
    month = date.today().strftime("%Y-%m")
    q = (request.query_params.get("q") or "").strip().upper()
    rows = db.all(
        "SELECT o.id, o.name, o.brand, o.plan, o.billing_cycle, o.status, o.trial_ends_at, o.vetted, o.created_at, "
        "(SELECT email FROM users u WHERE u.org_id=o.id AND u.role IN ('owner','staff') ORDER BY u.created_at LIMIT 1) AS owner_email, "
        "(SELECT COUNT(*) FROM users u WHERE u.org_id=o.id) AS users, "
        "(SELECT MAX(last_login) FROM users u WHERE u.org_id=o.id) AS last_login, "
        "(SELECT COUNT(*) FROM users u WHERE u.org_id=o.id AND u.role='staff') AS staff_users, "
        "COALESCE(us.export_rows,0) AS export_rows, COALESCE(us.exports,0) AS exports "
        "FROM orgs o LEFT JOIN usage us ON us.org_id=o.id AND us.month=? ORDER BY o.created_at DESC LIMIT 1000", (month,))
    if q:
        rows = [r for r in rows if q in (r["name"] or "").upper() or q in (r["owner_email"] or "").upper() or q in r["id"].upper()]
    for r in rows:
        r["vetted"] = bool(r["vetted"])
        r["is_internal"] = bool(r.pop("staff_users"))
        r["brand_name"] = brands.get(r["brand"]).name if brands.get(r["brand"]) else r["brand"]
        r["needs_vetting"] = bool(brands.get(r["brand"]) and brands.get(r["brand"]).vetted_only and not r["vetted"])
    return ok({"customers": rows, "plans": [{"id": k, "name": v["name"]} for k, v in plans.PLANS.items()]})


async def admin_customer(request):
    require_staff(request)
    org = db.one("SELECT * FROM orgs WHERE id=?", (request.path_params["id"],))
    if not org:
        raise ApiError(404, "Customer not found")
    users = db.all("SELECT id, email, name, role, created_at, last_login FROM users WHERE org_id=? ORDER BY created_at", (org["id"],))
    ids = [u["id"] for u in users]
    activity = db.all(f"SELECT actor, action, detail, at FROM audit_log WHERE actor IN ({','.join('?' * len(ids))}) ORDER BY at DESC LIMIT 50",
                      tuple(ids)) if ids else []
    ent = plans.entitlements(org)
    return ok({
        "org": {k: org[k] for k in ("id", "name", "brand", "plan", "billing_cycle", "status", "trial_ends_at", "created_at",
                                    "stripe_customer_id", "timezone", "digest_hour")} | {"vetted": bool(org["vetted"]),
                                                                                          "regions": db.loads(org["regions"], [])},
        "entitlements": {k: (sorted(v) if isinstance(v, set) else v) for k, v in ent.items()},
        "users": users,
        "usage": db.all("SELECT * FROM usage WHERE org_id=? ORDER BY month DESC LIMIT 12", (org["id"],)),
        "watchlist_items": _n("SELECT COUNT(*) AS n FROM watchlist_items i JOIN watchlists w ON w.id=i.watchlist_id WHERE w.org_id=?", (org["id"],)),
        "rules": _n("SELECT COUNT(*) AS n FROM alert_rules WHERE org_id=?", (org["id"],)),
        "channels": db.all("SELECT kind, target, active FROM channels WHERE org_id=?", (org["id"],)),
        "deliveries": db.all("SELECT status, COUNT(*) AS n FROM deliveries WHERE org_id=? GROUP BY status", (org["id"],)),
        "activity": [dict(a, detail=db.loads(a["detail"], {})) for a in activity],
        "allowance": views.export_allowance(org),
    })


class TrialIn(BaseModel):
    days: int = Field(ge=1, le=90)


async def admin_extend_trial(request):
    staff, _ = require_staff(request)
    org = db.one("SELECT * FROM orgs WHERE id=?", (request.path_params["id"],))
    if not org:
        raise ApiError(404, "Customer not found")
    s = await body(request, TrialIn)
    start = max(date.today(), date.fromisoformat(org["trial_ends_at"][:10]) if org["trial_ends_at"] else date.today())
    new_end = (start + timedelta(days=s.days)).isoformat()
    db.execute("UPDATE orgs SET trial_ends_at=?, status=CASE WHEN status='canceled' AND stripe_subscription_id IS NULL THEN 'trialing' ELSE status END "
               "WHERE id=?", (new_end, org["id"]))
    db.audit(staff["id"], "admin_extend_trial", {"org": org["id"], "days": s.days, "until": new_end})
    return ok({"trial_ends_at": new_end})


async def admin_reset_password(request):
    staff, _ = require_staff(request)
    u = db.one("SELECT * FROM users WHERE id=?", (request.path_params["id"],))
    if not u:
        raise ApiError(404, "User not found")
    if u["role"] == "staff":
        raise ApiError(403, "Staff passwords can't be reset here. Use the server.")
    temp = "-".join(secrets.token_urlsafe(4) for _ in range(3))
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(temp), u["id"]))
    db.audit(staff["id"], "admin_reset_password", {"user": u["id"]})
    return ok({"email": u["email"], "temporary_password": temp,
               "message": "Give this to the customer by phone or a separate channel. They should change it after signing in."})


async def admin_events(request):
    require_staff(request)
    q = request.query_params
    where, params = ["1=1"], []
    if q.get("type"):
        where.append("e.event_type=?")
        params.append(q["type"])
    if q.get("status"):
        where.append("e.status=?")
        params.append(q["status"])
    if q.get("brand"):
        where.append("EXISTS (SELECT 1 FROM event_brands eb WHERE eb.event_id=e.id AND eb.brand=?)")
        params.append(q["brand"])
    if q.get("min_score"):
        where.append("COALESCE(e.risk_score,0)>=?")
        params.append(int(q["min_score"]) if q["min_score"].isdigit() else 0)
    if q.get("q"):
        where.append("(UPPER(c.name) LIKE ? OR UPPER(e.title) LIKE ? OR e.company_number=?)")
        s = q["q"].strip().upper()
        params += [f"%{s}%", f"%{s}%", s]
    order = "COALESCE(e.risk_score,0) DESC, e.published_at DESC" if q.get("sort") == "score" else "e.created_at DESC"
    limit = min(int(q.get("limit") or 100) if (q.get("limit") or "").isdigit() else 100, 500)
    agg = "STRING_AGG(eb.brand, ',')" if db.is_postgres() else "GROUP_CONCAT(eb.brand)"
    rows = db.all(f"SELECT e.id, e.source, e.event_type, e.company_number, COALESCE(c.name, e.title) AS company_name, c.region, e.risk_score, "
                  f"e.status, e.published_at, e.created_at, e.personal_data, e.restricted_until, "
                  f"(SELECT {agg} FROM event_brands eb WHERE eb.event_id=e.id) AS brands "
                  f"FROM events e LEFT JOIN companies c ON c.number=e.company_number WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                  (*params, limit))
    for r in rows:
        if r.get("personal_data") and not r.get("company_number"):
            r["company_name"] = "(notice about an individual)"
    return ok({"events": rows, "types": sorted(brands.ALL_TYPES), "brands": list(brands.BRANDS)})


async def admin_audit(request):
    require_staff(request)
    action = request.query_params.get("action")
    rows = db.all("SELECT * FROM audit_log" + (" WHERE action=?" if action else "") + " ORDER BY at DESC LIMIT 300", (action,) if action else ())
    return ok({"audit": [dict(r, detail=db.loads(r["detail"], {})) for r in rows],
               "actions": [r["action"] for r in db.all("SELECT DISTINCT action FROM audit_log ORDER BY action")]})


async def admin_jobs(request):
    require_staff(request)
    return ok({"jobs": _job_health()})


ROUTES = [
    Route("/", root),
    Route("/app", app_page),
    Route("/admin", admin_page),
    Route("/v1/brands", public_brands),
    Route("/v1/auth/password", change_password, methods=["POST"]),
    Route("/v1/org/members", list_members, methods=["GET"]),
    Route("/v1/extract/options", extract_options),
    Route("/v1/extract/preview", extract_preview),
    Route("/v1/extract/export.csv", extract_csv),
    Route("/v1/admin/overview", admin_overview),
    Route("/v1/admin/customers", admin_customers),
    Route("/v1/admin/customers/{id}", admin_customer),
    Route("/v1/admin/orgs/{id}/extend-trial", admin_extend_trial, methods=["POST"]),
    Route("/v1/admin/users/{id}/reset-password", admin_reset_password, methods=["POST"]),
    Route("/v1/admin/events", admin_events),
    Route("/v1/admin/audit", admin_audit),
    Route("/v1/admin/jobs", admin_jobs),
]
