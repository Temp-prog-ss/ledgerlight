import json
import random
from datetime import datetime, timedelta, timezone

from app import db
from app.outreach import engine, inbox, leads
from tests.test_api import signed_up

# Same columns as the Google Sheet in the video
SHEET = """Name,Position,Email,Website,Date Found,Company
Leanne Hart,Founder,leanne@wildthing.co.uk,wildthing.co.uk,11/09/2022,Wildthing Ltd
Mauricio Lopez,Co-Founder,mauricio@arloskye.com,arloskye.com,11/09/2022,Arlo Skye
Matt Green,CEO & Founder,matt.green@gmail.com,guidesco.com,11/09/2022,Guides
Bad Row,Founder,not-an-email,,11/09/2022,
"""
_today = datetime.now(timezone.utc).date()
_monday = _today + timedelta(days=(7 - _today.weekday()) % 7 or 7)
MONDAY_10AM = datetime(_monday.year, _monday.month, _monday.day, 10, 0, tzinfo=timezone.utc)  # next Monday, inside 9–17 UK


def mailbox(cap=40, start=10, step=3, created=None):
    mid = db.new_id("mb_")
    db.insert("outreach_mailboxes", {"id": mid, "email": "sam@getsitesentinel.co.uk", "from_name": "Sam", "smtp_host": "smtp.gmail.com",
                                     "smtp_port": 587, "smtp_user": None, "password_env": "OUTREACH_PW_SAM", "imap_host": "imap.gmail.com",
                                     "imap_port": 993, "daily_cap": cap, "warmup_start": start, "warmup_step": step, "sent_date": None,
                                     "sent_today": 0, "last_sent_at": None, "last_imap_uid": 0, "active": 1,
                                     "created_at": created or MONDAY_10AM.replace(hour=0).isoformat()})
    return mid


def outbox(tmp_path):
    return sorted((json.loads(p.read_text()) for p in (tmp_path / "outbox").glob("*outreach*.json")), key=lambda m: m["message_id"])


def test_import_matches_video_sheet_and_blocks_personal_mailboxes():
    r = leads.import_csv(SHEET, brand="sitesentinel")
    assert (r["added"], r["invalid"], r["blocked"]) == (3, 1, 1)
    gm = db.one("SELECT * FROM outreach_leads WHERE email='matt.green@gmail.com'")
    assert gm["stage"] == "blocked" and "consent" in gm["block_reason"]
    lh = db.one("SELECT * FROM outreach_leads WHERE email='leanne@wildthing.co.uk'")
    assert (lh["first_name"], lh["last_name"], lh["position"], lh["brand"]) == ("Leanne", "Hart", "Founder", "sitesentinel")
    assert leads.import_csv(SHEET)["updated"] == 3  # re-import updates, never duplicates


def test_sequence_sends_threads_and_respects_caps(tmp_path):
    leads.import_csv(SHEET, brand="sitesentinel")
    mid = mailbox(start=1, step=1)  # warm-up: 1 email on day one
    c = engine.create_campaign("Test", "sitesentinel", [mid])
    assert engine.enroll(c["id"]) == {"added": 2, "skipped": 0}  # gmail lead is blocked, not enrolled
    db.execute("UPDATE outreach_campaigns SET status='active'")
    rng = random.Random(1)
    assert engine.send_due(MONDAY_10AM - timedelta(hours=3), rng) == 0  # 7am: outside window
    assert engine.send_due(MONDAY_10AM, rng) == 1
    assert engine.send_due(MONDAY_10AM + timedelta(minutes=10), rng) == 0  # warm-up cap of 1 reached
    day2 = MONDAY_10AM + timedelta(days=1)
    assert engine.send_due(day2, rng) == 1  # cap now 2: second lead's first email
    assert engine.send_due(day2 + timedelta(minutes=1), rng) == 0  # spacing between sends
    msgs = outbox(tmp_path)
    assert all(m["list_unsubscribe"] and "unsubscribe?t=" in m["body"] for m in msgs)
    assert msgs[0]["subject"].startswith("Quick question about")
    day4 = MONDAY_10AM + timedelta(days=3, minutes=5)
    engine.send_due(day4, rng)
    follow = [m for m in outbox(tmp_path) if m["in_reply_to"]]
    assert follow and follow[0]["subject"].startswith("Re: Quick question")
    assert db.one("SELECT stage FROM outreach_leads WHERE email='leanne@wildthing.co.uk'")["stage"] == "contacted"


