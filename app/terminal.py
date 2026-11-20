"""Market terminal: one live wire of market news and Ledgerlight signals, linked to company insight,
plus "What this means to you" explanations produced by a three-step agent pipeline.

Agents
  1. Researcher (deterministic): gathers only facts Ledgerlight holds - the item, linked companies'
     risk bands and reasons, the customer's profile, watchlist exposure in the same sectors, sector stats.
  2. Analyst (Claude when ANTHROPIC_API_KEY is set, otherwise a rules engine): turns the facts into
     what happened / what it means / exposure / actions / opportunities / what to watch, for one perspective.
  3. Reviewer (deterministic): keeps the answer to the facts, removes empty or over-long parts, sets urgency,
     and adds the "information, not advice" note.
Rules kept: signals come through views.visible_events (brand, vetting, cooling-off, suppression, plan);
personal-data events are never sent to the model or explained; AI use counts against briefs_per_month.
"""
import json
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field
from starlette.responses import HTMLResponse
from starlette.routing import Route

from app import config, db, insight, plans, views
from app.sources.http import session
from app.web import ApiError, body, current, ok, require_active

ROLES = {"supplier": "Supplier or trade creditor", "contractor": "Contractor or subcontractor", "landlord": "Landlord",
         "lender": "Lender or financier", "investor": "Investor or acquirer", "adviser": "Professional adviser",
         "recruiter": "Recruiter", "other": "Other"}
PERSPECTIVES = {"my_company": "My company", "customers": "My customers (who owe me)", "contractors": "My suppliers and contractors",
                "creditors": "My lenders and landlords", "opportunity": "How could I benefit?"}
CACHE_HOURS = 6
DISCLAIMER = "Information from public records and news, not financial, legal or investment advice. You make the decision."


def _since(days):
    return (date.today() - timedelta(days=days)).isoformat()


def profile(org):
    p = db.loads(org.get("profile"), None) or {}
    return {"role": p.get("role") or "supplier", "sector": p.get("sector"), "region": p.get("region"),
            "company_number": p.get("company_number"), "about": p.get("about")}


def _snapshot_bands(numbers):
    if not numbers:
        return {}
    rows = db.all(f"SELECT company_number, band, probability, month FROM insight_snapshots WHERE company_number IN "
                  f"({','.join('?' * len(numbers))}) ORDER BY month", tuple(numbers))
    return {r["company_number"]: {"band": r["band"], "probability": r["probability"]} for r in rows}


def _company_meta(numbers):
    if not numbers:
        return {}
    rows = db.all(f"SELECT number, name, sic_codes, region FROM companies WHERE number IN ({','.join('?' * len(numbers))})",
                  tuple(numbers))
    return {r["number"]: {"name": r["name"], "sector": insight.sic_section(r["sic_codes"]), "region": r["region"]} for r in rows}


def _available(numbers):
    return [n for n in numbers if insight.available(n)]


# ---------------- feed ----------------

