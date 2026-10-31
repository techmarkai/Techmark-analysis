"""Daily and monthly statistics from the store. Employees are never in customer metrics."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from .attendance import attendance_rows
from .store import Store


def _bounds(store: Store, first: date, last_exclusive: date) -> tuple[datetime, datetime]:
    start = datetime.combine(first, time.min, store.tz).astimezone(timezone.utc)
    end = datetime.combine(last_exclusive, time.min, store.tz).astimezone(timezone.utc)
    return start, end


def _period(store: Store, day_from: str, day_to: str, now: datetime) -> dict:
    """Inclusive day range (ISO strings)."""
    one = lambda sql, *a: store.query(sql, a)[0][0] or 0  # noqa: E731
    span = (day_from, day_to)
    customers = one(
        "SELECT COUNT(DISTINCT human_id) FROM sessions WHERE role='customer' AND day BETWEEN ? AND ?", *span
    )
    returning = one(
        "SELECT COUNT(DISTINCT s.human_id) FROM sessions s WHERE s.role='customer' AND s.day BETWEEN ? AND ?"
        " AND EXISTS (SELECT 1 FROM sessions p WHERE p.role='customer' AND p.human_id=s.human_id AND p.day < ?)",
        *span,
        day_from,
    )
    visits = one("SELECT COUNT(*) FROM sessions WHERE role='customer' AND day BETWEEN ? AND ?", *span)
    kinds = dict(
        store.query("SELECT kind, COUNT(*) FROM events WHERE day BETWEEN ? AND ? GROUP BY kind", span)
    )
    attendance = [
        {
            "day": r["day"],
            "human_id": r["human_id"],
            "name": r["name"],
            "first_seen": r["first"].isoformat(timespec="seconds"),
            "last_seen": r["last"].isoformat(timespec="seconds"),
        }
        for r in attendance_rows(store, date.fromisoformat(day_from), date.fromisoformat(day_to))
    ]
    after_last = date.fromordinal(date.fromisoformat(day_to).toordinal() + 1)
    start, end = _bounds(store, date.fromisoformat(day_from), after_last)
    gaps = [
        {
            "start": a.astimezone(store.tz).isoformat(timespec="seconds"),
            "end": b.astimezone(store.tz).isoformat(timespec="seconds") if b else None,
            "reason": r,
        }
        for a, b, r in store.gaps_between(start, end, now)
    ]
    return {
        "period_start": day_from,
        "period_end": day_to,
        "employees_present": len({a["human_id"] for a in attendance}),
        "attendance": attendance,
        "known_customers_unique": customers,
        "returning_customers": returning,
        "customer_visits": visits,
        "unmatched_face_events": kinds.get("unmatched", 0),
        "low_quality_faces": kinds.get("low_quality", 0),
        "other_matches": kinds.get("other_match", 0),
        "door_entries": kinds.get("line_in"),
        "door_exits": kinds.get("line_out"),
        "gaps": gaps,
    }


def daily(store: Store, day: date, now: datetime) -> dict:
    d = day.isoformat()
    return _period(store, d, d, now)


def monthly(store: Store, year: int, month: int, now: datetime) -> dict:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1)
    return _period(store, first.isoformat(), last.isoformat(), now)
