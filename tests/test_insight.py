"""Company Insight: risk model, bands, triggers, snapshots/labels/fit, news, API rules and decision log."""
from datetime import date, timedelta

from app import db, events, insight
from app.sources import news

TODAY = date.today()


def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat() + "T09:00:00"


def healthy(cf, number="01111111", **kw):
    args = dict(sic="62020", net_assets=500_000, accounts_overdue=0, incorporated_on="2010-01-01")
    args.update(kw)
    return cf(number, "STEADY SOFTWARE LTD", **args)


def signup(client, **kw):
    r = client.post("/v1/auth/signup", json={"email": "cm@supplier.example", "password": "credit manager 1",
                                             "name": "Cara", "org_name": "Supplier Ltd", "brand": "creditpulse", **kw})
    assert r.status_code == 201, r.text
    client.token = r.json()["token"]


def test_sic_sections_and_base_rates():
    assert insight.sic_section("41201") == "F" and insight.sic_section("56101, 62020") == "I"
    assert insight.sic_section("") is None
    assert insight.base_rate("F") > insight.base_rate("L")
    insight.set_base_rate("F", 0.011, "Insolvency Service, 12 months to Aug 2026")
    assert insight.base_rate("F") == 0.011


def test_healthy_company_is_low_with_reducing_reason(company_factory):
    healthy(company_factory)
    a = insight.assess("01111111")
    assert a["band"] == "Low" and a["probability"] < 0.01 and a["trigger"] is None
    assert any(r["feature"] == "established_on_time" for r in a["reducing"])
    assert a["sector"] == "Information and communication"


def test_distress_signals_raise_band_and_explain(company_factory):
    company_factory("02222222", "SHAKY BUILD LTD", ccj_count_12m=2, cs_overdue=1)  # construction, overdue, negative net assets
    for i in range(3):
        events.record("companies_house", f"res{i}", "director_resigned", "02222222", "Resigned", published_at=days_ago(10 + i))
    events.record("companies_house", "ch1", "new_charge", "02222222", "Charge", published_at=days_ago(20))
    a = insight.assess("02222222")
    assert a["band"] in ("High", "Critical") and a["probability"] > 0.08
    top = [r["feature"] for r in a["reasons"]]
    assert "accounts_overdue" in top and len(a["reasons"]) == 3
    assert [r["effect"] for r in a["reasons"]] == sorted([r["effect"] for r in a["reasons"]], reverse=True)
    assert any(r["feature"] == "new_charge_under_stress" for r in a["reasons"] + a["other_reasons"])


def test_triggers_override_the_model(company_factory):
    healthy(company_factory, number="03333333")
    events.record("gazette", "wup1", "winding_up_petition", "03333333", "Petition", published_at=days_ago(3))
    a = insight.assess("03333333")
    assert a["band"] == "Critical" and a["probability"] >= 0.35 and a["trigger"]["event_type"] == "winding_up_petition"
    events.record("gazette", "adm1", "administration", "03333333", "Administrators appointed", published_at=days_ago(1))
    assert insight.assess("03333333")["probability"] >= 0.95


def test_personal_data_events_are_never_inputs(company_factory):
    healthy(company_factory, number="04444444")
    events.record("gazette", "pd1", "winding_up_petition", "04444444", "x", published_at=days_ago(2), personal_data=True)
    assert insight.assess("04444444")["trigger"] is None


def test_protocol_per_product():
    p = insight.protocol("Critical", "tenantguard")
    assert "agent" in p["action"] and p["two_person_rule"] and p["review"] == "Same day"
    assert insight.protocol("Critical", "distressradar")["action"].startswith("Make contact")
    assert insight.protocol("Watch", "unknownbrand")["action"].startswith("No new credit-limit")


def test_snapshot_label_calibration_and_fit(company_factory, monkeypatch):
    healthy(company_factory, number="05555555")
    company_factory("06666666", "FAILING LTD")
    events.record("companies_house", "x1", "director_resigned", "05555555", "r", published_at=days_ago(5))
    events.record("companies_house", "x2", "director_resigned", "06666666", "r", published_at=days_ago(5))
    assert insight.snapshot() == 2
    assert insight.snapshot() == 2  # refreshes the same month, no duplicates
    assert db.one("SELECT COUNT(*) AS n FROM insight_snapshots")["n"] == 2
    events.record("gazette", "f1", "cvl", "06666666", "CVL", published_at=days_ago(0))
    assert insight.label() == 1  # the failure is labelled now; the healthy one waits 12 months
    assert db.one("SELECT label_failed FROM insight_snapshots WHERE company_number='06666666'")["label_failed"] == 1
    # past snapshots older than 12 months get a 0 label
    db.execute("INSERT INTO insight_snapshots (company_number, month, sic_section, features, probability, band, model_version, created_at) "
               "VALUES ('05555555','2024-01','J','{}',0.004,'Low','v1-priors',?)", (db.now(),))
    insight.label()
    assert db.one("SELECT label_failed FROM insight_snapshots WHERE month='2024-01'")["label_failed"] == 0
    cal = insight.calibration()
    assert cal["labelled"] == 2 and cal["failures"] == 1 and cal["auc"] == 1.0
    assert insight.fit()["fitted"] is False  # not enough failures yet: priors stay
    r = insight.fit(min_positives=1, epochs=20)
    assert r["fitted"] and insight.assess("05555555")["model_version"].startswith("v2-fitted")


