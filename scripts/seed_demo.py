"""Load sample companies and notices, run the pipeline, and create one demo
customer per brand. Prints logins so you can click around every dashboard.

    python -m scripts.seed_demo
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brands, db, security  # noqa: E402
from app.pipeline import run  # noqa: E402
from app.sources import gazette, registers, risk_watch  # noqa: E402

COMPANIES = [
    ("07654321", "HARBOURLINE BUILD LTD", "41201", "Manchester", "M4 6BF", 120, 48_000_000, -250_000, None),
    ("09876543", "OAKMERE GROUNDWORKS LTD", "43120", "Leeds", "LS1 4DL", 45, 6_200_000, 120_000, None),
    ("01112223", "NORTHGATE CARE HOMES LTD", "87300", "Leeds", "LS2 7EA", 85, 4_100_000, -40_000, {"route": "Skilled Worker", "rating": "A", "town": "Leeds"}),
    ("03334445", "VANTOR ENGINEERING LTD", "25620", "Sheffield", "S1 2HE", 120, 11_000_000, 300_000, None),
    ("05556667", "BRIGHTWELL RETAIL LTD", "47190", "Bristol", "BS1 6QA", 60, 7_500_000, -80_000, None),
]
NOTICES = [
    ("D1", "2410", "Appointment of Administrators", "HARBOURLINE BUILD LIMITED Company Number: 07654321. Office Holder Details: Jane Smith (IP No. 12345) of Harrow & Vale Recovery LLP. Tel: 0161 555 0123. Claims on or before 20 January 2027."),
    ("D2", "2450", "Petitions to Wind Up (Companies)", "In the High Court of Justice CR-2026-004512. OAKMERE GROUNDWORKS LIMITED (Company Number 09876543). Petition presented by HM Revenue and Customs, of Enforcement. The Petition will be heard on 9 December 2026."),
    ("D3", "2431", "Resolutions for Winding-up", "NORTHGATE CARE HOMES LIMITED Company Number: 01112223. Liquidator: Crestmoor Insolvency LLP."),
    ("D4", "2410", "Appointment of Administrators", "VANTOR ENGINEERING LIMITED Company Number: 03334445. Office Holder Details: Raj Patel (IP No. 22222) of Northbridge Restructuring LLP."),
    ("D5", "2452", "Winding-Up Orders", "BRIGHTWELL RETAIL LIMITED Company Number: 05556667. Winding up order made."),
    ("D6", "2903", "Deceased Estates", "Claims to Hollis & Grant LLP solicitors by 20 January 2027. BS1 6QA"),
]


def main():
    db.init_db()
    risk_watch.seed()
    for num, name, sic, town, pc, staff, turn, na, sponsor in COMPANIES:
        if not db.one("SELECT number FROM companies WHERE number=?", (num,)):
            from app.sources.companies_house import region_for
            db.insert("companies", {"number": num, "name": name, "status": "active", "company_type": "ltd", "sic_codes": sic,
                                    "town": town, "postcode": pc, "region": region_for(pc), "employees": staff, "turnover": turn,
                                    "net_assets": na, "accounts_overdue": int(na < 0), "sponsor": json.dumps(sponsor) if sponsor else None,
                                    "subscriber_type": "corporate", "incorporated_on": "2011-05-01", "updated_at": db.now()})
    logins = []
    for bid in brands.BRANDS:
        email = f"demo+{bid}@ledgerlight.example"
        if db.one("SELECT id FROM users WHERE email=?", (email,)):
            continue
        oid = db.new_id("org_")
        db.insert("orgs", {"id": oid, "name": f"Demo {brands.BRANDS[bid].name}", "brand": bid, "plan": "pro", "billing_cycle": "annual",
                           "status": "active", "trial_ends_at": None, "stripe_customer_id": None, "stripe_subscription_id": None,
                           "vetted": 1, "regions": "[]", "timezone": "Europe/London", "digest_hour": 7, "last_digest_at": None, "created_at": db.now()})
        db.insert("users", {"id": db.new_id("usr_"), "org_id": oid, "email": email, "password_hash": security.hash_password("demo-password-123"),
                            "name": "Demo", "role": "owner", "created_at": db.now(), "last_login": None})
        ch = db.new_id("chn_")
        db.insert("channels", {"id": ch, "org_id": oid, "kind": "email", "target": email, "config": "{}", "active": 1, "created_at": db.now()})
        db.insert("alert_rules", {"id": db.new_id("rul_"), "org_id": oid, "name": "All", "event_types": "[]", "regions": "[]", "sic_prefixes": "[]",
                                  "keywords": "[]", "min_score": 0, "watchlist_only": 0, "instant": 1, "digest": 1, "channel_ids": json.dumps([ch]),
                                  "active": 1, "created_at": db.now()})
        wl = db.new_id("wl_")
        db.insert("watchlists", {"id": wl, "org_id": oid, "name": "Demo watchlist", "created_at": db.now()})
        for num in ("07654321", "05556667"):
            db.insert("watchlist_items", {"id": db.new_id("wi_"), "watchlist_id": wl, "company_number": num, "label": None,
                                          "amount_owed": 18400, "credit_limit": 25000, "extra": None, "created_at": db.now()})
        logins.append((bid, email))
    gazette.ingest_entries([{"id": nid, "notice-code": code, "title": t, "content": c, "published": date.today().isoformat()} for nid, code, t, c in NOTICES])
    from app import events
    db.insert("companies", {"number": "16000001", "name": "COPPERLEAF STUDIOS LTD", "status": "active", "company_type": "ltd", "sic_codes": "74100",
                            "town": "Leeds", "postcode": "LS1 4DL", "region": "Yorkshire and The Humber", "incorporated_on": date.today().isoformat(),
                            "subscriber_type": "corporate", "updated_at": db.now()}) if not db.one("SELECT number FROM companies WHERE number='16000001'") else None
    events.record("ch_stream", "demo-inc-1", "incorporated", "16000001", "COPPERLEAF STUDIOS LTD", effective_date=date.today().isoformat())
    registers.ingest_ccj_rows([{"company_number": "09876543", "judgment_id": "DEMO-J1", "date": date.today().isoformat(), "amount": 4800}])
    done = run.process_new()
    print(f"Processed {len(done)} events.")
    for e in db.all("SELECT id, event_type, risk_score FROM events ORDER BY created_at"):
        tags = ",".join(r["brand"] for r in db.all("SELECT brand FROM event_brands WHERE event_id=?", (e["id"],)))
        print(f"  {e['event_type']:<24} score {e['risk_score']}  -> {tags}")
    print("\nDemo logins (password: demo-password-123):")
    for bid, email in logins:
        print(f"  {bid:<14} {email}")


if __name__ == "__main__":
    main()
