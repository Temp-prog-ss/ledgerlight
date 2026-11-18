"""Campaigns, sequences and the sending engine.

Deliverability guard-rails (on by default):
- Each mailbox has a daily cap and a warm-up ramp: start at `warmup_start` a day and add `warmup_step` each day, up to `daily_cap`.
- Sends only on chosen weekdays, inside the campaign's hours, in the campaign's time zone.
- Random 90–240 s gap between emails from the same mailbox, and one email per mailbox per worker tick.
- Follow-ups are sent as replies in the same thread and stop the moment the lead replies, bounces or unsubscribes.
- Every email carries one-click List-Unsubscribe headers and a footer with sender identity and postal address.
"""
import hashlib
import hmac
import json
import os
import random
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

from app import brands, config, db
from app.outreach import leads, templates
from app.pipeline import compliance


# ---------------- unsubscribe tokens ----------------

def unsubscribe_token(lead_id):
    sig = hmac.new(config.settings.secret_key.encode(), f"unsub:{lead_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{lead_id}.{sig}"


def check_token(token):
    try:
        lead_id, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    return lead_id if hmac.compare_digest(unsubscribe_token(lead_id), token) else None


def unsubscribe_url(lead_id):
    return f"{config.settings.public_base_url}/v1/outreach/unsubscribe?t={unsubscribe_token(lead_id)}"


# ---------------- campaigns ----------------

def create_campaign(name, brand, mailbox_ids, steps=None, **opts):
    if brand not in brands.BRANDS:
        raise ValueError("unknown brand")
    cid = db.new_id("cmp_")
    row = {"id": cid, "name": name, "brand": brand, "mailbox_ids": db.dumps(mailbox_ids), "status": "draft",
           "window_start": opts.get("window_start", 9), "window_end": opts.get("window_end", 17),
           "weekdays": db.dumps(opts.get("weekdays", [0, 1, 2, 3, 4])), "timezone": opts.get("timezone", "Europe/London"),
           "min_gap_seconds": opts.get("min_gap_seconds", 90), "max_gap_seconds": opts.get("max_gap_seconds", 240), "created_at": db.now()}
    db.insert("outreach_campaigns", row)
    set_steps(cid, steps or templates.default_sequence(brand))
    return row


def set_steps(campaign_id, steps):
    db.execute("DELETE FROM outreach_steps WHERE campaign_id=?", (campaign_id,))
    for i, s in enumerate(sorted(steps, key=lambda s: s.get("step_no", 0)), 1):
        if not (s.get("body") or "").strip():
            raise ValueError(f"step {i} has no body")
        if i == 1 and not (s.get("subject") or "").strip():
            raise ValueError("the first step needs a subject")
        db.insert("outreach_steps", {"id": db.new_id("stp_"), "campaign_id": campaign_id, "step_no": i,
                                     "delay_days": int(s.get("delay_days", 0 if i == 1 else 3)), "subject": s.get("subject"), "body": s["body"]})


def enroll(campaign_id, lead_ids=None, stage="new", brand=None):
    """Add leads to a campaign. Skips blocked/closed leads and anyone already in an active sequence."""
    camp = db.one("SELECT * FROM outreach_campaigns WHERE id=?", (campaign_id,))
    if not camp:
        raise ValueError("campaign not found")
    if lead_ids:
        rows = [db.one("SELECT * FROM outreach_leads WHERE id=?", (i,)) for i in lead_ids]
    else:
        rows = db.all("SELECT * FROM outreach_leads WHERE stage=?" + (" AND (brand=? OR brand IS NULL)" if brand else ""),
                      (stage, brand) if brand else (stage,))
    added, skipped = 0, 0
    for l in rows:
        if not l or l["stage"] in leads.CLOSED or l["block_reason"]:
            skipped += 1
            continue
        if db.one("SELECT id FROM outreach_enrollments WHERE lead_id=? AND status='active'", (l["id"],)):
            skipped += 1
            continue
        if db.one("SELECT id FROM outreach_enrollments WHERE campaign_id=? AND lead_id=?", (campaign_id, l["id"])):
            skipped += 1
            continue
        db.insert("outreach_enrollments", {"id": db.new_id("enr_"), "campaign_id": campaign_id, "lead_id": l["id"], "status": "active",
                                           "next_step": 1, "next_send_at": db.now(), "mailbox_id": None, "first_message_id": None,
                                           "first_subject": None, "created_at": db.now()})
        added += 1
    return {"added": added, "skipped": skipped}


# ---------------- mailboxes ----------------

def mailbox_cap(mb, today=None):
    today = today or date.today()
    created = date.fromisoformat((mb["created_at"] or db.now())[:10])
    days = max(0, (today - created).days)
    return min(int(mb["daily_cap"]), int(mb["warmup_start"]) + days * int(mb["warmup_step"]))


def mailbox_sent_today(mb, today=None):
    today = (today or date.today()).isoformat()
    return int(mb["sent_today"] or 0) if mb["sent_date"] == today else 0


def in_window(camp, now_utc):
    try:
        tz = ZoneInfo(camp["timezone"] or "Europe/London")
    except Exception:
        tz = ZoneInfo("Europe/London")
    local = now_utc.astimezone(tz)
    days = db.loads(camp["weekdays"], [0, 1, 2, 3, 4])
    return local.weekday() in days and int(camp["window_start"]) <= local.hour < int(camp["window_end"])


# ---------------- sending ----------------

def smtp_send(mb, msg):
    if config.settings.dry_run_delivery:
        out = Path("outbox")
        out.mkdir(exist_ok=True)
        p = out / f"{db.now().replace(':', '')}_outreach_{db.new_id()}.json"
        p.write_text(json.dumps({"from": msg["From"], "to": msg["To"], "subject": msg["Subject"], "message_id": msg["Message-ID"],
                                 "in_reply_to": msg["In-Reply-To"], "list_unsubscribe": msg["List-Unsubscribe"],
                                 "body": msg.get_content()}, indent=1))
        return
    password = os.environ.get(mb["password_env"] or "", "")
    if not password:
        raise RuntimeError(f"Mailbox password not set: put it in .env as {mb['password_env']}")
    port = int(mb["smtp_port"] or 587)
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(mb["smtp_host"], port, timeout=30) as s:
        if port != 465:
            s.starttls()
        s.login(mb["smtp_user"] or mb["email"], password)
        s.send_message(msg)


def build_message(mb, lead, step, enrollment, camp):
    b = brands.get(camp["brand"])
    values = {**lead, "sender_name": mb["from_name"] or mb["email"].split("@")[0].title(), "brand": b.name, "brand_url": b.site_url}
    body = templates.render(step["body"], values) + templates.footer(unsubscribe_url(lead["id"]))
    if step["step_no"] == 1 or not enrollment["first_subject"]:
        subject = templates.render(step["subject"], values)
    else:
        subject = templates.render(step["subject"], values) if step["subject"] else "Re: " + enrollment["first_subject"]
    msg = EmailMessage()
    msg["From"] = f"{values['sender_name']} <{mb['email']}>"
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=mb["email"].split("@")[-1])
    if enrollment["first_message_id"] and step["step_no"] > 1:
        msg["In-Reply-To"] = enrollment["first_message_id"]
        msg["References"] = enrollment["first_message_id"]
    msg["List-Unsubscribe"] = f"<mailto:{mb['email']}?subject=unsubscribe>, <{unsubscribe_url(lead['id'])}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    return msg, subject


