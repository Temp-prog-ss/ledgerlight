"""Recording raw events from any source (idempotent)."""
from app import db


def record(source, source_id, event_type, company_number=None, title=None, published_at=None,
           effective_date=None, data=None, personal_data=False):
    """Insert an event once per (source, source_id). Returns the new id, or None if already seen."""
    existing = db.one("SELECT id FROM events WHERE source=? AND source_id=?", (source, str(source_id)))
    if existing:
        return None
    eid = db.new_id("evt_")
    db.insert("events", {
        "id": eid, "source": source, "source_id": str(source_id), "event_type": event_type,
        "category": category(event_type), "company_number": company_number, "title": title,
        "published_at": published_at or db.now(), "effective_date": effective_date,
        "data": db.dumps(data or {}), "personal_data": int(bool(personal_data)),
        "status": "new", "created_at": db.now(),
    })
    return eid


def category(event_type):
    from app import brands
    for name, group in (("insolvency", brands.INSOLVENCY), ("distress", brands.DISTRESS), ("growth", brands.GROWTH),
                        ("sponsor", brands.SPONSOR), ("estate", brands.ESTATE), ("change", brands.CHANGE)):
        if event_type in group:
            return name
    return "other"
