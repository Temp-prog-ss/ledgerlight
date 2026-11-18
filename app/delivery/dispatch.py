"""Send pending deliveries: instant alerts every minute, digests and file drops daily."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import brands, config, db, fields, plans, views
from app.pipeline import compliance
from app.delivery import senders

MAX_ATTEMPTS = 5


def _load(dlv):
    org = db.one("SELECT * FROM orgs WHERE id=?", (dlv["org_id"],))
    ch = db.one("SELECT * FROM channels WHERE id=?", (dlv["channel_id"],))
    ev = db.one("SELECT * FROM events WHERE id=?", (dlv["event_id"],))
    return org, ch, ev


def _row(org, ev):
    return views.project_events(org, [ev])[0]


def _subject(brand, row):
    who = row.get("company_name") or "A company"
    return f"[{brand.name}] {str(row.get('event_type', '')).capitalize()}: {who}"


def _text(brand, row):
    lines = [f"{str(row.get('event_type', '')).capitalize()}: {row.get('company_name') or ''}".strip(),
             row.get("ai_brief") or "", f"Next step: {row.get('next_action')}" if row.get("next_action") else "",
             f"Score: {row.get('risk_score')}" if row.get("risk_score") is not None else "",
             f"Source: {row.get('notice_url')}" if row.get("notice_url") else "",
             f"— {brand.name}, powered by Ledgerlight. Manage alerts: {config.settings.public_base_url}"]
    return "\n".join(x for x in lines if x)


def _html(brand, rows, title):
    items = "".join(
        f"<tr><td style='padding:12px;border-bottom:1px solid #eee'><b>{r.get('company_name') or ''}</b><br>"
        f"<span style='color:#666'>{str(r.get('event_type', '')).capitalize()} · {r.get('event_date') or ''}"
        f"{' · score ' + str(r['risk_score']) if r.get('risk_score') is not None else ''}</span>"
        f"<p style='margin:6px 0'>{r.get('ai_brief') or ''}</p>"
        f"{'<p style=margin:0><b>Next:</b> ' + r['next_action'] + '</p>' if r.get('next_action') else ''}"
        f"{'<a href=' + r['notice_url'] + '>Source notice</a>' if r.get('notice_url') else ''}</td></tr>" for r in rows)
    return (f"<div style='font-family:Arial,sans-serif;max-width:640px'><h2 style='color:{brand.accent}'>{brand.name}</h2>"
            f"<p>{title}</p><table style='width:100%;border-collapse:collapse'>{items}</table>"
            f"<p style='color:#888;font-size:12px'>Public data reused under the Open Government Licence. "
            f"You receive this because your organisation subscribes to {brand.name}.</p></div>")


def send_instant(limit=200):
    today = date.today().isoformat()
    sent = 0
    for d in db.all("SELECT * FROM deliveries WHERE status='pending' AND mode='instant' AND attempts<? "
                    "AND (not_before IS NULL OR not_before<=?) ORDER BY created_at LIMIT ?", (MAX_ATTEMPTS, today, limit)):
        org, ch, ev = _load(d)
        company = db.one("SELECT * FROM companies WHERE number=?", (ev["company_number"],)) if ev and ev.get("company_number") else None
        if not (org and ch and ev) or not ch["active"] or not compliance.deliverable(org, ev, company):
            db.execute("UPDATE deliveries SET status='skipped' WHERE id=?", (d["id"],))
            continue
        brand = brands.get(org["brand"])
        try:
            row = _row(org, ev)
            text = _text(brand, row)
            cfg = db.loads(ch["config"], {})
            if ch["kind"] == "email":
                senders.email(ch["target"], _subject(brand, row), _html(brand, [row], "New alert"), text, brand_name=brand.name)
            elif ch["kind"] == "slack":
                senders.slack(ch["target"], text)
            elif ch["kind"] == "teams":
                senders.teams(ch["target"], _subject(brand, row), text)
            elif ch["kind"] == "sms":
                senders.sms(ch["target"], f"{brand.name}: {row.get('event_type')} {row.get('company_name') or ''}. {row.get('next_action') or ''}")
            elif ch["kind"] == "hubspot":
                senders.hubspot(cfg.get("token") or ch["target"], row)
            db.execute("UPDATE deliveries SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?", (db.now(), d["id"]))
            views.bump_usage(org["id"], briefs=1)
            sent += 1
        except Exception as e:
            st = "failed" if d["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
            db.execute("UPDATE deliveries SET status=?, attempts=attempts+1, last_error=? WHERE id=?", (st, str(e)[:300], d["id"]))
    return sent


def _local_now(org, now_utc=None):
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    try:
        tz = ZoneInfo(org.get("timezone") or "Europe/London")
    except Exception:
        tz = ZoneInfo("Europe/London")
    return now_utc.astimezone(tz)


def digest_due(org, now_utc=None):
    """last_digest_at holds the customer's LOCAL date of the last digest."""
    local = _local_now(org, now_utc)
    if local.hour < int(org.get("digest_hour") or 7):
        return False
    last = org.get("last_digest_at")
    return not last or last[:10] < local.date().isoformat()