def test_weekend_and_suppression_stop_sending(tmp_path):
    leads.import_csv(SHEET)
    mid = mailbox()
    c = engine.create_campaign("Test", "creditpulse", [mid])
    engine.enroll(c["id"])
    db.execute("UPDATE outreach_campaigns SET status='active'")
    assert engine.send_due(MONDAY_10AM + timedelta(days=5)) == 0  # Saturday
    from app.pipeline import compliance
    compliance.suppress("email", "@wildthing.co.uk", "domain objection")
    compliance.suppress("email", "MAURICIO@arloskye.com", "objection")
    assert engine.send_due(MONDAY_10AM) == 0
    assert db.one("SELECT COUNT(*) AS n FROM outreach_enrollments WHERE status='stopped'")["n"] == 2


def _sent_lead():
    leads.import_csv(SHEET)
    mid = mailbox()
    c = engine.create_campaign("Test", "sitesentinel", [mid])
    engine.enroll(c["id"])
    db.execute("UPDATE outreach_campaigns SET status='active'")
    engine.send_due(MONDAY_10AM, random.Random(2))
    return db.one("SELECT * FROM outreach_messages WHERE status='sent'")


def test_reply_stops_sequence():
    m = _sent_lead()
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (m["lead_id"],))
    raw = (f"From: Someone <{lead['email']}>\r\nTo: sam@getsitesentinel.co.uk\r\nSubject: Re: {m['subject']}\r\n"
           f"In-Reply-To: {m['message_id']}\r\n\r\nYes, interested. Can we talk Thursday?\r\n\r\nOn Mon, Sam wrote:\r\n> hello\r\n").encode()
    assert inbox.handle_message(raw).startswith("reply:")
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (lead["id"],))
    assert lead["stage"] == "replied" and lead["replied_at"]
    assert db.one("SELECT status FROM outreach_enrollments WHERE lead_id=?", (lead["id"],))["status"] == "replied"


def test_unsubscribe_reply_bounce_and_auto_reply():
    m = _sent_lead()
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (m["lead_id"],))
    ooo = f"From: {lead['email']}\r\nSubject: Automatic reply: Quick question\r\nAuto-Submitted: auto-replied\r\n\r\nI'm away.".encode()
    assert inbox.handle_message(ooo) == "auto-reply:ignored"
    unsub = f"From: {lead['email']}\r\nSubject: Re: hi\r\n\r\nPlease unsubscribe me.".encode()
    assert inbox.handle_message(unsub).startswith("unsubscribe:")
    from app.pipeline import compliance
    assert compliance.suppressed("email", lead["email"])
    other = db.one("SELECT * FROM outreach_leads WHERE stage NOT IN ('unsubscribed','blocked')")
    db.execute("UPDATE outreach_leads SET last_contacted_at='2026-01-01T09:00:00+00:00' WHERE id=?", (other["id"],))
    from email.utils import formatdate
    dsn = (f"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nDate: {formatdate()}\r\nSubject: Delivery Status Notification (Failure)\r\n"
           f"Content-Type: text/plain\r\n\r\nAddress not found.\r\nFinal-Recipient: rfc822; {other['email']}\r\n").encode()
    assert inbox.handle_message(dsn) == f"bounce:{other['email']}"
    assert db.one("SELECT stage FROM outreach_leads WHERE id=?", (other["id"],))["stage"] == "bounced"


def test_unsubscribe_link(client):
    leads.import_csv(SHEET)
    lead = db.one("SELECT * FROM outreach_leads WHERE email='leanne@wildthing.co.uk'")
    tok = engine.unsubscribe_token(lead["id"])
    page = client.get("/v1/outreach/unsubscribe", params={"t": tok})
    assert page.status_code == 200 and "Yes, stop emailing me" in page.text
    assert db.one("SELECT stage FROM outreach_leads WHERE id=?", (lead["id"],))["stage"] == "new"  # GET alone does nothing
    assert client.post("/v1/outreach/unsubscribe", params={"t": tok}).status_code == 200
    assert db.one("SELECT stage FROM outreach_leads WHERE id=?", (lead["id"],))["stage"] == "unsubscribed"
    assert client.get("/v1/outreach/unsubscribe", params={"t": lead["id"] + ".forged"}).status_code == 400


