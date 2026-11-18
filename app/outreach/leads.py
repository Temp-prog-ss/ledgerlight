"""Leads: import (same columns as the spreadsheet in the video), PECR checks, pipeline stages.

PECR (UK): you may email *corporate subscribers* (limited companies, LLPs, public bodies)
without prior consent, provided you identify yourself and offer an opt-out. Sole traders
and partnerships are *individual subscribers* and need consent (or the soft opt-in).
So by default we block personal-mailbox domains (gmail.com etc.) and anything flagged
as an individual subscriber, and we never email suppressed addresses or domains.
"""
import csv
import io
import re

from app import db
from app.pipeline import compliance

STAGES = ["new", "contacted", "replied", "interested", "trial", "customer", "not_interested", "bounced", "unsubscribed", "blocked"]
CLOSED = {"replied", "interested", "trial", "customer", "not_interested", "bounced", "unsubscribed", "blocked"}
EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "live.co.uk", "yahoo.com",
            "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
            "protonmail.com", "proton.me", "gmx.com", "mail.com", "ntlworld.com", "blueyonder.co.uk"}

# column name variants -> field (matches "Name, Position, Email, Website, Date Found" from the video, and more)
HEADERS = {
    "email": ("email", "emailaddress", "workemail", "e-mail"),
    "name": ("name", "fullname", "contactname", "contact"),
    "first_name": ("firstname", "first", "forename"),
    "last_name": ("lastname", "last", "surname"),
    "position": ("position", "title", "jobtitle", "role"),
    "company": ("company", "companyname", "organisation", "organization", "business"),
    "website": ("website", "url", "domain", "site", "web"),
    "date_found": ("datefound", "date", "added", "dateadded"),
    "company_number": ("companynumber", "companyno", "crn", "registrationnumber"),
    "notes": ("notes", "note", "comments"),
    "brand": ("brand", "product"),
}


def _key(h):
    return re.sub(r"[^a-z]", "", (h or "").lower())


def test_recipients():
    """Your own addresses for test sends (OUTREACH_TEST_RECIPIENTS, comma-separated). You consent to email yourself."""
    import os
    return {e.strip().lower() for e in os.environ.get("OUTREACH_TEST_RECIPIENTS", "").split(",") if e.strip()}


def classify(email, company_number=None):
    """Return (subscriber_type, block_reason or None)."""
    domain = email.split("@")[-1].lower()
    if email.lower() in test_recipients() and not compliance.suppressed("email", email):
        return "consented", None
    if compliance.suppressed("email", email):
        return "unknown", "suppressed address"
    if db.one("SELECT id FROM suppressions WHERE kind='email' AND value=?", ("@" + domain,)):
        return "unknown", "suppressed domain"
    if domain in FREEMAIL:
        return "individual", "personal mailbox: needs consent under PECR"
    if company_number:
        co = db.one("SELECT company_type, status FROM companies WHERE number=?", (company_number,))
        if co and co.get("status") in ("dissolved", "liquidation", "administration"):
            return "corporate", f"company is {co['status']}"
        if co and compliance.subscriber_type(co) == "individual":
            return "individual", "sole trader or partnership: needs consent under PECR"
    return "corporate", None


