"""In-house HTTP API for the nine websites' sign-up, dashboard and admin.

This API is NOT sold. There are no customer API keys: every call needs a
logged-in user's session token, and CORS only allows our own brand sites.
"""
import csv
import io
import re
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from app import billing, brands, config, db, fields, plans, security, views
from app.pipeline import compliance

auth_throttle = security.Throttle(limit=10, window=60)
public_throttle = security.Throttle(limit=30, window=60)
EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
from app.sources.companies_house import REGIONS as _REGIONS  # noqa: E402
REGION_NAMES = set(_REGIONS)


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


async def body(request, model):
    try:
        return model(**(await request.json()))
    except ValidationError as e:
        raise ApiError(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    except ValueError:
        raise ApiError(400, "Body must be JSON")


def current(request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise ApiError(401, "Sign in required")
    try:
        claims = security.read_token(h[7:])
    except Exception:
        raise ApiError(401, "Session expired, please sign in again")
    user = db.one("SELECT * FROM users WHERE id=?", (claims["sub"],))
    org = db.one("SELECT * FROM orgs WHERE id=?", (claims["org"],))
    if not user or not org or user["org_id"] != org["id"]:
        raise ApiError(401, "Account not found")
    return user, org


def require_staff(request):
    user, org = current(request)
    if not security.is_staff(user):
        raise ApiError(403, "Staff only")
    return user, org


def require_active(org):
    ent = plans.entitlements(org)
    if not ent["active"]:
        raise ApiError(402, "Your subscription is not active")
    return ent


# ---------------- models ----------------

class Signup(BaseModel):
    email: str
    password: str = Field(min_length=10)
    name: str = Field(min_length=1, max_length=120)
    org_name: str = Field(min_length=1, max_length=160)
    brand: str = "ledgerlight"
    plan: str = "solo"

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if not EMAIL_RX.match(v):
            raise ValueError("invalid email")
        return v.lower().strip()

    @field_validator("brand")
    @classmethod
    def _brand(cls, v):
        if v not in brands.BRANDS:
            raise ValueError("unknown brand")
        return v

    regions: list[str] = []

    @field_validator("plan")
    @classmethod
    def _plan(cls, v):
        if v not in ("solo", "team", "pro"):  # Enterprise is arranged with sales
            raise ValueError("choose solo, team or pro")
        return v

    @field_validator("regions")
    @classmethod
    def _regions(cls, v):
        bad = [r for r in v if r not in REGION_NAMES]
        if bad:
            raise ValueError(f"unknown regions: {bad}")
        return v


class Login(BaseModel):
    email: str
    password: str


class OrgUpdate(BaseModel):
    name: str | None = None
    regions: list[str] | None = None
    digest_hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v is not None:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError("unknown time zone, e.g. Europe/London")
        return v

    @field_validator("regions")
    @classmethod
    def _regions(cls, v):
        if v is not None and any(r not in REGION_NAMES for r in v):
            raise ValueError(f"regions must be from: {sorted(REGION_NAMES)}")
        return v


class Member(BaseModel):
    email: str
    name: str
    password: str = Field(min_length=10)


class WatchlistIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ItemIn(BaseModel):
    company_number: str
    label: str | None = None
    amount_owed: int | None = None
    credit_limit: int | None = None


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    event_types: list[str] = []
    regions: list[str] = []
    sic_prefixes: list[str] = []
    keywords: list[str] = []
    min_score: int = Field(default=0, ge=0, le=100)
    watchlist_only: bool = False
    instant: bool = True
    digest: bool = True
    channel_ids: list[str] = []
    active: bool = True

    @field_validator("event_types")
    @classmethod
    def _types(cls, v):
        bad = [t for t in v if t not in brands.ALL_TYPES]
        if bad:
            raise ValueError(f"unknown event types: {bad}")
        return v


class ChannelIn(BaseModel):
    kind: str
    target: str = Field(min_length=3, max_length=500)
    config: dict = {}


class CheckoutIn(BaseModel):
    plan: str
    cycle: str = "monthly"
    success_url: str
    cancel_url: str


class BulkIn(BaseModel):
    type: str | None = None
    region: str | None = None
    min_score: int | None = None
    since: str | None = None
    q: str | None = None
    watchlist: bool = False
    max_rows: int = Field(default=50000, ge=1, le=100000)


class ObjectionIn(BaseModel):
    kind: str
    value: str = Field(min_length=2, max_length=200)
    reason: str | None = None
    contact_email: str | None = None


# ---------------- auth & account ----------------

def _client_ip(request):
    """Client address for security-sensitive throttling.

    Never trust X-Forwarded-For: clients can set it themselves, and Render's proxy only appends to it.
    Behind Render (LL_TRUST_PROXY=true) Cloudflare sits in front and sets True-Client-IP / CF-Connecting-IP
    from the real connection, overwriting anything the client sent. Otherwise use the transport peer.
    """
    if config.settings.trust_proxy:
        for h in ("true-client-ip", "cf-connecting-ip"):
            v = (request.headers.get(h) or "").strip()
            if v:
                return v
    return request.client.host if request.client else "unknown"


async def signup(request):
    if not auth_throttle.allow(_client_ip(request)):
        raise ApiError(429, "Too many attempts, try again in a minute")
    s = await body(request, Signup)
    if db.one("SELECT id FROM users WHERE email=?", (s.email,)):
        raise ApiError(409, "An account with this email already exists")
    org = {"id": db.new_id("org_"), "name": s.org_name, "brand": s.brand, "plan": s.plan, "billing_cycle": "monthly",
           "status": "trialing", "trial_ends_at": (date.today() + timedelta(days=plans.TRIAL_DAYS)).isoformat(),
           "stripe_customer_id": None, "stripe_subscription_id": None, "vetted": 0, "regions": db.dumps(s.regions[:1] if s.plan == "solo" else s.regions),
           "timezone": "Europe/London", "digest_hour": 7, "last_digest_at": None, "created_at": db.now()}
    user = {"id": db.new_id("usr_"), "org_id": org["id"], "email": s.email, "password_hash": security.hash_password(s.password),
            "name": s.name, "role": "owner", "created_at": db.now(), "last_login": db.now()}
    db.insert("orgs", org)
    db.insert("users", user)
    ch = {"id": db.new_id("chn_"), "org_id": org["id"], "kind": "email", "target": s.email, "config": db.dumps({}), "active": 1, "created_at": db.now()}
    db.insert("channels", ch)
    db.insert("alert_rules", {"id": db.new_id("rul_"), "org_id": org["id"], "name": "Everything for my product", "event_types": db.dumps([]),
                              "regions": db.dumps([]), "sic_prefixes": db.dumps([]), "keywords": db.dumps([]), "min_score": 0,
                              "watchlist_only": 0, "instant": 1, "digest": 1, "channel_ids": db.dumps([ch["id"]]), "active": 1, "created_at": db.now()})
    db.insert("watchlists", {"id": db.new_id("wl_"), "org_id": org["id"], "name": "My watchlist", "created_at": db.now()})
    db.audit(user["id"], "signup", {"brand": s.brand, "plan": s.plan})
    return ok({"token": security.issue_token(user, org), "org_id": org["id"], "trial_ends_at": org["trial_ends_at"]}, 201)


async def login(request):
    if not auth_throttle.allow(_client_ip(request)):
        raise ApiError(429, "Too many attempts, try again in a minute")
    s = await body(request, Login)
    user = db.one("SELECT * FROM users WHERE email=?", (s.email.lower().strip(),))
    if not user or not security.verify_password(s.password, user["password_hash"]):
        raise ApiError(401, "Email or password is wrong")
    org = db.one("SELECT * FROM orgs WHERE id=?", (user["org_id"],))
    db.execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), user["id"]))
    return ok({"token": security.issue_token(user, org)})


