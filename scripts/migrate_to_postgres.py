"""Copy everything from the local SQLite database into the cloud Postgres database.

    python -m scripts.migrate_to_postgres "postgresql://…external URL from Render…"

Safe to run more than once: rows that already exist are skipped. Stop the local
worker first (close its Terminal window) so nothing changes while copying.
"""
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BATCH = 1000


def tables_in_schema_order():
    ddl = (ROOT / "app" / "schema.sql").read_text()
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl)


def main(url, sqlite_path=ROOT / "ledgerlight.db"):
    import psycopg
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("Paste the External Database URL from Render (it starts with postgresql://)")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    import os
    os.environ["DATABASE_URL"] = url
    os.environ["LL_SKIP_DOTENV"] = "1"
    from app import config, db
    config.reload()
    db.init_db()  # create tables in the cloud database if this is the first run
    dst = psycopg.connect(url, autocommit=False)
    local_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables_in_schema_order():
        if table not in local_tables:
            continue
        local_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        cloud_cols = {r[0] for r in dst.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s AND table_schema=current_schema()", (table,))}
        cols = [c for c in local_cols if c in cloud_cols]
        total = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING")
        copied = 0
        cur = src.execute(f"SELECT {','.join(cols)} FROM {table}")
        with dst.cursor() as w:
            while True:
                rows = cur.fetchmany(BATCH)
                if not rows:
                    break
                w.executemany(sql, [tuple(r) for r in rows])
                copied += len(rows)
                print(f"\r  {table:24} {copied:>8,} / {total:,}", end="", flush=True)
        dst.commit()
        print(f"\r  {table:24} {total:>8,} rows  ✓" + " " * 12)
    dst.close()
    print("\nDone. Your data is in the cloud database.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "ledgerlight.db")
