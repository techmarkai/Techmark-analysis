"""The four primary sensors: Unique customers today / this month, Employees present today,
Returning customers this month (visit-day based, HA local time zone)."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from core.entities import OBSOLETE_SENSOR_KEYS, PRIMARY_SENSOR_KEYS, obsolete_unique_ids
from core.models import FaceMatch
from core.processor import Processor, ProcessorConfig
from core.stats import daily, monthly
from core.store import Store

TZ = ZoneInfo("Etc/GMT-3")  # fixed UTC+3


def at(y, mo, d, h, mi=0, tz=TZ):
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(timezone.utc)


def cust(pid, name=""):
    return FaceMatch("2", "Customers", pid, name, 0.9, 80, None)


def emp(pid):
    return FaceMatch("1", "Employee", pid, f"Employee {pid}", 0.95, 80, None)


@pytest.fixture
def sp(tmp_path):
    s = Store(str(tmp_path / "s.db"), TZ)
    yield s, Processor(ProcessorConfig(customer_window_min=30))
    s.close()


def feed(s, p, items):
    for ev, t in items:
        s.apply(p.handle(ev, t))


def today(s, d, now=None):
    return daily(s, d, now or at(d.year, d.month, d.day, 23))


def month(s, y, m):
    return monthly(s, y, m, at(y, m, 28, 23))


def test_exactly_four_primary_sensors_and_obsolete_cleanup():
    assert PRIMARY_SENSOR_KEYS == (
        "known_customers_today", "known_customers_month", "employees_present_today", "returning_customers_month")
    assert not set(PRIMARY_SENSOR_KEYS) & OBSOLETE_SENSOR_KEYS
    ids = ["SER_known_customers_today", "SER_customer_visits_today", "SER_door_exits_today",
           "SER_camera_clock_skew", "OTHER_customer_visits_today", "SER_low_quality_faces_today"]
    assert obsolete_unique_ids(ids, "SER") == [
        "SER_customer_visits_today", "SER_door_exits_today", "SER_low_quality_faces_today"]


def test_duplicate_recognitions_count_one_unique_customer(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(2026, 11, 2, 10, m)) for m in (0, 0, 1, 2, 3)])
    st = today(s, date(2026, 11, 2))
    assert st["unique_customers"] == 1 and st["customer_visits"] == 1


def test_repeat_sessions_same_day_still_one_unique_customer(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(2026, 11, 2, 10)), (cust("1"), at(2026, 11, 2, 15))])  # 5 h apart: 2 sessions
    st = today(s, date(2026, 11, 2))
    assert st["customer_visits"] == 2 and st["unique_customers"] == 1


def test_two_identities_of_one_visitor_count_once(sp):
    s, p = sp
    v = s.create_visitor(at(2026, 11, 1, 9))
    s.link_camera_id("9", v)  # legacy camera-ID link
    s.link_camera_id("TMV-legacy-name", v)
    feed(s, p, [(cust("9"), at(2026, 11, 2, 10)), (cust("TMV-legacy-name"), at(2026, 11, 2, 11))])
    assert today(s, date(2026, 11, 2))["unique_customers"] == 1  # was 2 with session identities


def test_unique_customers_month_distinct_across_days(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(2026, 11, d, 10)) for d in (1, 2, 3)] + [(cust("2"), at(2026, 11, 3, 12))])
    assert month(s, 2026, 11)["unique_customers"] == 2


def test_returning_month_uses_history_and_excludes_first_timers(sp):
    s, p = sp
    feed(s, p, [
        (cust("old"), at(2026, 10, 15, 10)),  # visited last month
        (cust("old"), at(2026, 11, 5, 10)),   # and this month -> returning
        (cust("new"), at(2026, 11, 5, 11)),   # first time this month
        (cust("new"), at(2026, 11, 20, 11)),  # again this month: still NOT returning
        (cust("gone"), at(2026, 9, 1, 10)),   # only before this month: not counted
    ])
    nov = month(s, 2026, 11)
    assert (nov["unique_customers"], nov["returning_customers"]) == (2, 1)


def test_month_boundary_in_local_time(sp):
    s, p = sp
    # 23:30 Oct 31 and 00:30 Nov 1, local UTC+3
    feed(s, p, [(cust("x"), at(2026, 10, 31, 23, 30)), (cust("x"), at(2026, 11, 1, 0, 30))])
    assert month(s, 2026, 10)["unique_customers"] == 1
    nov = month(s, 2026, 11)
    assert nov["unique_customers"] == 1 and nov["returning_customers"] == 1  # Oct visit day precedes November


def test_timezone_boundary_decides_day(tmp_path):
    t = datetime(2026, 10, 31, 21, 30, tzinfo=timezone.utc)  # 00:30 Nov 1 in UTC+3, still Oct 31 in UTC
    a, b = Store(str(tmp_path / "a.db"), ZoneInfo("UTC")), Store(str(tmp_path / "b.db"), TZ)
    a.apply(Processor(ProcessorConfig()).handle(cust("1"), t))
    b.apply(Processor(ProcessorConfig()).handle(cust("1"), t))
    assert daily(a, date(2026, 10, 31), t)["unique_customers"] == 1
    assert daily(b, date(2026, 11, 1), t)["unique_customers"] == 1
    assert daily(b, date(2026, 10, 31), t)["unique_customers"] == 0
    a.close()
    b.close()


def test_employees_present_unchanged_and_never_customers(sp):
    s, p = sp
    feed(s, p, [(emp("1"), at(2026, 11, 2, 8)), (emp("1"), at(2026, 11, 2, 17)), (emp("2"), at(2026, 11, 2, 9))])
    st = today(s, date(2026, 11, 2))
    assert st["employees_present"] == 2 and st["unique_customers"] == 0


def test_erased_and_retired_visitors_not_counted(sp):
    s, p = sp
    feed(s, p, [(cust("1"), at(2026, 11, 2, 10)), (cust("2"), at(2026, 11, 2, 11))])
    s.erase_visitor(1, at(2026, 11, 2, 12))
    s.add_review(2, "TMV-x", ["2"], "failed", at(2026, 11, 2, 12))
    feed(s, p, [(cust("2"), at(2026, 11, 2, 13))])  # retired after failed modeling: no new visit day
    assert today(s, date(2026, 11, 2))["unique_customers"] == 1  # visitor 2's earlier day remains history
