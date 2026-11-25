"""Profit playbooks: specific ways each industry turns Ledgerlight signals into money.

Content lives in data/playbooks.json (one source for the portal, the terminal and the nine websites).
Playbooks are guidance content, not customer data, but the API route still needs a signed-in user,
because the API is in-house only.
"""
import json
import re
from pathlib import Path

from starlette.routing import Route

from app.web import ApiError, current, ok

DATA = json.loads((Path(__file__).resolve().parent.parent / "data" / "playbooks.json").read_text(encoding="utf-8"))
BRANDS = DATA["brands"]
FEATURES = DATA["features"]
PLAYS = {p["id"]: dict(p, brand=b) for b, v in BRANDS.items() for p in v["plays"]}

# Words in a terminal item that point at a play's signals (whole-word stems).
_KEYWORDS = {
    "petition": ["petition", "winding"], "administration": ["administration", "administrator", "administrators"],
    "liquidation": ["liquidation", "liquidator", "cvl", "insolvency", "insolvent", "collapse", "collapses", "bust"],
    "charge": ["charge", "lender", "loan", "debt", "refinanc"], "accounts": ["accounts", "filing", "overdue"],
    "judgment": ["ccj", "judgment", "judgments"], "incorporation": ["incorporat", "startup", "start-up", "new company"],
    "sponsor": ["sponsor", "visa", "immigration", "migrant"], "estate": ["estate", "probate", "inheritance", "deceased"],
    "land": ["land", "property", "title", "dissolved"], "growth": ["raises", "funding", "deal", "contract", "growth", "expands"],
    "director": ["director", "resign", "resigns"],
}


def _concepts(text):
    text = (text or "").lower()
    return {c for c, words in _KEYWORDS.items()
            if any(re.search(r"\b" + re.escape(w), text) for w in words)}


def for_brand(brand):
    """The org's own plays first; everything else grouped by industry. The master brand sees all."""
    own = BRANDS.get(brand)
    others = [{"brand": b, "name": v["name"], "industry": v["industry"], "plays": v["plays"]}
              for b, v in BRANDS.items() if b != brand]
    return {"brand": brand, "name": own["name"] if own else "Ledgerlight",
            "industry": own["industry"] if own else "All industries",
            "plays": own["plays"] if own else [], "other": others, "features": FEATURES}


def suggest(brand, item, limit=2):
    """Plays that fit a terminal item, from the org's own brand (all brands for the master brand)."""
    pool = [p for p in PLAYS.values() if brand not in BRANDS or p["brand"] == brand]
    text = " ".join([item.get("headline") or "", item.get("event_type") or "", " ".join(item.get("themes") or [])])
    want = _concepts(text)
    scored = []
    for i, p in enumerate(pool):
        have = _concepts(" ".join(p["signals"]) + " " + p["title"])
        scored.append((len(want & have), -i, p))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    picks = [s[2] for s in scored if s[0] > 0][:limit] or [pool[0]] if pool else []
    return [{"id": p["id"], "title": p["title"], "brand": p["brand"]} for p in picks]


async def api_playbooks(request):
    user, org = current(request)
    play = request.query_params.get("id")
    if play:
        if play not in PLAYS:
            raise ApiError(404, "Playbook not found")
        return ok({"play": PLAYS[play], "features": FEATURES})
    return ok(for_brand(org["brand"]))


ROUTES = [Route("/v1/playbooks", api_playbooks)]
