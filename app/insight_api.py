"""Company Insight API: per-company risk assessment, watchlist ranking, decision log; admin calibration.

Customer output follows the usual rules: the event timeline comes through views.visible_events (brand,
vetting, cooling-off, suppression and plan rules), objections hide a company entirely, personal-data
notices are never used as model inputs, and lookups count against the plan's monthly allowance
(companies on the customer's own watchlist are free).
"""
from datetime import date

from pydantic import BaseModel, Field
from starlette.routing import Route

from app import config, db, insight, plans, views
from app.sources import companies_house
from app.web import ApiError, body, current, ok, require_active, require_staff

ACTIONS = ["no_change", "monitor", "shorten_terms", "reduce_limit", "deposit_required", "stop_credit",
           "increase_limit", "new_order", "chase_debt", "legal_action", "contact_opportunity", "other"]
EXPOSURE_UP = {"increase_limit", "new_order"}
OUTCOMES = ["paid_on_time", "paid_late", "partly_paid", "unpaid", "company_failed", "won_business", "no_outcome_yet"]


def _number(raw):
    n = companies_house.normalise_number(raw)
    if not n:
        raise ApiError(422, "Enter a valid company number")
    return n


def _allowance(org):
    ent = plans.entitlements(org)
    cap = ent.get("insight_lookups_month")
    used = (db.one("SELECT insight_lookups FROM usage WHERE org_id=? AND month=?",
                   (org["id"], date.today().strftime("%Y-%m"))) or {}).get("insight_lookups") or 0
    return {"used": used, "limit": cap, "remaining": None if cap is None else max(0, cap - used)}


def _ensure_company(number):
    if db.one("SELECT number FROM companies WHERE number=?", (number,)):
        return True
    if not config.settings.companies_house_api_key:
        return False
    try:
        companies_house.enrich(number)
    except Exception:
        return False
    return bool(db.one("SELECT number FROM companies WHERE number=?", (number,)))


def _report(org, number):
    a = insight.assess(number)
    if not a:
        raise ApiError(404, "We don't hold that company yet")
    a["trend"] = insight.trend(number, a["probability"])
    a["peers"] = insight.peers(a["sic_section"], a["probability"])
    a["protocol"] = insight.protocol(a["band"], org["brand"])
    a.pop("features", None)
    return a


async def company_insight(request):
    user, org = current(request)
    require_active(org)
    number = _number(request.path_params["number"])
    if not insight.available(number):
        raise ApiError(404, "Insight isn't available for this company")
    on_watchlist = number in views.watch_items(org["id"])
    allow = _allowance(org)
    if not on_watchlist and allow["remaining"] is not None and allow["remaining"] <= 0:
        raise ApiError(402, "You've used this month's company insight lookups. Watchlist companies stay free; upgrade for more.")
    if not _ensure_company(number):
        raise ApiError(404, "We couldn't find that company")
    report = _report(org, number)
    timeline = views.project_events(org, views.visible_events(org, {"company": number}, limit=50), level=1)
    report["timeline"] = [{"date": e.get("event_date"), "label": (e.get("event_type") or "").replace("_", " ").capitalize(),
                           "notice_url": e.get("notice_url")} for e in timeline]
    report["news"] = db.all("SELECT title, url, domain, published_at, flagged, matched_terms FROM news_mentions "
                            "WHERE company_number=? ORDER BY published_at DESC LIMIT 10", (number,))
    report["decisions"] = db.all("SELECT d.id, d.created_at, d.band, d.probability, d.action, d.note, d.outcome, d.outcome_at, "
                                 "d.second_reviewer, u.email AS by_email FROM insight_decisions d LEFT JOIN users u ON u.id=d.user_id "
                                 "WHERE d.org_id=? AND d.company_number=? ORDER BY d.created_at DESC LIMIT 20", (org["id"], number))
    report["on_watchlist"] = on_watchlist
    if not on_watchlist:
        views.bump_usage(org["id"], insight=1)
    report["allowance"] = _allowance(org)
    db.audit(user["id"], "insight_lookup", {"org": org["id"], "company": number, "band": report["band"]})
    return ok(report)