def test_auc_handles_ties():
    assert insight.auc([(0.1, 0), (0.1, 1)]) == 0.5
    assert insight.auc([(0.1, 0), (0.9, 1), (0.5, 0)]) == 1.0
    assert insight.auc([(0.2, 0)]) is None


def test_news_name_matching_flags_and_storage(company_factory):
    assert news.search_name("ACME LTD") is None  # too generic
    assert news.search_name("Harbourline Construction Group Limited") == "Harbourline Construction"
    assert news.flag_terms("Builder Harbourline falls into administration") == ["administration"]
    healthy(company_factory, number="07777777")
    arts = [{"url": "https://news.example/a", "title": "Steady Software lost contract, redundancies feared",
             "domain": "news.example", "seendate": TODAY.strftime("%Y%m%d") + "T080000Z"},
            {"url": "javascript:alert(1)", "title": "bad"}, {"url": "https://news.example/b", "title": "Steady Software wins award"}]
    assert news.store("07777777", arts) == 2 and news.store("07777777", arts) == 0
    a = insight.assess("07777777")
    assert a["features"]["negative_news_30d"] == 1

    class Resp:
        status_code, text = 200, '{"articles": []}'

        def json(self):
            return {"articles": [{"url": "https://n.example/c", "title": "Steady Software collapse", "seendate": "20261101T000000Z"}]}

    class Http:
        def get(self, url, params=None, timeout=None):
            assert '"steady software"' in params["query"].lower()
            return Resp()

    db.insert("orgs", {"id": "o1", "name": "O", "brand": "creditpulse", "plan": "team", "status": "active", "created_at": db.now()})
    db.insert("watchlists", {"id": "wl1", "org_id": "o1", "name": "W", "created_at": db.now()})
    db.insert("watchlist_items", {"id": "wi1", "watchlist_id": "wl1", "company_number": "07777777", "created_at": db.now()})
    news.LIMIT.calls.clear()
    assert news.poll_watched(http=Http()) == 1


def test_api_insight_report_rules_and_allowance(client, company_factory):
    signup(client, brand="sitesentinel")  # trial = Team: 250 lookups; SiteSentinel covers construction petitions
    company_factory("08888888", "RISKY TRADING LTD")
    events.record("gazette", "w1", "winding_up_petition", "08888888", "Petition", published_at=days_ago(4))
    from app.pipeline import run
    run.process_new()
    r = client.get("/v1/insight/company/8888888")  # number normalised
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["band"] == "Critical" and d["protocol"]["two_person_rule"] and d["trigger"]["label"]
    assert d["timeline"] and d["timeline"][0]["label"] == "Winding up petition"
    assert "features" not in d and d["allowance"]["used"] == 1
    assert client.get("/v1/insight/company/99999999").status_code == 404
    # objections hide the company everywhere, including insight
    from app.pipeline import compliance
    compliance.suppress("company", "08888888", "objection")
    assert client.get("/v1/insight/company/08888888").status_code == 404
    # allowance
    org = db.one("SELECT * FROM orgs")
    from app import views
    views.bump_usage(org["id"], insight=500)
    healthy(company_factory, number="09999999")
    assert client.get("/v1/insight/company/09999999").status_code == 402
    # watchlist companies stay free
    wl = client.post("/v1/watchlists", json={"name": "Customers"}).json()
    assert client.post(f"/v1/watchlists/{wl['id']}/items", json={"company_number": "09999999"}).status_code in (200, 201)
    assert client.get("/v1/insight/company/09999999").status_code == 200
    w = client.get("/v1/insight/watchlist").json()
    assert w["companies"][0]["company_number"] == "09999999" and w["companies"][0]["band"] == "Low"


