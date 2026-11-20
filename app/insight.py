"""Company Insight: a 12-month failure probability per company, with reasons, bands and protocols.

Method (see the "Ledgerlight Company Insight" design doc):
  * Prior   = annual insolvency rate for the company's SIC section (base_rates table, Insolvency
              Service statistics), adjusted for company age.
  * Evidence = behavioural / event signals from data Ledgerlight already holds (late filing,
              director exits, creditor action, charges, ownership changes, licences, news). Each adds
              log-odds; recent events count more than old ones (half-life decay).
  * Output  = probability, band, top reasons (plain words), trend vs last month, peer percentile.
  * Formal insolvency events are "triggers" and override the model.
The starting weights are priors. `fit()` replaces them with weights fitted on Ledgerlight's own labelled
monthly snapshots once there are enough failures (discrete-time hazard model, Shumway 2001).
"""
import math
from datetime import date, datetime, timedelta

from app import db
from app.pipeline import compliance
from app.pipeline.score import network

MODEL_VERSION = "v1-priors"
OVERALL_RATE = 0.005          # 49.9 per 10,000 companies a year (Insolvency Service, 12 months to Sept 2026)
MIN_POSITIVES_TO_FIT = 200    # labelled failures needed before fitted weights replace the priors

# Starting sector multipliers on the overall rate. Priors only: refresh from official sector statistics
# with `set_base_rate` (admin) - construction, wholesale/retail and hospitality lead UK insolvencies.
SECTOR_MULTIPLIER = {
    "F": 1.8,   # construction
    "I": 2.0,   # accommodation and food
    "G": 1.2,   # wholesale and retail
    "N": 1.3,   # administrative and support
    "C": 1.0,   # manufacturing
    "H": 1.1,   # transport and storage
    "J": 0.9,   # information and communication
    "M": 0.7,   # professional, scientific, technical
    "L": 0.6,   # real estate
    "K": 0.5,   # financial
    "Q": 0.6,   # health
    "P": 0.7,   # education
    "R": 1.3,   # arts and recreation
    "S": 1.0,   # other services
}

# SIC 2007 division (first two digits) -> section letter
_SECTIONS = [(1, 3, "A"), (5, 9, "B"), (10, 33, "C"), (35, 35, "D"), (36, 39, "E"), (41, 43, "F"), (45, 47, "G"),
             (49, 53, "H"), (55, 56, "I"), (58, 63, "J"), (64, 66, "K"), (68, 68, "L"), (69, 75, "M"), (77, 82, "N"),
             (84, 84, "O"), (85, 85, "P"), (86, 88, "Q"), (90, 93, "R"), (94, 96, "S"), (97, 98, "T"), (99, 99, "U")]
SECTION_NAMES = {"A": "Agriculture", "B": "Mining", "C": "Manufacturing", "D": "Energy", "E": "Water and waste",
                 "F": "Construction", "G": "Wholesale and retail", "H": "Transport and storage",
                 "I": "Accommodation and food", "J": "Information and communication", "K": "Finance and insurance",
                 "L": "Real estate", "M": "Professional services", "N": "Administrative and support",
                 "O": "Public administration", "P": "Education", "Q": "Health and social work",
                 "R": "Arts and recreation", "S": "Other services", "T": "Households", "U": "Extraterritorial"}

# feature -> (log-odds weight per unit, cap on units, plain-words reason). Positive = more risk.
WEIGHTS = {
    "accounts_overdue":        (1.2, 1, "Accounts are overdue at Companies House"),
    "accounts_first_late":     (0.4, 1, "First late filing after a record of filing on time"),
    "cs_overdue":              (0.6, 1, "Confirmation statement is overdue"),
    "ccj_12m":                 (0.5, 3, "County court judgment(s) in the last 12 months"),
    "resignations_90d":        (0.45, 3, "Director resignations in the last 90 days"),
    "director_failures":       (0.4, 3, "Directors linked to companies that failed"),
    "phoenix":                 (0.8, 1, "Directors run a new company in the same trade (phoenix pattern)"),
    "new_charge_under_stress": (0.5, 1, "New secured borrowing while other warning signs are present"),
    "charge_satisfied_180d":   (-0.3, 1, "A lender charge was recently paid off"),
    "psc_change_90d":          (0.3, 1, "Change of owner or controller in the last 90 days"),
    "ro_change_90d":           (0.25, 1, "Registered office moved in the last 90 days"),
    "sponsor_lost_12m":        (0.9, 1, "Home Office sponsor licence removed or downgraded"),
    "strike_off_proposed":     (1.0, 1, "Proposal to strike the company off the register"),
    "negative_net_assets":     (0.9, 1, "Negative net assets in the latest accounts"),
    "net_assets_fall":         (0.5, 1, "Net assets fell by more than 30%"),
    "negative_news_30d":       (0.3, 2, "Negative news coverage in the last 30 days"),
    "established_on_time":     (-0.3, 1, "Established company with a record of filing on time"),
}

