"""Market terminal: news tagging and linking, unified feed rules, sector monitor, agents, limits, profile."""
import json
from datetime import date, timedelta

from app import db, events
from app.pipeline import run
from app.sources import market_news

NOW = date.today()


def days_ago(n):
    return (NOW - timedelta(days=n)).isoformat() + "T09:00:00"


def signup(client, brand="sitesentinel", email="ops@builder.example"):
    r = client.post("/v1/auth/signup", json={"email": email, "password": "terminal user 1", "name": "Ops",
                                             "org_name": "Builder Supplies", "brand": brand})
    assert r.status_code == 201, r.text
    client.token = r.json()["token"]


def watch(client, number):
    wl = client.get("/v1/watchlists").json()["watchlists"]
    wid = wl[0]["id"] if wl else client.post("/v1/watchlists", json={"name": "Main"}).json()["id"]
    assert client.post(f"/v1/watchlists/{wid}/items", json={"company_number": number, "amount_owed": 18000}).status_code in (200, 201)


def art(url, title, day=0):
    return {"url": url, "title": title, "domain": "news.example", "seendate": (NOW - timedelta(days=day)).strftime("%Y%m%d") + "T080000Z"}


def test_tagging_tone_and_company_linking(company_factory):
    themes, tone = market_news.tag("Builder falls into administration as energy costs bite")
    assert {"insolvency", "costs"} <= set(themes) and tone == "negative"
    assert market_news.tag("Contractor wins contract worth £40m")[1] == "positive"
    company_factory("01112222", "HARBOURLINE CONSTRUCTION LIMITED")
    events.record("companies_house", "e1", "director_resigned", "01112222", "r", published_at=days_ago(3))
    idx = market_news.tracked_names()
    assert market_news.link_companies("Harbourline Construction calls in administrators", idx) == ["01112222"]
    assert market_news.link_companies("Harbourline Constructions Group news", idx) == []  # whole-name match only
    n = market_news.store([art("https://n.example/1", "Harbourline Construction calls in administrators"),
                           art("javascript:x", "bad"), art("https://n.example/2", "")], "F", idx)
    assert n == 1
    assert market_news.store([art("https://n.example/1", "Harbourline Construction calls in administrators")], "MACRO", idx) == 0
    row = db.one("SELECT * FROM market_news")
    assert json.loads(row["sectors"]) == ["F", "MACRO"] and json.loads(row["companies"]) == ["01112222"]


def test_poll_rotates_sectors(monkeypatch):
    seen = []

    class R:
        status_code, text = 200, '{"articles":[]}'

        def json(self):
            return {"articles": [art(f"https://n.example/{len(seen)}", "Retailer cuts jobs")]}

    class H:
        def get(self, url, params=None, timeout=None):
            seen.append(params["query"])
            return R()

    from app.sources import news
    from app.sources.http import RateLimiter
    monkeypatch.setattr(news, "LIMIT", RateLimiter(1000, window=1))
    assert market_news.poll(per_run=2, http=H()) == 2
    market_news.poll(per_run=2, http=H())
    assert len(set(seen)) == 4 and db.get_state("market_news_cursor") == "4"


def test_feed_merges_signals_and_news_with_rules(client, company_factory):
    signup(client)
    company_factory("02223333", "OAKMERE GROUNDWORKS LTD")
    events.record("gazette", "w1", "winding_up_petition", "02223333", "Petition", published_at=days_ago(1))
    events.record("gazette", "pd", "deceased_estate", None, "Deceased estate", published_at=days_ago(1), personal_data=True)
    run.process_new()
    market_news.store([art("https://n.example/a", "Oakmere Groundworks faces winding-up petition"),
                       art("https://n.example/b", "Bank of England raises interest rates")], "F")
    watch(client, "02223333")
    d = client.get("/v1/terminal/feed").json()
    kinds = {i["kind"] for i in d["items"]}
    assert kinds == {"signal", "news"}
    sig = [i for i in d["items"] if i["kind"] == "signal"][0]
    assert sig["headline"].startswith("WINDING UP PETITION") and sig["relevance"] == "watchlist"
    assert all("deceased" not in i["headline"].lower() for i in d["items"])  # personal-data notices never shown
    news_item = [i for i in d["items"] if "Oakmere" in i["headline"] and i["kind"] == "news"][0]
    assert news_item["companies"][0]["on_watchlist"]
    assert client.get("/v1/terminal/feed", params={"q": "interest"}).json()["items"][0]["themes"] == ["rates"]
    assert all(i["companies"] for i in client.get("/v1/terminal/feed", params={"watchlist": "1"}).json()["items"])
    assert client.get("/v1/terminal/feed", params={"sector": "ZZ"}).status_code == 422
    # objections hide linked companies
    from app.pipeline import compliance
    compliance.suppress("company", "02223333", "objection")
    d = client.get("/v1/terminal/feed").json()
    assert not any(c["number"] == "02223333" for i in d["items"] for c in i["companies"])


def test_sector_monitor_and_movers(client, company_factory):
    signup(client)
    company_factory("03334444", "SITE CO LTD")
    events.record("gazette", "w2", "winding_up_petition", "03334444", "Petition", published_at=days_ago(2))
    run.process_new()
    market_news.store([art("https://n.example/c", "Builder collapses into administration")], "F")
    s = {x["sector"]: x for x in client.get("/v1/terminal/sectors").json()["sectors"]}
    assert s["F"]["signals_7d"] == 1 and s["F"]["news_7d"] == 1 and s["F"]["negative_share"] == 100
    watch(client, "03334444")
    m = client.get("/v1/terminal/movers").json()["movers"]
    assert m[0]["number"] == "03334444" and m[0]["band"] == "Critical"


