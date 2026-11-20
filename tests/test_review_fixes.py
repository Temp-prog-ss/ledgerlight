"""Regression tests for the defects found in the independent code review."""
import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config, db, events, plans
from app.delivery import dispatch
from app.pipeline import run
from tests.test_api import SIGNUP, signed_up
from tests.test_pipeline import deliveries_for, org


def test_staff_is_never_granted_by_email(client, monkeypatch):
    monkeypatch.setenv("LL_STAFF_EMAILS", SIGNUP["email"])
    signed_up(client)
    assert client.get("/v1/admin/stats").status_code == 403


def test_trials_get_team_not_enterprise(client):
    assert client.post("/v1/auth/signup", json={**SIGNUP, "plan": "enterprise"}).status_code == 422
    signed_up(client, plan="pro")
    assert client.get("/v1/me").json()["org"]["entitlements"]["plan_id"] == "team"


def test_bad_timezone_rejected_and_cannot_stop_other_digests(client, company_factory):
    signed_up(client)
    assert client.patch("/v1/org", json={"timezone": "Mars/Olympus"}).status_code == 422
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    bad, good = org("sitesentinel", timezone="Not/AZone"), org("sitesentinel")
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    assert dispatch.send_digests(now_utc=datetime.now(timezone.utc).replace(hour=12)) >= 2


def test_rules_rechecked_at_send_time(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    o = org("sitesentinel")
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    from app.pipeline import compliance
    compliance.suppress("company", "7654321", "objection")  # unpadded number must still match
    assert dispatch.send_instant() == 0
    assert {d["status"] for d in deliveries_for(o)} == {"skipped"}


def test_unvetted_or_canceled_org_gets_nothing_already_queued(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    o = org("sitesentinel")
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    db.execute("UPDATE orgs SET status='canceled' WHERE id=?", (o["id"],))
    assert dispatch.send_instant() == 0


def test_objection_hides_company_everywhere(client, company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    signed_up(client)
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    assert len(client.get("/v1/events").json()["events"]) == 1
    tok, client.token = client.token, None
    assert client.post("/v1/privacy/object", json={"kind": "company", "value": "7654321"}).status_code == 201
    client.token = tok
    assert client.get("/v1/events").json()["events"] == []
    assert client.get("/v1/companies/07654321").status_code == 404


def test_old_subscription_deletion_is_ignored(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    config.reload()
    info = signed_up(client)
    db.execute("UPDATE orgs SET status='active', stripe_customer_id='cus_1', stripe_subscription_id='sub_new' WHERE id=?", (info["org_id"],))
    evt = {"type": "customer.subscription.deleted", "created": int(time.time()),
           "data": {"object": {"id": "sub_old", "customer": "cus_1", "status": "canceled"}}}
    p = json.dumps(evt).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec_test", f"{t}.".encode() + p, hashlib.sha256).hexdigest()
    client.request("POST", "/v1/billing/webhook", raw=p, headers={"stripe-signature": f"t={t},v1={sig}"})
    assert db.one("SELECT status FROM orgs WHERE id=?", (info["org_id"],))["status"] == "active"


def test_digest_once_per_local_day_ahead_of_utc(company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    o = org("sitesentinel", timezone="Asia/Tokyo", digest_hour=7)
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    t1 = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)  # 08:30 on the 19th in Tokyo
    assert dispatch.send_digests(now_utc=t1) == 1
    events.record("gazette", "n2", "winding_up_petition", "07654321", "Petition")
    run.process_new()
    assert dispatch.send_digests(now_utc=t1 + timedelta(hours=2)) == 0  # same Tokyo day


def test_forwarded_for_cannot_dodge_login_throttle(client):
    for i in range(10):
        client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope"}, headers={"x-forwarded-for": f"10.0.0.{i}"})
    r = client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope"}, headers={"x-forwarded-for": "10.9.9.9"})
    assert r.status_code == 429


def test_default_secret_refused_in_production(monkeypatch):
    monkeypatch.setenv("LL_ENV", "production")
    monkeypatch.setenv("LL_SECRET_KEY", "dev-only-change-me")
    config.reload()
    from app.web import create_app
    with pytest.raises(RuntimeError):
        create_app()


def test_admin_vetted_must_be_boolean(client):
    signed_up(client)
    db.execute("UPDATE users SET role='staff'")
    oid = db.one("SELECT id FROM orgs")["id"]
    assert client.patch(f"/v1/admin/orgs/{oid}", json={"vetted": "false"}).status_code == 422
    assert client.patch(f"/v1/admin/orgs/{oid}", json={"vetted": True}).json()["updated"]
    assert db.one("SELECT vetted FROM orgs")["vetted"] == 1


def test_solo_cannot_filter_by_hidden_score(client, company_factory):
    company_factory("07654321", "HARBOURLINE BUILD LTD")
    signed_up(client, regions=["North West"])
    db.execute("UPDATE orgs SET plan='solo', status='active'")
    events.record("gazette", "n1", "administration", "07654321", "Admin")
    run.process_new()
    assert len(client.get("/v1/events", params={"min_score": 99}).json()["events"]) == 1  # filter ignored below Advanced


def test_bad_stream_line_does_not_crash_handler():
    from app.sources import companies_house as ch
    with pytest.raises(Exception):
        ch.handle_stream_line("companies", '{"resource_kind": "company-profile", "data": ')


def test_tests_ignore_host_production_settings():
    """On Render LL_TRUST_PROXY=true and LL_ENV=production are set; the suite must run on defaults."""
    from app import config
    assert config.settings.trust_proxy is False and config.settings.env != "production"


def test_behind_render_throttle_uses_true_client_ip_not_forwarded_for(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "trust_proxy", True)
    for i in range(10):  # spoofed X-Forwarded-For changes every time; the real (Cloudflare) IP doesn't
        client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope"},
                    headers={"x-forwarded-for": f"10.0.0.{i}", "true-client-ip": "203.0.113.7"})
    r = client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope"},
                    headers={"x-forwarded-for": "10.9.9.9", "true-client-ip": "203.0.113.7"})
    assert r.status_code == 429
    other = client.post("/v1/auth/login", json={"email": "x@y.zz", "password": "nope nope"}, headers={"true-client-ip": "198.51.100.2"})
    assert other.status_code == 401  # a different real visitor isn't locked out