# Formal insolvency: override the model (probability floor, label)
TRIGGERS = {
    "administration": (0.95, "In administration"),
    "compulsory_liquidation": (0.99, "Wound up by the court"),
    "cvl": (0.99, "In creditors' voluntary liquidation"),
    "liquidator_appointed": (0.99, "Liquidator appointed"),
    "receivership": (0.9, "Receiver appointed"),
    "winding_up_petition": (0.35, "Winding-up petition presented"),
    "moratorium": (0.4, "Under a statutory moratorium"),
    "cva": (0.3, "Company voluntary arrangement"),
    "restructuring_plan": (0.3, "Restructuring plan"),
}
FAILURE_TYPES = {"administration", "compulsory_liquidation", "cvl", "liquidator_appointed", "receivership"}

BANDS = [(0.01, "Low"), (0.03, "Watch"), (0.08, "Elevated"), (0.20, "High"), (1.01, "Critical")]
BAND_REVIEW = {"Low": "Quarterly", "Watch": "Monthly", "Elevated": "Fortnightly", "High": "Weekly", "Critical": "Same day"}

# Decision protocols: default (suppliers / trade credit), then per product
PROTOCOL = {
    "default": {
        "Low": "Normal terms.",
        "Watch": "No new credit-limit increases without a fresh check.",
        "Elevated": "Shorten payment terms, cap exposure at its current level and ask for updated accounts.",
        "High": "Take a deposit or pro-forma on new orders; a second person signs off any extension; chase overdue invoices now.",
        "Critical": "Stop further credit, secure retention of title, and contact your solicitor or collection partner.",
    },
    "tenantguard": {
        "Low": "Normal.", "Watch": "Normal; diary the next rent review.",
        "Elevated": "Check the rent deposit and guarantor.", "High": "Check deposit and guarantor; speak to your agent.",
        "Critical": "Speak to your agent now about arrears, forfeiture or claims before other creditors.",
    },
    "distressradar": {
        "Low": "No action.", "Watch": "No action.", "Elevated": "Add to your prospect list.",
        "High": "Add to your prospect list and research the directors' advisers.",
        "Critical": "Make contact now: this is the opportunity window.",
    },
    "talentrelease": {
        "Low": "No action.", "Watch": "No action.", "Elevated": "Map key staff.", "High": "Map key staff and roles.",
        "Critical": "Approach staff who may be released.",
    },
    "filingpulse": {
        "Low": "Routine.", "Watch": "Routine.", "Elevated": "Offer a filing or compliance call.",
        "High": "Offer a filing or compliance call this week.", "Critical": "Refer to a restructuring partner.",
    },
    "sponsorwatch": {
        "Low": "No action.", "Watch": "No action.", "Elevated": "Flag sponsored workers who may be at risk.",
        "High": "Flag sponsored workers who may be at risk.", "Critical": "Contact workers whose sponsor is failing.",
    },
}
RULES = [
    "Start from the sector base rate before the company's own story.",
    "Score filing, creditors, people and financing separately, then form the overall view.",
    "Any decision that increases exposure to a High or Critical company needs a second reviewer.",
    "Before a large new order, write one line on how this customer could fail to pay (pre-mortem).",
    "Record the decision and, later, the outcome so the model and protocol keep learning.",
]


# ---------------- helpers ----------------

def sic_section(sic_codes):
    for s in (sic_codes or "").split(","):
        s = s.strip()
        if len(s) >= 2 and s[:2].isdigit():
            div = int(s[:2])
            for lo, hi, letter in _SECTIONS:
                if lo <= div <= hi:
                    return letter
    return None


def base_rate(section):
    row = db.one("SELECT annual_rate FROM base_rates WHERE sic_section=?", (section,)) if section else None
    if row:
        return float(row["annual_rate"])
    return OVERALL_RATE * SECTOR_MULTIPLIER.get(section or "", 1.0)


