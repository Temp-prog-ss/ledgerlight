import hashlib
import hmac
import json
import time

from app import db, events
from app.pipeline import run

SIGNUP = {"email": "ann@builder.example", "password": "correct horse 99", "name": "Ann", "org_name": "Ann's Electrical", "brand": "sitesentinel"}


def signed_up(client, **kw):
    r = client.post("/v1/auth/signup", json={**SIGNUP, **kw})
    assert r.status_code == 201, r.text
    client.token = r.json()["token"]
    return r.json()


def test_health_and_public(client):
    assert client.get("/health").json()["ok"]
    plans = {p["id"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert plans["team"]["monthly_gbp"] == 99 and plans["team"]["annual_gbp"] == 990
    r = client.get("/v1/exports/fields.csv", params={"brand": "creditpulse"})
    assert r.status_code == 200 and "amount_owed_gbp" in r.text and r.headers["content-type"].startswith("text/csv")


def test_signup_login_me(client):
    signed_up(client)
    me = client.get("/v1/me").json()
    assert me["org"]["brand"] == "sitesentinel" and me["org"]["status"] == "trialing"
    assert me["org"]["entitlements"]["instant"] is True  # trial gets Team features
    assert client.post("/v1/auth/signup", json=SIGNUP).status_code == 409
    client.token = None
    assert client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": "wrong password!"}).status_code == 401
    r = client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": SIGNUP["password"]})
    assert r.status_code == 200
    assert client.get("/v1/me").status_code == 401  # no token set


def test_validation_errors(client):
    r = client.post("/v1/auth/signup", json={**SIGNUP, "password": "short", "brand": "nope"})
    assert r.status_code == 422 and "password" in r.json()["error"] and "brand" in r.json()["error"]


def test_events_projected_by_level_and_csv(client, company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    signed_up(client)
    events.record("gazette", "n1", "administration", "07654321", "Admin", data={"ip_firm": "Harrow & Vale LLP", "url": "https://x/1"})
    run.process_new()
    r = client.get("/v1/events").json()
    assert r["level_name"] == "Standard" and len(r["events"]) == 1
    ev = r["events"][0]
    assert ev["company_name"] == "HARBOURLINE BUILD LTD" and ev["ip_firm"] == "Harrow & Vale LLP"
    assert "directors" not in ev and "ai_brief" not in ev  # Advanced fields hidden on Standard
    db.execute("UPDATE orgs SET plan='pro', status='active'")
    ev = client.get("/v1/events").json()["events"][0]
    assert ev["ai_brief"] and ev["risk_score"] >= 80
    csv = client.get("/v1/exports/events.csv")
    assert csv.status_code == 200 and csv.text.startswith("﻿event_id,") and "HARBOURLINE" in csv.text
    assert client.get(f"/v1/events/{ev['event_id']}").json()["company_number"] == "07654321"
    comp = client.get("/v1/companies/07654321").json()
    assert comp["company"]["company_name"] == "HARBOURLINE BUILD LTD" and comp["timeline"][0]["event_type"] == "administration"


def test_watchlist_import_limits_and_export(client):
    signed_up(client, brand="creditpulse")
    wl = client.get("/v1/watchlists").json()["watchlists"][0]
    csv_text = "Company Number,Company Name,Amount Owed\n1234567,ACME LTD,\"£18,400\"\nSC1234,SCOT LTD,500\n,missing,1\n"
    r = client.request("POST", f"/v1/watchlists/{wl['id']}/import", raw=csv_text.encode())
    assert r.json() == {"added": 2, "skipped": 1}
    items = client.get(f"/v1/watchlists/{wl['id']}").json()["items"]
    assert {i["company_number"] for i in items} == {"01234567", "SC001234"}
    assert next(i for i in items if i["company_number"] == "01234567")["amount_owed"] == 18400
    db.execute("UPDATE orgs SET plan='solo', status='active'")  # 25 companies on Solo
    for n in range(23):
        client.post(f"/v1/watchlists/{wl['id']}/items", json={"company_number": str(1000 + n)})
    assert client.post(f"/v1/watchlists/{wl['id']}/items", json={"company_number": "99999"}).status_code == 402
    assert "01234567" in client.get(f"/v1/watchlists/{wl['id']}/export.csv").text


def test_channels_follow_plan(client):
    signed_up(client)
    db.execute("UPDATE orgs SET plan='solo', status='active'")
    assert client.post("/v1/channels", json={"kind": "sms", "target": "+447700900123"}).status_code == 402
    db.execute("UPDATE orgs SET plan='pro'")
    assert client.post("/v1/channels", json={"kind": "slack", "target": "https://evil.example/hook"}).status_code == 422
    assert client.post("/v1/channels", json={"kind": "slack", "target": "https://hooks.slack.com/services/T/B/x"}).status_code == 201
    assert len(client.get("/v1/channels").json()["channels"]) == 2


def test_rules_crud(client):
    signed_up(client)
    r = client.post("/v1/alert-rules", json={"name": "Big ones", "event_types": ["administration"], "min_score": 70})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert client.post("/v1/alert-rules", json={"name": "x", "event_types": ["nonsense"]}).status_code == 422
    assert client.put(f"/v1/alert-rules/{rid}", json={"name": "Renamed", "min_score": 80}).json()["updated"]
    assert client.delete(f"/v1/alert-rules/{rid}").json()["deleted"]


def test_bulk_export_job(client, company_factory):
    from app.delivery import dispatch
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    signed_up(client)
    events.record("gazette", "n1", "winding_up_petition", "07654321", "Petition")
    run.process_new()
    job = client.post("/v1/exports/bulk", json={"max_rows": 100}).json()
    assert dispatch.run_export_jobs() == 1
    assert client.get(f"/v1/exports/bulk/{job['id']}").json()["row_count"] == 1
    assert "HARBOURLINE" in client.get(f"/v1/exports/bulk/{job['id']}/download").text


def test_canceled_org_blocked(client):
    signed_up(client)
    db.execute("UPDATE orgs SET status='canceled'")
    assert client.get("/v1/events").status_code == 402


def test_stripe_webhook(client, monkeypatch):
    from app import config
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", "price_pro_y")
    config.reload()
    info = signed_up(client)
    evt = {"type": "checkout.session.completed", "data": {"object": {"client_reference_id": info["org_id"], "customer": "cus_1",
           "subscription": "sub_1", "metadata": {"plan": "pro", "cycle": "annual"}}}}
    payload = json.dumps(evt).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_test", f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    assert client.request("POST", "/v1/billing/webhook", raw=payload, headers={"stripe-signature": "t=0,v1=bad"}).status_code == 400
    assert client.request("POST", "/v1/billing/webhook", raw=payload, headers={"stripe-signature": f"t={t},v1={sig}"}).status_code == 200
    o = db.one("SELECT * FROM orgs WHERE id=?", (info["org_id"],))
    assert (o["plan"], o["billing_cycle"], o["status"], o["stripe_customer_id"]) == ("pro", "annual", "active", "cus_1")
    upd = {"type": "customer.subscription.updated", "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "past_due",
           "items": {"data": [{"price": {"id": "price_pro_y"}}]}}}}
    p2 = json.dumps(upd).encode()
    s2 = hmac.new(b"whsec_test", f"{t}.".encode() + p2, hashlib.sha256).hexdigest()
    client.request("POST", "/v1/billing/webhook", raw=p2, headers={"stripe-signature": f"t={t},v1={s2}"})
    assert db.one("SELECT status FROM orgs WHERE id=?", (info["org_id"],))["status"] == "past_due"


def test_admin_requires_staff_and_risks(client):
    from app.sources import risk_watch
    risk_watch.seed()
    signed_up(client)
    assert client.get("/v1/admin/stats").status_code == 403
    db.execute("UPDATE users SET role='staff'")
    stats = client.get("/v1/admin/stats").json()
    assert stats["orgs_by_brand"][0]["brand"] == "sitesentinel"
    risks = client.get("/v1/risks", params={"brand": "sponsorwatch"}).json()["risks"]
    assert len(risks) == 6
    assert client.patch(f"/v1/admin/risk-factors/{risks[0]['id']}", json={"fact": "Updated fact"}).json()["updated"]
    flagged = risk_watch.check_sources(fetch=lambda url: "<p>v1</p>")
    assert flagged == []
    flagged = risk_watch.check_sources(fetch=lambda url: "<p>v2</p>")
    assert len(flagged) == 48
    assert client.post("/v1/admin/run/nonsense").status_code == 404


def test_objection_endpoint(client, company_factory):
    r = client.post("/v1/privacy/object", json={"kind": "company", "value": "07654321", "reason": "please stop"})
    assert r.status_code == 201
    assert db.one("SELECT * FROM suppressions WHERE value='07654321'")
    assert client.post("/v1/privacy/object", json={"kind": "bogus", "value": "x1"}).status_code == 422


def test_login_throttle(client):
    for _ in range(10):
        client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope nope"})
    assert client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope nope"}).status_code == 429