def _pick_mailbox(camp, enrollment, now_utc, rng):
    ids = db.loads(camp["mailbox_ids"], [])
    if enrollment["mailbox_id"]:  # follow-ups go from the same mailbox
        ids = [enrollment["mailbox_id"]]
    today = now_utc.date()
    for mid in rng.sample(ids, len(ids)):
        mb = db.one("SELECT * FROM outreach_mailboxes WHERE id=? AND active=1", (mid,))
        if not mb or mailbox_sent_today(mb, today) >= mailbox_cap(mb, today):
            continue
        if mb["last_sent_at"]:
            gap = rng.randint(int(camp["min_gap_seconds"]), max(int(camp["min_gap_seconds"]), int(camp["max_gap_seconds"])))
            if (now_utc - datetime.fromisoformat(mb["last_sent_at"])).total_seconds() < gap:
                continue
        return mb
    return None


def send_due(now_utc=None, rng=None, max_per_tick=50):
    """Send what is due. Call every minute. Returns number sent."""
    now_utc = (now_utc or datetime.now(timezone.utc)).replace(microsecond=0)
    rng = rng or random.Random()
    sent, used = 0, set()
    for camp in db.all("SELECT * FROM outreach_campaigns WHERE status='active'"):
        if not in_window(camp, now_utc):
            continue
        steps = {s["step_no"]: s for s in db.all("SELECT * FROM outreach_steps WHERE campaign_id=?", (camp["id"],))}
        due = db.all("SELECT * FROM outreach_enrollments WHERE campaign_id=? AND status='active' AND next_send_at<=? "
                     "ORDER BY next_step DESC, next_send_at LIMIT 200", (camp["id"], now_utc.isoformat()))
        for enr in due:
            if sent >= max_per_tick:
                return sent
            lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (enr["lead_id"],))
            # re-check everything at send time
            stype, reason = leads.classify(lead["email"], lead["company_number"]) if lead else (None, "lead deleted")
            if not lead or lead["stage"] in leads.CLOSED or reason:
                db.execute("UPDATE outreach_enrollments SET status='stopped' WHERE id=?", (enr["id"],))
                if lead and reason and lead["stage"] not in leads.CLOSED:
                    db.execute("UPDATE outreach_leads SET stage='blocked', block_reason=? WHERE id=?", (reason, lead["id"]))
                continue
            step = steps.get(enr["next_step"])
            if not step:
                db.execute("UPDATE outreach_enrollments SET status='finished' WHERE id=?", (enr["id"],))
                continue
            mb = _pick_mailbox(camp, enr, now_utc, rng)
            if not mb or mb["id"] in used:
                continue
            msg, subject = build_message(mb, lead, step, enr, camp)
            try:
                smtp_send(mb, msg)
            except Exception as e:
                db.insert("outreach_messages", {"id": db.new_id("om_"), "enrollment_id": enr["id"], "lead_id": lead["id"], "mailbox_id": mb["id"],
                                                "step_no": step["step_no"], "message_id": msg["Message-ID"], "subject": subject,
                                                "status": "failed", "error": str(e)[:300], "sent_at": now_utc.isoformat()})
                continue
            used.add(mb["id"])
            sent += 1
            today = now_utc.date().isoformat()
            db.execute("UPDATE outreach_mailboxes SET sent_today=CASE WHEN sent_date=? THEN sent_today+1 ELSE 1 END, sent_date=?, last_sent_at=? WHERE id=?",
                       (today, today, now_utc.isoformat(), mb["id"]))
            db.insert("outreach_messages", {"id": db.new_id("om_"), "enrollment_id": enr["id"], "lead_id": lead["id"], "mailbox_id": mb["id"],
                                            "step_no": step["step_no"], "message_id": msg["Message-ID"], "subject": subject,
                                            "status": "sent", "error": None, "sent_at": now_utc.isoformat()})
            nxt = steps.get(step["step_no"] + 1)
            db.execute("UPDATE outreach_enrollments SET next_step=?, next_send_at=?, mailbox_id=?, status=?, "
                       "first_message_id=COALESCE(first_message_id, ?), first_subject=COALESCE(first_subject, ?) WHERE id=?",
                       (step["step_no"] + 1, (now_utc + timedelta(days=int(nxt["delay_days"]))).isoformat() if nxt else None,
                        mb["id"], "active" if nxt else "finished", msg["Message-ID"], subject, enr["id"]))
            db.execute("UPDATE outreach_leads SET stage=CASE WHEN stage='new' THEN 'contacted' ELSE stage END, last_contacted_at=?, updated_at=? WHERE id=?",
                       (now_utc.isoformat(), now_utc.isoformat(), lead["id"]))
            leads.log(lead["id"], "sent", {"step": step["step_no"], "subject": subject, "mailbox": mb["email"]})
    return sent


