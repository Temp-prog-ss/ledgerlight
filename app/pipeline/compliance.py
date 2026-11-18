"""UK GDPR / PECR guard-rails applied before anything reaches a customer.

- Personal-data events (deceased estates, unclaimed estates) only go to brands that
  allow them, only to vetted orgs, and only after a cooling-off period.
- Anyone on the suppression list (company number, name, email or phone) is never sent.
- Sole traders / partnerships are flagged as 'individual' subscribers so customers
  follow the stricter PECR consent rules for email and SMS.
- Individual (personal) insolvency notices are never turned into leads.
"""
from datetime import date, datetime, timedelta

from app import brands, config, db

NEVER_ROUTE = {"bankruptcy", "iva", "dro"}


def restricted_until(event):
    if event["event_type"] in ("deceased_estate", "unclaimed_estate"):
        pub = (event.get("published_at") or db.now())[:10]
        try:
            d = date.fromisoformat(pub)
        except ValueError:
            d = date.today()
        return (d + timedelta(days=config.settings.probate_cooling_off_days)).isoformat()
    return None


SUPPRESSION_KINDS = ("company", "company_name", "email", "phone")


def normalise(kind, value):
    v = (value or "").strip()
    if kind == "company":
        from app.sources.companies_house import normalise_number
        return normalise_number(v)
    if kind == "company_name":
        return " ".join(v.upper().split())
    if kind == "phone":
        return "".join(ch for ch in v if ch.isdigit() or ch == "+")
    return v.lower()


def suppressed(kind, value):
    v = normalise(kind, value)
    return bool(v and db.one("SELECT id FROM suppressions WHERE kind=? AND value=?", (kind, v)))


def suppress(kind, value, reason=None):
    """Record an objection and apply it to everything we already hold."""
    v = normalise(kind, value)
    if not v:
        raise ValueError("empty value")
    if not db.one("SELECT id FROM suppressions WHERE kind=? AND value=?", (kind, v)):
        db.insert("suppressions", {"id": db.new_id("sup_"), "kind": kind, "value": v, "reason": reason, "created_at": db.now()})
    numbers = []
    if kind == "company":
        numbers = [v]
    elif kind == "company_name":
        numbers = [r["number"] for r in db.all("SELECT number FROM companies WHERE UPPER(name)=?", (v,))]
    for n in numbers:
        db.execute("UPDATE events SET status='suppressed' WHERE company_number=?", (n,))
        db.execute("UPDATE deliveries SET status='skipped' WHERE status='pending' AND event_id IN (SELECT id FROM events WHERE company_number=?)", (n,))
    return v


def is_suppressed(event, company):
    return suppressed("company", event.get("company_number")) or suppressed("company_name", (company or {}).get("name"))


def subscriber_type(company):
    t = ((company or {}).get("company_type") or "").lower()
    if not t:
        return "check"
    return "corporate" if t.startswith(("ltd", "plc", "llp", "private", "public", "limited")) else "individual"


def can_route(brand_id, org, event):
    """Is this org allowed to receive this event at all?"""
    b = brands.get(brand_id)
    if event["event_type"] in NEVER_ROUTE:
        return False
    if event.get("personal_data") and not b.allows_personal_data:
        return False
    if b.vetted_only and not org.get("vetted"):
        return False
    return True


def embargoed(event, today=None):
    ru = event.get("restricted_until")
    return bool(ru and ru > (today or date.today().isoformat()))


def deliverable(org, event, company=None):
    """Final check at send time: rules may have changed since the alert was queued."""
    from app import plans
    if not org or not event or event.get("status") != "processed":
        return False
    if not plans.entitlements(org)["active"]:
        return False
    if not can_route(org["brand"], org, event) or embargoed(event):
        return False
    return not is_suppressed(event, company)
