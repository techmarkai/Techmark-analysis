import csv
import io
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from core.models import FaceMatch, FaceUnmatched
from core.processor import Processor, ProcessorConfig
from core.store import Store, StoreError
from core.visitors import (
    annual_rows,
    monthly_rows,
    period_summary,
    render_annual_csv,
    render_monthly_csv,
    render_visitor_csv,
    visitor_report,
)

TZ = ZoneInfo("Etc/GMT-3")
BERLIN = ZoneInfo("Europe/Berlin")


def at(tz, y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(timezone.utc)


def cust(pid):
    return FaceMatch("2", "Customers", pid, "", 0.9, 80, None)


def emp(pid):
    return FaceMatch("1", "Employee", pid, "Test Employee", 0.95, 80, None)


def feed(store, proc, items):
    for ev, t in items:
        store.apply(proc.handle(ev, t))


@pytest.fixture
def sp(tmp_path):
    s = Store(str(tmp_path / "v.db"), TZ)
    yield s, Processor(ProcessorConfig())
    s.close()


def test_sequential_ids_and_camera_mapping(sp):
    s, p = sp
    feed(s, p, [(cust("501"), at(TZ, 2026, 1, 5, 10)), (cust("502"), at(TZ, 2026, 1, 5, 11)), (cust("501"), at(TZ, 2026, 1, 6, 10))])
    assert s.query("SELECT camera_pid, visitor_no FROM visitor_camera_ids ORDER BY visitor_no") == [("501", 1), ("502", 2)]
    assert visitor_report(s, 1)["visit_days"] == 2


def test_ids_never_reused(sp):
    s, _ = sp
    a = s.create_visitor(at(TZ, 2026, 1, 1, 9))
    s.query("DELETE FROM visitors WHERE visitor_no = ?", (a,))  # even if a row were removed
    assert s.create_visitor(at(TZ, 2026, 1, 1, 9)) == a + 1


def test_multiple_camera_ids_one_visitor_and_no_silent_remap(sp):
    s, p = sp
    v = s.create_visitor(at(TZ, 2026, 1, 1, 9))
    s.link_camera_id("700", v)
    s.link_camera_id("701", v)  # second photo record of the same person
    s.link_camera_id("700", v)  # idempotent retry
    feed(s, p, [(cust("700"), at(TZ, 2026, 1, 2, 9)), (cust("701"), at(TZ, 2026, 1, 3, 9))])
    r = visitor_report(s, v)
    assert r["camera_ids"] == ["700", "701"] and r["visit_days"] == 2
    w = s.create_visitor(at(TZ, 2026, 1, 1, 9))
    with pytest.raises(StoreError):
        s.link_camera_id("700", w)
    with pytest.raises(StoreError):
        s.link_camera_id("800", 999)


def test_duplicates_count_one_visit_day(sp):
    s, p = sp
    t = at(TZ, 2026, 2, 1, 10)
    feed(s, p, [(cust("1"), t + timedelta(minutes=m)) for m in (0, 0, 1, 90, 400)])  # incl. exact duplicate
    r = visitor_report(s, 1)
    assert r["visit_days"] == 1 and r["recognition_events"] == 5
    assert r["visits"][0]["first_seen"] == "10:00:00" and r["visits"][0]["last_seen"] == "16:40:00"


def test_out_of_order_events_keep_first_last(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(TZ, 2026, 2, 1, 15)), (cust("1"), at(TZ, 2026, 2, 1, 9))])
    v = visitor_report(s, 1)["visits"][0]
    assert (v["first_seen"], v["last_seen"]) == ("09:00:00", "15:00:00")


def test_employees_and_unmatched_never_visitors(sp):
    s, p = sp
    feed(s, p, [(emp("1"), at(TZ, 2026, 2, 1, 9)), (FaceUnmatched(95, 0.0, None, None, None), at(TZ, 2026, 2, 1, 9))])
    assert s.query("SELECT COUNT(*) FROM visitors")[0][0] == 0
    assert s.query("SELECT COUNT(*) FROM visit_days")[0][0] == 0


