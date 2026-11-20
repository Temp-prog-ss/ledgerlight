import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

os.environ["LL_SKIP_DOTENV"] = "1"  # tests never read your real .env keys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    pg = os.environ.get("LL_TEST_DATABASE_URL")  # run the suite against real Postgres (see scripts/pg_selftest.sh)
    for k in list(os.environ):  # never inherit the host's production settings (Render sets LL_TRUST_PROXY etc.)
        if k.startswith(("LL_", "OUTREACH_", "STRIPE_", "SMTP_", "TWILIO_")) and k not in ("LL_SKIP_DOTENV", "LL_TEST_DATABASE_URL"):
            monkeypatch.delenv(k)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", pg or f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("LL_SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes")
    monkeypatch.setenv("LL_DRY_RUN_DELIVERY", "1")
    monkeypatch.setenv("LL_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.chdir(tmp_path)
    from app import config, db
    config.reload()
    db.close()
    if pg:  # fresh, empty schema for every test; production tables are never touched
        db.execute("DROP SCHEMA IF EXISTS ll_selftest CASCADE")
        db.execute("CREATE SCHEMA ll_selftest")
    db.init_db()
    from app import web
    web.auth_throttle.hits.clear()
    web.public_throttle.hits.clear()
    yield
    db.close()


class Client:
    """Minimal ASGI test client (Starlette's TestClient needs httpx, which we avoid)."""

    def __init__(self, app):
        self.app, self.token = app, None

    def request(self, method, path, json_body=None, raw=None, params=None, headers=None):
        body = raw if raw is not None else (json.dumps(json_body).encode() if json_body is not None else b"")
        hdrs = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        if self.token:
            hdrs.append((b"authorization", f"Bearer {self.token}".encode()))
        for k, v in (headers or {}).items():
            hdrs.append((k.lower().encode(), v.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": urlencode(params or {}).encode(),
                 "headers": hdrs, "client": ("127.0.0.1", 5000), "server": ("testserver", 80), "root_path": ""}
        out = {"status": None, "headers": {}, "body": b""}
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        return Resp(out)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json_body=json, **kw)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json_body=json, **kw)

    def patch(self, path, json=None, **kw):
        return self.request("PATCH", path, json_body=json, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


class Resp:
    def __init__(self, out):
        self.status_code, self.headers, self.content = out["status"], out["headers"], out["body"]

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)


@pytest.fixture
def client():
    from app.web import create_app
    return Client(create_app())


def make_company(number, name, sic="41201", status="active", **kw):
    from app import db
    row = {"number": number, "name": name, "status": status, "company_type": "ltd", "sic_codes": sic,
           "sic_description": "Construction of commercial buildings" if sic.startswith("41") else "Other",
           "town": "Manchester", "postcode": "M4 6BF", "region": "North West", "incorporated_on": "2012-03-01",
           "employees": 120, "turnover": 48_000_000, "net_assets": -250_000, "accounts_overdue": 1, "updated_at": db.now()}
    row.update(kw)
    db.insert("companies", row)
    return row


@pytest.fixture
def company_factory():
    return make_company
