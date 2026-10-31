import csv
import io
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.attendance import CSV_HEADER, attendance_rows, render_attendance_csv, resolve_range
from core.models import FaceMatch, FaceUnmatched
from core.processor import Processor, ProcessorConfig
from core.stats import daily
from core.store import Store

TZ = ZoneInfo("Etc/GMT-3")  # fixed UTC+3
BERLIN = ZoneInfo("Europe/Berlin")  # DST: 2026-03-29 02:00 -> 03:00


def at(tz, y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=tz).astimezone(timezone.utc)


def emp(hid="1", name="Test Employee A"):
    return FaceMatch("1", "Employee", hid, name, 0.95, 80, None)


def cust(hid="c1"):
    return FaceMatch("2", "Customers", hid, "Test Customer", 0.9, 80, None)


def make(tmp_path, tz=TZ, name="a.db"):
    return Store(str(tmp_path / name), tz), Processor(ProcessorConfig())


def feed(store, proc, items):
    for ev, t in items:
        store.apply(proc.handle(ev, t))


def parse(text):
    return list(csv.reader(io.StringIO(text)))


def test_single_event_zero_duration(tmp_path):
    s, p = make(tmp_path)
    feed(s, p, [(emp(), at(TZ, 2026, 10, 31, 8, 12, 15))])
    rows = parse(render_attendance_csv(attendance_rows(s, date(2026, 10, 31), date(2026, 10, 31))))
    assert rows == [CSV_HEADER, ["Test Employee A", "1", "2026-10-31", "08:12:15", "08:12:15", "00:00:00"]]


def test_multiple_and_duplicate_events_one_row(tmp_path):
    s, p = make(tmp_path)
    t = [at(TZ, 2026, 10, 31, 12), at(TZ, 2026, 10, 31, 8, 12, 15), at(TZ, 2026, 10, 31, 17, 35, 42)]
    feed(s, p, [(emp(), x) for x in t] + [(emp(), t[0]), (emp(), t[0])])  # duplicates + out of order
    rows = parse(render_attendance_csv(attendance_rows(s, date(2026, 10, 31), date(2026, 10, 31))))
    assert rows[1:] == [["Test Employee A", "1", "2026-10-31", "08:12:15", "17:35:42", "09:23:27"]]


def test_multiple_employees_ordered_by_first_in(tmp_path):
    s, p = make(tmp_path)
    feed(s, p, [(emp("2", "B"), at(TZ, 2026, 10, 31, 9, 3, 20)), (emp("1", "A"), at(TZ, 2026, 10, 31, 8))])
    rows = parse(render_attendance_csv(attendance_rows(s, date(2026, 10, 31), date(2026, 10, 31))))
    assert [r[0] for r in rows[1:]] == ["A", "B"]


def test_across_midnight_two_days(tmp_path):
    s, p = make(tmp_path)
    feed(s, p, [(emp(), at(TZ, 2026, 10, 31, 23, 50)), (emp(), at(TZ, 2026, 11, 1, 0, 10))])
    rows = parse(render_attendance_csv(attendance_rows(s, date(2026, 10, 31), date(2026, 11, 1))))
    assert [r[2:5] for r in rows[1:]] == [["2026-10-31", "23:50:00", "23:50:00"], ["2026-11-01", "00:10:00", "00:10:00"]]


def test_custom_range_filtering_and_empty(tmp_path):
    s, p = make(tmp_path)
    feed(s, p, [(emp(), at(TZ, 2026, 10, d, 9)) for d in (1, 15, 31)])
    assert [r["day"] for r in attendance_rows(s, date(2026, 10, 10), date(2026, 10, 31))] == ["2026-10-15", "2026-10-31"]
    assert parse(render_attendance_csv(attendance_rows(s, date(2026, 9, 1), date(2026, 9, 30)))) == [CSV_HEADER]


