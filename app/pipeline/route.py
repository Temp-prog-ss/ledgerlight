"""Route a processed event to brands and to each matching customer's channels."""
from app import brands, db, plans
from app.pipeline import compliance


def watching_orgs(company_number):
    if not company_number:
        return {}
    rows = db.all("SELECT w.org_id, i.* FROM watchlist_items i JOIN watchlists w ON w.id=i.watchlist_id WHERE i.company_number=?",
                  (company_number,))
    return {r["org_id"]: r for r in rows}


def tag_brands(event, company):
    """Store general (non-watchlist) relevance per brand for browsing in each brand's dashboard."""
    out = {}
    for bid in brands.BRANDS:
        rel = brands.relevance(bid, event["event_type"], company, on_watchlist=False)
        if rel is not None:
            out[bid] = rel
            db.upsert("event_brands", {"event_id": event["id"], "brand": bid, "relevance": rel}, ["event_id", "brand"])
    return out


def rule_matches(rule, event, company, risk, on_watch, org):
    types = db.loads(rule["event_types"], [])
    if types and event["event_type"] not in types:
        return False
    if rule["watchlist_only"] and not on_watch:
        return False
    cap = plans.entitlements(org).get("regions")
    org_regions = (db.loads(org.get("regions"), []) or [])[:cap] if cap else db.loads(org.get("regions"), [])
    if cap and not org_regions and not on_watch:
        return False  # plans with a region limit must choose their region first
    regions = db.loads(rule["regions"], []) or org_regions
    if cap:
        regions = [r for r in regions if r in org_regions] or org_regions
    if regions and (company or {}).get("region") not in regions and not on_watch:
        return False
    sics = db.loads(rule["sic_prefixes"], [])
    codes = [s.strip() for s in ((company or {}).get("sic_codes") or "").split(",") if s.strip()]
    if sics and not on_watch and not any(c.startswith(tuple(sics)) for c in codes):
        return False
    kws = [k.lower() for k in db.loads(rule["keywords"], [])]
    hay = " ".join(str(x or "") for x in ((company or {}).get("name"), event.get("title"), (company or {}).get("sic_description"))).lower()
    if kws and not any(k in hay for k in kws):
        return False
    return (risk or 0) >= (rule["min_score"] or 0) or on_watch


def route_to_customers(event, company):
    """Create pending deliveries. Returns the number created."""
    watchers = watching_orgs(event.get("company_number"))
    created = 0
    for org in db.all("SELECT * FROM orgs WHERE status IN ('trialing','active','past_due')"):
        ent = plans.entitlements(org)
        if not ent["active"] or not compliance.can_route(org["brand"], org, event):
            continue
        on_watch = org["id"] in watchers
        rel = brands.relevance(org["brand"], event["event_type"], company, on_watchlist=on_watch)
        if rel is None:
            continue
        channels = {c["id"]: c for c in db.all("SELECT * FROM channels WHERE org_id=? AND active=1", (org["id"],))}
        for rule in db.all("SELECT * FROM alert_rules WHERE org_id=? AND active=1", (org["id"],)):
            if not rule_matches(rule, event, company, event.get("risk_score"), on_watch, org):
                continue
            ids = db.loads(rule["channel_ids"], []) or list(channels)
            for cid in ids:
                ch = channels.get(cid)
                if not ch or ch["kind"] not in ent["channels"]:
                    continue
                modes = []
                if rule["instant"] and ent["instant"] and ch["kind"] != "sftp":
                    modes.append("instant")
                if rule["digest"] and ch["kind"] in ("email", "sftp"):
                    modes.append("digest")
                for mode in modes:
                    if db.one("SELECT id FROM deliveries WHERE org_id=? AND event_id=? AND channel_id=? AND mode=?",
                              (org["id"], event["id"], cid, mode)):
                        continue
                    db.insert("deliveries", {"id": db.new_id("dlv_"), "org_id": org["id"], "event_id": event["id"],
                                             "channel_id": cid, "mode": mode, "status": "pending", "attempts": 0,
                                             "last_error": None, "not_before": event.get("restricted_until"),
                                             "sent_at": None, "created_at": db.now()})
                    created += 1
    return created