async def watchlist_insight(request):
    user, org = current(request)
    require_active(org)
    out = []
    for number in views.watch_items(org["id"]):
        if not insight.available(number):
            continue
        a = insight.assess(number)
        if not a:
            continue
        t = insight.trend(number, a["probability"])
        out.append({"company_number": number, "company_name": a["company_name"], "probability": a["probability"],
                    "band": a["band"], "trend": t["direction"], "sector": a["sector"],
                    "top_reason": (a["trigger"] or {}).get("label") or (a["reasons"][0]["reason"] if a["reasons"] else None),
                    "action": insight.protocol(a["band"], org["brand"])["action"]})
    out.sort(key=lambda r: -r["probability"])
    counts = {}
    for r in out:
        counts[r["band"]] = counts.get(r["band"], 0) + 1
    return ok({"companies": out, "bands": counts, "allowance": _allowance(org)})


class DecisionIn(BaseModel):
    action: str
    note: str | None = Field(default=None, max_length=1000)
    second_reviewer: str | None = Field(default=None, max_length=200)


async def add_decision(request):
    user, org = current(request)
    require_active(org)
    number = _number(request.path_params["number"])
    s = await body(request, DecisionIn)
    if s.action not in ACTIONS:
        raise ApiError(422, f"action must be one of: {', '.join(ACTIONS)}")
    if not insight.available(number):
        raise ApiError(404, "Insight isn't available for this company")
    a = insight.assess(number)
    if not a:
        raise ApiError(404, "We don't hold that company yet")
    reviewer = None
    if s.action in EXPOSURE_UP and a["band"] in ("High", "Critical"):
        if not s.second_reviewer:
            raise ApiError(422, f"This company is in the {a['band']} band: a second reviewer must approve more exposure.")
        r = db.one("SELECT id, email FROM users WHERE org_id=? AND LOWER(email)=LOWER(?)", (org["id"], s.second_reviewer.strip()))
        if not r or r["id"] == user["id"]:
            raise ApiError(422, "The second reviewer must be another person in your account.")
        reviewer = r["email"]
    row = {"id": db.new_id("dec_"), "org_id": org["id"], "user_id": user["id"], "company_number": number, "band": a["band"],
           "probability": a["probability"], "action": s.action, "note": s.note, "second_reviewer": reviewer,
           "outcome": None, "outcome_at": None, "created_at": db.now()}
    db.insert("insight_decisions", row)
    db.audit(user["id"], "insight_decision", {"org": org["id"], "company": number, "action": s.action, "band": a["band"]})
    return ok(row, status=201)


class OutcomeIn(BaseModel):
    outcome: str


async def set_outcome(request):
    user, org = current(request)
    s = await body(request, OutcomeIn)
    if s.outcome not in OUTCOMES:
        raise ApiError(422, f"outcome must be one of: {', '.join(OUTCOMES)}")
    d = db.one("SELECT * FROM insight_decisions WHERE id=? AND org_id=?", (request.path_params["id"], org["id"]))
    if not d:
        raise ApiError(404, "Decision not found")
    db.execute("UPDATE insight_decisions SET outcome=?, outcome_at=? WHERE id=?", (s.outcome, db.now(), d["id"]))
    return ok({"id": d["id"], "outcome": s.outcome})


async def list_decisions(request):
    user, org = current(request)
    rows = db.all("SELECT d.*, c.name AS company_name FROM insight_decisions d LEFT JOIN companies c ON c.number=d.company_number "
                  "WHERE d.org_id=? ORDER BY d.created_at DESC LIMIT 500", (org["id"],))
    return ok({"decisions": rows, "actions": ACTIONS, "outcomes": OUTCOMES})


