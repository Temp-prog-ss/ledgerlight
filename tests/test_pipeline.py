import json
from datetime import date, datetime, timedelta, timezone

from app import db, events, plans
from app.delivery import dispatch
from app.pipeline import compliance, run, score
from app.sources import gazette


def org(brand, plan="team", status="active", vetted=0, **kw):
    o = {"id": db.new_id("org_"), "name": f"{brand} customer", "brand": brand, "plan": plan, "billing_cycle": "monthly",
         "status": status, "trial_ends_at": None, "stripe_customer_id": None, "stripe_subscription_id": None, "vetted": vetted,
         "regions": db.dumps([]), "timezone": "Europe/London", "digest_hour": 7, "last_digest_at": None, "created_at": db.now()}
    o.update(kw)
    db.insert("orgs", o)
    ch = {"id": db.new_id("chn_"), "org_id": o["id"], "kind": "email", "target": f"{brand}@example.com", "config": "{}", "active": 1, "created_at": db.now()}
    db.insert("channels", ch)
    db.insert("alert_rules", {"id": db.new_id("rul_"), "org_id": o["id"], "name": "all", "event_types": "[]", "regions": "[]",
                              "sic_prefixes": "[]", "keywords": "[]", "min_score": 0, "watchlist_only": 0, "instant": 1, "digest": 1,
                              "channel_ids": db.dumps([ch["id"]]), "active": 1, "created_at": db.now()})
    return o


def watch(o, number, owed=None):
    wid = db.new_id("wl_")
    db.insert("watchlists", {"id": wid, "org_id": o["id"], "name": "w", "created_at": db.now()})
    db.insert("watchlist_items", {"id": db.new_id("wi_"), "watchlist_id": wid, "company_number": number, "label": None,
                                  "amount_owed": owed, "credit_limit": None, "extra": None, "created_at": db.now()})


def deliveries_for(o):
    return db.all("SELECT * FROM deliveries WHERE org_id=?", (o["id"],))


def test_construction_admin_routes_to_right_brands(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    site, credit_unwatched, credit_watched, talent, sponsor = org("sitesentinel"), org("creditpulse"), org("creditpulse"), org("talentrelease"), org("sponsorwatch")
    watch(credit_watched, "07654321", owed=18400)
    eid = events.record("gazette", "n1", "administration", "07654321", "Appointment of Administrators", data={"ip_firm": "X LLP"})
    res = run.process_new()
    assert res[0][1]["status"] == "processed"
    ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
    assert ev["risk_score"] >= 90  # admin + overdue accounts + negative net assets
    assert "negative net assets" in ev["reasons"] and ev["brief"] and ev["next_action"]
    assert {"sitesentinel", "talentrelease", "ledgerlight", "distressradar"} <= set(res[0][1]["brands"])
    assert "sponsorwatch" not in res[0][1]["brands"]  # not a sponsor
    assert deliveries_for(site) and deliveries_for(talent) and deliveries_for(credit_watched)
    assert not deliveries_for(credit_unwatched) and not deliveries_for(sponsor)


def test_sponsor_insolvency_goes_to_sponsorwatch(company_factory):
    company_factory("01112223", "NORTHGATE CARE HOMES LTD", sic="87300", sponsor=json.dumps({"route": "Skilled Worker", "rating": "A"}))
    sw = org("sponsorwatch")
    events.record("gazette", "n2", "cvl", "01112223", "Resolutions for Winding-up")
    run.process_new()
    d = deliveries_for(sw)
    assert {x["mode"] for x in d} == {"instant", "digest"}


def test_solo_plan_gets_digest_only_and_trial_gets_team_features(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    no_region = org("sitesentinel", plan="solo")
    solo = org("sitesentinel", plan="solo", regions=db.dumps(["North West"]))
    elsewhere = org("sitesentinel", plan="solo", regions=db.dumps(["London"]))
    trial = org("sitesentinel", plan="solo", status="trialing", trial_ends_at=(date.today() + timedelta(days=5)).isoformat())
    events.record("gazette", "n3", "winding_up_petition", "07654321", "Petition")
    run.process_new()
    assert {x["mode"] for x in deliveries_for(solo)} == {"digest"}
    assert not deliveries_for(no_region) and not deliveries_for(elsewhere)  # Solo = one chosen region
    assert {x["mode"] for x in deliveries_for(trial)} == {"instant", "digest"}
    assert plans.entitlements({"plan": "solo", "status": "canceled"})["active"] is False


def test_probate_only_vetted_and_after_cooling_off():
    unvetted, vetted = org("estatesignal"), org("estatesignal", vetted=1)
    gazette.ingest_entries([{"id": "777", "title": "Deceased Estates", "published": date.today().isoformat(), "notice-code": "2903",
                             "content": "Claims to Hollis & Grant LLP solicitors by 20 January 2027. BS1 6QA"}])
    run.process_new()
    assert not deliveries_for(unvetted)
    d = deliveries_for(vetted)
    assert d and all(x["not_before"] >= (date.today() + timedelta(days=13)).isoformat() for x in d)
    assert dispatch.send_instant() == 0  # embargoed today
    ev = db.one("SELECT * FROM events WHERE event_type='deceased_estate'")
    assert compliance.embargoed(ev) and not compliance.embargoed(ev, today="2099-01-01")
    assert "Hollis" in ev["brief"] and "SMITH" not in (ev["brief"] or "")


def test_suppression_blocks_everything(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    o = org("sitesentinel")
    db.insert("suppressions", {"id": "s1", "kind": "company", "value": "07654321", "reason": "objection", "created_at": db.now()})
    events.record("gazette", "n4", "administration", "07654321", "Admin")
    run.process_new()
    assert db.one("SELECT status FROM events WHERE source_id='n4'")["status"] == "suppressed"
    assert not deliveries_for(o)


def test_phoenix_detection(company_factory):
    company_factory("01000001", "OLD BUILD LTD", status="liquidation")
    company_factory("01000002", "NEW BUILD LTD", status="active", incorporated_on=date.today().isoformat())
    for num in ("01000001", "01000002"):
        db.insert("officers", {"id": db.new_id(), "company_number": num, "officer_key": "OFF1", "name": "SMITH, Ann",
                               "role": "director", "appointed_on": "2020-01-01", "resigned_on": None})
    net = score.network("01000001")
    assert net["phoenix_flag"] and net["connected_count"] == 1


def test_dispatch_instant_and_digest(company_factory, tmp_path):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    o = org("sitesentinel")
    events.record("gazette", "n5", "administration", "07654321", "Admin")
    run.process_new()
    assert dispatch.send_instant() == 1
    out = list((tmp_path / "outbox").glob("*email*.json"))
    msg = json.loads(out[0].read_text())
    assert msg["to"] == "sitesentinel@example.com" and "HARBOURLINE" in msg["subject"]
    after7 = datetime.now(timezone.utc).replace(hour=12)
    assert dispatch.send_digests(now_utc=after7) == 1
    digest = [json.loads(p.read_text()) for p in (tmp_path / "outbox").glob("*email*.json")]
    assert any("Daily digest" in m["subject"] and m["attachments"] for m in digest)
    assert dispatch.send_digests(now_utc=after7) == 0  # once a day


def test_unknown_company_is_still_processed():
    o = org("ledgerlight")
    events.record("gazette", "n6", "winding_up_petition", None, "Some Unmatched Co")
    res = run.process_new()
    assert res[0][1]["status"] == "processed"