def feed(org, sector=None, q=None, watchlist_only=False, days=7, limit=120):
    watch = views.watch_items(org["id"])
    prof = profile(org)
    q_up = (q or "").strip().upper()

    # Ledgerlight signals, through the shared view rules
    sig = views.visible_events(org, {"since": _since(days), "watchlist": watchlist_only}, limit=limit)
    sig = [e for e in sig if not e.get("personal_data")]
    nums = list({e["company_number"] for e in sig if e.get("company_number")})
    meta, bands = _company_meta(nums), _snapshot_bands(nums)
    items = []
    for e in sig:
        n = e.get("company_number")
        m = meta.get(n, {})
        name = m.get("name") or e.get("title") or ""
        items.append({"id": f"signal:{e['id']}", "kind": "signal", "time": (e.get("published_at") or e.get("created_at") or ""),
                      "headline": f"{e['event_type'].replace('_', ' ').upper()}: {name}".strip(),
                      "source": "Ledgerlight", "sectors": [m["sector"]] if m.get("sector") else [],
                      "themes": ["insolvency"] if e["event_type"] in insight.TRIGGERS else ["company"],
                      "tone": "negative" if e["event_type"] in insight.TRIGGERS or e["event_type"] in
                              ("accounts_overdue", "cs_overdue", "ccj", "strike_off_proposed", "director_resigned") else "neutral",
                      "companies": [{"number": n, "name": name, **bands.get(n, {}), "on_watchlist": n in watch}] if n else [],
                      "url": None, "event_type": e["event_type"]})

    # Market news
    where, params = ["published_at>=?"], [_since(days)]
    rows = db.all(f"SELECT * FROM market_news WHERE {' AND '.join(where)} ORDER BY published_at DESC LIMIT ?", (*params, limit * 2))
    news_nums = list({n for r in rows for n in json.loads(r["companies"] or "[]")})
    news_nums = _available(news_nums)
    meta2, bands2 = _company_meta(news_nums), _snapshot_bands(news_nums)
    for r in rows:
        comps = [{"number": n, "name": meta2.get(n, {}).get("name"), **bands2.get(n, {}), "on_watchlist": n in watch}
                 for n in json.loads(r["companies"] or "[]") if n in meta2]
        if watchlist_only and not any(c["on_watchlist"] for c in comps):
            continue
        items.append({"id": f"news:{r['id']}", "kind": "news", "time": r["published_at"], "headline": r["title"],
                      "source": r["domain"] or "News", "sectors": [s for s in json.loads(r["sectors"] or "[]") if s != "MACRO"],
                      "macro": "MACRO" in (r["sectors"] or ""), "themes": json.loads(r["themes"] or "[]"), "tone": r["tone"],
                      "companies": comps, "url": r["url"]})

    if sector:
        items = [i for i in items if sector in i["sectors"] or (i.get("macro") and sector == "MACRO")]
    if q_up:
        items = [i for i in items if q_up in i["headline"].upper() or any(q_up in (c.get("name") or "").upper()
                                                                           or q_up == c["number"] for c in i["companies"])]
    my_sectors = {prof["sector"]} | {m.get("sector") for m in _company_meta(list(watch)).values()}
    for i in items:
        i["relevance"] = ("watchlist" if any(c.get("on_watchlist") for c in i["companies"]) else
                          "your sector" if set(i["sectors"]) & my_sectors else "macro" if i.get("macro") else None)
        i["flags"] = _flags(i)
    items.sort(key=lambda i: i["time"] or "", reverse=True)
    return items[:limit]


def _flags(i):
    out = []
    if any(c.get("band") in ("High", "Critical") for c in i["companies"]):
        out.append("High-risk company")
    if "insolvency" in i["themes"]:
        out.append("Insolvency")
    if "rates" in i["themes"]:
        out.append("Rates")
    if "contracts" in i["themes"] or "deals" in i["themes"]:
        out.append("Opportunity")
    return out


def sector_monitor(org):
    """Per sector: Ledgerlight signals this week vs last, news volume and negative share, base rate."""
    cur = views.visible_events(org, {"since": _since(7)}, limit=3000)
    prev = [e for e in views.visible_events(org, {"since": _since(14)}, limit=6000) if (e.get("published_at") or "") < _since(7)]
    meta = _company_meta(list({e["company_number"] for e in cur + prev if e.get("company_number")}))
    news_rows = db.all("SELECT sectors, tone FROM market_news WHERE published_at>=?", (_since(7),))
    out = []
    for sec in [s for s in insight.SECTION_NAMES if s in ("C", "F", "G", "H", "I", "J", "K", "L", "N", "Q")]:
        c = sum(1 for e in cur if meta.get(e.get("company_number"), {}).get("sector") == sec)
        p = sum(1 for e in prev if meta.get(e.get("company_number"), {}).get("sector") == sec)
        n = [r for r in news_rows if f'"{sec}"' in (r["sectors"] or "")]
        neg = sum(1 for r in n if r["tone"] == "negative")
        out.append({"sector": sec, "name": insight.SECTION_NAMES[sec], "signals_7d": c, "signals_prev_7d": p,
                    "change": None if not p else round((c - p) / p * 100), "news_7d": len(n),
                    "negative_share": round(100 * neg / len(n)) if n else None,
                    "base_rate": round(insight.base_rate(sec), 4)})
    out.sort(key=lambda r: -(r["signals_7d"] + r["news_7d"] / 10))
    return out


def movers(org, limit=12):
    out = []
    for n in views.watch_items(org["id"]):
        if not insight.available(n):
            continue
        a = insight.assess(n)
        if not a:
            continue
        t = insight.trend(n, a["probability"])
        prev = t["previous"]
        out.append({"number": n, "name": a["company_name"], "probability": a["probability"], "band": a["band"],
                    "previous": prev, "change": None if prev is None else round(a["probability"] - prev, 4)})
    out.sort(key=lambda r: (-(abs(r["change"]) if r["change"] is not None else -1), -r["probability"]))
    return out[:limit]