async def company_search(request):
    """Find companies by name or number: our own records first, then Companies House if a key is set."""
    user, org = current(request)
    q = (request.query_params.get("q") or "").strip()
    if len(q) < 2:
        raise ApiError(422, "Type at least 2 characters")
    q = q[:80]
    limit = 10
    found, seen = [], set()
    num = companies_house.normalise_number(q) if any(ch.isdigit() for ch in q) else None
    rows = []
    if num:
        rows += db.all("SELECT number, name, status, town, incorporated_on FROM companies WHERE number=?", (num,))
    rows += db.all("SELECT number, name, status, town, incorporated_on FROM companies WHERE UPPER(name) LIKE ? "
                   "ORDER BY CASE WHEN UPPER(name) LIKE ? THEN 0 ELSE 1 END, name LIMIT ?",
                   (f"%{q.upper()}%", f"{q.upper()}%", limit * 2))
    for r in rows:
        if r["number"] in seen or not insight.available(r["number"]):
            continue
        seen.add(r["number"])
        found.append({"number": r["number"], "name": r["name"], "status": r["status"], "town": r["town"],
                      "incorporated_on": r["incorporated_on"], "source": "ledgerlight"})
    if len(found) < limit and config.settings.companies_house_api_key:
        try:
            for it in companies_house.search(q):
                n = it.get("company_number")
                if not n or n in seen or not insight.available(n):
                    continue
                seen.add(n)
                found.append({"number": n, "name": it.get("title"), "status": it.get("company_status"),
                              "town": (it.get("address") or {}).get("locality"), "incorporated_on": it.get("date_of_creation"),
                              "source": "companies_house"})
        except Exception:
            pass  # local results still help; Companies House may be rate limited
    return ok({"results": found[:limit]})


# ---------------- admin ----------------

async def admin_insight(request):
    require_staff(request)
    snaps = db.one("SELECT COUNT(*) AS n, MAX(month) AS latest FROM insight_snapshots") or {}
    return ok({
        "calibration": insight.calibration(),
        "fit": db.loads(db.get_state("insight_weights"), None),
        "snapshots": snaps.get("n", 0), "latest_month": snaps.get("latest"),
        "base_rates": [{"sic_section": k, "sector": insight.SECTION_NAMES.get(k), "annual_rate": round(insight.base_rate(k), 4),
                        "source": (db.one("SELECT source FROM base_rates WHERE sic_section=?", (k,)) or {}).get("source") or "starting prior"}
                       for k in sorted(insight.SECTION_NAMES)],
        "bands": db.all("SELECT band, COUNT(*) AS n FROM insight_snapshots WHERE month=? GROUP BY band",
                        (insight.month_key(),)),
        "decisions": db.all("SELECT action, outcome, COUNT(*) AS n FROM insight_decisions GROUP BY action, outcome"),
    })


class BaseRateIn(BaseModel):
    sic_section: str = Field(min_length=1, max_length=1)
    annual_rate: float = Field(gt=0, lt=1)
    source: str = Field(min_length=3, max_length=300)


async def admin_set_base_rate(request):
    staff, _ = require_staff(request)
    s = await body(request, BaseRateIn)
    sec = s.sic_section.upper()
    if sec not in insight.SECTION_NAMES:
        raise ApiError(422, "Unknown SIC section")
    insight.set_base_rate(sec, s.annual_rate, s.source)
    db.audit(staff["id"], "insight_base_rate", {"section": sec, "rate": s.annual_rate, "source": s.source})
    return ok({"sic_section": sec, "annual_rate": s.annual_rate})


ROUTES = [
    Route("/v1/companies/search", company_search),
    Route("/v1/insight/company/{number}", company_insight),
    Route("/v1/insight/company/{number}/decisions", add_decision, methods=["POST"]),
    Route("/v1/insight/watchlist", watchlist_insight),
    Route("/v1/insight/decisions", list_decisions),
    Route("/v1/insight/decisions/{id}", set_outcome, methods=["PATCH"]),
    Route("/v1/admin/insight", admin_insight),
    Route("/v1/admin/insight/base-rates", admin_set_base_rate, methods=["POST"]),
]
