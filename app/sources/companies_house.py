"""Companies House: REST API (enrichment) and Streaming API (change events).

REST:   https://api.company-information.service.gov.uk  (API key as basic-auth user)
Stream: https://stream.companieshouse.gov.uk           (stream key as basic-auth user)
Limit:  600 requests per 5 minutes per application, so we stay under CH_RATE_LIMIT.
"""
import json
import re
from datetime import date, datetime, timedelta

from app import config, db, events
from app.sources.http import RateLimiter, session

API = "https://api.company-information.service.gov.uk"
STREAM = "https://stream.companieshouse.gov.uk"
_limiter = None
_session = None

INSOLVENT_STATUSES = {"liquidation": "cvl", "administration": "administration", "receivership": "receivership",
                      "voluntary-arrangement": "cva", "insolvency-proceedings": "liquidator_appointed"}

REGIONS = {
    "London": "E EC N NW SE SW W WC BR CR DA EN HA IG KT RM SM TW UB",
    "South East": "BN CT GU HP ME MK OX PO RG RH SL SO TN",
    "East of England": "AL CB CM CO IP LU NR PE SG SS",
    "South West": "BA BH BS DT EX GL PL SN SP TA TQ TR",
    "West Midlands": "B CV DY HR ST TF WR WS WV",
    "East Midlands": "DE LE LN NG NN",
    "Yorkshire and The Humber": "BD DN HD HG HU HX LS S WF YO",
    "North West": "BB BL CA CH CW FY L LA M OL PR SK WA WN",
    "North East": "DH DL NE SR TS",
    "Wales": "CF LD LL NP SA SY",
    "Scotland": "AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE",
    "Northern Ireland": "BT",
}
_AREA = {a: r for r, areas in REGIONS.items() for a in areas.split()}


def region_for(postcode):
    m = re.match(r"^\s*([A-Z]{1,2})\d", (postcode or "").upper())
    return _AREA.get(m.group(1)) if m else None


def _client():
    global _limiter, _session
    if _session is None:
        _session = session()
        _limiter = RateLimiter(config.settings.ch_rate_limit, 300)
    return _session, _limiter