# ---------------- agents ----------------

def _item(org, item_id):
    kind, _, ident = (item_id or "").partition(":")
    if kind == "news":
        r = db.one("SELECT * FROM market_news WHERE id=?", (ident,))
        if not r:
            raise ApiError(404, "News item not found")
        return {"kind": "news", "id": item_id, "headline": r["title"], "source": r["domain"], "url": r["url"],
                "time": r["published_at"], "sectors": [s for s in json.loads(r["sectors"] or "[]") if s != "MACRO"],
                "macro": "MACRO" in (r["sectors"] or ""), "themes": json.loads(r["themes"] or "[]"), "tone": r["tone"],
                "companies": _available(json.loads(r["companies"] or "[]"))}
    if kind == "signal":
        rows = views.visible_events(org, {"event_id": ident}, limit=1)
        if not rows:
            raise ApiError(404, "Signal not found in your feed")
        e = rows[0]
        if e.get("personal_data"):
            raise ApiError(422, "Explanations aren't available for notices about individuals")
        m = _company_meta([e["company_number"]]).get(e["company_number"], {}) if e.get("company_number") else {}
        return {"kind": "signal", "id": item_id, "headline": f"{e['event_type'].replace('_', ' ').upper()}: {m.get('name') or ''}".strip(),
                "source": "Ledgerlight", "url": None, "time": e.get("published_at"), "event_type": e["event_type"],
                "sectors": [m["sector"]] if m.get("sector") else [], "macro": False,
                "themes": ["insolvency"] if e["event_type"] in insight.TRIGGERS else ["company"],
                "tone": "negative", "companies": _available([e["company_number"]]) if e.get("company_number") else []}
    raise ApiError(422, "Unknown item")


def research(org, item):
    """Agent 1: gather facts only (company-level data; no individuals)."""
    prof = profile(org)
    linked = []
    for n in item["companies"][:5]:
        a = insight.assess(n)
        if a:
            linked.append({"company": a["company_name"], "number": n, "sector": a["sector"], "band": a["band"],
                           "probability_12m": a["probability"], "trigger": (a["trigger"] or {}).get("label"),
                           "reasons": [r["reason"] for r in a["reasons"]],
                           "recommended_action": insight.protocol(a["band"], org["brand"])["action"],
                           "on_your_watchlist": n in views.watch_items(org["id"])})
    watch = views.watch_items(org["id"])
    wmeta = _company_meta(list(watch))
    secs = set(item["sectors"]) or ({prof["sector"]} if item.get("macro") else set())
    exposed = []
    linked_nums = {c["number"] for c in linked}
    for n, m in wmeta.items():
        if n in linked_nums:
            continue  # already covered under linked companies
        if (m.get("sector") in secs or item.get("macro")) and insight.available(n):
            snap = _snapshot_bands([n]).get(n) or {}
            w = watch[n]
            exposed.append({"company": m["name"], "number": n, "sector": insight.SECTION_NAMES.get(m.get("sector"), "Unclassified"),
                            "band": snap.get("band"), "amount_owed_gbp": w.get("amount_owed"), "label": w.get("label")})
    order = {"Critical": 0, "High": 1, "Elevated": 2, "Watch": 3, "Low": 4, None: 5}
    exposed.sort(key=lambda x: order.get(x["band"], 5))
    return {
        "item": {k: item[k] for k in ("headline", "source", "time", "themes", "tone")} | {
            "sectors": [insight.SECTION_NAMES.get(s, s) for s in item["sectors"]], "macro": item.get("macro", False),
            "event_type": item.get("event_type")},
        "your_business": {"role": ROLES.get(prof["role"], prof["role"]), "sector": insight.SECTION_NAMES.get(prof["sector"]),
                          "region": prof["region"], "about": (prof.get("about") or "")[:300], "product": org["brand"]},
        "linked_companies": linked,
        "your_watchlist_in_these_sectors": {"count": len(exposed), "companies": exposed[:8],
                                            "high_or_critical": sum(1 for x in exposed if x["band"] in ("High", "Critical"))},
        "sector_base_rates": {insight.SECTION_NAMES.get(s, s): insight.base_rate(s) for s in item["sectors"]},
    }


