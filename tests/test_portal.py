"""Customer portal (login + manual extraction) and the staff admin dashboard."""
from app import db, events
from app.pipeline import run

SIGNUP = {"email": "ann@builder.example", "password": "correct horse 99", "name": "Ann", "org_name": "Ann's Electrical", "brand": "sitesentinel"}


def signed_up(client, **kw):
    r = client.post("/v1/auth/signup", json={**SIGNUP, **kw})
    assert r.status_code == 201, r.text
    client.token = r.json()["token"]
    return r.json()


def seed(company_factory, n=3):
    for i in range(n):
        num = f"0765432{i}"
        company_factory(num, f"HARBOURLINE BUILD {i} LTD")
        events.record("gazette", f"n{i}", "administration", num, "Admin",
                      data={"ip_firm": "Harrow & Vale LLP", "url": f"https://x/{i}"}, published_at=f"2026-11-1{i}T09:00:00")
    run.process_new()


def make_staff(client):
    signed_up(client, email="staff@ledgerlight.example", org_name="Ledgerlight", brand="ledgerlight")
    db.execute("UPDATE users SET role='staff' WHERE email='staff@ledgerlight.example'")


def test_pages_served_with_security_headers(client):
    for path in ("/app", "/admin"):
        r = client.get(path)
        assert r.status_code == 200 and "<!DOCTYPE html>" in r.text
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/").status_code == 302
    b = client.get("/v1/brands").json()
    assert {x["id"] for x in b["brands"]} >= {"ledgerlight", "sitesentinel", "estatesignal"} and "London" in b["regions"]


def test_options_show_locked_levels_and_fields(client):
    signed_up(client)  # trial = Team = Standard
    o = client.get("/v1/extract/options").json()
    assert o["level"] == 2 and o["brand"]["id"] == "sitesentinel"
    lv = {x["level"]: x for x in o["levels"]}
    assert lv[2]["unlocked"] and not lv[3]["unlocked"] and lv[3]["plan_needed"] == "Pro"
    f = {x["key"]: x for x in o["fields"]}
    assert f["company_name"]["unlocked"] and not f["directors"]["unlocked"]
    assert "sponsor_route" not in f  # other products' fields never show
    assert "deceased_estate" not in {t["id"] for t in o["event_types"]}
    assert o["allowance"]["rows_per_export"] == 5000 and not o["score_filter"]