def set_base_rate(section, rate, source):
    db.upsert("base_rates", {"sic_section": section, "annual_rate": float(rate), "source": source, "updated_at": db.now()},
              ["sic_section"])


def age_multiplier(incorporated_on, today=None):
    today = today or date.today()
    try:
        years = (today - date.fromisoformat((incorporated_on or "")[:10])).days / 365.25
    except ValueError:
        return 1.0
    return 1.6 if years < 2 else 1.2 if years < 5 else 0.8


def _logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + math.exp(-max(min(x, 40), -40)))


def band_for(p, triggered=False):
    if triggered:
        return "Critical"
    for limit, name in BANDS:
        if p < limit:
            return name
    return "Critical"


def _weights():
    fitted = db.loads(db.get_state("insight_weights"), None)
    if fitted and fitted.get("weights"):
        merged = dict(WEIGHTS)
        for k, w in fitted["weights"].items():
            if k in merged:
                merged[k] = (w, merged[k][1], merged[k][2])
        return merged, fitted.get("version", "v2-fitted")
    return WEIGHTS, MODEL_VERSION


# ---------------- features ----------------

def _events(number, since):
    """Company-level events only: personal-data notices and personal insolvency are never used."""
    return db.all("SELECT event_type, published_at, effective_date, created_at FROM events WHERE company_number=? "
                  "AND COALESCE(personal_data,0)=0 AND COALESCE(published_at, created_at)>=? ORDER BY published_at",
                  (number, since))


def features(number, today=None):
    today = today or date.today()
    c = db.one("SELECT * FROM companies WHERE number=?", (number,))
    if not c:
        return None
    d = lambda days: (today - timedelta(days=days)).isoformat()
    ev = _events(number, d(400))

    def count(types, days):
        cut = d(days)
        return sum(1 for e in ev if e["event_type"] in types and (e["published_at"] or e["created_at"] or "")[:10] >= cut)

    net = network(number)
    f = {
        "accounts_overdue": 1 if c.get("accounts_overdue") else 0,
        "cs_overdue": 1 if c.get("cs_overdue") else 0,
        "ccj_12m": int(c.get("ccj_count_12m") or 0) or count({"ccj"}, 365),
        "resignations_90d": count({"director_resigned"}, 90),
        "director_failures": int(net.get("prior_insolvencies") or 0),
        "phoenix": 1 if net.get("phoenix_flag") else 0,
        "charge_satisfied_180d": count({"charge_satisfied"}, 180) and 1,
        "psc_change_90d": 1 if count({"psc_change"}, 90) else 0,
        "ro_change_90d": 1 if count({"ro_change"}, 90) else 0,
        "sponsor_lost_12m": 1 if count({"sponsor_removed", "sponsor_rating_change"}, 365) else 0,
        "strike_off_proposed": 1 if count({"strike_off_proposed"}, 365) else 0,
        "negative_net_assets": 1 if (c.get("net_assets") is not None and c["net_assets"] < 0) else 0,
        "net_assets_fall": 1 if (c.get("net_assets") and c.get("net_assets_prev") and c["net_assets"] >= 0
                                 and c["net_assets"] < 0.7 * c["net_assets_prev"]) else 0,
        "negative_news_30d": (db.one("SELECT COUNT(*) AS n FROM news_mentions WHERE company_number=? AND flagged=1 AND published_at>=?",
                                     (number, d(30))) or {}).get("n", 0) or 0,
    }
    stress = f["accounts_overdue"] or f["ccj_12m"] or f["resignations_90d"] >= 2 or f["cs_overdue"]
    f["new_charge_under_stress"] = 1 if (stress and count({"new_charge"}, 180)) else 0
    # first late filing after a clean record: overdue now, no accounts_overdue event before the last 120 days
    f["accounts_first_late"] = 1 if (f["accounts_overdue"] and not any(
        e["event_type"] == "accounts_overdue" and (e["published_at"] or "")[:10] < d(120) for e in ev)) else 0
    f["established_on_time"] = 1 if (age_multiplier(c.get("incorporated_on"), today) < 1 and not f["accounts_overdue"]
                                     and not f["cs_overdue"]) else 0
    trig = [e["event_type"] for e in ev if e["event_type"] in TRIGGERS]
    return {"company": c, "features": f, "triggers": trig, "network": net}


# ---------------- scoring ----------------

