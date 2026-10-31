import os
import stat
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.models import FaceMatch, FaceUnmatched
from core.processor import Processor, ProcessorConfig
from core.report import render_csv, render_markdown
from core.stats import daily, monthly
from core.store import Store, StoreError

TZ = ZoneInfo("Etc/GMT-3")  # fixed UTC+3, no DST


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(timezone.utc)


def m(fdid="1", hid="1"):
    return FaceMatch(fdid, "lib", hid, f"P{hid}", 0.95, 80, None)


def feed(store, proc, items):
    for ev, t in items:
        store.apply(proc.handle(ev, t))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"), TZ)
    yield s
    s.close()


def test_restart_restores_open_session(tmp_path):
    path = str(tmp_path / "r.db")
    s1 = Store(path, TZ)
    p1 = Processor(ProcessorConfig())
    feed(s1, p1, [(m("2", "5"), local(2026, 11, 1, 10))])
    before = p1.sessions
    s1.close()
    s2 = Store(path, TZ)
    restored = s2.load_latest_sessions()
    assert restored == before
    p2 = Processor(ProcessorConfig(), restored)
    out = p2.handle(m("2", "5"), local(2026, 11, 1, 10, 10))
    assert out[0].new is False and out[0].session.id == before[("customer", "5")].id
    s2.apply(out)
    assert s2.query("SELECT COUNT(*) FROM sessions")[0][0] == 1
    s2.close()


def test_attendance_first_last_out_of_order(store):
    p = Processor(ProcessorConfig())
    feed(store, p, [(m(), local(2026, 11, 1, 12)), (m(), local(2026, 11, 1, 8)), (m(), local(2026, 11, 1, 17))])
    st = daily(store, date(2026, 11, 1), local(2026, 11, 1, 18))
    a = st["attendance"][0]
    assert a["first_seen"].startswith("2026-11-01T08:00") and a["last_seen"].startswith("2026-11-01T17:00")


def test_schema_idempotent_and_no_biometric_columns(tmp_path):
    path = str(tmp_path / "s.db")
    Store(path, TZ).close()
    s = Store(path, TZ)
    cols = {c[1] for t in ("events", "sessions", "attendance") for c in s.query(f"PRAGMA table_info({t})")}
    assert not {c for c in cols if any(w in c.lower() for w in ("image", "model", "cert", "photo"))}
    s.close()


def test_purge(store):
    p = Processor(ProcessorConfig())
    now = local(2026, 11, 1, 10)
    feed(store, p, [(m("2", "1"), now - timedelta(days=500)), (m("2", "2"), now)])
    store.purge(now, 400)
    assert store.query("SELECT human_id FROM sessions") == [("2",)]


def test_write_error_raises_store_error(tmp_path):
    path = tmp_path / "ro.db"
    Store(str(path), TZ).close()
    for f in tmp_path.iterdir():
        os.chmod(f, stat.S_IREAD)
    try:
        s = Store(str(path), TZ)
        with pytest.raises(StoreError):
            s.apply(Processor(ProcessorConfig()).handle(m(), local(2026, 11, 1, 9)))
    except StoreError:
        pass  # opening read-only already failed: also a typed error
    finally:
        for f in tmp_path.iterdir():
            os.chmod(f, stat.S_IWRITE | stat.S_IREAD)


def test_stats_separation_and_returning(store):
    p = Processor(ProcessorConfig())
    feed(
        store,
        p,
        [
            (m("2", "c1"), local(2026, 10, 30, 12)),  # earlier day -> returning later
            (m("1", "e1"), local(2026, 11, 1, 9)),  # employee
            (m("2", "c1"), local(2026, 11, 1, 11)),
            (m("2", "c2"), local(2026, 11, 1, 12)),
            (FaceUnmatched(90, 0.0, None, None, None), local(2026, 11, 1, 12)),
            (FaceUnmatched(10, 0.0, None, None, None), local(2026, 11, 1, 12)),
        ],
    )
    st = daily(store, date(2026, 11, 1), local(2026, 11, 1, 20))
    assert st["employees_present"] == 1
    assert st["known_customers_unique"] == 2
    assert st["returning_customers"] == 1
    assert st["customer_visits"] == 2
    assert st["unmatched_face_events"] == 1 and st["low_quality_faces"] == 1
    assert st["door_entries"] is None
    mo = monthly(store, 2026, 11, local(2026, 11, 30, 20))
    assert mo["known_customers_unique"] == 2 and mo["returning_customers"] == 1


def test_utc_plus3_day_boundary_and_midnight_session(store):
    p = Processor(ProcessorConfig(customer_window_min=30))
    feed(store, p, [(m("2", "x"), local(2026, 11, 1, 23, 59)), (m("2", "x"), local(2026, 11, 2, 0, 1))])
    d1 = daily(store, date(2026, 11, 1), local(2026, 11, 2, 1))
    d2 = daily(store, date(2026, 11, 2), local(2026, 11, 2, 1))
    assert d1["customer_visits"] == 1 and d2["customer_visits"] == 0  # session belongs to start day
    assert store.query("SELECT day FROM events ORDER BY ts") == [("2026-11-01",), ("2026-11-02",)]


def test_gap_reported(store):
    store.start_gap(local(2026, 11, 1, 10), "stream_disconnected")
    store.start_gap(local(2026, 11, 1, 10, 5), "ignored_while_open")
    store.end_gap(local(2026, 11, 1, 10, 30))
    st = daily(store, date(2026, 11, 1), local(2026, 11, 1, 20))
    assert len(st["gaps"]) == 1 and st["gaps"][0]["reason"] == "stream_disconnected"
    assert daily(store, date(2026, 11, 2), local(2026, 11, 2, 20))["gaps"] == []


def test_report_wording(store):
    store.start_gap(local(2026, 11, 1, 10), "x")
    st = daily(store, date(2026, 11, 1), local(2026, 11, 1, 20))
    md = render_markdown(st, "Daily report")
    assert "Unmatched face events (not unique people)" in md and "Known customers (unique)" in md
    assert "footfall" not in md.lower()
    assert "| Door entries (line crossing) | not configured |" in md
    assert "Data gaps" in md
    assert render_csv(st).splitlines()[0] == "metric,value"


def test_report_escapes_untrusted_names():
    st = {"period_start": "d", "period_end": "d", "gaps": [], "attendance": [
        {"day": "d", "human_id": "1", "name": "=HYPERLINK(\"x\")|evil\nrow", "first_seen": "a", "last_seen": "b"}]}
    csv_text = render_csv(st)
    assert "'=HYPERLINK" in csv_text
    md = render_markdown(st, "t")
    assert r"\|evil row" in md