def test_preview_and_csv_respect_level_columns_and_filters(client, company_factory):
    signed_up(client)
    seed(company_factory)
    p = client.get("/v1/extract/preview", params={"level": 1}).json()
    assert p["total"] == 3 and p["level_name"] == "Simple"
    assert "ip_firm" not in p["columns"][0] and all("ip_firm" not in r for r in p["rows"])
    p = client.get("/v1/extract/preview", params={"fields": "company_name,ip_firm"}).json()
    assert [c["key"] for c in p["columns"]] == ["company_name", "ip_firm"] and p["rows"][0]["ip_firm"] == "Harrow & Vale LLP"
    assert client.get("/v1/extract/preview", params={"since": "2026-11-11", "until": "2026-11-11"}).json()["total"] == 1
    assert client.get("/v1/extract/preview", params={"q": "BUILD 2"}).json()["total"] == 1
    r = client.get("/v1/extract/export.csv", params={"fields": "company_number,company_name", "max_rows": 2})
    assert r.status_code == 200 and r.headers["x-rows"] == "2"
    assert r.text.splitlines()[0] == "﻿company_name,company_number"  # catalogue order
    assert client.get("/v1/me").json()["usage"]["export_rows"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM audit_log WHERE action='extract_csv'")["n"] == 1


def test_plan_limits_enforced_server_side(client, company_factory):
    signed_up(client)
    seed(company_factory, 1)
    assert client.get("/v1/extract/preview", params={"level": 3}).status_code == 402
    r = client.get("/v1/extract/preview", params={"fields": "company_name,directors"})
    assert r.status_code == 402 and "directors" in r.json()["error"]
    assert client.get("/v1/extract/preview", params={"min_score": 50}).status_code == 402
    assert client.get("/v1/extract/preview", params={"type": "deceased_estate"}).status_code == 422
    assert client.get("/v1/extract/preview", params={"since": "yesterday"}).status_code == 422
    db.execute("UPDATE orgs SET plan='pro', status='active'")
    assert client.get("/v1/extract/preview", params={"level": 3, "min_score": 50}).json()["total"] == 1
    assert client.get("/v1/extract/preview", params={"level": 4}).status_code == 402


def test_monthly_allowance(client, company_factory):
    signed_up(client)
    seed(company_factory, 3)
    org = db.one("SELECT * FROM orgs")
    from app import views
    views.bump_usage(org["id"], rows=25000 - 2)  # Team trial: 25,000 a month
    r = client.get("/v1/extract/export.csv")
    assert r.status_code == 200 and r.headers["x-rows"] == "2"
    assert client.get("/v1/extract/export.csv").status_code == 402
    assert client.get("/v1/exports/events.csv").status_code == 402


def test_inactive_and_unvetted(client, company_factory):
    signed_up(client)
    db.execute("UPDATE orgs SET status='canceled'")
    assert client.get("/v1/extract/preview").status_code == 402
    client.token = None
    signed_up(client, email="probate@example.com", brand="estatesignal")
    assert client.get("/v1/extract/preview").json()["total"] == 0  # not vetted yet


def test_change_password_and_members(client):
    signed_up(client)
    assert client.post("/v1/auth/password", json={"current_password": "wrong", "new_password": "another good one"}).status_code == 401
    assert client.post("/v1/auth/password", json={"current_password": SIGNUP["password"], "new_password": "another good one"}).status_code == 200
    client.token = None
    assert client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": "another good one"}).status_code == 200
    client.token = client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": "another good one"}).json()["token"]
    m = client.get("/v1/org/members").json()
    assert m["members"][0]["email"] == SIGNUP["email"] and "password_hash" not in m["members"][0]


def test_admin_requires_staff(client):
    signed_up(client)
    for path in ("/v1/admin/overview", "/v1/admin/customers", "/v1/admin/events", "/v1/admin/audit", "/v1/admin/jobs"):
        assert client.get(path).status_code == 403
    client.token = None
    assert client.get("/v1/admin/overview").status_code == 401


def test_admin_dashboard_flow(client, company_factory):
    signed_up(client)
    seed(company_factory, 2)
    customer_org = db.one("SELECT id FROM orgs")["id"]
    customer_user = db.one("SELECT id FROM users")["id"]
    client.token = None
    make_staff(client)
    o = client.get("/v1/admin/overview").json()
    assert o["kpis"]["companies"] == 2 and o["kpis"]["customers"] == 1 and o["kpis"]["trialing"] == 2
    assert len(o["events_per_day"]) == 14 and {j["job"] for j in o["jobs"]} >= {"gazette", "unclaimed_estates"}
    c = client.get("/v1/admin/customers").json()["customers"]
    assert {x["is_internal"] for x in c} == {True, False}
    d = client.get(f"/v1/admin/customers/{customer_org}").json()
    assert d["org"]["brand"] == "sitesentinel" and d["users"][0]["email"] == SIGNUP["email"]
    assert client.post(f"/v1/admin/orgs/{customer_org}/extend-trial", json={"days": 7}).status_code == 200
    assert client.patch(f"/v1/admin/orgs/{customer_org}", json={"plan": "pro", "status": "active"}).json()["updated"]
    r = client.post(f"/v1/admin/users/{customer_user}/reset-password").json()
    temp = r["temporary_password"]
    staff_id = db.one("SELECT id FROM users WHERE role='staff'")["id"]
    assert client.post(f"/v1/admin/users/{staff_id}/reset-password").status_code == 403
    ev = client.get("/v1/admin/events", params={"sort": "score", "brand": "sitesentinel"}).json()["events"]
    assert len(ev) == 2 and "sitesentinel" in ev[0]["brands"]
    actions = {a["action"] for a in client.get("/v1/admin/audit").json()["audit"]}
    assert {"admin_reset_password", "admin_extend_trial", "admin_org_update"} <= actions
    client.token = None
    login = client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": temp})
    assert login.status_code == 200
    client.token = login.json()["token"]
    assert client.get("/v1/extract/options").json()["level"] == 3  # now Pro
