"""Background worker: collectors, pipeline, delivery and housekeeping on a schedule.

Run with:  python -m app.worker
One worker process is enough to start; the Companies House streams run in threads.
"""
import os
import signal
import threading
import time
from datetime import datetime

from app import config, db, insight
from app.delivery import dispatch
from app.pipeline import run as pipeline
from app.outreach import engine as outreach, inbox as outreach_inbox
from app.sources import companies_house, gazette, market_news, news, registers, risk_watch


def _gazette():
    return len(gazette.poll())


def _sponsors():
    return len(registers.poll_sponsors())


def _ccjs():
    return len(registers.poll_ccjs())


def _ccod():
    return len(registers.ingest_ccod(config.settings.ccod_path)) if config.settings.ccod_path else 0


def _unclaimed():  # pragma: no cover - network
    from app.sources.http import session
    s = session()
    page = s.get(registers.UNCLAIMED_PAGE, timeout=30)
    page.raise_for_status()
    url = registers.unclaimed_csv_url(page.text)
    r = s.get(url, timeout=120)
    r.raise_for_status()
    db.set_state("unclaimed_estates_source", url)
    return len(registers.ingest_unclaimed_estates(r.content.decode("utf-8-sig", "replace")))


def _enrich_watched(batch=200):
    """Keep every company on any customer watchlist fresh (daily)."""
    if not config.settings.companies_house_api_key:
        return 0
    n = 0
    for r in db.all("SELECT DISTINCT company_number FROM watchlist_items LIMIT ?", (batch,)):
        try:
            n += companies_house.enrich(r["company_number"], max_age_hours=24)
        except Exception as e:
            db.audit("worker", "enrich_error", {"company": r["company_number"], "error": str(e)[:200]})
    return n


def _expire_trials():
    today = datetime.utcnow().date().isoformat()
    return db.execute("UPDATE orgs SET status='canceled' WHERE status='trialing' AND trial_ends_at<? AND stripe_subscription_id IS NULL", (today,))


JOBS = {
    "gazette": _gazette,
    "process": lambda: len(pipeline.process_new()),
    "instant": dispatch.send_instant,
    "digests": dispatch.send_digests,
    "exports": dispatch.run_export_jobs,
    "sponsors": _sponsors,
    "unclaimed_estates": _unclaimed,
    "ccjs": _ccjs,
    "ccod": _ccod,
    "enrich_watched": _enrich_watched,
    "risk_sources": lambda: len(risk_watch.check_sources()),
    "expire_trials": _expire_trials,
    "purge": dispatch.purge_old,
    "outreach_send": outreach.send_due,
    "outreach_inbox": lambda: len(outreach_inbox.poll_all()),
    "insight_snapshot": lambda: insight.snapshot(),
    "insight_label": lambda: insight.label(),
    "insight_fit": lambda: insight.fit(),
    "insight_news": lambda: news.poll_watched(),
    "market_news": lambda: market_news.poll(),
}

# job -> seconds between runs
SCHEDULE = {
    "gazette": 300, "process": 30, "instant": 60, "digests": 600, "exports": 60,
    "sponsors": 86400, "unclaimed_estates": 86400, "ccjs": 3600, "ccod": 30 * 86400,
    "enrich_watched": 86400, "risk_sources": 7 * 86400, "expire_trials": 3600, "purge": 86400,
    "outreach_send": 60, "outreach_inbox": 300,
    "insight_snapshot": 86400, "insight_label": 86400, "insight_fit": 7 * 86400, "insight_news": 3600,
    "market_news": 900,
}


def _adopt_public_url():
    """In the cloud the worker has no web address of its own; use the one the web service recorded."""
    s = config.settings
    if s.public_base_url.startswith("http://localhost") and not os.environ.get("LL_PUBLIC_BASE_URL"):
        url = db.get_state("public_base_url")
        if url:
            s.public_base_url = url


def run_forever():  # pragma: no cover - long-running
    db.init_db()
    _adopt_public_url()
    risk_watch.seed()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    if config.settings.companies_house_stream_key:
        for stream in ("companies", "insolvency-cases", "charges", "officers", "persons-with-significant-control"):
            threading.Thread(target=companies_house.run_stream, args=(stream, stop), daemon=True, name=f"ch-{stream}").start()
    last = {}
    while not stop.is_set():
        _adopt_public_url()
        for job, every in SCHEDULE.items():
            if time.monotonic() - last.get(job, -1e9) >= every:
                last[job] = time.monotonic()
                try:
                    result = JOBS[job]()
                    db.set_state(f"job:{job}", f"{db.now()} ok {result}")
                except Exception as e:
                    db.set_state(f"job:{job}", f"{db.now()} error {str(e)[:200]}")
        stop.wait(5)


if __name__ == "__main__":
    run_forever()