def upsert_lead(row, source="import", brand=None):
    """Create or update one lead. Returns (lead_id, created, block_reason)."""
    email = (row.get("email") or "").strip().lower()
    if not EMAIL_RX.match(email):
        return None, False, "invalid email"
    first, last = row.get("first_name"), row.get("last_name")
    if not first and row.get("name"):
        parts = row["name"].strip().split()
        first, last = parts[0], " ".join(parts[1:]) or None
    num = None
    if row.get("company_number"):
        from app.sources.companies_house import normalise_number
        num = normalise_number(row["company_number"])
    stype, reason = classify(email, num)
    existing = db.one("SELECT * FROM outreach_leads WHERE email=?", (email,))
    fields = {"first_name": first, "last_name": last, "position": row.get("position"), "company": row.get("company"),
              "website": row.get("website"), "company_number": num, "brand": row.get("brand") or brand,
              "date_found": row.get("date_found"), "notes": row.get("notes"), "subscriber_type": stype}
    if existing:
        sets = {k: v for k, v in fields.items() if v}
        if reason and existing["stage"] not in CLOSED:
            sets.update(stage="blocked", block_reason=reason)
        sets["updated_at"] = db.now()
        db.execute(f"UPDATE outreach_leads SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), existing["id"]))
        return existing["id"], False, reason
    lid = db.new_id("ld_")
    db.insert("outreach_leads", {"id": lid, "email": email, **fields, "source": source,
                                 "stage": "blocked" if reason else "new", "block_reason": reason,
                                 "last_contacted_at": None, "replied_at": None, "created_at": db.now(), "updated_at": db.now()})
    return lid, True, reason


def import_csv(text, brand=None, source="csv"):
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    colmap = {}
    for h in reader.fieldnames or []:
        k = _key(h)
        for field, names in HEADERS.items():
            if k in names and field not in colmap.values():
                colmap[h] = field
                break
    if "email" not in colmap.values():
        raise ValueError("No Email column found. Expected columns like: Name, Position, Email, Website, Date Found")
    added = updated = 0
    blocked, invalid = [], 0
    for raw in reader:
        row = {colmap[h]: (v or "").strip() for h, v in raw.items() if h in colmap}
        lid, created, reason = upsert_lead(row, source, brand)
        if lid is None:
            invalid += 1
            continue
        added += created
        updated += not created
        if reason:
            blocked.append({"email": row.get("email"), "reason": reason})
    return {"added": added, "updated": updated, "invalid": invalid, "blocked": len(blocked), "blocked_examples": blocked[:20]}


def set_stage(lead_id, stage, note=None, actor="staff"):
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    db.execute("UPDATE outreach_leads SET stage=?, updated_at=? WHERE id=?", (stage, db.now(), lead_id))
    if stage in CLOSED:
        db.execute("UPDATE outreach_enrollments SET status=? WHERE lead_id=? AND status='active'",
                   ("replied" if stage in ("replied", "interested", "trial", "customer", "not_interested") else stage, lead_id))
    log(lead_id, "stage", {"stage": stage, "note": note, "by": actor})


def log(lead_id, kind, detail=None):
    db.insert("outreach_activity", {"id": db.new_id("act_"), "lead_id": lead_id, "kind": kind, "detail": db.dumps(detail or {}), "at": db.now()})


def unsubscribe(lead_id, how="link"):
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (lead_id,))
    if not lead:
        return False
    compliance.suppress("email", lead["email"], f"outreach unsubscribe ({how})")
    set_stage(lead_id, "unsubscribed", how, actor="recipient")
    return True


def export_rows():
    return db.all("SELECT first_name, last_name, position, company, email, website, company_number, brand, stage, block_reason, "
                  "date_found, last_contacted_at, replied_at, notes FROM outreach_leads ORDER BY created_at")


# Which kinds of company buy each product (used by the prospect finder).
PROSPECT_SIC = {
    "sitesentinel": ("43", "4120", "4211", "4221", "4222", "4299", "7112", "7732"),
    "sponsorwatch": ("6910",),
    "estatesignal": ("6910", "6831", "8299"),
    "distressradar": ("6920", "6910", "7022"),
    "tenantguard": ("6820", "6831", "6832"),
    "talentrelease": ("7810", "7820", "7830"),
    "creditpulse": ("46", "8291", "6491", "6499"),
    "filingpulse": ("6920",),
}


def prospects(brand, region=None, limit=500):
    """Companies in our database that look like buyers of `brand` (no emails: research them before importing)."""
    pre = PROSPECT_SIC.get(brand, ())
    rows = db.all("SELECT number, name, sic_codes, town, postcode, region, status FROM companies WHERE status='active'" +
                  (" AND region=?" if region else "") + " LIMIT 20000", (region,) if region else ())
    out = []
    for r in rows:
        codes = [c.strip() for c in (r["sic_codes"] or "").split(",") if c.strip()]
        if any(c.startswith(pre) for c in codes) and not compliance.suppressed("company", r["number"]):
            out.append(r)
            if len(out) >= limit:
                break
    return out
