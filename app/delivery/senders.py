"""Outbound channels. In dry-run mode (default outside production) every message
is written to ./outbox as JSON instead of being sent, so the whole flow can be
tested safely."""
import json
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app import config, db
from app.sources.http import session

OUTBOX = Path("outbox")


def _dry(kind, payload):
    OUTBOX.mkdir(exist_ok=True)
    p = OUTBOX / f"{db.now().replace(':', '')}_{kind}_{db.new_id()}.json"
    p.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    return str(p)


def email(to, subject, html, text, attachments=None, brand_name="Ledgerlight"):
    s = config.settings
    if s.dry_run_delivery or not s.smtp_host:
        return _dry("email", {"to": to, "subject": subject, "text": text, "attachments": [a[0] for a in attachments or []]})
    msg = EmailMessage()
    msg["From"] = f"{brand_name} <{s.mail_from}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<{s.public_base_url}/v1/unsubscribe?email={to}>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/")
        msg.add_attachment(data.encode() if isinstance(data, str) else data, maintype=maintype, subtype=subtype, filename=name)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return "sent"


def slack(webhook_url, text, blocks=None):
    if config.settings.dry_run_delivery:
        return _dry("slack", {"url": webhook_url, "text": text})
    r = session().post(webhook_url, json={"text": text, **({"blocks": blocks} if blocks else {})}, timeout=20)
    r.raise_for_status()
    return "sent"


def teams(webhook_url, title, text):
    if config.settings.dry_run_delivery:
        return _dry("teams", {"url": webhook_url, "title": title, "text": text})
    card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
        "type": "AdaptiveCard", "version": "1.4", "body": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": text, "wrap": True}]}}]}
    r = session().post(webhook_url, json=card, timeout=20)
    r.raise_for_status()
    return "sent"


def sms(to, text):
    s = config.settings
    if s.dry_run_delivery or not s.twilio_sid:
        return _dry("sms", {"to": to, "text": text[:320]})
    r = session().post(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_sid}/Messages.json",
                       data={"To": to, "From": s.twilio_from, "Body": text[:320]}, auth=(s.twilio_sid, s.twilio_token), timeout=20)
    r.raise_for_status()
    return "sent"


def sftp_put(cfg, filename, data):
    """cfg: {host, port, username, password|private_key, path}. Needs `paramiko` in production."""
    if config.settings.dry_run_delivery:
        return _dry("sftp", {"host": cfg.get("host"), "file": filename, "bytes": len(data)})
    import io
    import paramiko  # optional dependency
    t = paramiko.Transport((cfg["host"], int(cfg.get("port", 22))))
    pkey = paramiko.RSAKey.from_private_key(io.StringIO(cfg["private_key"])) if cfg.get("private_key") else None
    t.connect(username=cfg["username"], password=cfg.get("password"), pkey=pkey)
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        with sftp.open(f"{cfg.get('path', '.').rstrip('/')}/{filename}", "w") as fh:
            fh.write(data)
    finally:
        t.close()
    return "sent"


def hubspot(token, row):
    """Managed CRM sync: upsert the company by domain/name and add a note with the brief."""
    if config.settings.dry_run_delivery:
        return _dry("hubspot", {"company": row.get("company_name"), "event": row.get("event_type")})
    s = session()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    props = {"name": row.get("company_name"), "city": row.get("ro_town"), "zip": row.get("ro_postcode"),
             "ledgerlight_company_number": row.get("company_number")}
    r = s.post("https://api.hubapi.com/crm/v3/objects/companies", headers=h, json={"properties": {k: v for k, v in props.items() if v}}, timeout=20)
    if r.status_code == 409:  # already exists: look it up
        q = s.post("https://api.hubapi.com/crm/v3/objects/companies/search", headers=h, timeout=20, json={
            "filterGroups": [{"filters": [{"propertyName": "name", "operator": "EQ", "value": row.get("company_name")}]}]}).json()
        cid = (q.get("results") or [{}])[0].get("id")
    else:
        r.raise_for_status()
        cid = r.json()["id"]
    body = f"{row.get('event_type')} ({row.get('event_date')}): {row.get('ai_brief') or ''} Next: {row.get('next_action') or ''}"
    s.post("https://api.hubapi.com/crm/v3/objects/notes", headers=h, timeout=20, json={
        "properties": {"hs_note_body": body, "hs_timestamp": db.now()},
        "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 190}]}]})
    return "sent"