def test_resolve_ranges():
    sat = date(2026, 10, 31)  # Saturday
    assert resolve_range("today", sat) == (sat, sat)
    assert resolve_range("yesterday", sat) == (date(2026, 10, 30),) * 2
    assert resolve_range("this_week", sat) == (date(2026, 10, 26), sat)  # Monday start
    assert resolve_range("this_month", sat) == (date(2026, 10, 1), sat)
    assert resolve_range("yesterday", date(2026, 3, 1)) == (date(2026, 2, 28),) * 2
    assert resolve_range("custom", sat, date(2026, 10, 1), date(2026, 10, 5)) == (date(2026, 10, 1), date(2026, 10, 5))
    for args in (("custom", sat, None, sat), ("custom", sat, sat, date(2026, 10, 1)), ("custom", sat, date(2025, 1, 1), sat), ("bogus", sat)):
        with pytest.raises(ValueError):
            resolve_range(*args)


def test_timezone_decides_the_day(tmp_path):
    t = datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc)  # 01:30 next day in UTC+3
    s_utc, p1 = make(tmp_path, ZoneInfo("UTC"), "u.db")
    s_p3, p2 = make(tmp_path, TZ, "p.db")
    feed(s_utc, p1, [(emp(), t)])
    feed(s_p3, p2, [(emp(), t)])
    assert attendance_rows(s_utc, date(2026, 10, 31), date(2026, 11, 1))[0]["day"] == "2026-10-31"
    assert attendance_rows(s_p3, date(2026, 10, 31), date(2026, 11, 1))[0]["day"] == "2026-11-01"


def test_dst_transition_duration_is_real_elapsed_time(tmp_path):
    s, p = make(tmp_path, BERLIN)
    feed(s, p, [(emp(), at(BERLIN, 2026, 3, 29, 1, 30)), (emp(), at(BERLIN, 2026, 3, 29, 3, 30))])
    r = parse(render_attendance_csv(attendance_rows(s, date(2026, 3, 29), date(2026, 3, 29))))[1]
    assert r[3:] == ["01:30:00", "03:30:00", "01:00:00"]  # wall clock 2 h apart, 1 h elapsed


def test_customers_and_unmatched_never_in_attendance(tmp_path):
    s, p = make(tmp_path)
    feed(
        s,
        p,
        [
            (cust(), at(TZ, 2026, 10, 31, 10)),
            (FaceUnmatched(95, 0.0, None, None, None), at(TZ, 2026, 10, 31, 10)),
            (emp(), at(TZ, 2026, 10, 31, 11)),
        ],
    )
    rows = attendance_rows(s, date(2026, 10, 31), date(2026, 10, 31))
    assert [r["human_id"] for r in rows] == ["1"]
    st = daily(s, date(2026, 10, 31), at(TZ, 2026, 10, 31, 20))
    assert st["known_customers_unique"] == 1 and st["customer_visits"] == 1  # employee not counted


def test_persistence_across_restart(tmp_path):
    s, p = make(tmp_path, name="r.db")
    feed(s, p, [(emp(), at(TZ, 2026, 10, 31, 8))])
    s.close()
    s2, p2 = Store(str(tmp_path / "r.db"), TZ), Processor(ProcessorConfig())
    p2.sessions = s2.load_latest_sessions()
    feed(s2, p2, [(emp(), at(TZ, 2026, 10, 31, 18))])
    r = attendance_rows(s2, date(2026, 10, 31), date(2026, 10, 31))
    assert len(r) == 1 and r[0]["duration_s"] == 10 * 3600


def test_csv_escaping_and_formula_injection(tmp_path):
    s, p = make(tmp_path)
    names = ['=HYPERLINK("http://x")', "+SUM(1)", "-1+2", "@cmd", 'Doe, "Jo"', "سهى Ñandú 李"]
    feed(s, p, [(emp(str(i), n), at(TZ, 2026, 10, 31, 8, i)) for i, n in enumerate(names)])
    text = render_attendance_csv(attendance_rows(s, date(2026, 10, 31), date(2026, 10, 31)))
    got = [r[0] for r in parse(text)[1:]]
    assert got == ["'" + names[0], "'" + names[1], "'" + names[2], "'" + names[3], names[4], names[5]]
    assert '"Doe, ""Jo"""' in text
