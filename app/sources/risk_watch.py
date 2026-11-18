"""Wider-risk factors shown on each brand site.

Seeded from data/risk_factors.json. A weekly job re-fetches each source page and
flags the factor for human review when the page changes. Facts are never
rewritten automatically: a person checks and edits them in the admin endpoints.
"""
import hashlib
import json
import re
from pathlib import Path

from app import db
from app.sources.http import session

SEED = Path(__file__).resolve().parents[2] / "data" / "risk_factors.json"


def seed(path=SEED, overwrite=False):
    items = json.loads(Path(path).read_text())
    n = 0
    for brand, factors in items.items():
        for i, f in enumerate(factors):
            rid = f"{brand}-{i + 1}"
            if db.one("SELECT id FROM risk_factors WHERE id=?", (rid,)) and not overwrite:
                continue
            db.upsert("risk_factors", {"id": rid, "brand": brand, "title": f["t"], "why": f.get("why"), "watch": f.get("watch"),
                                       "fact": f["fact"], "source_url": f.get("src"), "plan_note": f.get("lvl"),
                                       "source_hash": None, "last_checked": None, "needs_review": 0, "updated_at": db.now()}, ["id"])
            n += 1
    return n


def page_fingerprint(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(text.encode()).hexdigest()


def check_sources(fetch=None):
    """Re-fetch every source; mark changed ones for review. Returns ids flagged."""
    fetch = fetch or (lambda url: session().get(url, timeout=30).text)
    flagged = []
    for rf in db.all("SELECT * FROM risk_factors WHERE source_url IS NOT NULL"):
        try:
            fp = page_fingerprint(fetch(rf["source_url"]))
        except Exception as e:
            db.audit("worker", "risk_source_error", {"id": rf["id"], "error": str(e)})
            continue
        changed = rf["source_hash"] is not None and rf["source_hash"] != fp
        db.execute("UPDATE risk_factors SET source_hash=?, last_checked=?, needs_review=? WHERE id=?",
                   (fp, db.now(), 1 if changed or rf["needs_review"] else 0, rf["id"]))
        if changed:
            flagged.append(rf["id"])
    return flagged
