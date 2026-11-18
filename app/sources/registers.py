"""Other public registers: Home Office sponsors, HMLR company-owned land,
the unclaimed estates (bona vacantia) list, and county court judgments."""
import csv
import hashlib
import io
import re

from app import config, db, events
from app.sources.companies_house import normalise_number
from app.sources.http import session

SPONSOR_PAGE = "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers"


def norm_name(name):
    n = (name or "").upper().replace("&", " AND ")
    n = re.sub(r"\b(LIMITED|LTD|PLC|LLP|THE|T/A.*)\b", " ", n)
    return re.sub(r"[^A-Z0-9]+", " ", n).strip()


# ---------------- Home Office register of licensed sponsors ----------------

def sponsor_csv_url():  # pragma: no cover - network
    r = session().get(SPONSOR_PAGE, timeout=30)
    r.raise_for_status()
    blob = str(r.json())
    m = re.search(r"https://assets\.publishing\.service\.gov\.uk/[^\"' ]+\.csv", blob)
    if not m:
        raise RuntimeError("Sponsor register CSV link not found")
    return m.group(0)


def ingest_sponsor_csv(text, today=None):
    """Diff today's register against what we hold. Returns created event ids."""
    today = today or db.now()[:10]
    seen, created = set(), []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for row in reader:
        name = row.get("Organisation Name") or row.get("Organisation name") or ""
        town = row.get("Town/City") or ""
        route = row.get("Route") or ""
        rating = row.get("Type & Rating") or ""
        key = hashlib.sha1(f"{norm_name(name)}|{town.upper()}|{route.upper()}".encode()).hexdigest()[:20]
        seen.add(key)
        old = db.one("SELECT * FROM sponsor_register WHERE key=?", (key,))
        number = (old or {}).get("company_number") or _match_company(name, town)
        if old is None:
            db.insert("sponsor_register", {"key": key, "org_name": name, "town": town, "county": row.get("County"),
                                           "rating": rating, "route": route, "company_number": number,
                                           "first_seen": today, "last_seen": today, "removed_at": None})
            if db.get_state("sponsor_baseline_done"):
                created.append(events.record("home_office", f"add:{key}:{today}", "sponsor_added", number, name,
                                             data={"route": route, "rating": rating, "town": town}))
        else:
            db.execute("UPDATE sponsor_register SET last_seen=?, removed_at=NULL, rating=?, company_number=? WHERE key=?",
                       (today, rating, number, key))
            if old["rating"] != rating:
                created.append(events.record("home_office", f"rating:{key}:{today}", "sponsor_rating_change", number, name,
                                             data={"from": old["rating"], "to": rating, "route": route, "town": town}))
        if number:
            _tag_company_as_sponsor(number, {"route": route, "rating": rating, "town": town,
                                             "first_seen": (old or {}).get("first_seen") or today})
    if db.get_state("sponsor_baseline_done"):
        for old in db.all("SELECT * FROM sponsor_register WHERE removed_at IS NULL AND last_seen < ?", (today,)):
            db.execute("UPDATE sponsor_register SET removed_at=? WHERE key=?", (today, old["key"]))
            created.append(events.record("home_office", f"rm:{old['key']}:{today}", "sponsor_removed",
                                         old["company_number"], old["org_name"],
                                         data={"route": old["route"], "rating": old["rating"], "town": old["town"]}))
    db.set_state("sponsor_baseline_done", 1)
    return [c for c in created if c]


def _match_company(name, town):
    n = norm_name(name)
    if not n:
        return None
    cands = db.all("SELECT number, name, town FROM companies WHERE UPPER(name) LIKE ? LIMIT 20", (f"%{name.split()[0].upper()}%",))
    for c in cands:
        if norm_name(c["name"]) == n and (not town or (c["town"] or "").upper() == town.upper() or not c["town"]):
            return c["number"]
    return None


def _tag_company_as_sponsor(number, info):
    if db.one("SELECT number FROM companies WHERE number=?", (number,)):
        db.execute("UPDATE companies SET sponsor=? WHERE number=?", (db.dumps(info), number))


def poll_sponsors():  # pragma: no cover - network
    r = session().get(sponsor_csv_url(), timeout=120)
    r.raise_for_status()
    return ingest_sponsor_csv(r.content.decode("utf-8-sig", "replace"))


# ---------------- HM Land Registry: UK companies that own property (CCOD) ----------------

