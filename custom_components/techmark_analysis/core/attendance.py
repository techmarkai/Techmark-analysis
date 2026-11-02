"""Employee attendance rows, export date ranges, and CSV rendering.

Single source of attendance data: the `attendance` table, which the store updates on
every employee match (first = earliest match, last = latest match per local day).
Unmatched faces and customers never enter that table.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from .report import csv_safe
from .store import Store

RANGES = ("today", "yesterday", "this_week", "this_month", "custom")
MAX_RANGE_DAYS = 366
CSV_HEADER = ["Employee Name", "Employee ID", "Date", "First In", "Last Seen", "Duration"]


def resolve_range(kind: str, today: date, start: date | None = None, end: date | None = None) -> tuple[date, date]:
    """Inclusive (start, end) local dates. Weeks start on Monday (ISO). Raises ValueError."""
    if kind == "today":
        return today, today
    if kind == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if kind == "this_week":
        return today - timedelta(days=today.weekday()), today
    if kind == "this_month":
        return today.replace(day=1), today
    if kind == "custom":
        if start is None or end is None:
            raise ValueError("custom range requires start_date and end_date")
        if end < start:
            raise ValueError("end_date is before start_date")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"range longer than {MAX_RANGE_DAYS} days")
        return start, end
    raise ValueError(f"unknown range {kind!r}")


def _hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def attendance_rows(store: Store, start: date, end: date) -> list[dict]:
    """One row per employee per local day in [start, end], ordered by date then first-in."""
    rows = store.query(
        "SELECT day, human_id, name, first, last FROM attendance WHERE day BETWEEN ? AND ? ORDER BY day, first, human_id",
        (start.isoformat(), end.isoformat()),
    )
    out = []
    for day, human_id, name, first, last in rows:
        f = datetime.fromtimestamp(first, timezone.utc).astimezone(store.tz)
        la = datetime.fromtimestamp(last, timezone.utc).astimezone(store.tz)
        out.append(
            {
                "day": day,
                "human_id": human_id,
                "name": name or "",
                "first": f,
                "last": la,
                # Real elapsed time from UTC timestamps: correct across DST changes.
                "duration_s": last - first,
            }
        )
    return out


def render_attendance_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow(
            [
                csv_safe(r["name"]),
                csv_safe(r["human_id"]),
                r["day"],
                r["first"].strftime("%H:%M:%S"),
                r["last"].strftime("%H:%M:%S"),
                _hms(r["duration_s"]),
            ]
        )
    return buf.getvalue()