def assess(number, today=None):
    """Full assessment for one company, or None if we don't hold it."""
    today = today or date.today()
    fx = features(number, today)
    if not fx:
        return None
    c, f = fx["company"], fx["features"]
    section = sic_section(c.get("sic_codes"))
    prior = min(0.5, base_rate(section) * age_multiplier(c.get("incorporated_on"), today))
    weights, version = _weights()
    logit, contrib = _logit(prior), []
    for k, (w, cap, why) in weights.items():
        units = min(int(f.get(k) or 0), cap)
        if units:
            logit += w * units
            contrib.append({"feature": k, "effect": round(w * units, 2), "reason": why,
                            "detail": f"{units}" if cap > 1 else None})
    p = _sigmoid(logit)
    trigger = None
    if fx["triggers"]:
        top = max(fx["triggers"], key=lambda t: TRIGGERS[t][0])
        floor, label = TRIGGERS[top]
        p = max(p, floor)
        trigger = {"event_type": top, "label": label}
    status = (c.get("status") or "").lower()
    if status in ("liquidation", "administration", "receivership", "insolvency-proceedings"):
        p, trigger = max(p, 0.95), trigger or {"event_type": status, "label": f"Company status: {status}"}
    band = band_for(p, trigger is not None)
    up = sorted([x for x in contrib if x["effect"] > 0], key=lambda x: -x["effect"])
    down = sorted([x for x in contrib if x["effect"] < 0], key=lambda x: x["effect"])
    return {
        "company_number": c["number"], "company_name": c["name"], "status": c.get("status"),
        "sic_section": section, "sector": SECTION_NAMES.get(section, "Unclassified"),
        "probability": round(p, 4), "band": band, "review": BAND_REVIEW[band],
        "sector_base_rate": round(base_rate(section), 4), "prior": round(prior, 4),
        "trigger": trigger, "reasons": up[:3], "other_reasons": up[3:], "reducing": down,
        "features": f, "model_version": version,
    }


def protocol(band, brand):
    table = PROTOCOL.get(brand) or PROTOCOL["default"]
    return {"band": band, "action": table[band], "review": BAND_REVIEW[band], "rules": RULES,
            "two_person_rule": band in ("High", "Critical")}


def month_key(today=None):
    return (today or date.today()).strftime("%Y-%m")


