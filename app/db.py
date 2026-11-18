"""Tiny database layer: SQLite for development/tests, Postgres in production.

Write SQL with `?` placeholders; they are converted for Postgres automatically.
JSON columns are stored as TEXT and handled with `dumps`/`loads`.
"""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app import config

_local = threading.local()
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix=""):
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def dumps(v):
    return json.dumps(v, ensure_ascii=False, default=str) if v is not None else None


def loads(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def is_postgres():
    return config.settings.database_url.startswith(("postgres://", "postgresql://"))


def _connect():
    url = config.settings.database_url
    if is_postgres():  # pragma: no cover - needs a Postgres server
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(url, row_factory=dict_row, autocommit=True)
    path = url.replace("sqlite:///", "", 1)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
    return conn


def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "url", None) != config.settings.database_url:
        c = _connect()
        _local.conn, _local.url = c, config.settings.database_url
    return c


def close():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


def _sql(sql):
    return sql.replace("?", "%s") if is_postgres() else sql


def _param(v):
    return int(v) if isinstance(v, bool) else v  # Postgres won't put True into an INTEGER column


def _clean(row):
    """Postgres returns SUM/AVG as Decimal, which JSON can't encode; SQLite returns plain numbers."""
    if row is None:
        return None
    for k, v in row.items():
        if isinstance(v, Decimal):
            row[k] = int(v) if v == v.to_integral_value() else float(v)
    return row


def _run(sql, params, fetch=None, many=False):
    """Run one statement; on any error roll back so the shared connection stays usable."""
    c = conn()
    cur = c.cursor()
    try:
        if many:
            cur.executemany(_sql(sql), [tuple(_param(v) for v in r) for r in params])
        else:
            cur.execute(_sql(sql), tuple(_param(v) for v in params))
        if fetch == "one":
            out = _clean(cur.fetchone())
        elif fetch == "all":
            out = [_clean(r) for r in cur.fetchall()]
        else:
            out = cur.rowcount
        if not is_postgres() and not getattr(_local, "in_tx", False):  # Postgres connections run in autocommit mode
            c.commit()
        return out
    except Exception:
        if not getattr(_local, "in_tx", False):
            try:
                c.rollback()
            except Exception:
                pass
        raise


def execute(sql, params=()):
    return _run(sql, params)


def executemany(sql, rows):
    _run(sql, rows, many=True)


def one(sql, params=()):
    return _run(sql, params, fetch="one")


def all(sql, params=()):  # noqa: A001 - deliberate short name
    return _run(sql, params, fetch="all")


@contextmanager
def transaction():
    """Group several statements atomically."""
    c = conn()
    _local.in_tx = True
    try:
        if is_postgres():  # pragma: no cover
            with c.transaction():
                yield c
        else:
            yield c
            c.commit()
    except Exception:
        if not is_postgres():
            c.rollback()
        raise
    finally:
        _local.in_tx = False


def insert(table, row):
    cols = list(row)
    execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[k] for k in cols])
    return row


def upsert(table, row, keys, update=None):
    cols = list(row)
    update = [c for c in (update or cols) if c not in keys]
    sets = ",".join(f"{c}=excluded.{c}" for c in update) or f"{keys[0]}=excluded.{keys[0]}"
    execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {sets}",
        [row[k] for k in cols],
    )


# Columns added after a table was first created: (table, column, type). init_db adds any that are missing,
# so existing databases (e.g. a SQLite file from a laptop) upgrade themselves on start.
ADDED_COLUMNS = [
    ("export_jobs", "content", "TEXT"),
]


def _columns(table):
    if is_postgres():  # pragma: no cover
        rows = all("SELECT column_name AS name FROM information_schema.columns WHERE table_name=? AND table_schema=current_schema()", (table,))
    else:
        rows = all(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows}


def init_db():
    """Create tables if they do not exist, then add any newer columns. Safe to run on every start."""
    ddl = SCHEMA_PATH.read_text()
    c = conn()
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        c.cursor().execute(stmt)
    c.commit()
    for table, col, typ in ADDED_COLUMNS:
        if col not in _columns(table):
            execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def get_state(key, default=None):
    r = one("SELECT value FROM ingest_state WHERE key=?", (key,))
    return r["value"] if r else default


def set_state(key, value):
    upsert("ingest_state", {"key": key, "value": str(value), "updated_at": now()}, ["key"])


def audit(actor, action, detail=None):
    insert("audit_log", {"id": new_id("aud_"), "actor": actor or "system", "action": action,
                         "detail": dumps(detail or {}), "at": now()})
