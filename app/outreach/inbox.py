"""Watch each sending mailbox over IMAP: replies stop the sequence, bounces and
'unsubscribe' replies suppress the address. Out-of-office auto-replies are ignored.

Gmail / Google Workspace: IMAP host imap.gmail.com, port 993, with an app password.
"""
import email
import imaplib
import os
import re
from email import policy
from datetime import timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from app import db
from app.outreach import leads

START_FROM_NOW = 10 ** 9  # a last_imap_uid at or above this means "skip everything already in the mailbox"


def _imap_date(iso):
    from datetime import date
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None
    return f"{d.day:02d}-{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.month - 1]}-{d.year}"


UNSUB_RX = re.compile(r"\b(unsubscribe|remove me|take me off|stop emailing|opt.?out|do not contact)\b", re.I)
AUTO_RX = re.compile(r"(out of (the )?office|automatic reply|auto.?reply|away from (the )?office|on annual leave)", re.I)


def _text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_content()
                except Exception:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
        return ""
    try:
        return msg.get_content()
    except Exception:
        return (msg.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _strip_quoted(text):
    """Keep only what the person wrote, not the quoted email below it."""
    out = []
    for line in (text or "").splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:$", line.strip()) or line.strip() == "--":
            break
        out.append(line)
    return "\n".join(out)


def _bounced_address(msg):
    body = msg.as_string()
    m = re.search(r"Final-Recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?", body, re.I) or \
        re.search(r"Original-Recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?", body, re.I) or \
        re.search(r"(?:delivery to|could not be delivered to|address not found)[^\n]*?([A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", body, re.I)
    return m.group(1).lower() if m else None


def _after_contact(msg, lead):
    if not lead.get("last_contacted_at"):
        return False
    try:
        sent = parsedate_to_datetime(msg.get("Date"))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        from datetime import datetime
        return sent >= datetime.fromisoformat(lead["last_contacted_at"])
    except Exception:
        return False


def handle_message(raw_bytes):
    """Classify one inbound email and update the pipeline. Returns a short description."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "") or ""
    ctype = msg.get_content_type()
    is_bounce = sender.startswith(("mailer-daemon@", "postmaster@")) or ctype == "multipart/report" or \
        re.search(r"(delivery status notification|undeliver|returned mail|failure notice)", subject, re.I)
    if is_bounce:
        addr = _bounced_address(msg)
        lead = db.one("SELECT * FROM outreach_leads WHERE email=?", (addr,)) if addr else None
        if lead and not _after_contact(msg, lead):
            return "bounce:before-contact"
        if lead:
            from app.pipeline import compliance
            compliance.suppress("email", lead["email"], "hard bounce")
            leads.set_stage(lead["id"], "bounced", "bounce received", actor="mailbox")
            return f"bounce:{addr}"
        return "bounce:unmatched"
    if msg.get("Auto-Submitted", "no").lower() not in ("no", "") or msg.get("X-Autoreply") or AUTO_RX.search(subject):
        return "auto-reply:ignored"
    refs = " ".join(filter(None, [msg.get("In-Reply-To"), msg.get("References")]))
    lead = None
    for mid in re.findall(r"<[^>]+>", refs):
        m = db.one("SELECT lead_id FROM outreach_messages WHERE message_id=?", (mid,))
        if m:
            lead = db.one("SELECT * FROM outreach_leads WHERE id=?", (m["lead_id"],))
            break
    if not lead:
        lead = db.one("SELECT * FROM outreach_leads WHERE email=?", (sender,))
        # Matched on sender only: it is a reply only if it arrived after we last emailed them.
        if lead and not _after_contact(msg, lead):
            said_early = _strip_quoted(_text(msg))
            if UNSUB_RX.search(said_early) or UNSUB_RX.search(subject):  # always honour an opt-out
                leads.unsubscribe(lead["id"], how="reply")
                return f"unsubscribe:{lead['email']}"
            return "other:before-contact"
    if not lead:
        return "other"
    said = _strip_quoted(_text(msg))
    if UNSUB_RX.search(said) or UNSUB_RX.search(subject):
        leads.unsubscribe(lead["id"], how="reply")
        return f"unsubscribe:{lead['email']}"
    db.execute("UPDATE outreach_leads SET replied_at=COALESCE(replied_at, ?) WHERE id=?", (db.now(), lead["id"]))
    if lead["stage"] in ("new", "contacted"):
        leads.set_stage(lead["id"], "replied", said[:300], actor="mailbox")
    else:
        leads.log(lead["id"], "reply", {"text": said[:300]})
    return f"reply:{lead['email']}"


def poll_mailbox(mb):  # pragma: no cover - needs a real IMAP server
    password = os.environ.get(mb["password_env"] or "", "")
    if not (mb["imap_host"] and password):
        return []
    results = []
    with imaplib.IMAP4_SSL(mb["imap_host"], int(mb["imap_port"] or 993)) as im:
        im.login(mb["smtp_user"] or mb["email"], password)
        im.select("INBOX", readonly=True)
        last = int(mb["last_imap_uid"] or 0)
        if last == 0 or last >= START_FROM_NOW:  # first check: start from now, never read old mail
            typ, data = im.uid("search", None, "ALL")
            uids = [int(u) for u in (data[0] or b"").split()]
            db.execute("UPDATE outreach_mailboxes SET last_imap_uid=? WHERE id=?", (max(uids) if uids else 0, mb["id"]))
            if uids:
                return []
        since = _imap_date(mb.get("created_at"))
        typ, data = im.uid("search", None, f"UID {last + 1}:*" + (f" SINCE {since}" if since else ""))
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last]
        for uid in uids[:500]:
            typ, parts = im.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                try:
                    results.append(handle_message(raw))
                except Exception as e:
                    db.audit("worker", "outreach_inbox_error", {"mailbox": mb["email"], "uid": uid, "error": str(e)[:200]})
            db.execute("UPDATE outreach_mailboxes SET last_imap_uid=? WHERE id=?", (uid, mb["id"]))
    return results


def poll_all():  # pragma: no cover
    out = []
    for mb in db.all("SELECT * FROM outreach_mailboxes WHERE active=1"):
        try:
            out += poll_mailbox(mb)
        except Exception as e:
            db.audit("worker", "outreach_imap_error", {"mailbox": mb["email"], "error": str(e)[:200]})
    return out
