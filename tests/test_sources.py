import json

from app import config, db
from app.sources import companies_house as ch
from app.sources import gazette, registers

PETITION = {
    "id": "https://www.thegazette.co.uk/notice/5123456", "title": "Petitions to Wind Up (Companies)",
    "published": "2026-11-17T09:00:00Z", "f:notice-code": "2450",
    "content": "In the High Court of Justice, Business and Property Courts. CR-2026-004512. In the Matter of OAKMERE GROUNDWORKS LIMITED "
               "(Company Number 09876543). A Petition to wind up the above-named Company was presented on 1 November 2026 by "
               "HM Revenue and Customs, of Enforcement. The Petition will be heard at Court 3 on 9 December 2026 at 10.30am.",
}
ADMIN = {
    "id": "5123999", "title": "Appointment of Administrators", "published": "2026-11-17", "notice-code": "2410",
    "content": "HARBOURLINE BUILD LIMITED Company Number: 07654321. Office Holder Details: Jane Smith (IP No. 12345) of "
               "Harrow & Vale Recovery LLP, 1 King Street, Manchester M2 4LQ. Tel: 0161 555 0123. Email: cases@harrowvale.example. "
               "Creditors must send claims on or before 20 January 2027.",
}
ESTATE = {
    "id": "5124000", "title": "Deceased Estates", "published": "2026-11-17", "notice-code": "2903",
    "content": "SMITH, John Arthur. 12 Mill Lane, Bristol BS1 6QA. Date of death 1 October 2026. Claims to Hollis & Grant LLP "
               "solicitors, by 20 January 2027.",
}


def test_gazette_parses_petition_and_administration():
    ids = gazette.ingest_entries([PETITION, ADMIN])
    assert len(ids) == 2
    p = db.one("SELECT * FROM events WHERE source_id='5123456'")
    d = json.loads(p["data"])
    assert p["event_type"] == "winding_up_petition"
    assert p["company_number"] == "09876543"
    assert d["hearing_date"] == "2026-12-09" and d["case_number"] == "CR-2026-004512"
    assert d["petitioner"].startswith("HM Revenue")
    a = db.one("SELECT * FROM events WHERE source_id='5123999'")
    ad = json.loads(a["data"])
    assert a["event_type"] == "administration" and a["company_number"] == "07654321"
    assert ad["ip_name"] == "Jane Smith" and "Harrow & Vale Recovery LLP" in ad["ip_firm"]
    assert ad["ip_phone"].startswith("0161") and ad["claim_deadline"] == "2027-01-20"
    assert gazette.ingest_entries([PETITION]) == []  # idempotent


def test_deceased_estate_is_minimised():
    gazette.ingest_entries([ESTATE])
    e = db.one("SELECT * FROM events WHERE event_type='deceased_estate'")
    d = json.loads(e["data"])
    assert e["personal_data"] == 1 and e["title"] == "Deceased estate notice"
    assert "SMITH" not in e["data"] and "Mill Lane" not in e["data"]
    assert d["postcode_district"] == "BS1" and d["claim_deadline"] == "2027-01-20"


def test_title_classifier_fallback():
    assert gazette.classify(None, "First Gazette Notice for compulsory strike-off") == "strike_off_proposed"
    assert gazette.classify("9999", "Resolutions for Winding-up") == "cvl"


def test_number_normalisation_and_region():
    assert ch.normalise_number("123456") == "00123456"
    assert ch.normalise_number("sc 1234") == "SC001234"
    assert ch.region_for("LS1 4DL") == "Yorkshire and The Humber"
    assert ch.region_for("EC2A 4NE") == "London"


def _profile(status="active", overdue=False, created="2010-01-01"):
    return json.dumps({"resource_kind": "company-profile", "resource_id": "07654321", "resource_uri": "/company/07654321",
                       "event": {"timepoint": 101, "published_at": "2026-11-17T10:00:00", "type": "changed"},
                       "data": {"company_number": "07654321", "company_name": "HARBOURLINE BUILD LIMITED", "company_status": status,
                                "type": "ltd", "date_of_creation": created, "sic_codes": ["41201"],
                                "registered_office_address": {"address_line_1": "1 Canal Wharf", "locality": "Manchester", "postal_code": "M4 6BF"},
                                "accounts": {"overdue": overdue, "next_due": "2026-09-30"}, "confirmation_statement": {"overdue": False}}})


def test_stream_detects_status_change_and_overdue_accounts():
    assert ch.handle_stream_line("companies", _profile()) == []  # first sight of an old company: no event
    created = ch.handle_stream_line("companies", _profile(status="administration", overdue=True).replace('"timepoint": 101', '"timepoint": 102'))
    types = {db.one("SELECT event_type FROM events WHERE id=?", (i,))["event_type"] for i in created}
    assert types == {"administration", "accounts_overdue"}
    assert db.get_state("ch_stream_timepoint:companies") == "102"
    co = db.one("SELECT * FROM companies WHERE number='07654321'")
    assert co["region"] == "North West" and co["sic_codes"] == "41201"


def test_stream_new_incorporation_and_heartbeat():
    from datetime import date
    assert ch.handle_stream_line("companies", "") == []
    created = ch.handle_stream_line("companies", _profile(created=date.today().isoformat()))
    assert db.one("SELECT event_type FROM events WHERE id=?", (created[0],))["event_type"] == "incorporated"


SPONSORS_DAY1 = "Organisation Name,Town/City,County,Type & Rating,Route\nNorthgate Care Homes Ltd,Leeds,,Worker (A rating),Skilled Worker\nKestrel Software Ltd,Reading,,Worker (A rating),Skilled Worker\n"
SPONSORS_DAY2 = "Organisation Name,Town/City,County,Type & Rating,Route\nKestrel Software Ltd,Reading,,Worker (B rating),Skilled Worker\nOrchard Hotels Ltd,London,,Worker (A rating),Skilled Worker\n"