def test_api_flow(client, monkeypatch):
    signed_up(client)
    assert client.get("/v1/outreach/stats").status_code == 403
    db.execute("UPDATE users SET role='staff'")
    bad = {"email": "sam@getsitesentinel.co.uk", "smtp_host": "smtp.gmail.com", "password_env": "my-real-password"}
    assert client.post("/v1/outreach/mailboxes", json=bad).status_code == 422
    assert client.post("/v1/outreach/mailboxes", json={**bad, "password_env": "OUTREACH_PW_SAM"}).status_code == 201
    r = client.request("POST", "/v1/outreach/leads/import", params={"brand": "sitesentinel"}, raw=SHEET.encode(), headers={"content-type": "text/csv"})
    assert r.json()["added"] == 3
    c = client.post("/v1/outreach/campaigns", json={"name": "NW subbies", "brand": "sitesentinel"}).json()
    assert client.post(f"/v1/outreach/campaigns/{c['id']}/enroll", json={}).json()["added"] == 2
    pv = client.post(f"/v1/outreach/campaigns/{c['id']}/preview", json={}).json()
    assert len(pv["steps"]) == 3 and "SiteSentinel" in pv["steps"][0]["body"] and "Unsubscribe" in pv["steps"][0]["body"]
    monkeypatch.setenv("LL_DRY_RUN_DELIVERY", "0")
    from app import config
    config.reload()
    blocked = client.post(f"/v1/outreach/campaigns/{c['id']}/status", json={"status": "active"})
    assert blocked.status_code == 409 and "OUTREACH_POSTAL_ADDRESS" in blocked.json()["error"]
    monkeypatch.setenv("OUTREACH_POSTAL_ADDRESS", "1 King Street, Manchester M2 4LQ")
    monkeypatch.setenv("OUTREACH_PW_SAM", "app-password")
    assert client.post(f"/v1/outreach/campaigns/{c['id']}/status", json={"status": "active"}).json()["status"] == "active"
    lead = client.get("/v1/outreach/leads", params={"q": "leanne"}).json()["leads"][0]
    assert client.patch(f"/v1/outreach/leads/{lead['id']}", json={"stage": "interested"}).json()["updated"]
    assert "leanne@wildthing.co.uk" in client.get("/v1/outreach/leads.csv").text
    assert client.get("/outreach").status_code == 200


def test_prospects_by_trade(company_factory):
    company_factory("01234567", "SPARKS ELECTRICAL LTD", sic="43210", region="North West")
    company_factory("07654321", "HIGH STREET CAFE LTD", sic="56101", region="North West")
    names = [r["name"] for r in leads.prospects("sitesentinel", "North West")]
    assert names == ["SPARKS ELECTRICAL LTD"]


def test_own_test_addresses_allowed_even_on_gmail(monkeypatch):
    monkeypatch.setenv("OUTREACH_TEST_RECIPIENTS", "me.myself@gmail.com")
    leads.import_csv("Name,Email\nMe Myself,me.myself@gmail.com\nOther Person,other@gmail.com\n")
    assert db.one("SELECT stage FROM outreach_leads WHERE email='me.myself@gmail.com'")["stage"] == "new"
    assert db.one("SELECT stage FROM outreach_leads WHERE email='other@gmail.com'")["stage"] == "blocked"


def test_old_mail_from_a_lead_is_not_a_reply():
    m = _sent_lead()
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (m["lead_id"],))
    old = (f"From: {lead['email']}\r\nTo: sam@getsitesentinel.co.uk\r\nDate: Sat, 24 Feb 2024 00:15:00 +0000\r\n"
           f"Subject: Holiday photos\r\n\r\nSee you soon").encode()
    assert inbox.handle_message(old) == "other:before-contact"
    assert db.one("SELECT stage FROM outreach_leads WHERE id=?", (lead["id"],))["stage"] == "contacted"
    assert not db.all("SELECT * FROM outreach_activity WHERE kind='reply' AND lead_id=?", (lead["id"],))


def test_old_bounce_ignored_and_imap_date():
    m = _sent_lead()
    lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (m["lead_id"],))
    old = (f"From: mailer-daemon@googlemail.com\r\nDate: Sat, 24 Feb 2024 00:15:00 +0000\r\nSubject: Delivery Status Notification (Failure)\r\n\r\n"
           f"Final-Recipient: rfc822; {lead['email']}\r\n").encode()
    assert inbox.handle_message(old) == "bounce:before-contact"
    assert inbox._imap_date("2026-11-18T16:40:46+00:00") == "18-Nov-2026"
