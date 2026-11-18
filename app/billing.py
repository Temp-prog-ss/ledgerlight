"""Stripe subscriptions over the REST API (no SDK needed).

Create one Product per plan and a monthly + annual Price for each, then set
STRIPE_PRICE_<PLAN>_<MONTHLY|ANNUAL>. Annual prices are 10 x monthly.
"""
import hashlib
import hmac
import json
import time

from app import config, db, plans
from app.sources.http import session

API = "https://api.stripe.com/v1"
STATUS = {"active": "active", "trialing": "trialing", "past_due": "past_due", "unpaid": "unpaid",
          "canceled": "canceled", "incomplete": "past_due", "incomplete_expired": "canceled", "paused": "past_due"}


def _post(path, data):
    r = session().post(API + path, data=data, auth=(config.settings.stripe_secret_key, ""), timeout=30)
    r.raise_for_status()
    return r.json()


def checkout_url(org, user_email, plan_id, cycle, success_url, cancel_url):
    price = plans.stripe_price_id(plan_id, cycle)
    if not price:
        raise ValueError(f"No Stripe price configured for {plan_id}/{cycle}")
    data = {"mode": "subscription", "line_items[0][price]": price, "line_items[0][quantity]": 1,
            "success_url": success_url, "cancel_url": cancel_url, "client_reference_id": org["id"],
            "metadata[org_id]": org["id"], "metadata[plan]": plan_id, "metadata[cycle]": cycle,
            "subscription_data[metadata][org_id]": org["id"], "allow_promotion_codes": "true",
            "billing_address_collection": "required", "tax_id_collection[enabled]": "true",
            "automatic_tax[enabled]": "true"}
    if org.get("stripe_customer_id"):
        data["customer"] = org["stripe_customer_id"]
    else:
        data["customer_email"] = user_email
    return _post("/checkout/sessions", data)["url"]


def portal_url(org, return_url):
    if not org.get("stripe_customer_id"):
        raise ValueError("No billing account yet")
    return _post("/billing_portal/sessions", {"customer": org["stripe_customer_id"], "return_url": return_url})["url"]


def verify_signature(payload: bytes, header: str, secret: str, tolerance=300, now=None):
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    t, sig = parts.get("t"), parts.get("v1")
    if not t or not sig:
        return False
    if abs((now or time.time()) - int(t)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def handle_event(evt):
    """Apply a verified Stripe webhook event to our orgs table."""
    typ, obj = evt.get("type"), (evt.get("data") or {}).get("object") or {}
    if typ == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("org_id")
        md = obj.get("metadata") or {}
        db.execute("UPDATE orgs SET stripe_customer_id=?, stripe_subscription_id=?, plan=COALESCE(?,plan), "
                   "billing_cycle=COALESCE(?,billing_cycle), status='active' WHERE id=?",
                   (obj.get("customer"), obj.get("subscription"), md.get("plan"), md.get("cycle"), org_id))
        db.audit("stripe", "checkout_completed", {"org": org_id, "plan": md.get("plan")})
    elif typ in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        org = db.one("SELECT * FROM orgs WHERE stripe_subscription_id=? OR stripe_customer_id=?", (obj.get("id"), obj.get("customer")))
        if not org:
            org_id = (obj.get("metadata") or {}).get("org_id")
            org = db.one("SELECT * FROM orgs WHERE id=?", (org_id,)) if org_id else None
        if not org:
            return
        current_sub = org.get("stripe_subscription_id")
        if current_sub and obj.get("id") != current_sub and typ != "customer.subscription.created":
            db.audit("stripe", "ignored_old_subscription_event", {"org": org["id"], "sub": obj.get("id"), "type": typ})
            return
        created = int(evt.get("created") or 0)
        last = int(db.get_state(f"stripe_last_event:{org['id']}", 0) or 0)
        if created and created < last:
            return  # an older event arriving late must not overwrite newer state
        if created:
            db.set_state(f"stripe_last_event:{org['id']}", created)
        items = ((obj.get("items") or {}).get("data") or [{}])
        plan_id, cycle = plans.plan_for_price(((items[0] or {}).get("price") or {}).get("id"))
        status = "canceled" if typ.endswith("deleted") else STATUS.get(obj.get("status"), "past_due")
        db.execute("UPDATE orgs SET status=?, plan=COALESCE(?,plan), billing_cycle=COALESCE(?,billing_cycle), "
                   "stripe_subscription_id=?, stripe_customer_id=COALESCE(stripe_customer_id,?) WHERE id=?",
                   (status, plan_id, cycle, obj.get("id"), obj.get("customer"), org["id"]))
        db.audit("stripe", typ, {"org": org["id"], "status": status, "plan": plan_id})
    elif typ == "invoice.payment_failed":
        db.execute("UPDATE orgs SET status='past_due' WHERE stripe_customer_id=?", (obj.get("customer"),))


def parse_webhook(payload: bytes, header: str):
    secret = config.settings.stripe_webhook_secret
    if not secret or not verify_signature(payload, header, secret):
        raise PermissionError("Invalid Stripe signature")
    return json.loads(payload)