def test_sponsor_register_diff(company_factory):
    company_factory("01112223", "NORTHGATE CARE HOMES LTD", sic="87300", town="Leeds")
    assert registers.ingest_sponsor_csv(SPONSORS_DAY1, today="2026-11-16") == []  # baseline
    assert json.loads(db.one("SELECT sponsor FROM companies WHERE number='01112223'")["sponsor"])["route"] == "Skilled Worker"
    created = registers.ingest_sponsor_csv(SPONSORS_DAY2, today="2026-11-17")
    types = sorted(db.one("SELECT event_type FROM events WHERE id=?", (i,))["event_type"] for i in created)
    assert types == ["sponsor_added", "sponsor_rating_change", "sponsor_removed"]
    removed = db.one("SELECT * FROM events WHERE event_type='sponsor_removed'")
    assert removed["company_number"] == "01112223"


def test_ccod_flags_land_in_dissolved_company(company_factory):
    company_factory("04445556", "MILL LANE INVESTMENTS LTD", sic="68100", status="dissolved")
    text = "Title Number,Tenure,Property Address,Company Registration No. (1)\nMAN123,Freehold,1 Mill Lane,4445556\nMAN124,Freehold,2 Mill Lane,04445556\n"
    created = registers.ingest_ccod(text)
    assert len(created) == 1
    assert db.one("SELECT titles_owned FROM companies WHERE number='04445556'")["titles_owned"] == 2


def test_ccj_rows_increment_count(company_factory):
    company_factory("05556667", "REDBROOK INTERIORS LTD", ccj_count_12m=0)
    registers.ingest_ccj_rows([{"company_number": "5556667", "judgment_id": "J1", "date": "2026-11-10", "amount": 4800}])
    assert db.one("SELECT ccj_count_12m FROM companies WHERE number='05556667'")["ccj_count_12m"] == 1


def test_rate_limiter_waits():
    from app.sources.http import RateLimiter
    t = [0.0]
    waits = []
    rl = RateLimiter(2, 10, sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)), clock=lambda: t[0])
    rl.acquire(); rl.acquire(); rl.acquire()
    assert waits and 9.9 < waits[0] <= 10.1


def test_writes_are_committed_and_visible_to_a_new_connection(tmp_path):
    db.set_state("durability_check", "yes")
    if db.is_postgres():
        import psycopg
        with psycopg.connect(config.settings.database_url) as other:
            assert other.execute("SELECT value FROM ingest_state WHERE key='durability_check'").fetchone() == ("yes",)
        return
    import sqlite3
    other = sqlite3.connect(str(tmp_path / "test.db"))
    assert other.execute("SELECT value FROM ingest_state WHERE key='durability_check'").fetchone() == ("yes",)


def test_unclaimed_estates_csv_link_found_on_govuk_page():
    html = ('<a href="https://assets.publishing.service.gov.uk/media/abc/Guidance.csv">x</a>'
            '<a class="govuk-link" href="https://assets.publishing.service.gov.uk/media/6afc17fd7972a3713dc60fd3/UnclaimedEstatesList.csv">'
            'Unclaimed estates list</a>')
    assert registers.unclaimed_csv_url(html).endswith("/UnclaimedEstatesList.csv")
    import pytest
    with pytest.raises(RuntimeError):
        registers.unclaimed_csv_url("<p>no files</p>")


DAY1 = "Date of Publication,BV Reference,Surname,Forename,Date of Death,Place of Death\n01/03/2026,BV111,SMITH,John,01/01/2026,Leeds\n02/03/2026,BV222,JONES,Mary,05/01/2026,Bristol\n"
DAY2 = "Date of Publication,BV Reference,Surname,Forename,Date of Death,Place of Death\n02/03/2026,BV222,JONES,Mary,05/01/2026,Bristol\n17/11/2026,BV333,BROWN,Alan,20/10/2026,Cardiff\n"


def test_unclaimed_estates_baseline_then_new_and_removed():
    assert registers.ingest_unclaimed_estates(DAY1, today="2026-11-17") == []  # baseline: no alert flood
    assert db.get_state("unclaimed_estates_headers").startswith("Date of Publication|BV Reference")
    created = registers.ingest_unclaimed_estates(DAY2, today="2026-11-18")
    assert len(created) == 1
    e = db.one("SELECT * FROM events WHERE id=?", (created[0],))
    d = json.loads(e["data"])
    assert e["personal_data"] == 1 and d["place_of_death"] == "Cardiff" and d["bv_reference"] == "BV333"
    assert "BROWN" not in e["data"]  # names never go into the event
    assert db.one("SELECT removed_at FROM unclaimed_estates WHERE id='ref:BV111'")["removed_at"] == "2026-11-18"
    assert registers.ingest_unclaimed_estates(DAY2, today="2026-11-19") == []  # idempotent


def test_unclaimed_estates_tolerates_other_column_names():
    alt = "Surname,Forenames,Date of death,Place of death,Date advertised\nX,Y,1/1/2026,York,2/2/2026\n"
    registers.ingest_unclaimed_estates(alt, today="2026-11-17")
    alt2 = alt + "Z,W,3/3/2026,Hull,4/4/2026\n"
    created = registers.ingest_unclaimed_estates(alt2, today="2026-11-18")
    d = json.loads(db.one("SELECT data FROM events WHERE id=?", (created[0],))["data"])
    assert d["place_of_death"] == "Hull" and d["date_listed"] == "4/4/2026"
