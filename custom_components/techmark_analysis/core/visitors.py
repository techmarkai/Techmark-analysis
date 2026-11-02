"""Permanent customer-visitor reports from the visit_days table.

Terms kept distinct everywhere:
- unique visitors: distinct visitor IDs (people the camera recognised in Customers)
- visit days: one per visitor per local calendar day
- recognition events: raw camera matches (many per visit day)
Employees and unmatched faces never enter these tables.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, timezone
from itertools import pairwise

from .report import csv_safe
from .store import Store

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _intervals(days: list[date]) -> dict:
    gaps = [(b - a).days for a, b in pairwise(days)]
    return {
        "avg_interval_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "min_interval_days": min(gaps) if gaps else None,
        "max_interval_days": max(gaps) if gaps else None,
    }


def _hms(store: Store, ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(store.tz).strftime("%H:%M:%S")


def visitor_report(store: Store, visitor_no: int) -> dict | None:
    if not store.query("SELECT 1 FROM visitors WHERE visitor_no=?", (visitor_no,)):
        return None
    if store.query("SELECT 1 FROM erased_visitors WHERE visitor_no=?", (visitor_no,)):
        return {"visitor_id": visitor_no, "erased": True, "visits": []}
    rows = store.query(
        "SELECT day, first, last, events FROM visit_days WHERE visitor_no=? ORDER BY day", (visitor_no,)
    )
    days = [date.fromisoformat(r[0]) for r in rows]
    return {
        "visitor_id": visitor_no,
        "camera_ids": [r[0] for r in store.query(
            "SELECT camera_pid FROM visitor_camera_ids WHERE visitor_no=? ORDER BY camera_pid", (visitor_no,)
        )],
        "visit_days": len(rows),
        "recognition_events": sum(r[3] for r in rows),
        "first_visit": rows[0][0] if rows else None,
        "last_visit": rows[-1][0] if rows else None,
        **_intervals(days),
        "monthly_visit_days": dict(sorted(Counter(r[0][:7] for r in rows).items())),
        "visits": [
            {"date": d, "first_seen": _hms(store, f), "last_seen": _hms(store, la), "recognitions": n}
            for d, f, la, n in rows
        ],
    }


def period_summary(store: Store, start: date, end: date) -> dict:
    span = (start.isoformat(), end.isoformat())
    unique, visit_days, events = store.query(
        "SELECT COUNT(DISTINCT visitor_no), COUNT(*), COALESCE(SUM(events), 0) FROM visit_days"
        " WHERE day BETWEEN ? AND ?",
        span,
    )[0]
    new = store.query(
        "SELECT COUNT(*) FROM (SELECT MIN(day) AS d FROM visit_days GROUP BY visitor_no) WHERE d BETWEEN ? AND ?",
        span,
    )[0][0]
    return {
        "start": span[0],
        "end": span[1],
        "unique_visitors": unique,
        "new_visitors": new,
        "returning_visitors": unique - new,
        "visit_days": visit_days,
        "recognition_events": events,
    }


def monthly_rows(store: Store, year: int) -> list[dict]:
    out = []
    for m in range(1, 13):
        nxt = date(year + (m == 12), m % 12 + 1, 1)
        out.append({"month": f"{year}-{m:02d}", **period_summary(store, date(year, m, 1), date.fromordinal(nxt.toordinal() - 1))})
    return out


def annual_rows(store: Store, year: int) -> list[dict]:
    rows = store.query(
        "SELECT visitor_no, day, events FROM visit_days WHERE day BETWEEN ? AND ? ORDER BY visitor_no, day",
        (f"{year}-01-01", f"{year}-12-31"),
    )
    per: dict[int, list] = {}
    for vno, day, events in rows:
        per.setdefault(vno, []).append((day, events))
    out = []
    for vno, items in per.items():
        days = [date.fromisoformat(d) for d, _ in items]
        months = Counter(d.month for d in days)
        out.append(
            {
                "visitor_id": vno,
                "visit_days": len(items),
                "first_visit": items[0][0],
                "last_visit": items[-1][0],
                **_intervals(days),
                "recognition_events": sum(e for _, e in items),
                "months": [months.get(m, 0) for m in range(1, 13)],
            }
        )
    return out


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return buf.getvalue()


def render_visitor_csv(report: dict) -> str:
    return _csv(
        ["Visitor ID", "Date", "First Seen", "Last Seen", "Recognitions"],
        [[report["visitor_id"], v["date"], v["first_seen"], v["last_seen"], v["recognitions"]] for v in report["visits"]],
    )


def render_annual_csv(rows: list[dict]) -> str:
    return _csv(
        ["Visitor ID", "Visit Days", "First Visit", "Last Visit", "Avg Interval (days)", "Min Interval (days)",
         "Max Interval (days)", "Recognition Events", *MONTHS],
        [[r["visitor_id"], r["visit_days"], r["first_visit"], r["last_visit"], r["avg_interval_days"],
          r["min_interval_days"], r["max_interval_days"], r["recognition_events"], *r["months"]] for r in rows],
    )


def render_monthly_csv(rows: list[dict]) -> str:
    return _csv(
        ["Month", "Unique Visitors", "New Visitors", "Returning Visitors", "Visit Days", "Recognition Events"],
        [[r["month"], r["unique_visitors"], r["new_visitors"], r["returning_visitors"], r["visit_days"],
          r["recognition_events"]] for r in rows],
    )