def test_explain_agents_perspectives_cache_and_limits(client, company_factory):
    signup(client)
    assert client.put("/v1/terminal/profile", json={"role": "supplier", "sector": "F", "about": "We supply timber"}).status_code == 200
    company_factory("04445555", "TIMBERLINE BUILD LTD")
    events.record("gazette", "w3", "winding_up_petition", "04445555", "Petition", published_at=days_ago(1))
    run.process_new()
    watch(client, "04445555")
    market_news.store([art("https://n.example/d", "Timberline Build calls in administrators after energy costs soar")], "F")
    item = [i for i in client.get("/v1/terminal/feed").json()["items"] if i["kind"] == "news"][0]
    r = client.post("/v1/terminal/explain", json={"item_id": item["id"], "perspective": "customers"}).json()
    assert r["model"] == "rules" and not r["cached"] and len(r["agents"]) == 3
    assert any("TIMBERLINE" in x.upper() for x in r["your_exposure"]) and r["actions"][0]["urgency"] == "now"
    assert r["disclaimer"].startswith("Information")
    again = client.post("/v1/terminal/explain", json={"item_id": item["id"], "perspective": "customers"}).json()
    assert r["playbooks"] and again["playbooks"] and r["playbooks"][0]["id"].startswith("ss-")
    assert again["cached"]
    opp = client.post("/v1/terminal/explain", json={"item_id": item["id"], "perspective": "opportunity"}).json()
    assert opp["opportunities"] and "competitor" in opp["opportunities"][0]
    sig = [i for i in client.get("/v1/terminal/feed").json()["items"] if i["kind"] == "signal"][0]
    assert client.post("/v1/terminal/explain", json={"item_id": sig["id"], "perspective": "contractors"}).status_code == 200
    assert client.post("/v1/terminal/explain", json={"item_id": "news:nope", "perspective": "customers"}).status_code == 404
    assert client.post("/v1/terminal/explain", json={"item_id": item["id"], "perspective": "gossip"}).status_code == 422
    assert db.one("SELECT briefs FROM usage")["briefs"] == 3  # cached answer is free
    from app import views
    views.bump_usage(db.one("SELECT id FROM orgs")["id"], briefs=1000)
    assert client.post("/v1/terminal/explain", json={"item_id": item["id"], "perspective": "creditors"}).status_code == 402


def test_explain_never_sends_personal_data_and_signals_need_visibility(client, company_factory, monkeypatch):
    signup(client, brand="estatesignal", email="probate@firm.example")
    db.execute("UPDATE orgs SET vetted=1")
    events.record("gazette", "pd2", "deceased_estate", None, "Deceased estate", published_at=days_ago(40), personal_data=True)
    run.process_new()
    eid = db.one("SELECT id FROM events WHERE source_id='pd2'")["id"]
    r = client.post("/v1/terminal/explain", json={"item_id": f"signal:{eid}", "perspective": "opportunity"})
    assert r.status_code in (404, 422)
    # AI analyst gets only facts; check the payload when a key is configured
    from app import config, terminal
    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": json.dumps({"summary": "S", "what_it_means": ["m"], "your_exposure": [],
                                                                       "actions": [{"action": "a", "why": "w", "urgency": "soon"}],
                                                                       "opportunities": [], "watch_next": [], "confidence": "high"})}]}

    class Sess:
        def post(self, url, timeout=None, headers=None, json=None):
            sent.update(json)
            return Resp()

    monkeypatch.setattr(config.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(terminal, "session", lambda: Sess())
    market_news.store([art("https://n.example/e", "Interest rates rise again")], "MACRO")
    nid = db.one("SELECT id FROM market_news")["id"]
    out = client.post("/v1/terminal/explain", json={"item_id": f"news:{nid}", "perspective": "my_company"}).json()
    assert out["model"] != "rules" and out["actions"][0]["urgency"] == "monitor"  # reviewer normalised a bad urgency
    assert out["confidence"] == "medium"  # reviewer downgrades 'high' with no linked data
    payload = sent["messages"][0]["content"]
    assert "probate@firm.example" not in payload and "Deceased" not in payload


def test_profile_validation_and_pages(client):
    signup(client)
    assert client.put("/v1/terminal/profile", json={"role": "pirate"}).status_code == 422
    assert client.put("/v1/terminal/profile", json={"role": "lender", "sector": "Q"}).status_code == 200
    assert client.get("/v1/terminal/profile").json()["profile"]["role"] == "lender"
    r = client.get("/terminal")
    assert r.status_code == 200 and "LEDGERLIGHT" in r.text and r.headers["x-frame-options"] == "DENY"
    client.token = None
    assert client.get("/v1/terminal/feed").status_code == 401


def test_exposure_does_not_repeat_linked_companies(client, company_factory):
    signup(client)
    client.put("/v1/terminal/profile", json={"role": "supplier", "sector": "F"})
    company_factory("05556666", "QUARRYSIDE BUILD LTD")
    events.record("gazette", "w9", "winding_up_petition", "05556666", "Petition", published_at=days_ago(1))
    run.process_new()
    watch(client, "05556666")
    sig = [i for i in client.get("/v1/terminal/feed").json()["items"] if i["kind"] == "signal"][0]
    r = client.post("/v1/terminal/explain", json={"item_id": sig["id"], "perspective": "customers"}).json()
    assert r["summary"].startswith("WINDING UP PETITION: QUARRYSIDE BUILD LTD")
    assert sum("QUARRYSIDE" in x for x in r["your_exposure"]) == 1