def stats():
    q = lambda sql, p=(): db.one(sql, p)["n"]  # noqa: E731
    sent = q("SELECT COUNT(*) AS n FROM outreach_messages WHERE status='sent'")
    first = q("SELECT COUNT(*) AS n FROM outreach_messages WHERE status='sent' AND step_no=1")
    replied = q("SELECT COUNT(*) AS n FROM outreach_leads WHERE replied_at IS NOT NULL")
    bounced = q("SELECT COUNT(*) AS n FROM outreach_leads WHERE stage='bounced'")
    unsub = q("SELECT COUNT(*) AS n FROM outreach_leads WHERE stage='unsubscribed'")
    return {"emails_sent": sent, "leads_contacted": first, "replies": replied, "bounces": bounced, "unsubscribes": unsub,
            "reply_rate": round(replied / first * 100, 1) if first else 0.0,
            "bounce_rate": round(bounced / first * 100, 1) if first else 0.0,
            "pipeline": db.all("SELECT stage, COUNT(*) AS n FROM outreach_leads GROUP BY stage"),
            "mailboxes": [{"email": m["email"], "sent_today": mailbox_sent_today(m), "cap_today": mailbox_cap(m)}
                          for m in db.all("SELECT * FROM outreach_mailboxes WHERE active=1")]}
