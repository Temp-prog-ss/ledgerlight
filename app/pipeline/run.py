"""Process new events end to end: resolve -> enrich -> score -> brief -> route."""
from app import config, db
from app.pipeline import brief, compliance, route, score
from app.sources import companies_house as ch


def resolve_company(event):
    """Make sure the event points at a company we hold (fetching it when we can)."""
    num = event.get("company_number")
    if num:
        num = ch.normalise_number(num)
        if num != event["company_number"]:
            db.execute("UPDATE events SET company_number=? WHERE id=?", (num, event["id"]))
            event["company_number"] = num
    elif event.get("title") and not event.get("personal_data") and config.settings.companies_house_api_key:
        hits = ch.search(event["title"])
        if hits and hits[0].get("title", "").upper() == (event["title"] or "").upper():
            num = ch.normalise_number(hits[0]["company_number"])
            db.execute("UPDATE events SET company_number=? WHERE id=?", (num, event["id"]))
            event["company_number"] = num
    if num and config.settings.companies_house_api_key:
        try:
            ch.enrich(num)
        except Exception as e:
            db.audit("worker", "enrich_error", {"company": num, "error": str(e)[:200]})
    return db.one("SELECT * FROM companies WHERE number=?", (num,)) if num else None


def process_event(event):
    company = resolve_company(event)
    if company and company.get("subscriber_type") in (None, "check"):
        st = compliance.subscriber_type(company)
        db.execute("UPDATE companies SET subscriber_type=? WHERE number=?", (st, company["number"]))
        company["subscriber_type"] = st
    if compliance.is_suppressed(event, company):
        db.execute("UPDATE events SET status='suppressed' WHERE id=?", (event["id"],))
        return {"status": "suppressed"}
    net = score.network(company["number"]) if company else {"connected_count": 0, "prior_insolvencies": 0, "phoenix_flag": False}
    risk, reasons = score.score(event, company, net)
    restricted = compliance.restricted_until(event)
    f = brief.facts(event, company, net, reasons)
    text, action, model = brief.write(event, f)
    db.execute("UPDATE events SET risk_score=?, reasons=?, brief=?, brief_model=?, next_action=?, restricted_until=?, status='processed' WHERE id=?",
               (risk, db.dumps(reasons), text, model, action, restricted, event["id"]))
    event.update(risk_score=risk, reasons=db.dumps(reasons), brief=text, next_action=action, restricted_until=restricted)
    tagged = route.tag_brands(event, company)
    n = route.route_to_customers(event, company)
    return {"status": "processed", "risk": risk, "brands": tagged, "deliveries": n}


def process_new(limit=200):
    done = []
    for ev in db.all("SELECT * FROM events WHERE status='new' ORDER BY created_at LIMIT ?", (limit,)):
        try:
            done.append((ev["id"], process_event(dict(ev))))
        except Exception as e:
            db.execute("UPDATE events SET status='error' WHERE id=?", (ev["id"],))
            db.audit("worker", "process_error", {"event": ev["id"], "error": str(e)[:300]})
    return done
