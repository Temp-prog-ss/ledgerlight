"""One-off: create the staff login, the sending mailbox and a test campaign, then
send step 1 to your own test addresses. Run by `outreach-test-setup.command`.

Reads from the environment / .env:
  OUTREACH_FROM, OUTREACH_FROM_NAME, OUTREACH_PW_MAIN, OUTREACH_TEST_RECIPIENTS,
  LL_STAFF_EMAIL, LL_STAFF_PASSWORD (only used if the staff user doesn't exist yet),
  OUTREACH_TEST_BRAND (default sitesentinel)
"""
import os
import smtplib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, security  # noqa: E402
from app.outreach import engine, leads  # noqa: E402
from app.sources import risk_watch  # noqa: E402


def say(msg):
    print(f"  {msg}", flush=True)


def check_smtp(email_addr, password):
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(email_addr, password)
        return None
    except smtplib.SMTPAuthenticationError:
        return ("Google rejected the login. Check: 2-Step Verification is on, you used an APP password "
                "(not your normal password), and the address is right. Workspace admins can disable app passwords.")
    except Exception as e:
        return f"Couldn't reach Gmail's mail server: {e}"


def main():
    config.reload()
    db.init_db()
    risk_watch.seed()
    sender = os.environ.get("OUTREACH_FROM", "").strip().lower()
    name = os.environ.get("OUTREACH_FROM_NAME", "").strip()
    pw = os.environ.get("OUTREACH_PW_MAIN", "")
    tests = sorted(leads.test_recipients())
    staff_email = os.environ.get("LL_STAFF_EMAIL", "").strip().lower()
    brand = os.environ.get("OUTREACH_TEST_BRAND", "sitesentinel")
    if not (sender and pw and tests and staff_email):
        sys.exit("Missing details: sender, app password, test addresses or staff email.")

    print("\n1. Checking the Gmail login…", flush=True)
    err = check_smtp(sender, pw)
    if err:
        sys.exit("  ✗ " + err)
    say("✓ Gmail accepted the app password")

    print("2. Console login…", flush=True)
    user = db.one("SELECT * FROM users WHERE email=?", (staff_email,))
    if user:
        db.execute("UPDATE users SET role='staff' WHERE id=?", (user["id"],))
        say("✓ Existing account made staff")
    else:
        spw = os.environ.get("LL_STAFF_PASSWORD", "")
        if len(spw) < 10:
            sys.exit("  ✗ Console password must be at least 10 characters")
        oid = db.new_id("org_")
        db.insert("orgs", {"id": oid, "name": "Ledgerlight (internal)", "brand": "ledgerlight", "plan": "enterprise", "billing_cycle": "annual",
                           "status": "active", "trial_ends_at": None, "stripe_customer_id": None, "stripe_subscription_id": None, "vetted": 1,
                           "regions": "[]", "timezone": "Europe/London", "digest_hour": 7, "last_digest_at": None, "created_at": db.now()})
        db.insert("users", {"id": db.new_id("usr_"), "org_id": oid, "email": staff_email, "password_hash": security.hash_password(spw),
                            "name": "Admin", "role": "staff", "created_at": db.now(), "last_login": None})
        say(f"✓ Staff login created for {staff_email}")

    print("3. Sending mailbox…", flush=True)
    mb = db.one("SELECT * FROM outreach_mailboxes WHERE email=?", (sender,))
    if not mb:
        mid = db.new_id("mb_")
        db.insert("outreach_mailboxes", {"id": mid, "email": sender, "from_name": name or None, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                                         "smtp_user": sender, "password_env": "OUTREACH_PW_MAIN", "imap_host": "imap.gmail.com", "imap_port": 993,
                                         "daily_cap": 40, "warmup_start": 10, "warmup_step": 3, "sent_date": None, "sent_today": 0,
                                         "last_sent_at": None, "last_imap_uid": 0, "active": 1, "created_at": db.now()})
    else:
        mid = mb["id"]
        db.execute("UPDATE outreach_mailboxes SET from_name=COALESCE(?,from_name), password_env='OUTREACH_PW_MAIN', active=1 WHERE id=?", (name or None, mid))
    say(f"✓ {sender} (40 a day max, starting at 10 a day while it warms up)")

    print("4. Test leads and campaign…", flush=True)
    csv_text = "Name,Email,Company,Position\n" + "\n".join(f"Test Recipient,{t},Test Company Ltd,Director" for t in tests)
    r = leads.import_csv(csv_text, brand=brand, source="test")
    ids = [db.one("SELECT id FROM outreach_leads WHERE email=?", (t,))["id"] for t in tests]
    for lid in ids:  # allow re-running the test
        db.execute("UPDATE outreach_leads SET stage='new', block_reason=NULL WHERE id=? AND stage NOT IN ('unsubscribed','bounced')", (lid,))
    c = engine.create_campaign(f"TEST · {brand} · own addresses", brand, [mid], window_start=0, window_end=24,
                               weekdays=[0, 1, 2, 3, 4, 5, 6], min_gap_seconds=3, max_gap_seconds=6)
    engine.set_steps(c["id"], engine.templates.default_sequence(brand)[:1])  # test = first email only
    e = engine.enroll(c["id"], ids)
    db.execute("UPDATE outreach_campaigns SET status='active' WHERE id=?", (c["id"],))
    say(f"✓ {len(tests)} test address(es); {e['added']} enrolled ({e['skipped']} skipped: unsubscribed or bounced)")

    print("5. Sending…", flush=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        n = engine.send_due()
        left = db.one("SELECT COUNT(*) AS n FROM outreach_enrollments WHERE campaign_id=? AND status='active'", (c["id"],))["n"]
        if n:
            say(f"sent {n}")
        if not left:
            break
        time.sleep(4)
    db.execute("UPDATE outreach_campaigns SET status='paused' WHERE id=?", (c["id"],))
    rows = db.all("SELECT l.email, m.status, m.error, m.subject FROM outreach_messages m JOIN outreach_leads l ON l.id=m.lead_id "
                  "JOIN outreach_enrollments en ON en.id=m.enrollment_id WHERE en.campaign_id=?", (c["id"],))
    print("\nResult:")
    for row in rows:
        say(("✓ " if row["status"] == "sent" else "✗ ") + f"{row['email']}: {row['status']}" + (f" ({row['error']})" if row["error"] else ""))
    if not rows:
        say("Nothing was sent. Check the addresses above weren't unsubscribed or bounced.")
    else:
        say(f'Subject: "{rows[0]["subject"]}". Check those inboxes (and spam).')
        say("Reply to one: within 5 minutes the console should move that lead to 'Replied'.")


if __name__ == "__main__":
    main()
