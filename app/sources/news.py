"""News monitoring for watched companies via the GDELT DOC 2.0 API (free, keyless, ~15-minute refresh).

Rules (Company Insight design): news only nudges the score (max 2 units); it raises a "look now" flag
instead; every mention keeps its source link; names of private individuals are never stored - only
the company number, headline, link and the matched warning terms.
"""
import re
from datetime import datetime

from app import db
from app.sources.http import RateLimiter, session

API = "https://api.gdeltproject.org/api/v2/doc/doc"
LIMIT = RateLimiter(1, window=5)  # GDELT asks for about one request every 5 seconds
NEGATIVE = ["administration", "administrators", "liquidation", "liquidators", "insolvent", "insolvency",
            "winding-up", "winding up", "collapse", "collapsed", "redundancies", "job cuts", "profit warning",
            "lost contract", "unpaid", "owed", "ceased trading", "stops trading", "rescue deal", "debt", "lawsuit",
            "fraud", "investigation", "fined", "recall", "closure", "closes", "bankrupt"]
_SUFFIX = re.compile(r"\b(LIMITED|LTD|PLC|LLP|L\.L\.P\.|COMPANY|CO|HOLDINGS|GROUP|UK|\(UK\)|THE)\b\.?", re.I)


def search_name(name):
    """A name precise enough to search: suffixes removed; too-generic names (one short word) skipped."""
    core = re.sub(r"[^\w&' -]", " ", _SUFFIX.sub(" ", name or "")).strip()
    core = re.sub(r"\s+", " ", core)
    words = [w for w in core.split() if len(w) > 1]
    if len(words) < 2 and (not words or len(words[0]) < 7):
        return None
    return core


def flag_terms(title):
    t = (title or "").lower()
    return [w for w in NEGATIVE if re.search(r"\b" + re.escape(w) + r"\b", t)]


def _when(seendate):
    try:
        return datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return db.now()


def store(company_number, articles):
    """Save new articles for a company. Returns how many were new."""
    new = 0
    for a in articles or []:
        url = a.get("url")
        if not url or not url.startswith(("http://", "https://")):
            continue
        if db.one("SELECT id FROM news_mentions WHERE company_number=? AND url=?", (company_number, url)):
            continue
        terms = flag_terms(a.get("title"))
        db.insert("news_mentions", {"id": db.new_id("nw_"), "company_number": company_number, "url": url[:1000],
                                    "title": (a.get("title") or "")[:300], "domain": (a.get("domain") or "")[:200],
                                    "published_at": _when(a.get("seendate")), "flagged": 1 if terms else 0,
                                    "matched_terms": ",".join(terms), "created_at": db.now()})
        new += 1
    return new


def fetch(name, timespan="7d", http=None):
    q = search_name(name)
    if not q:
        return []
    LIMIT.acquire()
    r = (http or session()).get(API, params={"query": f'"{q}" sourcecountry:UK', "mode": "artlist", "format": "json",
                                             "maxrecords": 50, "timespan": timespan, "sort": "datedesc"}, timeout=30)
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        return []
    return r.json().get("articles", [])


def poll_watched(limit=40, http=None):
    """Check news for watched companies, oldest-checked first. Returns new mentions stored."""
    rows = db.all("SELECT DISTINCT i.company_number, c.name FROM watchlist_items i JOIN companies c ON c.number=i.company_number")
    rows.sort(key=lambda r: db.get_state(f"news_checked:{r['company_number']}") or "")
    total = 0
    for r in rows[:limit]:
        try:
            total += store(r["company_number"], fetch(r["name"], http=http))
        finally:
            db.set_state(f"news_checked:{r['company_number']}", db.now())
    return total