def send_digests(now_utc=None):
    """One email per org per day (and a CSV file drop on Pro+), from pending digest deliveries."""
    count = 0
    for org in db.all("SELECT * FROM orgs WHERE status IN ('trialing','active','past_due')"):
        if not digest_due(org, now_utc):
            continue
        try:
            count += _send_org_digest(org, now_utc)
        except Exception as e:  # one bad org never stops the others
            db.audit("worker", "digest_error", {"org": org["id"], "error": str(e)[:300]})
    return count


def _send_org_digest(org, now_utc=None):
        count = 0
        brand = brands.get(org["brand"])
        ent = plans.entitlements(org)
        today = date.today().isoformat()
        pending = db.all("SELECT * FROM deliveries WHERE org_id=? AND mode='digest' AND status='pending' "
                         "AND (not_before IS NULL OR not_before<=?)", (org["id"], today))
        by_channel = {}
        for d in pending:
            by_channel.setdefault(d["channel_id"], []).append(d)
        for cid, ds in by_channel.items():
            ch = db.one("SELECT * FROM channels WHERE id=?", (cid,))
            evs = []
            for d in ds:
                ev = db.one("SELECT * FROM events WHERE id=?", (d["event_id"],))
                co = db.one("SELECT * FROM companies WHERE number=?", (ev["company_number"],)) if ev and ev.get("company_number") else None
                if ch and ch["active"] and compliance.deliverable(org, ev, co):
                    evs.append(ev)
                else:
                    db.execute("UPDATE deliveries SET status='skipped' WHERE id=?", (d["id"],))
            if not evs:
                continue
            rows = sorted(views.project_events(org, [e for e in evs if e]), key=lambda r: -(r.get("risk_score") or 0))
            csv_text = views.to_csv(rows, [c[0] for c in fields.columns(ent["level"], org["brand"])])
            fname = f"{org['brand']}_{today}.csv"
            try:
                if ch["kind"] == "email":
                    title = f"Good morning. {len(rows)} new event{'s' if len(rows) != 1 else ''} match your alerts."
                    senders.email(ch["target"], f"[{brand.name}] Daily digest: {len(rows)} new", _html(brand, rows[:25], title),
                                  title + "\n\n" + "\n\n".join(_text(brand, r) for r in rows[:25]),
                                  attachments=[(fname, csv_text, "text/csv")], brand_name=brand.name)
                elif ch["kind"] == "sftp" and ent["file_drops"]:
                    senders.sftp_put(db.loads(ch["config"], {}), fname, csv_text)
                for d in ds:
                    db.execute("UPDATE deliveries SET status='sent', sent_at=? WHERE id=? AND status='pending'", (db.now(), d["id"]))
                count += 1
            except Exception as e:
                for d in ds:
                    db.execute("UPDATE deliveries SET attempts=attempts+1, last_error=? WHERE id=?", (str(e)[:300], d["id"]))
        db.execute("UPDATE orgs SET last_digest_at=? WHERE id=?", (_local_now(org, now_utc).date().isoformat(), org["id"]))
        return count


def run_export_jobs():
    """Bulk CSV exports requested from the dashboard."""
    done = 0
    for job in db.all("SELECT * FROM export_jobs WHERE status='queued' LIMIT 5"):
        org = db.one("SELECT * FROM orgs WHERE id=?", (job["org_id"],))
        p = db.loads(job["params"], {})
        try:
            rows = views.visible_events(org, p, limit=min(int(p.get("max_rows", 50000)), 100000))
            proj = views.project_events(org, rows)
            level = views.org_level(org)
            text = views.to_csv(proj, [c[0] for c in fields.columns(level, org["brand"])])
            # Stored in the database, not on disk: cloud servers lose their local disk on every deploy.
            db.execute("UPDATE export_jobs SET status='done', content=?, row_count=?, finished_at=? WHERE id=?",
                       (text, len(proj), db.now(), job["id"]))
            views.bump_usage(org["id"], exports=1, rows=len(proj))
            done += 1
        except Exception as e:
            db.execute("UPDATE export_jobs SET status='failed', error=? WHERE id=?", (str(e)[:300], job["id"]))
    return done


def purge_old(days=400):
    """Data retention: drop deliveries and personal-data events older than the retention window."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
    db.execute("UPDATE export_jobs SET content=NULL WHERE finished_at<?", ((date.today() - timedelta(days=30)).isoformat(),))
    db.execute("DELETE FROM events WHERE personal_data=1 AND published_at<?", (cutoff,))