def ingest_ccod(path_or_text):
    """Load the CCOD CSV (download needs a free HMLR account) and flag land in dissolved companies.

    Check the CCOD licence terms before resale: we expose counts and title numbers only to paid plans.
    """
    text = open(path_or_text, encoding="utf-8-sig").read() if not path_or_text.lstrip().startswith("Title") else path_or_text
    owned = {}
    for row in csv.DictReader(io.StringIO(text)):
        for i in (1, 2, 3, 4):
            num = normalise_number(row.get(f"Company Registration No. ({i})"))
            if num:
                owned.setdefault(num, []).append(row.get("Title Number"))
    created = []
    for num, titles in owned.items():
        co = db.one("SELECT number, status, name FROM companies WHERE number=?", (num,))
        if not co:
            continue
        db.execute("UPDATE companies SET titles_owned=?, title_numbers=? WHERE number=?",
                   (len(titles), db.dumps(titles[:50]), num))
        if co["status"] == "dissolved":
            created.append(events.record("hmlr", f"dissolved-land:{num}", "land_owned_by_dissolved", num, co["name"],
                                         data={"titles": len(titles)}))
    return [c for c in created if c]


# ---------------- Unclaimed estates (Government Legal Department, updated daily) ----------------
# The CSV is attached to this GOV.UK page; its file URL changes with each update, so we find it on the page.
UNCLAIMED_PAGE = "https://www.gov.uk/government/statistical-data-sets/unclaimed-estates-list"


def unclaimed_csv_url(html):
    links = re.findall(r"https://assets\.publishing\.service\.gov\.uk/[^\"'\s<>]+?\.csv", html)
    if not links:
        raise RuntimeError("Unclaimed estates CSV link not found on the GOV.UK page")
    named = [l for l in links if "unclaimed" in l.lower()]
    return (named or links)[0]


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _pick(row_norm, *wanted):
    for w in wanted:
        for k, v in row_norm.items():
            if w in k and (v or "").strip():
                return v.strip()
    return None


def ingest_unclaimed_estates(text, today=None):
    """Compare today's list with what we hold. The first load is a baseline (no alerts for
    thousands of historic estates); after that each newly listed estate becomes an event.
    Only vetted EstateSignal customers ever see these."""
    today = today or db.now()[:10]
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = [h for h in (reader.fieldnames or []) if h]
    db.set_state("unclaimed_estates_headers", "|".join(headers)[:500])
    baseline = not db.get_state("unclaimed_baseline_done")
    created, seen = [], set()
    for row in reader:
        rn = {_norm(k): (v or "") for k, v in row.items() if k}
        if not any(v.strip() for v in rn.values()):
            continue
        ref = _pick(rn, "bvreference", "reference", "refno", "ref")
        rid = ("ref:" + ref) if ref else hashlib.sha1("|".join(f"{k}={rn[k]}" for k in sorted(rn)).encode()).hexdigest()[:20]
        seen.add(rid)
        if db.one("SELECT id FROM unclaimed_estates WHERE id=?", (rid,)):
            db.execute("UPDATE unclaimed_estates SET last_seen=?, removed_at=NULL WHERE id=?", (today, rid))
            continue
        db.insert("unclaimed_estates", {"id": rid, "data": db.dumps(row), "first_seen": today, "last_seen": today, "removed_at": None})
        if baseline:
            continue
        created.append(events.record("bona_vacantia", rid, "unclaimed_estate", None, "Unclaimed estate", data={
            "place_of_death": _pick(rn, "placeofdeath", "deathplace", "place"),
            "date_of_death": _pick(rn, "dateofdeath", "deathdate", "dod"),
            "date_listed": _pick(rn, "dateofpublication", "publicationdate", "dateadvertised", "dateofadvert"),
            "bv_reference": ref, "url": UNCLAIMED_PAGE}, personal_data=True))
    if seen:  # anything no longer listed has been claimed or withdrawn
        db.execute("UPDATE unclaimed_estates SET removed_at=? WHERE removed_at IS NULL AND last_seen<?", (today, today))
        db.set_state("unclaimed_baseline_done", 1)
    return [c for c in created if c]


# ---------------- County court judgments (licensed provider) ----------------

def ingest_ccj_rows(rows):
    """Rows from a licensed judgment provider (e.g. Registry Trust): company_number, judgment_id, date, amount, court."""
    created = []
    for r in rows:
        num = normalise_number(r.get("company_number"))
        if not num:
            continue
        eid = events.record("ccj", r.get("judgment_id"), "ccj", num, r.get("company_name"), effective_date=r.get("date"),
                            data={"amount_gbp": r.get("amount"), "court": r.get("court")})
        if eid:
            created.append(eid)
            db.execute("UPDATE companies SET ccj_count_12m=COALESCE(ccj_count_12m,0)+1 WHERE number=?", (num,))
    return created


def poll_ccjs():  # pragma: no cover - needs a commercial licence
    s = config.settings
    if not s.ccj_provider_url:
        return []
    r = session().get(s.ccj_provider_url, headers={"Authorization": f"Bearer {s.ccj_provider_key}"},
                      params={"since": db.get_state("ccj_since", "")}, timeout=60)
    r.raise_for_status()
    out = ingest_ccj_rows(r.json().get("judgments", []))
    db.set_state("ccj_since", db.now()[:10])
    return out