ANALYST_SYSTEM = (
    "You are an analyst in a UK business-risk terminal. Explain what one news item or company signal means for the "
    "reader's business from a stated perspective. Use ONLY the facts in the JSON; never invent companies, people, "
    "figures or dates. Plain English a non-specialist understands. Be specific and practical; ranking matters. "
    "This is information, not financial, legal or investment advice: no instructions to buy or sell securities. "
    "Reply with JSON only: {\"summary\": \"1-2 sentences\", \"what_it_means\": [\"...\"], \"your_exposure\": [\"...\"], "
    "\"actions\": [{\"action\": \"...\", \"why\": \"...\", \"urgency\": \"now|this week|monitor\"}], "
    "\"opportunities\": [\"...\"], \"watch_next\": [\"...\"], \"confidence\": \"high|medium|low\"}"
)


def analyst(facts, perspective):
    """Agent 2: Claude if configured, else the rules engine."""
    key = config.settings.anthropic_api_key
    if key:
        try:
            r = session().post("https://api.anthropic.com/v1/messages", timeout=60, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": config.settings.anthropic_model, "max_tokens": 900, "system": ANALYST_SYSTEM,
                      "messages": [{"role": "user", "content": json.dumps(
                          {"perspective": PERSPECTIVES[perspective], "facts": facts}, default=str, ensure_ascii=False)}]})
            r.raise_for_status()
            text = "".join(p.get("text", "") for p in r.json().get("content", []) if p.get("type") == "text")
            return json.loads(text[text.find("{"): text.rfind("}") + 1]), config.settings.anthropic_model
        except Exception as e:
            db.audit("terminal", "analyst_fallback", {"error": str(e)[:200]})
    return rules_analyst(facts, perspective), "rules"


def rules_analyst(f, perspective):
    it, you, linked, ex = f["item"], f["your_business"], f["linked_companies"], f["your_watchlist_in_these_sectors"]
    themes, sectors = set(it["themes"]), ", ".join(it["sectors"]) or ("the UK economy" if it["macro"] else "this sector")
    hi = [c for c in linked if c["band"] in ("High", "Critical")]
    means, exposure, actions, opps, watch = [], [], [], [], []
    summary = f"{it['headline']} ({it['source']})."
    if linked:
        c = linked[0]
        summary += f" {c['company']} is rated {c['band']} risk ({c['probability_12m'] * 100:.1f}% over 12 months)."
    if "insolvency" in themes:
        means.append(f"Financial distress is showing in {sectors}; failures tend to cluster in a supply chain.")
    if "rates" in themes:
        means.append("Higher borrowing costs squeeze companies with heavy or short-term debt first.")
    if "costs" in themes:
        means.append(f"Rising costs put pressure on margins in {sectors}; weaker firms may pay suppliers later.")
    if "jobs" in themes:
        means.append("Job cuts often follow cash pressure; skilled staff may become available.")
    if "demand" in themes:
        means.append(f"Demand is shifting in {sectors}; watch order books and payment times.")
    if "contracts" in themes or "deals" in themes:
        means.append("New contracts or deals can create work further down the supply chain.")
    if not means:
        means.append(f"A development in {sectors}; on its own it is a weak signal, so weigh it with company data.")
    for c in linked:
        exposure.append(f"{c['company']}: {c['band']} risk" + (f" — {c['trigger']}" if c.get("trigger") else
                        (f" — {c['reasons'][0].lower()}" if c["reasons"] else "")) + (" (on your watchlist)" if c["on_your_watchlist"] else ""))
    if ex["count"]:
        exposure.append(f"{ex['count']} companies on your watchlist are in {sectors}; {ex['high_or_critical']} are High or Critical.")
        for w in ex["companies"][:3]:
            exposure.append(f"{w['company']}: {w['band'] or 'not yet scored'}" + (f", owes you £{w['amount_owed_gbp']:,}" if w.get("amount_owed_gbp") else ""))
    elif not linked:
        exposure.append("None of your watchlist companies are linked to this item.")

    if perspective == "customers":
        for c in hi:
            actions.append({"action": c["recommended_action"], "why": f"{c['company']} is {c['band']} risk", "urgency": "now"})
        if ex["high_or_critical"]:
            actions.append({"action": "Review credit terms for your High and Critical customers in this sector",
                            "why": f"{ex['high_or_critical']} of them sit in the affected sector", "urgency": "this week"})
        actions.append({"action": "Chase any overdue invoices from customers in this sector", "why": "Cash goes first to creditors who ask", "urgency": "this week"})
        watch.append("Late payments, new county court judgments and overdue accounts at your customers")
    elif perspective == "contractors":
        for c in hi:
            actions.append({"action": f"Line up an alternative to {c['company']}", "why": f"{c['band']} risk could disrupt supply", "urgency": "now"})
        actions.append({"action": "Avoid large prepayments or deposits to suppliers in this sector", "why": "Prepayments are usually lost in an insolvency", "urgency": "this week"})
        actions.append({"action": "Check retention of title and step-in terms in key contracts", "why": "Protects materials and work in progress", "urgency": "monitor"})
        watch.append("Director resignations, new charges and winding-up petitions at key suppliers")
    elif perspective == "creditors":
        if "rates" in themes or "costs" in themes:
            actions.append({"action": "Check covenant headroom and when your facilities renew", "why": "Lenders reprice and tighten when rates or costs rise", "urgency": "this week"})
        actions.append({"action": "Keep your own filings and payments on time", "why": "Late filing is one of the first signals lenders and suppliers watch", "urgency": "monitor"})
        watch.append("Your lender's or landlord's own sector news and rate decisions")
    elif perspective == "opportunity":
        if "insolvency" in themes or hi:
            opps.append("Customers of a failing competitor may need a new supplier quickly.")
            opps.append("Skilled staff may be released; recruit early.")
            opps.append("Assets and stock are often sold by administrators at a discount.")
            actions.append({"action": "Contact affected customers with a continuity offer", "why": "They need a reliable alternative now", "urgency": "this week"})
        if "contracts" in themes or "deals" in themes:
            opps.append("Winning bidders often need subcontractors and suppliers.")
            actions.append({"action": "Approach the contract winner about supply or subcontract work", "why": "Early approaches win work before it is tendered", "urgency": "this week"})
        if "rates" in themes:
            opps.append("Competitors with heavy debt may cut back, leaving room to win market share.")
        if not opps:
            opps.append("No clear opportunity in this item on its own.")
    else:  # my_company
        if "rates" in themes:
            actions.append({"action": "Review borrowing costs and any facilities due for renewal", "why": "Rate changes flow through to loans and overdrafts", "urgency": "this week"})
        if "costs" in themes:
            actions.append({"action": "Review prices and payment terms to protect margin", "why": "Cost pressure in your sector", "urgency": "this week"})
        if "insolvency" in themes or hi:
            actions.append({"action": "Check what you are owed by companies in this sector", "why": "Distress spreads through unpaid invoices", "urgency": "now" if hi else "this week"})
        if not actions:
            actions.append({"action": "No immediate action; keep monitoring", "why": "The item does not touch your watchlist directly", "urgency": "monitor"})
        watch.append(f"Insolvency statistics and news for {sectors}")
    return {"summary": summary, "what_it_means": means, "your_exposure": exposure, "actions": actions,
            "opportunities": opps, "watch_next": watch, "confidence": "medium" if linked or ex["count"] else "low"}


