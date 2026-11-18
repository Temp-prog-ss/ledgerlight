"""Runs on Render before each deploy goes live (preDeployCommand).

1. Runs the whole test suite against the real Postgres database, inside a separate
   `ll_selftest` schema, so production tables are never touched.
2. Creates/upgrades the production tables once, before the web service and worker start.
If either step fails, Render keeps the previous version running.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def with_search_path(url, schema):
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    q["options"] = f"-csearch_path={schema}"
    return urlunsplit(parts._replace(query=urlencode(q)))


def main():
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("DATABASE_URL must point at Postgres")
    test_url = with_search_path(url, "ll_selftest")
    env = dict(os.environ, LL_TEST_DATABASE_URL=test_url, LL_SKIP_DOTENV="1")
    print("Running the test suite against Postgres (schema ll_selftest)…", flush=True)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=ROOT, env=env)
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS ll_selftest CASCADE")
    if r.returncode != 0:
        sys.exit("Tests failed on Postgres; keeping the previous deploy.")
    from app import db
    db.init_db()
    print("Production tables ready.", flush=True)


if __name__ == "__main__":
    main()
