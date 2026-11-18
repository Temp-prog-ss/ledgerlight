"""Staff-only outreach API, the public unsubscribe page, and the console page."""
import html
import os
import re
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from app import brands, config, db, views
from app.outreach import engine, inbox, leads, templates
from app.web import ApiError, ok, require_staff

CONSOLE = Path(__file__).with_name("console.html")
MAILBOX_FIELDS = ("email", "from_name", "smtp_host", "smtp_port", "smtp_user", "password_env", "imap_host", "imap_port",
                  "daily_cap", "warmup_start", "warmup_step")


async def stats(request):
    require_staff(request)
    return ok(engine.stats())


async def mailboxes(request):
    staff, _ = require_staff(request)
    if request.method == "POST":
        d = await request.json()
        if not leads.EMAIL_RX.match(d.get("email", "")) or not d.get("smtp_host"):
            raise ApiError(422, "email and smtp_host are required")
        if d.get("password_env") and not re.match(r"^[A-Z][A-Z0-9_]{2,60}$", d["password_env"]):
            raise ApiError(422, "password_env must be an environment variable NAME like OUTREACH_PW_SALES, never the password itself")
        cap = int(d.get("daily_cap", 40))
        if cap > 200:
            raise ApiError(422, "daily_cap above 200 per mailbox harms deliverability; add more mailboxes instead")
        row = {"id": db.new_id("mb_"), **{k: d.get(k) for k in MAILBOX_FIELDS}, "daily_cap": cap,
               "smtp_port": int(d.get("smtp_port", 587)), "imap_port": int(d.get("imap_port", 993)),
               "warmup_start": int(d.get("warmup_start", 10)), "warmup_step": int(d.get("warmup_step", 3)),
               "sent_date": None, "sent_today": 0, "last_sent_at": None, "last_imap_uid": 0, "active": 1, "created_at": db.now()}
        row["email"] = row["email"].lower()
        if db.one("SELECT id FROM outreach_mailboxes WHERE email=?", (row["email"],)):
            raise ApiError(409, "Mailbox already added")
        db.insert("outreach_mailboxes", row)
        db.audit(staff["id"], "outreach_mailbox_added", {"email": row["email"]})
        return ok({"id": row["id"]}, 201)
    rows = db.all("SELECT * FROM outreach_mailboxes ORDER BY created_at")
    return ok({"mailboxes": [{**{k: m[k] for k in ("id", "email", "from_name", "smtp_host", "imap_host", "daily_cap", "active", "password_env")},
                              "password_set": bool(os.environ.get(m["password_env"] or "")), "cap_today": engine.mailbox_cap(m),
                              "sent_today": engine.mailbox_sent_today(m)} for m in rows]})


async def mailbox_update(request):
    require_staff(request)
    d = await request.json()
    sets = {}
    if "active" in d:
        sets["active"] = int(bool(d["active"]))
    if "daily_cap" in d:
        sets["daily_cap"] = min(200, max(1, int(d["daily_cap"])))
    if sets:
        db.execute(f"UPDATE outreach_mailboxes SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), request.path_params["id"]))
    return ok({"updated": bool(sets)})


async def list_leads(request):
    require_staff(request)
    q = request.query_params
    where, params = [], []
    if q.get("stage"):
        where.append("stage=?")
        params.append(q["stage"])
    if q.get("brand"):
        where.append("brand=?")
        params.append(q["brand"])
    if q.get("q"):
        where.append("(UPPER(email) LIKE ? OR UPPER(company) LIKE ? OR UPPER(first_name) LIKE ?)")
        params += [f"%{q['q'].upper()}%"] * 3
    sql = "SELECT * FROM outreach_leads" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?"
    return ok({"stages": leads.STAGES, "leads": db.all(sql, (*params, min(int(q.get("limit", 500)), 2000)))})


async def lead_detail(request):
    staff, _ = require_staff(request)
    lid = request.path_params["id"]
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (lid,))
    if not lead:
        raise ApiError(404, "Lead not found")
    if request.method == "PATCH":
        d = await request.json()
        if "notes" in d:
            db.execute("UPDATE outreach_leads SET notes=?, updated_at=? WHERE id=?", (d["notes"], db.now(), lid))
        if d.get("stage"):
            if d["stage"] in ("new", "contacted") and lead["block_reason"]:
                raise ApiError(409, f"Blocked: {lead['block_reason']}")
            try:
                leads.set_stage(lid, d["stage"], d.get("note"), actor=staff["email"])
            except ValueError as e:
                raise ApiError(422, str(e))
        return ok({"updated": True})
    return ok({"lead": lead, "activity": db.all("SELECT * FROM outreach_activity WHERE lead_id=? ORDER BY at DESC LIMIT 100", (lid,)),
               "messages": db.all("SELECT step_no, subject, status, error, sent_at FROM outreach_messages WHERE lead_id=? ORDER BY sent_at", (lid,))})