def reviewer(out, facts):
    """Agent 3: keep it short, factual and labelled."""
    def clean_list(xs, n):
        return [str(x).strip()[:300] for x in (xs or []) if str(x).strip()][:n]
    acts = []
    for a in (out.get("actions") or [])[:6]:
        if isinstance(a, dict) and str(a.get("action", "")).strip():
            u = a.get("urgency") if a.get("urgency") in ("now", "this week", "monitor") else "monitor"
            acts.append({"action": str(a["action"]).strip()[:200], "why": str(a.get("why", "")).strip()[:200], "urgency": u})
    rank = {"now": 0, "this week": 1, "monitor": 2}
    acts.sort(key=lambda a: rank[a["urgency"]])
    conf = out.get("confidence") if out.get("confidence") in ("high", "medium", "low") else "low"
    if not facts["linked_companies"] and not facts["your_watchlist_in_these_sectors"]["count"] and conf == "high":
        conf = "medium"
    return {"summary": str(out.get("summary") or "").strip()[:500], "what_it_means": clean_list(out.get("what_it_means"), 5),
            "your_exposure": clean_list(out.get("your_exposure"), 6), "actions": acts,
            "opportunities": clean_list(out.get("opportunities"), 5), "watch_next": clean_list(out.get("watch_next"), 4),
            "confidence": conf, "disclaimer": DISCLAIMER,
            "sources": [s for s in [facts["item"]["source"]] if s] + [f"Ledgerlight risk model ({c['company']})" for c in facts["linked_companies"]]}