def _org_out(org):
    ent = plans.entitlements(org)
    return {"id": org["id"], "name": org["name"], "brand": org["brand"], "plan": org["plan"], "billing_cycle": org["billing_cycle"],
            "status": org["status"], "trial_ends_at": org["trial_ends_at"], "vetted": bool(org["vetted"]),
            "regions": db.loads(org["regions"], []), "digest_hour": org["digest_hour"], "timezone": org["timezone"],
            "entitlements": {k: (sorted(v) if isinstance(v, set) else v) for k, v in ent.items()},
            "level_name": plans.LEVELS[ent["level"]]}


async def me(request):
    user, org = current(request)
    month = date.today().strftime("%Y-%m")
    usage = db.one("SELECT * FROM usage WHERE org_id=? AND month=?", (org["id"], month)) or {}
    return ok({"user": {k: user[k] for k in ("id", "email", "name", "role")}, "org": _org_out(org),
               "usage": {k: usage.get(k, 0) for k in ("briefs", "exports", "export_rows")}})


async def update_org(request):
    user, org = current(request)
    if user["role"] != "owner":
        raise ApiError(403, "Only the account owner can change settings")
    s = await body(request, OrgUpdate)
    ent = plans.entitlements(org)
    sets = {}
    if s.name:
        sets["name"] = s.name
    if s.regions is not None:
        if ent["regions"] and len(s.regions) > ent["regions"]:
            raise ApiError(402, f"Your plan covers {ent['regions']} region(s)")
        sets["regions"] = db.dumps(s.regions)
    if s.digest_hour is not None:
        sets["digest_hour"] = s.digest_hour
    if s.timezone:
        sets["timezone"] = s.timezone
    if sets:
        db.execute(f"UPDATE orgs SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), org["id"]))
    return ok(_org_out(db.one("SELECT * FROM orgs WHERE id=?", (org["id"],))))


async def add_member(request):
    user, org = current(request)
    if user["role"] != "owner":
        raise ApiError(403, "Only the account owner can add people")
    s = await body(request, Member)
    ent = require_active(org)
    count = db.one("SELECT COUNT(*) AS n FROM users WHERE org_id=?", (org["id"],))["n"]
    if ent["seats"] and count >= ent["seats"]:
        raise ApiError(402, f"Your plan includes {ent['seats']} seat(s)")
    if db.one("SELECT id FROM users WHERE email=?", (s.email.lower(),)):
        raise ApiError(409, "That email already has an account")
    u = {"id": db.new_id("usr_"), "org_id": org["id"], "email": s.email.lower(), "password_hash": security.hash_password(s.password),
         "name": s.name, "role": "member", "created_at": db.now(), "last_login": None}
    db.insert("users", u)
    return ok({"id": u["id"], "email": u["email"]}, 201)


# ---------------- events & companies ----------------

def _filters(request):
    q = request.query_params
    return {k: q.get(k) for k in ("type", "region", "min_score", "since", "q") if q.get(k)} | {"watchlist": q.get("watchlist") in ("1", "true")}


async def list_events(request):
    user, org = current(request)
    require_active(org)
    limit = min(int(request.query_params.get("limit", 50)), 200)
    offset = int(request.query_params.get("offset", 0))
    rows = views.visible_events(org, _filters(request), limit, offset)
    level = views.org_level(org)
    return ok({"level": level, "level_name": plans.LEVELS[level],
               "columns": [{"key": c[0], "label": c[1]} for c in fields.columns(level, org["brand"])],
               "events": views.project_events(org, rows)})


async def get_event(request):
    user, org = current(request)
    require_active(org)
    e = db.one("SELECT * FROM events WHERE id=? AND status='processed'", (request.path_params["id"],))
    if not e or not compliance.can_route(org["brand"], org, e) or compliance.embargoed(e):
        raise ApiError(404, "Not found")
    return ok(views.project_events(org, [e])[0])


async def get_company(request):
    user, org = current(request)
    ent = require_active(org)
    num = request.path_params["number"].upper()
    co = db.one("SELECT * FROM companies WHERE number=?", (num,))
    if not co or compliance.suppressed("company", num) or compliance.suppressed("company_name", co["name"]):
        raise ApiError(404, "Company not found")
    evs = [e for e in db.all("SELECT * FROM events WHERE company_number=? AND status='processed' ORDER BY published_at DESC LIMIT 100", (num,))
           if compliance.can_route(org["brand"], org, e) and not compliance.embargoed(e)]
    timeline = [{"date": (e.get("effective_date") or e["published_at"] or "")[:10], "event_type": e["event_type"],
                 "risk_score": e["risk_score"] if ent["level"] >= 3 else None, "url": db.loads(e["data"], {}).get("url")} for e in evs]
    stub = {"id": "", "event_type": "profile", "data": "{}", "company_number": num, "title": co["name"]}
    profile = fields.project(views.ctx(stub, co, views.watch_items(org["id"]).get(num), None, ent["level"]), ent["level"], org["brand"])
    for k in ("event_id", "event_type", "event_date", "notice_url", "ai_brief", "next_action", "risk_score", "reasons", "relevance"):
        profile.pop(k, None)
    return ok({"company": profile, "timeline": timeline})


# ---------------- watchlists ----------------

def _own_watchlist(org, wid):
    w = db.one("SELECT * FROM watchlists WHERE id=? AND org_id=?", (wid, org["id"]))
    if not w:
        raise ApiError(404, "Watchlist not found")
    return w


def _watch_count(org):
    return db.one("SELECT COUNT(*) AS n FROM watchlist_items i JOIN watchlists w ON w.id=i.watchlist_id WHERE w.org_id=?", (org["id"],))["n"]


async def watchlists(request):
    user, org = current(request)
    if request.method == "POST":
        s = await body(request, WatchlistIn)
        w = {"id": db.new_id("wl_"), "org_id": org["id"], "name": s.name, "created_at": db.now()}
        db.insert("watchlists", w)
        return ok(w, 201)
    rows = db.all("SELECT w.*, (SELECT COUNT(*) FROM watchlist_items i WHERE i.watchlist_id=w.id) AS items FROM watchlists w WHERE org_id=?", (org["id"],))
    return ok({"watchlists": rows, "limit": plans.entitlements(org)["watchlist_limit"], "used": _watch_count(org)})


async def watchlist_detail(request):
    user, org = current(request)
    w = _own_watchlist(org, request.path_params["id"])
    if request.method == "DELETE":
        db.execute("DELETE FROM watchlist_items WHERE watchlist_id=?", (w["id"],))
        db.execute("DELETE FROM watchlists WHERE id=?", (w["id"],))
        return ok({"deleted": True})
    items = db.all("SELECT i.*, c.name, c.status FROM watchlist_items i LEFT JOIN companies c ON c.number=i.company_number WHERE watchlist_id=? ORDER BY i.created_at", (w["id"],))
    return ok({"watchlist": w, "items": items})


def _add_item(org, w, num, label=None, owed=None, limit=None):
    from app.sources.companies_house import normalise_number
    num = normalise_number(num)
    if not num:
        return False
    if db.one("SELECT id FROM watchlist_items WHERE watchlist_id=? AND company_number=?", (w["id"], num)):
        db.execute("UPDATE watchlist_items SET label=COALESCE(?,label), amount_owed=COALESCE(?,amount_owed), credit_limit=COALESCE(?,credit_limit) "
                   "WHERE watchlist_id=? AND company_number=?", (label, owed, limit, w["id"], num))
        return True
    cap = plans.entitlements(org)["watchlist_limit"]
    if cap and _watch_count(org) >= cap:
        raise ApiError(402, f"Your plan allows {cap} watched companies")
    db.insert("watchlist_items", {"id": db.new_id("wi_"), "watchlist_id": w["id"], "company_number": num, "label": label,
                                  "amount_owed": owed, "credit_limit": limit, "extra": None, "created_at": db.now()})
    return True


async def watchlist_items(request):
    user, org = current(request)
    require_active(org)
    w = _own_watchlist(org, request.path_params["id"])
    s = await body(request, ItemIn)
    _add_item(org, w, s.company_number, s.label, s.amount_owed, s.credit_limit)
    return ok({"ok": True}, 201)


async def watchlist_item_delete(request):
    user, org = current(request)
    w = _own_watchlist(org, request.path_params["id"])
    db.execute("DELETE FROM watchlist_items WHERE watchlist_id=? AND company_number=?", (w["id"], request.path_params["number"].upper()))
    return ok({"deleted": True})


async def watchlist_import(request):
    """CSV upload: company_number[,label,amount_owed,credit_limit] — the column names are flexible."""
    user, org = current(request)
    require_active(org)
    w = _own_watchlist(org, request.path_params["id"])
    text = (await request.body()).decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    keymap = {k: re.sub(r"[^a-z]", "", (k or "").lower()) for k in reader.fieldnames or []}

    def pick(row, *names):
        for k, n in keymap.items():
            if n in names and row.get(k):
                return row[k].strip()
        return None

    added, skipped = 0, 0
    for row in reader:
        num = pick(row, "companynumber", "companyno", "number", "crn", "registrationnumber")
        money = lambda v: int(float(re.sub(r"[£,\s]", "", v))) if v and re.sub(r"[£,\s.]", "", v).isdigit() else None  # noqa: E731
        if num and _add_item(org, w, num, pick(row, "label", "name", "companyname", "units"),
                             money(pick(row, "amountowed", "owed", "balance", "rent", "retention")), money(pick(row, "creditlimit", "limit"))):
            added += 1
        else:
            skipped += 1
    return ok({"added": added, "skipped": skipped})


# ---------------- alert rules & channels ----------------

async def rules(request):
    user, org = current(request)
    if request.method == "POST":
        s = await body(request, RuleIn)
        r = {"id": db.new_id("rul_"), "org_id": org["id"], "name": s.name, "event_types": db.dumps(s.event_types),
             "regions": db.dumps(s.regions), "sic_prefixes": db.dumps(s.sic_prefixes), "keywords": db.dumps(s.keywords),
             "min_score": s.min_score, "watchlist_only": int(s.watchlist_only), "instant": int(s.instant), "digest": int(s.digest),
             "channel_ids": db.dumps(s.channel_ids), "active": int(s.active), "created_at": db.now()}
        db.insert("alert_rules", r)
        return ok(r, 201)
    return ok({"rules": db.all("SELECT * FROM alert_rules WHERE org_id=?", (org["id"],))})


async def rule_detail(request):
    user, org = current(request)
    r = db.one("SELECT * FROM alert_rules WHERE id=? AND org_id=?", (request.path_params["id"], org["id"]))
    if not r:
        raise ApiError(404, "Rule not found")
    if request.method == "DELETE":
        db.execute("DELETE FROM alert_rules WHERE id=?", (r["id"],))
        return ok({"deleted": True})
    s = await body(request, RuleIn)
    db.execute("UPDATE alert_rules SET name=?, event_types=?, regions=?, sic_prefixes=?, keywords=?, min_score=?, watchlist_only=?, "
               "instant=?, digest=?, channel_ids=?, active=? WHERE id=?",
               (s.name, db.dumps(s.event_types), db.dumps(s.regions), db.dumps(s.sic_prefixes), db.dumps(s.keywords), s.min_score,
                int(s.watchlist_only), int(s.instant), int(s.digest), db.dumps(s.channel_ids), int(s.active), r["id"]))
    return ok({"updated": True})


def _check_target(kind, target):
    if kind == "email" and not EMAIL_RX.match(target):
        raise ApiError(422, "Enter a valid email address")
    if kind == "slack" and not target.startswith("https://hooks.slack.com/"):
        raise ApiError(422, "Use a Slack incoming-webhook URL")
    if kind == "teams" and not re.match(r"^https://[a-z0-9.-]+\.(webhook\.office|logic\.azure|powerplatform)\.", target):
        raise ApiError(422, "Use a Microsoft Teams workflow webhook URL")
    if kind == "sms" and not re.match(r"^\+44\d{9,10}$", target):
        raise ApiError(422, "Use a UK mobile in +44 format")


async def channels(request):
    user, org = current(request)
    if request.method == "POST":
        s = await body(request, ChannelIn)
        ent = require_active(org)
        if s.kind not in ent["channels"]:
            raise ApiError(402, f"{s.kind} delivery is not included in your plan")
        _check_target(s.kind, s.target)
        c = {"id": db.new_id("chn_"), "org_id": org["id"], "kind": s.kind, "target": s.target, "config": db.dumps(s.config),
             "active": 1, "created_at": db.now()}
        db.insert("channels", c)
        return ok({k: v for k, v in c.items() if k != "config"}, 201)
    rows = db.all("SELECT id, kind, target, active, created_at FROM channels WHERE org_id=?", (org["id"],))
    return ok({"channels": rows})


async def channel_delete(request):
    user, org = current(request)
    db.execute("DELETE FROM channels WHERE id=? AND org_id=?", (request.path_params["id"], org["id"]))
    return ok({"deleted": True})


# ---------------- exports ----------------

def _csv(text, name):
    return Response(text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def export_events(request):
    user, org = current(request)
    ent = require_active(org)
    allow = views.export_allowance(org)
    if allow["max_now"] <= 0:
        raise ApiError(402, "You have used this month's export allowance. Upgrade your plan for more rows.")
    rows = views.visible_events(org, _filters(request), limit=allow["max_now"])
    proj = views.project_events(org, rows)
    views.bump_usage(org["id"], exports=1, rows=len(proj))
    cols = [c[0] for c in fields.columns(ent["level"], org["brand"])]
    return _csv(views.to_csv(proj, cols), f"{org['brand']}_events_{date.today()}.csv")


async def export_fields(request):
    brand = request.query_params.get("brand", "ledgerlight")
    if brand not in brands.BRANDS:
        raise ApiError(404, "Unknown brand")
    rows = fields.dictionary_rows(brand)
    return _csv(views.to_csv(rows, ["field_key", "label", "description", "minimum_level"]), f"{brand}_fields.csv")


async def export_watchlist(request):
    user, org = current(request)
    w = _own_watchlist(org, request.path_params["id"])
    items = db.all("SELECT i.company_number, c.name AS company_name, c.status, i.label, i.amount_owed, i.credit_limit "
                   "FROM watchlist_items i LEFT JOIN companies c ON c.number=i.company_number WHERE watchlist_id=?", (w["id"],))
    return _csv(views.to_csv(items, ["company_number", "company_name", "status", "label", "amount_owed", "credit_limit"]), f"watchlist_{w['id']}.csv")


async def bulk_export(request):
    user, org = current(request)
    require_active(org)
    s = await body(request, BulkIn)
    allow = views.export_allowance(org)
    if allow["max_now"] <= 0:
        raise ApiError(402, "You have used this month's export allowance. Upgrade your plan for more rows.")
    s.max_rows = min(s.max_rows, allow["remaining_month"] if allow["remaining_month"] is not None else s.max_rows)
    job = {"id": db.new_id("exp_"), "org_id": org["id"], "params": db.dumps(s.model_dump()), "status": "queued",
           "file_path": None, "row_count": None, "error": None, "created_at": db.now(), "finished_at": None}
    db.insert("export_jobs", job)
    return ok({"id": job["id"], "status": "queued"}, 202)


async def bulk_status(request):
    user, org = current(request)
    j = db.one("SELECT id, status, row_count, error, created_at, finished_at FROM export_jobs WHERE id=? AND org_id=?",
               (request.path_params["id"], org["id"]))
    if not j:
        raise ApiError(404, "Export not found")
    return ok(j)


async def bulk_download(request):
    user, org = current(request)
    j = db.one("SELECT * FROM export_jobs WHERE id=? AND org_id=? AND status='done'", (request.path_params["id"], org["id"]))
    if not j:
        raise ApiError(404, "Export not ready")
    if j.get("content") is not None:
        text = j["content"]
    elif j.get("file_path") and Path(j["file_path"]).exists():  # older exports written to disk
        text = Path(j["file_path"]).read_text(encoding="utf-8")
    else:
        raise ApiError(410, "This export has expired. Please run it again.")
    return _csv(text, f"{org['brand']}_bulk_{j['id']}.csv")


# ---------------- public ----------------

async def health(request):
    db.one("SELECT 1 AS ok")
    return ok({"ok": True, "time": db.now()})


async def public_plans(request):
    return ok({"plans": [{"id": pid, "name": p["name"], "monthly_gbp": p["monthly_gbp"], "annual_gbp": plans.annual_gbp(pid),
                          "level": p["level"], "level_name": plans.LEVELS[p["level"]], "watchlist_limit": p["watchlist_limit"],
                          "channels": sorted(p["channels"]), "trial_days": plans.TRIAL_DAYS} for pid, p in plans.PLANS.items()]})


async def public_risks(request):
    brand = request.query_params.get("brand", "")
    rows = db.all("SELECT id, brand, title, why, watch, fact, source_url, plan_note, updated_at FROM risk_factors WHERE brand=? ORDER BY id", (brand,))
    return ok({"brand": brand, "risks": rows})


async def object_request(request):
    """Right to object: anyone can ask us to stop processing a company/person name. Honoured across all brands."""
    if not public_throttle.allow(_client_ip(request)):
        raise ApiError(429, "Too many requests")
    s = await body(request, ObjectionIn)
    if s.kind not in ("company", "company_name", "email", "phone"):
        raise ApiError(422, "kind must be company, company_name, email or phone")
    try:
        compliance.suppress(s.kind, s.value, s.reason or "objection")
    except ValueError:
        raise ApiError(422, "Enter a value")
    db.audit("public", "objection", {"kind": s.kind, "contact": s.contact_email})
    return ok({"received": True, "message": "We have stopped using this in all our products. This takes effect within 72 hours."}, 201)


# ---------------- billing ----------------

async def checkout(request):
    user, org = current(request)
    s = await body(request, CheckoutIn)
    if s.plan not in ("solo", "team", "pro") or s.cycle not in ("monthly", "annual"):
        raise ApiError(422, "Choose solo, team or pro, monthly or annual")
    try:
        url = billing.checkout_url(org, user["email"], s.plan, s.cycle, s.success_url, s.cancel_url)
    except ValueError as e:
        raise ApiError(503, str(e))
    return ok({"url": url})


async def portal(request):
    user, org = current(request)
    try:
        return ok({"url": billing.portal_url(org, request.query_params.get("return_url", config.settings.public_base_url))})
    except ValueError as e:
        raise ApiError(400, str(e))


async def stripe_webhook(request):
    payload = await request.body()
    try:
        evt = billing.parse_webhook(payload, request.headers.get("stripe-signature", ""))
    except PermissionError:
        raise ApiError(400, "Bad signature")
    billing.handle_event(evt)
    return ok({"received": True})


# ---------------- admin (staff only) ----------------

async def admin_stats(request):
    require_staff(request)
    q = lambda sql, p=(): db.one(sql, p)["n"]  # noqa: E731
    by_brand = db.all("SELECT brand, status, COUNT(*) AS n FROM orgs GROUP BY brand, status ORDER BY brand")
    mrr = sum((plans.PLANS[o["plan"]]["monthly_gbp"] or 0) for o in db.all("SELECT plan FROM orgs WHERE status='active'"))
    return ok({"events_total": q("SELECT COUNT(*) AS n FROM events"),
               "events_today": q("SELECT COUNT(*) AS n FROM events WHERE created_at>=?", (date.today().isoformat(),)),
               "events_by_status": db.all("SELECT status, COUNT(*) AS n FROM events GROUP BY status"),
               "deliveries": db.all("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"),
               "orgs_by_brand": by_brand, "approx_mrr_gbp": mrr,
               "risk_factors_needing_review": q("SELECT COUNT(*) AS n FROM risk_factors WHERE needs_review=1"),
               "ingest_state": db.all("SELECT * FROM ingest_state ORDER BY key")})


async def admin_orgs(request):
    require_staff(request)
    return ok({"orgs": db.all("SELECT id, name, brand, plan, status, vetted, created_at FROM orgs ORDER BY created_at DESC LIMIT 500")})


async def admin_org_update(request):
    staff, _ = require_staff(request)
    data = await request.json()
    allowed = {}
    if "vetted" in data:
        if not isinstance(data["vetted"], bool):
            raise ApiError(422, "vetted must be true or false")
        allowed["vetted"] = int(data["vetted"])
    if "plan" in data:
        if data["plan"] not in plans.PLANS:
            raise ApiError(422, "unknown plan")
        allowed["plan"] = data["plan"]
    if "status" in data:
        if data["status"] not in ("trialing", "active", "past_due", "canceled", "unpaid"):
            raise ApiError(422, "unknown status")
        allowed["status"] = data["status"]
    if allowed:
        db.execute(f"UPDATE orgs SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), request.path_params["id"]))
        db.audit(staff["id"], "admin_org_update", {"org": request.path_params["id"], **allowed})
    return ok({"updated": bool(allowed)})


async def admin_risks(request):
    staff, _ = require_staff(request)
    if request.method == "PATCH":
        data = await request.json()
        allowed = {k: data[k] for k in ("title", "why", "watch", "fact", "source_url", "plan_note") if k in data}
        allowed["needs_review"], allowed["updated_at"] = 0, db.now()
        db.execute(f"UPDATE risk_factors SET {', '.join(f'{k}=?' for k in allowed)} WHERE id=?", (*allowed.values(), request.path_params["id"]))
        db.audit(staff["id"], "risk_factor_update", {"id": request.path_params["id"]})
        return ok({"updated": True})
    only = request.query_params.get("needs_review") in ("1", "true")
    return ok({"risks": db.all("SELECT * FROM risk_factors" + (" WHERE needs_review=1" if only else "") + " ORDER BY brand, id")})


async def admin_suppress(request):
    staff, _ = require_staff(request)
    if request.method == "GET":
        return ok({"suppressions": db.all("SELECT * FROM suppressions ORDER BY created_at DESC LIMIT 1000")})
    data = await request.json()
    if data.get("kind") not in compliance.SUPPRESSION_KINDS:
        raise ApiError(422, f"kind must be one of {compliance.SUPPRESSION_KINDS}")
    try:
        v = compliance.suppress(data["kind"], data.get("value"), data.get("reason"))
    except ValueError:
        raise ApiError(422, "Enter a value")
    return ok({"ok": True, "value": v}, 201)


async def admin_run(request):
    """Run a worker job now (useful in support and in staging)."""
    require_staff(request)
    from app import worker
    job = request.path_params["job"]
    if job not in worker.JOBS:
        raise ApiError(404, f"Unknown job. Choose from: {', '.join(worker.JOBS)}")
    return ok({"job": job, "result": str(worker.JOBS[job]())[:2000]})


# ---------------- app ----------------

async def on_api_error(request, exc):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


async def on_error(request, exc):  # pragma: no cover
    db.audit("api", "unhandled_error", {"path": request.url.path, "error": str(exc)[:300]})
    return JSONResponse({"error": "Something went wrong"}, status_code=500)


ROUTES = [
    Route("/health", health),
    Route("/v1/plans", public_plans),
    Route("/v1/risks", public_risks),
    Route("/v1/privacy/object", object_request, methods=["POST"]),
    Route("/v1/exports/fields.csv", export_fields),
    Route("/v1/auth/signup", signup, methods=["POST"]),
    Route("/v1/auth/login", login, methods=["POST"]),
    Route("/v1/me", me),
    Route("/v1/org", update_org, methods=["PATCH"]),
    Route("/v1/org/members", add_member, methods=["POST"]),
    Route("/v1/events", list_events),
    Route("/v1/events/{id}", get_event),
    Route("/v1/companies/{number}", get_company),
    Route("/v1/watchlists", watchlists, methods=["GET", "POST"]),
    Route("/v1/watchlists/{id}", watchlist_detail, methods=["GET", "DELETE"]),
    Route("/v1/watchlists/{id}/items", watchlist_items, methods=["POST"]),
    Route("/v1/watchlists/{id}/items/{number}", watchlist_item_delete, methods=["DELETE"]),
    Route("/v1/watchlists/{id}/import", watchlist_import, methods=["POST"]),
    Route("/v1/watchlists/{id}/export.csv", export_watchlist),
    Route("/v1/alert-rules", rules, methods=["GET", "POST"]),
    Route("/v1/alert-rules/{id}", rule_detail, methods=["PUT", "DELETE"]),
    Route("/v1/channels", channels, methods=["GET", "POST"]),
    Route("/v1/channels/{id}", channel_delete, methods=["DELETE"]),
    Route("/v1/exports/events.csv", export_events),
    Route("/v1/exports/bulk", bulk_export, methods=["POST"]),
    Route("/v1/exports/bulk/{id}", bulk_status),
    Route("/v1/exports/bulk/{id}/download", bulk_download),
    Route("/v1/billing/checkout", checkout, methods=["POST"]),
    Route("/v1/billing/portal", portal),
    Route("/v1/billing/webhook", stripe_webhook, methods=["POST"]),
    Route("/v1/admin/stats", admin_stats),
    Route("/v1/admin/orgs", admin_orgs),
    Route("/v1/admin/orgs/{id}", admin_org_update, methods=["PATCH"]),
    Route("/v1/admin/risk-factors", admin_risks),
    Route("/v1/admin/risk-factors/{id}", admin_risks, methods=["PATCH"]),
    Route("/v1/admin/suppressions", admin_suppress, methods=["GET", "POST"]),
    Route("/v1/admin/run/{job}", admin_run, methods=["POST"]),
]


def create_app():
    s = config.settings
    if s.env != "development" and (s.secret_key == "dev-only-change-me" or len(s.secret_key) < 32):
        raise RuntimeError("Set LL_SECRET_KEY to a random value of at least 32 characters before running outside development")
    db.init_db()
    if not s.public_base_url.startswith("http://localhost"):
        db.set_state("public_base_url", s.public_base_url)  # lets the worker build links to this site
    from app.outreach.api import ROUTES as OUTREACH_ROUTES
    from app.portal import ROUTES as PORTAL_ROUTES
    from app.insight_api import ROUTES as INSIGHT_ROUTES
    from app.terminal import ROUTES as TERMINAL_ROUTES
    return Starlette(routes=ROUTES + PORTAL_ROUTES + INSIGHT_ROUTES + TERMINAL_ROUTES + OUTREACH_ROUTES, exception_handlers={ApiError: on_api_error, Exception: on_error}, middleware=[
        Middleware(CORSMiddleware, allow_origins=config.settings.origins, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Authorization", "Content-Type"], max_age=600)])