def get(path, params=None):
    """GET a Companies House REST resource. Returns dict, or None on 404."""
    key = config.settings.companies_house_api_key
    if not key:
        raise RuntimeError("COMPANIES_HOUSE_API_KEY is not set")
    s, limiter = _client()
    limiter.acquire()
    r = s.get(API + path, params=params, auth=(key, ""), timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code == 429:  # back off and retry once
        import time
        time.sleep(30)
        limiter.acquire()
        r = s.get(API + path, params=params, auth=(key, ""), timeout=30)
    r.raise_for_status()
    return r.json()


def normalise_number(n):
    n = re.sub(r"\s", "", str(n or "")).upper()
    if not n:
        return None
    if n.isdigit():
        return n.zfill(8)
    m = re.match(r"^(SC|NI|OC|SO|NC|R0|LP|SL|NL|FC|GE|IP|SP|IC|SI|NP|NO|RC|SR|ZC)(\d{1,6})$", n)
    return f"{m.group(1)}{m.group(2).zfill(6)}" if m else n


# ---------- mapping profile JSON -> companies row ----------

def profile_to_row(p):
    addr = p.get("registered_office_address") or {}
    acc = p.get("accounts") or {}
    cs = p.get("confirmation_statement") or {}
    postcode = addr.get("postal_code")
    return {
        "number": normalise_number(p.get("company_number")),
        "name": p.get("company_name") or "",
        "status": p.get("company_status"),
        "company_type": p.get("type"),
        "incorporated_on": p.get("date_of_creation"),
        "dissolved_on": p.get("date_of_cessation"),
        "sic_codes": ",".join(p.get("sic_codes") or []),
        "address_line": ", ".join(x for x in (addr.get("address_line_1"), addr.get("address_line_2")) if x),
        "town": addr.get("locality"),
        "postcode": postcode,
        "region": region_for(postcode),
        "accounts_next_due": acc.get("next_due"),
        "accounts_overdue": int(bool(acc.get("overdue"))),
        "accounts_type": (acc.get("last_accounts") or {}).get("type"),
        "last_accounts_to": (acc.get("last_accounts") or {}).get("made_up_to"),
        "cs_next_due": cs.get("next_due"),
        "cs_overdue": int(bool(cs.get("overdue"))),
        "subscriber_type": "corporate" if (p.get("type") or "").startswith(("ltd", "plc", "llp", "private", "public")) else "check",
        "updated_at": db.now(),
    }


def upsert_company(row):
    existing = db.one("SELECT * FROM companies WHERE number=?", (row["number"],))
    if existing:
        sets = {k: v for k, v in row.items() if v is not None and k != "number"}
        if sets:
            db.execute(f"UPDATE companies SET {', '.join(f'{k}=?' for k in sets)} WHERE number=?",
                       [*sets.values(), row["number"]])
    else:
        db.insert("companies", row)
    return existing


def changes_to_events(before, after, source_id, published_at):
    """Compare an old and new company row; record the events the change implies."""
    out = []
    num, name = after["number"], after["name"]
    if before is None:
        inc = after.get("incorporated_on")
        if inc and inc >= (date.today() - timedelta(days=14)).isoformat():
            out.append(events.record("ch_stream", f"{source_id}:inc", "incorporated", num, name, published_at, inc))
        return [e for e in out if e]
    if after.get("status") != before.get("status"):
        t = INSOLVENT_STATUSES.get(after.get("status"))
        if t:
            out.append(events.record("ch_stream", f"{source_id}:status", t, num, name, published_at))
        if after.get("status") == "dissolved":
            out.append(events.record("ch_stream", f"{source_id}:dissolved", "dissolved", num, name, published_at))
    if after.get("accounts_overdue") and not before.get("accounts_overdue"):
        out.append(events.record("ch_stream", f"{source_id}:acc", "accounts_overdue", num, name, published_at,
                                 data={"accounts_next_due": after.get("accounts_next_due")}))
    if after.get("cs_overdue") and not before.get("cs_overdue"):
        out.append(events.record("ch_stream", f"{source_id}:cs", "cs_overdue", num, name, published_at))
    if after.get("postcode") and before.get("postcode") and after["postcode"] != before["postcode"]:
        out.append(events.record("ch_stream", f"{source_id}:ro", "ro_change", num, name, published_at,
                                 data={"from": before["postcode"], "to": after["postcode"]}))
    return [e for e in out if e]


# ---------- Streaming API ----------

def handle_stream_line(stream, line):
    """Process one newline-delimited JSON message from a CH stream. Returns event ids created."""
    if not line or not line.strip():
        return []  # heartbeat
    msg = json.loads(line)
    ev = msg.get("event") or {}
    tp = ev.get("timepoint")
    published = ev.get("published_at") or db.now()
    data = msg.get("data") or {}
    kind = msg.get("resource_kind", "")
    created = []
    if kind == "company-profile":
        row = profile_to_row(data)
        if row["number"]:
            before = upsert_company(row)
            created = changes_to_events(before, row, f"{row['number']}:{tp}", published)
    elif kind.startswith("company-charges"):
        num = _num_from_uri(msg.get("resource_uri"))
        if num and data.get("status") == "outstanding" and ev.get("type") != "deleted":
            lender = ", ".join(p.get("name", "") for p in data.get("persons_entitled") or [])
            created = [events.record("ch_stream", f"charge:{msg.get('resource_id')}", "new_charge", num, None, published,
                                     data.get("created_on"), {"lender": lender})]
    elif kind.startswith("company-officers"):
        num = _num_from_uri(msg.get("resource_uri"))
        if num and data.get("resigned_on"):
            created = [events.record("ch_stream", f"officer:{msg.get('resource_id')}:res", "director_resigned", num, None,
                                     published, data.get("resigned_on"), {"role": data.get("officer_role")})]
    elif kind.startswith("company-psc"):
        num = _num_from_uri(msg.get("resource_uri"))
        if num:
            created = [events.record("ch_stream", f"psc:{msg.get('resource_id')}:{tp}", "psc_change", num, None, published)]
    elif kind == "company-insolvency":
        num = _num_from_uri(msg.get("resource_uri"))
        for case in data.get("cases") or []:
            t = {"compulsory-liquidation": "compulsory_liquidation", "creditors-voluntary-liquidation": "cvl",
                 "administration-order": "administration", "in-administration": "administration",
                 "corporate-voluntary-arrangement": "cva", "receivership": "receivership",
                 "moratorium": "moratorium"}.get(case.get("type"))
            if t and num:
                prac = (case.get("practitioners") or [{}])[0]
                created.append(events.record("ch_stream", f"ins:{num}:{case.get('number')}", t, num, None, published, None,
                                             {"ip_name": prac.get("name")}))
    if tp is not None:
        db.set_state(f"ch_stream_timepoint:{stream}", tp)
    return [c for c in created if c]


def _num_from_uri(uri):
    m = re.search(r"/company/([A-Z0-9]+)", uri or "")
    return normalise_number(m.group(1)) if m else None


def run_stream(stream="companies", stop=None):  # pragma: no cover - long-running network loop
    """Consume a CH stream forever, resuming from the last saved timepoint."""
    key = config.settings.companies_house_stream_key
    if not key:
        raise RuntimeError("COMPANIES_HOUSE_STREAM_KEY is not set")
    s = session()
    while not (stop and stop.is_set()):
        params = {}
        tp = db.get_state(f"ch_stream_timepoint:{stream}")
        if tp:
            params["timepoint"] = int(tp) + 1
        try:
            with s.get(f"{STREAM}/{stream}", params=params, auth=(key, ""), stream=True, timeout=(10, 90)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if stop and stop.is_set():
                        return
                    try:
                        handle_stream_line(stream, line)
                    except Exception as e:  # skip a bad message rather than reconnecting onto it forever
                        db.audit("worker", "ch_stream_bad_message", {"stream": stream, "error": str(e)[:200], "line": (line or "")[:300]})
                        m = re.search(r'"timepoint"\s*:\s*(\d+)', line or "")
                        if m:
                            db.set_state(f"ch_stream_timepoint:{stream}", m.group(1))
        except Exception as e:  # reconnect after network errors
            db.audit("worker", "ch_stream_error", {"stream": stream, "error": str(e)})
            import time
            time.sleep(10)


# ---------- Enrichment ----------

def enrich(number, max_age_hours=24):
    """Fetch profile, officers, PSCs and charges for a company and store them."""
    number = normalise_number(number)
    row = db.one("SELECT enriched_at FROM companies WHERE number=?", (number,))
    if row and row["enriched_at"]:
        age = datetime.fromisoformat(db.now()) - datetime.fromisoformat(row["enriched_at"])
        if age < timedelta(hours=max_age_hours):
            return False
    prof = get(f"/company/{number}")
    if not prof:
        return False
    data = profile_to_row(prof)
    officers = (get(f"/company/{number}/officers", {"items_per_page": 100}) or {}).get("items", [])
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    directors = [{"name": o.get("name"), "role": o.get("officer_role"), "appointed_on": o.get("appointed_on"),
                  "resigned_on": o.get("resigned_on"),
                  "key": ((o.get("links") or {}).get("officer") or {}).get("appointments", "").split("/")[2]
                  if ((o.get("links") or {}).get("officer") or {}).get("appointments") else None}
                 for o in officers if "director" in (o.get("officer_role") or "") or o.get("officer_role") == "llp-member"]
    data["directors"] = db.dumps(directors)
    data["recent_resignations"] = sum(1 for d in directors if (d.get("resigned_on") or "") >= year_ago)
    pscs = (get(f"/company/{number}/persons-with-significant-control") or {}).get("items", [])
    data["pscs"] = db.dumps([{"name": p.get("name"), "natures": p.get("natures_of_control"), "ceased_on": p.get("ceased_on")} for p in pscs])
    charges = (get(f"/company/{number}/charges") or {}).get("items", [])
    outstanding = [c for c in charges if c.get("status") == "outstanding"]
    data["charges_outstanding"] = len(outstanding)
    if outstanding:
        latest = max(outstanding, key=lambda c: c.get("created_on") or "")
        data["latest_lender"] = ", ".join(p.get("name", "") for p in latest.get("persons_entitled") or [])
        data["latest_charge_date"] = latest.get("created_on")
    data["enriched_at"] = db.now()
    upsert_company(data)
    # officer index, used by the director-network / phoenix checks
    db.execute("DELETE FROM officers WHERE company_number=?", (number,))
    for d in directors:
        db.insert("officers", {"id": db.new_id("off_"), "company_number": number, "officer_key": d.get("key"),
                               "name": d.get("name"), "role": d.get("role"), "appointed_on": d.get("appointed_on"),
                               "resigned_on": d.get("resigned_on")})
    return True


def officer_appointments(officer_key):
    """All companies an officer has been appointed to (for the network graph)."""
    items = (get(f"/officers/{officer_key}/appointments", {"items_per_page": 50}) or {}).get("items", [])
    return [{"company_number": normalise_number((i.get("appointed_to") or {}).get("company_number")),
             "company_name": (i.get("appointed_to") or {}).get("company_name"),
             "company_status": (i.get("appointed_to") or {}).get("company_status"),
             "appointed_on": i.get("appointed_on"), "resigned_on": i.get("resigned_on")} for i in items]


def search(name):
    return (get("/search/companies", {"q": name, "items_per_page": 5}) or {}).get("items", [])