def test_decision_log_and_two_person_rule(client, company_factory):
    signup(client)
    company_factory("01234567", "OWES US LTD")
    events.record("gazette", "w2", "winding_up_petition", "01234567", "Petition", published_at=days_ago(2))
    r = client.post("/v1/insight/company/01234567/decisions", json={"action": "increase_limit"})
    assert r.status_code == 422 and "second reviewer" in r.json()["error"]
    r = client.post("/v1/insight/company/01234567/decisions", json={"action": "increase_limit", "second_reviewer": "cm@supplier.example"})
    assert r.status_code == 422  # can't approve your own decision
    client.post("/v1/org/members", json={"email": "fd@supplier.example", "password": "finance director 1", "name": "FD"})
    r = client.post("/v1/insight/company/01234567/decisions",
                    json={"action": "increase_limit", "second_reviewer": "FD@supplier.example", "note": "Paid up front before"})
    assert r.status_code == 201 and r.json()["second_reviewer"] == "fd@supplier.example"
    r2 = client.post("/v1/insight/company/01234567/decisions", json={"action": "stop_credit"})
    assert r2.status_code == 201
    assert client.patch(f"/v1/insight/decisions/{r2.json()['id']}", json={"outcome": "unpaid"}).json()["outcome"] == "unpaid"
    assert client.patch(f"/v1/insight/decisions/{r2.json()['id']}", json={"outcome": "maybe"}).status_code == 422
    assert client.post("/v1/insight/company/01234567/decisions", json={"action": "bribe"}).status_code == 422
    assert len(client.get("/v1/insight/decisions").json()["decisions"]) == 2


def test_admin_insight_staff_only(client, company_factory):
    signup(client)
    assert client.get("/v1/admin/insight").status_code == 403
    db.execute("UPDATE users SET role='staff'")
    d = client.get("/v1/admin/insight").json()
    assert len(d["base_rates"]) == len(insight.SECTION_NAMES) and d["calibration"]["model"] == insight.MODEL_VERSION
    assert client.post("/v1/admin/insight/base-rates", json={"sic_section": "f", "annual_rate": 0.012,
                                                             "source": "Insolvency Service Aug 2026"}).status_code == 200
    assert insight.base_rate("F") == 0.012
    assert client.post("/v1/admin/insight/base-rates", json={"sic_section": "Z", "annual_rate": 0.01, "source": "x y z"}).status_code == 422


def test_company_search_by_name_and_number_skips_suppressed(client, company_factory):
    from app.pipeline import compliance
    signup(client)
    company_factory("04444444", "HARBOURLINE BUILD LTD", sic="41201")
    company_factory("05555555", "HARBOURLINE HIDDEN LTD", sic="41201")
    compliance.suppress("company", "05555555", "objection")
    r = client.get("/v1/companies/search", params={"q": "harbourline"})
    assert r.status_code == 200, r.text
    names = [c["name"] for c in r.json()["results"]]
    assert names == ["HARBOURLINE BUILD LTD"]
    assert client.get("/v1/companies/search", params={"q": "4444444"}).json()["results"][0]["number"] == "04444444"
    assert client.get("/v1/companies/search", params={"q": "h"}).status_code == 422


def test_every_page_links_home(client):
    for path, link in (("/app", 'data-v="home"'), ("/terminal", "⌂ Home"), ("/admin", "⌂ Home"), ("/outreach", "⌂ Home")):
        r = client.get(path)
        assert r.status_code == 200 and link in r.text, path
    portal = client.get("/app").text
    assert "SAMPLE_INSIGHT" in portal and "Sample report" in portal and "/v1/companies/search" in portal


def test_official_sector_rates_are_the_starting_point():
    # Insolvency Service, Business insolvency demography 2025 (per 10,000 active businesses)
    assert abs(insight.base_rate("I") - 0.02676) < 1e-9      # hospitality, highest
    assert abs(insight.base_rate("F") - 0.01319) < 1e-9      # construction
    assert abs(insight.base_rate("L") - 0.00598) < 1e-9      # real estate
    assert insight.base_rate("O") == insight.OVERALL_RATE and abs(insight.OVERALL_RATE - 0.01159) < 1e-9   # suppressed -> overall
    assert insight.base_rate(None) == insight.OVERALL_RATE
    assert "Business insolvency demography" in insight.base_rate_source("F")
    insight.set_base_rate("F", 0.02, "Staff test")              # a staff override still wins
    assert insight.base_rate("F") == 0.02 and insight.base_rate_source("F") == "Staff test"
    assert all(len(k) == 1 and 0 < v["rate"] < 400 for k, v in insight.OFFICIAL["sections"].items())


def test_age_curve_follows_official_figures():
    today = date(2026, 11, 20)
    m = lambda inc: insight.age_multiplier(inc, today)
    assert m("2026-01-01") < 0.4                              # under 2 years: 37 per 10,000, the lowest
    assert m("2020-06-01") > 1.4 and m("2018-01-01") > 1.4    # 4-10 years: the peak (169-170)
    assert m("2000-01-01") == 1.0 and m("") == 1.0
    assert m("2026-01-01") < m("2023-06-01") < m("2020-06-01")


def test_established_on_time_needs_five_years(company_factory):
    company_factory("06666661", "YOUNG CLEAN LTD", sic="62020", accounts_overdue=0, net_assets=1000, incorporated_on=(TODAY - timedelta(days=300)).isoformat())
    company_factory("06666662", "OLD CLEAN LTD", sic="62020", accounts_overdue=0, net_assets=1000, incorporated_on="2012-01-01")
    assert insight.features("06666661")["features"]["established_on_time"] == 0
    assert insight.features("06666662")["features"]["established_on_time"] == 1