def explain(org, user, item_id, perspective):
    if perspective not in PERSPECTIVES:
        raise ApiError(422, f"perspective must be one of: {', '.join(PERSPECTIVES)}")
    item = _item(org, item_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    cached = db.one("SELECT result, model, created_at FROM terminal_explanations WHERE org_id=? AND item_type=? AND item_id=? "
                    "AND perspective=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                    (org["id"], item["kind"], item_id, perspective, cutoff))
    if cached:
        return db.loads(cached["result"], {}) | {"cached": True, "model": cached["model"], "perspective": perspective}
    ent = plans.entitlements(org)
    cap = ent.get("briefs_per_month")
    used = (db.one("SELECT briefs FROM usage WHERE org_id=? AND month=?", (org["id"], date.today().strftime("%Y-%m"))) or {}).get("briefs") or 0
    if cap is not None and used >= cap:
        raise ApiError(402, "You've used this month's AI explanations. Upgrade for more.")
    facts = research(org, item)
    raw, model = analyst(facts, perspective)
    result = reviewer(raw, facts)
    db.insert("terminal_explanations", {"id": db.new_id("tx_"), "org_id": org["id"], "user_id": user["id"], "item_type": item["kind"],
                                        "item_id": item_id, "perspective": perspective, "result": db.dumps(result),
                                        "model": model, "created_at": db.now()})
    views.bump_usage(org["id"], briefs=1)
    db.audit(user["id"], "terminal_explain", {"org": org["id"], "item": item_id, "perspective": perspective, "model": model})
    return result | {"cached": False, "model": model, "perspective": perspective,
                     "agents": ["Researcher: gathered Ledgerlight facts", f"Analyst: {'Claude' if model != 'rules' else 'rules engine'}",
                                "Reviewer: checked against the facts"]}


# ---------------- routes ----------------

async def terminal_page(request):
    from app.portal import SECURITY_HEADERS, HERE
    return HTMLResponse((HERE / "terminal.html").read_text(encoding="utf-8"), headers=SECURITY_HEADERS)


async def api_feed(request):
    user, org = current(request)
    require_active(org)
    q = request.query_params
    days = min(max(int(q.get("days") or 7) if (q.get("days") or "7").isdigit() else 7, 1), 30)
    sec = q.get("sector")
    if sec and sec != "MACRO" and sec not in insight.SECTION_NAMES:
        raise ApiError(422, "Unknown sector")
    items = feed(org, sector=sec, q=(q.get("q") or "")[:80], watchlist_only=q.get("watchlist") in ("1", "true"), days=days)
    return ok({"items": items, "updated": db.now(), "profile": profile(org)})


async def api_sectors(request):
    user, org = current(request)
    require_active(org)
    return ok({"sectors": sector_monitor(org)})


async def api_movers(request):
    user, org = current(request)
    require_active(org)
    return ok({"movers": movers(org)})


class ExplainIn(BaseModel):
    item_id: str = Field(max_length=80)
    perspective: str = "my_company"


async def api_explain(request):
    user, org = current(request)
    require_active(org)
    s = await body(request, ExplainIn)
    return ok(explain(org, user, s.item_id, s.perspective))


class ProfileIn(BaseModel):
    role: str = "supplier"
    sector: str | None = None
    region: str | None = Field(default=None, max_length=60)
    company_number: str | None = Field(default=None, max_length=10)
    about: str | None = Field(default=None, max_length=300)


async def api_profile(request):
    user, org = current(request)
    if request.method == "GET":
        return ok({"profile": profile(org), "roles": ROLES, "perspectives": PERSPECTIVES,
                   "sectors": {k: v for k, v in insight.SECTION_NAMES.items()}})
    s = await body(request, ProfileIn)
    if s.role not in ROLES:
        raise ApiError(422, "Unknown role")
    if s.sector and s.sector not in insight.SECTION_NAMES:
        raise ApiError(422, "Unknown sector")
    db.execute("UPDATE orgs SET profile=? WHERE id=?", (db.dumps(s.model_dump()), org["id"]))
    db.audit(user["id"], "terminal_profile", {"org": org["id"], "role": s.role, "sector": s.sector})
    return ok({"profile": s.model_dump()})


ROUTES = [
    Route("/terminal", terminal_page),
    Route("/v1/terminal/feed", api_feed),
    Route("/v1/terminal/sectors", api_sectors),
    Route("/v1/terminal/movers", api_movers),
    Route("/v1/terminal/explain", api_explain, methods=["POST"]),
    Route("/v1/terminal/profile", api_profile, methods=["GET", "PUT"]),
]
