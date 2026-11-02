"""Markdown and CSV report rendering. Face counts are never presented as footfall."""

from __future__ import annotations

import csv
import io

LABELS = [
    ("door_entries", "Door entries (line crossing)"),
    ("door_exits", "Door exits (line crossing)"),
    ("employees_present", "Employees present"),
    ("unique_customers", "Unique customers"),
    ("returning_customers", "Returning customers"),
    ("customer_visits", "Customer visits"),
    ("unmatched_face_events", "Unmatched face events (not unique people)"),
    ("low_quality_faces", "Low-quality faces (excluded)"),
    ("other_matches", "Matches in other libraries"),
]


def csv_safe(value) -> str:
    """CSV cell safe against spreadsheet formula injection (names come from the camera)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _md(value) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _val(stats: dict, key: str) -> str:
    v = stats.get(key)
    return "not configured" if v is None else str(v)


def render_markdown(stats: dict, title: str) -> str:
    lines = [f"# {title}", "", f"Period: {stats['period_start']} to {stats['period_end']}", ""]
    if stats["gaps"]:
        lines += ["> **Data gaps:** events during these periods were not received.", ""]
        lines += [f"> - {g['start']} → {g['end'] or 'ongoing'} ({g['reason']})" for g in stats["gaps"]]
        lines.append("")
    lines += ["| Metric | Value |", "|---|---|"]
    lines += [f"| {label} | {_val(stats, key)} |" for key, label in LABELS]
    lines += [
        "",
        "Door counts come from line-crossing alarms and approximate entries. Face-based figures cover only"
        " people whose face the camera captured; they are not total visitors.",
        "",
        "## Attendance",
        "",
        "| Day | Employee | First seen | Last seen |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {a['day']} | {_md(a['name'])} | {a['first_seen']} | {a['last_seen']} |" for a in stats["attendance"]
    ]
    lines += ["", "Last seen is the last camera match, not a confirmed departure time.", ""]
    return "\n".join(lines)


def render_csv(stats: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["metric", "value"])
    for key, label in LABELS:
        w.writerow([label, _val(stats, key)])
    w.writerow([])
    w.writerow(["day", "human_id", "name", "first_seen", "last_seen"])
    for a in stats["attendance"]:
        w.writerow([csv_safe(a[k]) for k in ("day", "human_id", "name", "first_seen", "last_seen")])
    return buf.getvalue()