def trend(number, current_p, today=None):
    prev_month = ((today or date.today()).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    row = db.one("SELECT probability FROM insight_snapshots WHERE company_number=? AND month=?", (number, prev_month))
    if not row or row["probability"] is None:
        return {"direction": "new", "previous": None}
    prev = float(row["probability"])
    if current_p > prev * 1.25 and current_p - prev > 0.002:
        d = "rising"
    elif current_p < prev * 0.8 and prev - current_p > 0.002:
        d = "falling"
    else:
        d = "steady"
    return {"direction": d, "previous": round(prev, 4)}


def peers(section, p, today=None):
    if not section:
        return {"count": 0, "percentile": None}
    rows = db.all("SELECT probability FROM insight_snapshots WHERE month=? AND sic_section=? AND probability IS NOT NULL",
                  (month_key(today), section))
    if len(rows) < 20:
        return {"count": len(rows), "percentile": None}
    below = sum(1 for r in rows if float(r["probability"]) < p)
    return {"count": len(rows), "percentile": round(100 * below / len(rows))}


# ---------------- snapshots, labels, fitting (worker jobs) ----------------

def snapshot_population(today=None):
    """Companies worth tracking: on any watchlist, or with an event in the last 13 months."""
    since = ((today or date.today()) - timedelta(days=400)).isoformat()
    rows = db.all("SELECT DISTINCT company_number AS n FROM watchlist_items UNION "
                  "SELECT DISTINCT company_number AS n FROM events WHERE company_number IS NOT NULL AND COALESCE(personal_data,0)=0 "
                  "AND COALESCE(published_at, created_at)>=?", (since,))
    return [r["n"] for r in rows if r["n"]]


def snapshot(today=None, limit=5000):
    """Write (or refresh) this month's snapshot for every tracked company. Returns the number written."""
    today = today or date.today()
    m, n = month_key(today), 0
    for number in snapshot_population(today)[:limit]:
        a = assess(number, today)
        if not a:
            continue
        db.upsert("insight_snapshots", {"company_number": number, "month": m, "sic_section": a["sic_section"],
                                        "features": db.dumps(a["features"]), "probability": a["probability"],
                                        "band": a["band"], "model_version": a["model_version"], "created_at": db.now()},
                  ["company_number", "month"], update=["sic_section", "features", "probability", "band", "model_version"])
        n += 1
    return n


def label(today=None):
    """Label snapshots: failed=1 if a formal insolvency event followed within 12 months; 0 once 12 months pass."""
    today = today or date.today()
    n = 0
    for s in db.all("SELECT company_number, month FROM insight_snapshots WHERE label_failed IS NULL"):
        start = s["month"] + "-01"
        end = (date.fromisoformat(start) + timedelta(days=366)).isoformat()
        types = tuple(FAILURE_TYPES)
        hit = db.one(f"SELECT 1 AS x FROM events WHERE company_number=? AND event_type IN ({','.join('?' * len(types))}) "
                     f"AND COALESCE(published_at, created_at)>=? AND COALESCE(published_at, created_at)<?",
                     (s["company_number"], *types, start, end))
        if hit:
            val = 1
        elif today.isoformat() >= end:
            val = 0
        else:
            continue
        db.execute("UPDATE insight_snapshots SET label_failed=?, labelled_at=? WHERE company_number=? AND month=?",
                   (val, db.now(), s["company_number"], s["month"]))
        n += 1
    return n


def fit(min_positives=MIN_POSITIVES_TO_FIT, epochs=300, lr=0.1, l2=0.01):
    """Discrete-time hazard model: logistic regression on labelled monthly snapshots.

    Keeps the sector/age prior as a fixed offset and learns the feature weights. Does nothing until
    there are enough failures; the priors stay in force until then."""
    rows = db.all("SELECT s.features, s.sic_section, s.label_failed, c.incorporated_on FROM insight_snapshots s "
                  "LEFT JOIN companies c ON c.number=s.company_number WHERE s.label_failed IS NOT NULL")
    pos = sum(1 for r in rows if r["label_failed"] == 1)
    if pos < min_positives:
        return {"fitted": False, "positives": pos, "needed": min_positives, "rows": len(rows)}
    keys = list(WEIGHTS)
    data = []
    for r in rows:
        f = db.loads(r["features"], {})
        x = [min(int(f.get(k) or 0), WEIGHTS[k][1]) for k in keys]
        offset = _logit(min(0.5, base_rate(r["sic_section"]) * age_multiplier(r["incorporated_on"])))
        data.append((x, offset, r["label_failed"]))
    w = [WEIGHTS[k][0] for k in keys]
    for _ in range(epochs):
        grad = [0.0] * len(keys)
        for x, off, y in data:
            err = _sigmoid(off + sum(wi * xi for wi, xi in zip(w, x))) - y
            for i, xi in enumerate(x):
                if xi:
                    grad[i] += err * xi
        w = [wi - lr * (g / len(data) + l2 * wi) for wi, g in zip(w, grad)]
    version = f"v2-fitted-{date.today().isoformat()}"
    db.set_state("insight_weights", db.dumps({"weights": dict(zip(keys, [round(v, 4) for v in w])), "version": version,
                                              "positives": pos, "rows": len(rows)}))
    return {"fitted": True, "version": version, "positives": pos, "rows": len(rows)}


def calibration():
    """Per band: labelled snapshots, actual failure rate vs average predicted; plus AUC."""
    rows = db.all("SELECT band, probability, label_failed FROM insight_snapshots WHERE label_failed IS NOT NULL")
    out = []
    for _, name in BANDS:
        b = [r for r in rows if r["band"] == name]
        out.append({"band": name, "labelled": len(b),
                    "predicted": round(sum(float(r["probability"]) for r in b) / len(b), 4) if b else None,
                    "actual": round(sum(r["label_failed"] for r in b) / len(b), 4) if b else None})
    return {"bands": out, "auc": auc([(float(r["probability"]), r["label_failed"]) for r in rows]), "labelled": len(rows),
            "failures": sum(r["label_failed"] for r in rows), "model": _weights()[1]}


def auc(pairs):
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return None
    ranked = sorted(pairs, key=lambda t: t[0])
    rank_sum, i = 0.0, 0
    while i < len(ranked):  # average ranks for ties
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        rank_sum += avg * sum(1 for k in range(i, j + 1) if ranked[k][1] == 1)
        i = j + 1
    return round((rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)), 4)


def available(number):
    """Objections apply here too: a suppressed company gets no insight."""
    c = db.one("SELECT name FROM companies WHERE number=?", (number,))
    return not (compliance.suppressed("company", number) or compliance.suppressed("company_name", (c or {}).get("name")))