async def import_leads(request):
    require_staff(request)
    text = (await request.body()).decode("utf-8-sig", "replace")
    brand = request.query_params.get("brand")
    if brand and brand not in brands.BRANDS:
        raise ApiError(422, "unknown brand")
    try:
        return ok(leads.import_csv(text, brand=brand, source=request.query_params.get("source", "csv")), 201)
    except ValueError as e:
        raise ApiError(422, str(e))


def _csv(text, name):
    return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def export_leads(request):
    require_staff(request)
    return _csv(views.to_csv(leads.export_rows(), ["first_name", "last_name", "position", "company", "email", "website", "company_number",
                                                   "brand", "stage", "block_reason", "date_found", "last_contacted_at", "replied_at", "notes"]),
                "outreach_pipeline.csv")


async def prospects(request):
    require_staff(request)
    brand = request.query_params.get("brand", "")
    if brand not in leads.PROSPECT_SIC:
        raise ApiError(422, f"brand must be one of {sorted(leads.PROSPECT_SIC)}")
    rows = leads.prospects(brand, request.query_params.get("region"))
    out = [{"Company": r["name"], "Company Number": r["number"], "Town": r["town"], "Postcode": r["postcode"], "Region": r["region"],
            "SIC": r["sic_codes"], "Name": "", "Position": "", "Email": "", "Website": "", "Date Found": db.now()[:10]} for r in rows]
    return _csv(views.to_csv(out, ["Company", "Company Number", "Town", "Postcode", "Region", "SIC", "Name", "Position", "Email", "Website", "Date Found"]),
                f"{brand}_prospects.csv")


async def campaigns(request):
    require_staff(request)
    if request.method == "POST":
        d = await request.json()
        mids = d.get("mailbox_ids") or [m["id"] for m in db.all("SELECT id FROM outreach_mailboxes WHERE active=1")]
        if not mids:
            raise ApiError(422, "Add a sending mailbox first")
        try:
            c = engine.create_campaign(d.get("name") or "New campaign", d.get("brand", "ledgerlight"), mids, d.get("steps"),
                                       **{k: d[k] for k in ("window_start", "window_end", "weekdays", "timezone", "min_gap_seconds", "max_gap_seconds") if k in d})
        except ValueError as e:
            raise ApiError(422, str(e))
        return ok(c, 201)
    rows = db.all("SELECT * FROM outreach_campaigns ORDER BY created_at DESC")
    for c in rows:
        c["counts"] = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM outreach_enrollments WHERE campaign_id=? GROUP BY status", (c["id"],))}
    return ok({"campaigns": rows})


def _campaign(cid):
    c = db.one("SELECT * FROM outreach_campaigns WHERE id=?", (cid,))
    if not c:
        raise ApiError(404, "Campaign not found")
    return c


async def campaign_detail(request):
    require_staff(request)
    c = _campaign(request.path_params["id"])
    c["steps"] = db.all("SELECT step_no, delay_days, subject, body FROM outreach_steps WHERE campaign_id=? ORDER BY step_no", (c["id"],))
    c["counts"] = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM outreach_enrollments WHERE campaign_id=? GROUP BY status", (c["id"],))}
    return ok(c)


async def campaign_steps(request):
    require_staff(request)
    c = _campaign(request.path_params["id"])
    d = await request.json()
    try:
        engine.set_steps(c["id"], d.get("steps") or [])
    except ValueError as e:
        raise ApiError(422, str(e))
    return ok({"updated": True})


async def campaign_enroll(request):
    require_staff(request)
    c = _campaign(request.path_params["id"])
    d = await request.json()
    return ok(engine.enroll(c["id"], d.get("lead_ids"), d.get("stage", "new"), d.get("brand") or c["brand"]))


async def campaign_status(request):
    staff, _ = require_staff(request)
    c = _campaign(request.path_params["id"])
    status = (await request.json()).get("status")
    if status not in ("active", "paused"):
        raise ApiError(422, "status must be active or paused")
    if status == "active" and not config.settings.dry_run_delivery:
        problems = []
        if not os.environ.get("OUTREACH_POSTAL_ADDRESS"):
            problems.append("set OUTREACH_POSTAL_ADDRESS (the law requires your sender identity and address)")
        for mid in db.loads(c["mailbox_ids"], []):
            mb = db.one("SELECT * FROM outreach_mailboxes WHERE id=?", (mid,))
            if mb and not os.environ.get(mb["password_env"] or ""):
                problems.append(f"set {mb['password_env'] or 'a password_env'} in .env for {mb['email']}")
        if problems:
            raise ApiError(409, "Can't start yet: " + "; ".join(problems))
    db.execute("UPDATE outreach_campaigns SET status=? WHERE id=?", (status, c["id"]))
    db.audit(staff["id"], "outreach_campaign_status", {"campaign": c["id"], "status": status})
    return ok({"status": status})


