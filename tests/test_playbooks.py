"""Profit playbooks: content shape, per-brand API, terminal suggestions, portal and terminal wiring."""
from app import brands, playbooks


def signup(client, brand="sitesentinel", email="plays@builder.example"):
    r = client.post("/v1/auth/signup", json={"email": email, "password": "playbook user 1", "name": "Ops",
                                             "org_name": "Builder Supplies", "brand": brand})
    assert r.status_code == 201, r.text
    client.token = r.json()["token"]


def test_every_brand_has_complete_plays():
    customer_brands = [b for b in brands.BRANDS if b != "ledgerlight"]
    assert sorted(playbooks.BRANDS) == sorted(customer_brands)
    for b, v in playbooks.BRANDS.items():
        assert len(v["plays"]) >= 3, b
        for p in v["plays"]:
            for key in ("id", "title", "profit", "money", "signals", "features", "steps", "track", "guardrails"):
                assert p.get(key), (b, p.get("id"), key)
            assert 4 <= len(p["steps"]) <= 8, p["id"]
            assert set(p["features"]) <= set(playbooks.FEATURES), p["id"]
    assert len(playbooks.PLAYS) == sum(len(v["plays"]) for v in playbooks.BRANDS.values())  # ids are unique


def test_api_needs_login_and_puts_own_industry_first(client):
    assert client.get("/v1/playbooks").status_code == 401
    signup(client, "creditpulse")
    d = client.get("/v1/playbooks").json()
    assert d["name"] == "CreditPulse" and d["plays"][0]["id"].startswith("cp-")
    assert "creditpulse" not in [o["brand"] for o in d["other"]] and len(d["other"]) == 7
    one = client.get("/v1/playbooks", params={"id": "ss-retention"}).json()["play"]
    assert one["brand"] == "sitesentinel" and one["steps"]
    assert client.get("/v1/playbooks", params={"id": "nope"}).status_code == 404


def test_terminal_items_suggest_matching_plays():
    petition = {"headline": "WINDING UP PETITION: HARBOURLINE BUILD LTD", "event_type": "winding_up_petition", "themes": ["insolvency"]}
    assert playbooks.suggest("sitesentinel", petition)[0]["id"] == "ss-retention"
    sponsor = {"headline": "Care operator loses sponsor licence", "themes": []}
    assert playbooks.suggest("sponsorwatch", sponsor)[0]["brand"] == "sponsorwatch"
    assert playbooks.suggest("filingpulse", {"headline": "Rates held", "themes": []})  # always something to show
    assert all(p["brand"] == "talentrelease" for p in playbooks.suggest("talentrelease", petition))


def test_portal_and_terminal_link_to_playbooks(client):
    portal = client.get("/app").text
    assert 'data-v="plays"' in portal and "/v1/playbooks" in portal and 'qs.get("play")' in portal
    assert "/app?play=" in client.get("/terminal").text


def test_site_blocks_carry_every_play_and_feature():
    import json
    from scripts import build_site_blocks as sb
    pb = json.loads((sb.ROOT / "data" / "playbooks.json").read_text(encoding="utf-8"))
    css, html, js = sb.build("ledgerlight", pb)
    assert all(p in js for p in playbooks.PLAYS) and 'id="v-playbooks"' in html and 'id="v-terminal"' in html
    for brand in playbooks.BRANDS:
        css, html, js = sb.build(brand, pb)
        assert all(p["id"] in js for p in playbooks.BRANDS[brand]["plays"])
        assert all(w[5] in playbooks.PLAYS for w in sb.WIRE[brand])           # every preview item links to a real play
        assert 'id="playbooks"' in html and 'id="platform"' in html and 'id="terminal"' in html
        assert "</script" not in js.split("<script>", 1)[1].split("</script>", 1)[0].replace("<\\/", "")
