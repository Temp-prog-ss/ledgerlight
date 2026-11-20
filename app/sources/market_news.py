"""Market news wire for the terminal: UK sector news from GDELT, tagged and linked to tracked companies.

Each run takes the next few sectors in rotation (GDELT asks for about one request every 5 seconds), stores
new headlines once, tags them with themes (rates, insolvency, costs...) and tone, and links them to companies
Ledgerlight tracks (watchlists and companies with recent events) by exact company-name match in the headline.
Only headlines, links and company numbers are stored - never names of private individuals.
"""
import json
import re

from app import db
from app.sources import news
from app.sources.http import session

API = news.API

SECTOR_QUERIES = {
    "F": "(construction OR housebuilder OR contractor OR \"building firm\")",
    "G": "(retailer OR retail OR wholesaler OR \"high street\")",
    "I": "(hospitality OR restaurant OR pub OR hotel)",
    "C": "(manufacturer OR manufacturing OR factory)",
    "H": "(haulage OR logistics OR freight OR airline)",
    "J": "(tech OR software OR telecoms OR startup)",
    "L": "(\"property developer\" OR landlord OR \"commercial property\")",
    "K": "(bank OR lender OR insurer OR \"private equity\")",
    "N": "(\"recruitment firm\" OR outsourcing OR \"staffing agency\")",
    "Q": "(\"care home\" OR \"care provider\" OR healthcare)",
    "MACRO": "(\"Bank of England\" OR \"interest rates\" OR inflation OR \"business rates\" OR insolvencies OR \"national insurance\")",
}
THEMES = {
    "rates": ["interest rate", "bank rate", "bank of england", "mortgage rates", "borrowing costs", "base rate"],
    "insolvency": ["administration", "administrators", "liquidation", "liquidators", "insolvenc*", "winding up", "winding-up",
                   "collapse*", "bust", "ceased trading", "rescue deal", "restructuring"],
    "costs": ["energy", "wage*", "national insurance", "business rates", "inflation", "prices", "costs", "tariff*"],
    "jobs": ["redundanc*", "job cuts", "layoffs", "jobs at risk", "hiring", "recruit*"],
    "demand": ["sales fell", "sales rise", "demand", "orders", "footfall", "spending", "profit warning", "trading update"],
    "deals": ["acquire*", "acquisition", "takeover", "merger", "buys", "sold to", "investment", "funding round", "raises £*"],
    "regulation": ["regulator", "fined", "investigation", "new rules", "legislation", "banned", "licence"],
    "contracts": ["contract", "contracts", "tender*", "awarded", "framework"],
}
POSITIVE = ["record profit", "profits rise", "wins contract", "awarded", "growth", "expands", "hiring", "raises", "funding",
            "sales rise", "upgrade", "beats"]


def _rx(words):
    """Whole-word patterns; a trailing * allows any ending (insolvenc* -> insolvency, insolvencies)."""
    return re.compile("|".join(r"\b" + re.escape(w[:-1]) + r"\S*" if w.endswith("*") else r"\b" + re.escape(w) + r"\b"
                               for w in words), re.I)


_THEME_RX = {k: _rx(v) for k, v in THEMES.items()}
_POS_RX = _rx(POSITIVE)


def tag(title):
    t = title or ""
    themes = [k for k, rx in _THEME_RX.items() if rx.search(t)]
    neg = news.flag_terms(title)
    pos = _POS_RX.findall(t)
    tone = "negative" if neg and not pos else "positive" if pos and not neg else "mixed" if neg and pos else "neutral"
    return themes, tone


def tracked_names():
    """Exact-match index of tracked companies: watchlists plus companies with events in the last 90 days."""
    rows = db.all("SELECT DISTINCT c.number, c.name FROM companies c WHERE c.number IN "
                  "(SELECT company_number FROM watchlist_items) OR c.number IN "
                  "(SELECT company_number FROM events WHERE COALESCE(published_at, created_at) >= ?)",
                  (_days_ago(90),))
    idx = {}
    for r in rows:
        n = news.search_name(r["name"])
        if n:
            idx[n.upper()] = r["number"]
    return idx


def _days_ago(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def link_companies(title, idx):
    t = " " + re.sub(r"[^A-Z0-9&' ]", " ", (title or "").upper()) + " "
    return sorted({num for name, num in idx.items() if f" {name} " in t})


def store(articles, sector, idx=None):
    idx = tracked_names() if idx is None else idx
    new = 0
    for a in articles or []:
        url, title = a.get("url"), (a.get("title") or "").strip()
        if not url or not url.startswith(("http://", "https://")) or not title:
            continue
        existing = db.one("SELECT id, sectors FROM market_news WHERE url=?", (url,))
        if existing:
            secs = set(json.loads(existing["sectors"] or "[]"))
            if sector not in secs:
                secs.add(sector)
                db.execute("UPDATE market_news SET sectors=? WHERE id=?", (json.dumps(sorted(secs)), existing["id"]))
            continue
        themes, tone = tag(title)
        db.insert("market_news", {"id": db.new_id("mn_"), "url": url[:1000], "title": title[:300],
                                  "domain": (a.get("domain") or "")[:200], "published_at": news._when(a.get("seendate")),
                                  "sectors": json.dumps([sector]), "themes": json.dumps(themes), "tone": tone,
                                  "companies": json.dumps(link_companies(title, idx)), "source": "gdelt",
                                  "created_at": db.now()})
        new += 1
    return new


def fetch(sector, timespan="6h", http=None):
    news.LIMIT.acquire()
    r = (http or session()).get(API, params={"query": f"{SECTOR_QUERIES[sector]} sourcecountry:UK sourcelang:english",
                                             "mode": "artlist", "format": "json", "maxrecords": 75,
                                             "timespan": timespan, "sort": "datedesc"}, timeout=30)
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        return []
    return r.json().get("articles", [])


def poll(per_run=4, http=None):
    """Fetch the next `per_run` sectors in rotation. Returns new headlines stored."""
    order = list(SECTOR_QUERIES)
    start = int(db.get_state("market_news_cursor") or 0) % len(order)
    idx = tracked_names()
    total = 0
    for i in range(per_run):
        sec = order[(start + i) % len(order)]
        try:
            total += store(fetch(sec, http=http), sec, idx)
        except Exception as e:
            db.set_state("market_news_last_error", f"{db.now()} {sec} {str(e)[:150]}")
    db.set_state("market_news_cursor", str((start + per_run) % len(order)))
    return total
