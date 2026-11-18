"""The Gazette: insolvency, strike-off and deceased-estate notices.

The Gazette publishes notice feeds (Atom/JSON) under the Open Government Licence.
Personal data inside notices is NOT covered by that licence, so we keep only what
each product needs (see app/pipeline/compliance.py).

Notice codes below are the ones we route on. Check them against The Gazette's
notice-code list before launch; the title-keyword classifier is a fallback in
case a code is missing or changes.
"""
import re
from datetime import date, timedelta

from app import config, db, events
from app.sources.companies_house import normalise_number
from app.sources.http import session

FEED_URL = "https://www.thegazette.co.uk/all-notices/notice/data.json"

NOTICE_CODES = {
    "2410": "administration",          # Appointment of Administrators
    "2450": "winding_up_petition",     # Petitions to Wind Up (Companies)
    "2452": "compulsory_liquidation",  # Winding-Up Orders
    "2431": "cvl",                     # Resolutions for Winding-up
    "2432": "liquidator_appointed",    # Appointment of Liquidators
    "2903": "deceased_estate",         # Deceased Estates
}
TITLE_RULES = [
    (r"petitions? to wind up|winding.up petition", "winding_up_petition"),
    (r"winding.up orders?", "compulsory_liquidation"),
    (r"appointment of administrators?|administration", "administration"),
    (r"resolutions? for (voluntary )?winding.up|creditors.? voluntary", "cvl"),
    (r"appointment of liquidators?", "liquidator_appointed"),
    (r"company voluntary arrangement", "cva"),
    (r"restructuring plan", "restructuring_plan"),
    (r"first gazette notice|proposal to strike off|compulsory strike.off", "strike_off_proposed"),
    (r"deceased estates?", "deceased_estate"),
]

RX = {
    "company_number": r"(?:Company|Registered)\s*(?:Number|No\.?)\s*[:\-]?\s*([A-Z]{0,2}\s?\d{5,8})",
    "court": r"(High Court of Justice[^.,;]*|County Court at [A-Z][A-Za-z ]+|Court of Session)",
    "case_number": r"\b((?:CR|BR)-\d{4}-\d{3,6}|No\.?\s*\d{2,6} of \d{4})",
    "hearing_date": r"(?:will be heard|hearing)[^.]*?(\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4})",
    "petitioner": r"presented[^.]*?by\s+(?:the\s+)?([A-Z][A-Za-z&.,' ]{2,80}?)(?:,| of | \(|\.)",
    "claim_deadline": r"(?:on or before|by no later than|by)\s+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4})",
    "ip_phone": r"(?:Tel(?:ephone)?|Phone)[^0-9+]{0,10}(\+?44\s?\d[\d ]{8,12}|0\d[\d ]{8,12})",
    "ip_email": r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    "ip_name": r"(?:Office Holder|Administrator|Liquidator|Insolvency Practitioner)s?\s*(?:Details)?\s*[:\-]?\s*([A-Z][a-zA-Z.'\- ]{3,60}?)\s*\(IP No",
    "ip_firm": r"\(IP No\.?\s*\d+\)[^A-Z]{0,20}(?:of\s+)?([A-Z][A-Za-z&.,' ]{3,80}?(?:LLP|Ltd|Limited|Partners|& Co))",
    "executor_firm": r"(?:[Ss]olicitors?|[Ee]xecutors?|[Cc]laims (?:to|should be sent to)|c/o|[Cc]are of)\s+(?:the\s+)?([A-Z][A-Za-z&' ]{2,60}?(?:LLP|Solicitors|Ltd|Limited))",
}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}


def to_iso(text):
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def classify(code, title):
    if code and str(code) in NOTICE_CODES:
        return NOTICE_CODES[str(code)]
    t = (title or "").lower()
    for rx, kind in TITLE_RULES:
        if re.search(rx, t):
            return kind
    return None


def extract(text):
    """Pull structured facts out of a notice's text."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    out = {}
    for key, rx in RX.items():
        m = re.search(rx, text, flags=re.I if key in ("court", "hearing_date", "claim_deadline", "ip_phone") else 0)
        if m:
            out[key] = m.group(1).strip(" ,.")
    if out.get("company_number"):
        out["company_number"] = normalise_number(out["company_number"])
    for k in ("hearing_date", "claim_deadline"):
        if out.get(k):
            out[k] = to_iso(out[k]) or out[k]
    pc = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?\d[A-Z]{2}\b", text)
    if pc:
        out["postcode_district"] = pc.group(1)
    return out


def _entries(payload):
    """Accept the feed as Atom-style JSON ({'entry': [...]}) or a plain list."""
    if isinstance(payload, list):
        return payload
    feed = payload.get("feed", payload)
    entries = feed.get("entry") or feed.get("entries") or []
    return entries if isinstance(entries, list) else [entries]


def _val(entry, *keys):
    for k in keys:
        for ek, ev in entry.items():
            if ek == k or ek.endswith(":" + k) or ek.endswith(k):
                if isinstance(ev, dict):
                    return ev.get("#text") or ev.get("value") or ev.get("href") or ev.get("$")
                return ev
    return None


def parse_entry(entry):
    nid = str(_val(entry, "notice-id", "id") or "").rstrip("/").split("/")[-1]
    title = _val(entry, "title") or ""
    if isinstance(title, dict):
        title = title.get("#text", "")
    code = _val(entry, "notice-code", "noticecode", "category-code")
    content = _val(entry, "content", "summary") or ""
    if isinstance(content, dict):
        content = content.get("#text", "")
    kind = classify(code, title + " " + content[:200])
    facts = extract(content)
    url = f"https://www.thegazette.co.uk/notice/{nid}" if nid.isdigit() else _val(entry, "link")
    return {"notice_id": nid, "title": title, "code": code, "kind": kind, "published": _val(entry, "published", "updated"),
            "url": url, **facts}


def ingest_entries(entries):
    created = []
    for e in entries:
        n = parse_entry(e)
        if not n["kind"] or not n["notice_id"]:
            continue
        personal = n["kind"] == "deceased_estate"
        data = {k: n.get(k) for k in ("url", "code", "court", "case_number", "hearing_date", "petitioner", "claim_deadline",
                                      "ip_name", "ip_firm", "ip_phone", "ip_email") if n.get(k)}
        if personal:  # keep the minimum: no names, no full address
            data = {k: n.get(k) for k in ("url", "claim_deadline", "executor_firm", "postcode_district") if n.get(k)}
        title = "Deceased estate notice" if personal else n["title"]
        eid = events.record("gazette", n["notice_id"], n["kind"], n.get("company_number"), title,
                            n.get("published"), n.get("hearing_date"), data, personal_data=personal)
        if eid:
            created.append(eid)
    return created


def poll(pages=3):  # pragma: no cover - network
    """Fetch the latest notices (newest first) and ingest the new ones."""
    s = session()
    since = db.get_state("gazette_since") or (date.today() - timedelta(days=2)).isoformat()
    created = []
    for page in range(1, pages + 1):
        r = s.get(FEED_URL, params={"results-page": page, "results-page-size": 100, "start-publish-date": since,
                                    "sort-by": "latest-date"}, timeout=60, headers={"Accept": "application/json"})
        r.raise_for_status()
        entries = _entries(r.json())
        if not entries:
            break
        created += ingest_entries(entries)
    db.set_state("gazette_since", (date.today() - timedelta(days=1)).isoformat())
    return created