async def campaign_preview(request):
    require_staff(request)
    c = _campaign(request.path_params["id"])
    d = await request.json()
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (d["lead_id"],)) if d.get("lead_id") else \
        db.one("SELECT * FROM outreach_leads WHERE stage='new' ORDER BY created_at LIMIT 1") or \
        {"id": "preview", "first_name": "Alex", "company": "Example Ltd", "email": "alex@example.com", "position": "Director"}
    mb = db.one("SELECT * FROM outreach_mailboxes WHERE id=?", ((db.loads(c["mailbox_ids"], []) or [""])[0],)) or \
        {"email": "you@yourdomain.co.uk", "from_name": "You"}
    b = brands.get(c["brand"])
    values = {**lead, "sender_name": mb.get("from_name") or mb["email"], "brand": b.name, "brand_url": b.site_url}
    out = []
    for s in db.all("SELECT * FROM outreach_steps WHERE campaign_id=? ORDER BY step_no", (c["id"],)):
        out.append({"step_no": s["step_no"], "delay_days": s["delay_days"],
                    "subject": templates.render(s["subject"], values) if s["subject"] else "(same thread: Re: first subject)",
                    "body": templates.render(s["body"], values) + templates.footer(engine.unsubscribe_url(lead["id"])),
                    "missing": templates.missing_fields((s["subject"] or "") + s["body"], values)})
    return ok({"lead": {k: lead.get(k) for k in ("email", "first_name", "company")}, "steps": out})


async def run_now(request):
    require_staff(request)
    job = request.path_params["job"]
    if job == "send":
        return ok({"sent": engine.send_due()})
    if job == "inbox":
        return ok({"processed": inbox.poll_all()})
    raise ApiError(404, "job must be send or inbox")


UNSUB_PAGE = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Unsubscribe</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:60px auto;padding:0 20px;color:#16191D}}button{{background:#0F4D3A;color:#fff;border:0;padding:12px 20px;border-radius:8px;font-size:16px;cursor:pointer}}</style></head>
<body>{content}</body></html>"""


async def unsubscribe(request):
    """GET shows a confirm button (so link scanners can't unsubscribe people); POST (incl. one-click) unsubscribes."""
    token = request.query_params.get("t", "")
    lead_id = engine.check_token(token)
    if not lead_id:
        return HTMLResponse(UNSUB_PAGE.format(content="<h2>This link isn't valid</h2><p>Reply 'unsubscribe' to any of our emails and we'll remove you.</p>"), 400)
    if request.method == "POST":
        leads.unsubscribe(lead_id, how="link")
        return HTMLResponse(UNSUB_PAGE.format(content="<h2>You're unsubscribed</h2><p>We won't email you again. Sorry for the interruption.</p>"))
    lead = db.one("SELECT email FROM outreach_leads WHERE id=?", (lead_id,))
    email = html.escape(lead["email"]) if lead else "this address"
    return HTMLResponse(UNSUB_PAGE.format(content=f"<h2>Unsubscribe {email}?</h2><form method='post' action='?t={html.escape(token)}'>"
                                                  f"<button>Yes, stop emailing me</button></form>"))


async def console(request):
    return HTMLResponse(CONSOLE.read_text())


ROUTES = [
    Route("/outreach", console),
    Route("/v1/outreach/unsubscribe", unsubscribe, methods=["GET", "POST"]),
    Route("/v1/outreach/stats", stats),
    Route("/v1/outreach/mailboxes", mailboxes, methods=["GET", "POST"]),
    Route("/v1/outreach/mailboxes/{id}", mailbox_update, methods=["PATCH"]),
    Route("/v1/outreach/leads", list_leads),
    Route("/v1/outreach/leads/import", import_leads, methods=["POST"]),
    Route("/v1/outreach/leads.csv", export_leads),
    Route("/v1/outreach/leads/{id}", lead_detail, methods=["GET", "PATCH"]),
    Route("/v1/outreach/prospects.csv", prospects),
    Route("/v1/outreach/campaigns", campaigns, methods=["GET", "POST"]),
    Route("/v1/outreach/campaigns/{id}", campaign_detail),
    Route("/v1/outreach/campaigns/{id}/steps", campaign_steps, methods=["PUT"]),
    Route("/v1/outreach/campaigns/{id}/enroll", campaign_enroll, methods=["POST"]),
    Route("/v1/outreach/campaigns/{id}/status", campaign_status, methods=["POST"]),
    Route("/v1/outreach/campaigns/{id}/preview", campaign_preview, methods=["POST"]),
    Route("/v1/outreach/run/{job}", run_now, methods=["POST"]),
]