def test_restart_keeps_ids_and_history(tmp_path):
    path = str(tmp_path / "r.db")
    s = Store(path, TZ)
    feed(s, Processor(ProcessorConfig()), [(cust("9"), at(TZ, 2026, 3, 1, 10))])
    s.close()
    s2 = Store(path, TZ)
    feed(s2, Processor(ProcessorConfig(), s2.load_latest_sessions()), [(cust("9"), at(TZ, 2026, 3, 2, 10)), (cust("10"), at(TZ, 2026, 3, 2, 11))])
    assert visitor_report(s2, 1)["visit_days"] == 2 and visitor_report(s2, 2)["camera_ids"] == ["10"]
    assert s2.query("SELECT value FROM meta WHERE key='schema_version'") == [("2",)]
    s2.close()


def test_dst_day_boundaries(tmp_path):
    s = Store(str(tmp_path / "d.db"), BERLIN)
    p = Processor(ProcessorConfig())
    # 2026-10-25 Berlin falls back 03:00 -> 02:00; 23:30 local on both sides of midnight
    feed(s, p, [(cust("1"), at(BERLIN, 2026, 10, 24, 23, 30)), (cust("1"), at(BERLIN, 2026, 10, 25, 23, 30))])
    assert [v["date"] for v in visitor_report(s, 1)["visits"]] == ["2026-10-24", "2026-10-25"]
    s.close()


def test_purge_never_touches_visitor_history(sp):
    s, p = sp
    old = at(TZ, 2020, 1, 1, 10)
    feed(s, p, [(cust("1"), old)])
    s.purge(at(TZ, 2026, 1, 1, 10), 400)
    assert s.query("SELECT COUNT(*) FROM events")[0][0] == 0  # raw events purged
    r = visitor_report(s, 1)
    assert r["first_visit"] == "2020-01-01" and r["visit_days"] == 1


def test_intervals_monthly_and_summaries(sp):
    s, p = sp
    days = [date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 10), date(2026, 2, 10)]
    feed(s, p, [(cust("1"), at(TZ, d.year, d.month, d.day, 12)) for d in days] + [(cust("2"), at(TZ, 2026, 2, 10, 13))])
    r = visitor_report(s, 1)
    assert (r["min_interval_days"], r["max_interval_days"], r["avg_interval_days"]) == (2, 31, 13.3)  # gaps 2, 7, 31
    assert r["monthly_visit_days"] == {"2026-01": 3, "2026-02": 1}
    feb = period_summary(s, date(2026, 2, 1), date(2026, 2, 28))
    assert feb == {"start": "2026-02-01", "end": "2026-02-28", "unique_visitors": 2, "new_visitors": 1,
                   "returning_visitors": 1, "visit_days": 2, "recognition_events": 2}
    a = {x["visitor_id"]: x for x in annual_rows(s, 2026)}
    assert a[1]["months"][:3] == [3, 1, 0] and a[2]["visit_days"] == 1 and a[2]["avg_interval_days"] is None
    assert [m["unique_visitors"] for m in monthly_rows(s, 2026)][:3] == [1, 2, 0]


def test_csv_outputs(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(TZ, 2026, 1, 1, 9)), (cust("1"), at(TZ, 2026, 1, 4, 9))])
    rows = list(csv.reader(io.StringIO(render_visitor_csv(visitor_report(s, 1)))))
    assert rows == [["Visitor ID", "Date", "First Seen", "Last Seen", "Recognitions"],
                    ["1", "2026-01-01", "09:00:00", "09:00:00", "1"], ["1", "2026-01-04", "09:00:00", "09:00:00", "1"]]
    annual = list(csv.reader(io.StringIO(render_annual_csv(annual_rows(s, 2026)))))
    assert annual[0][:3] == ["Visitor ID", "Visit Days", "First Visit"] and annual[1][:5] == ["1", "2", "2026-01-01", "2026-01-04", "3.0"]
    monthly = list(csv.reader(io.StringIO(render_monthly_csv(monthly_rows(s, 2026)))))
    assert monthly[0][0] == "Month" and len(monthly) == 13
    assert visitor_report(s, 99) is None
