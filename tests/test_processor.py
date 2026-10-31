from datetime import datetime, timedelta, timezone

from core.models import (
    AttendanceSeen,
    FaceMatch,
    FaceUnmatched,
    LineCounted,
    LineCrossing,
    LowQualityCounted,
    OtherMatch,
    SessionUpdate,
    UnmatchedCounted,
)
from core.processor import Processor, ProcessorConfig

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def m(fdid="1", hid="1", score=80) -> FaceMatch:
    return FaceMatch(fdid, "lib", hid, f"P{hid}", 0.95, score, None)


def u(score) -> FaceUnmatched:
    return FaceUnmatched(score, 0.05, None, None, None)


def sessions(p, events):
    out = []
    for ev, t in events:
        out += [o for o in p.handle(ev, t) if isinstance(o, SessionUpdate)]
    return out


def test_quality_gate_not_applied_to_matches():
    outs = Processor(ProcessorConfig()).handle(m(score=20), T0)
    assert any(isinstance(o, SessionUpdate) for o in outs)


def test_unmatched_quality_threshold():
    p = Processor(ProcessorConfig(min_face_quality=50))
    assert isinstance(p.handle(u(49), T0)[0], LowQualityCounted)
    assert isinstance(p.handle(u(50), T0)[0], UnmatchedCounted)
    assert isinstance(p.handle(u(None), T0)[0], LowQualityCounted)
    p.cfg.min_face_quality = 30
    assert isinstance(p.handle(u(40), T0)[0], UnmatchedCounted)


def test_unmatched_never_creates_people():
    p = Processor(ProcessorConfig())
    for i in range(20):
        outs = p.handle(u(90), T0 + timedelta(seconds=i))
        assert all(isinstance(o, UnmatchedCounted) for o in outs)
    assert p.sessions == {}


def test_duplicate_employee_matches_one_session_all_attendance():
    p = Processor(ProcessorConfig())
    outs = [o for i in range(3) for o in p.handle(m(), T0 + timedelta(seconds=3 * i))]
    s = [o for o in outs if isinstance(o, SessionUpdate)]
    assert [o.new for o in s] == [True, False, False] and len({o.session.id for o in s}) == 1
    assert sum(isinstance(o, AttendanceSeen) for o in outs) == 3


def test_employee_window_edges():
    s = sessions(Processor(ProcessorConfig(employee_window_min=10)), [(m(), T0), (m(), T0 + timedelta(minutes=9)), (m(), T0 + timedelta(minutes=20))])
    assert [o.new for o in s] == [True, False, True]


def test_customer_window_edges_and_long_stay():
    p = Processor(ProcessorConfig(customer_window_min=30))
    s = sessions(p, [(m("2"), T0), (m("2"), T0 + timedelta(minutes=29)), (m("2"), T0 + timedelta(minutes=29 + 31))])
    assert [o.new for o in s] == [True, False, True]
    p2 = Processor(ProcessorConfig(customer_window_min=30))
    stay = sessions(p2, [(m("2"), T0 + timedelta(minutes=20 * i)) for i in range(10)])
    assert sum(o.new for o in stay) == 1


def test_customer_never_attendance_and_employee_never_customer():
    p = Processor(ProcessorConfig())
    c = p.handle(m("2", "7"), T0)
    assert not any(isinstance(o, AttendanceSeen) for o in c)
    e = p.handle(m("1", "7"), T0)
    assert {o.session.role for o in e if isinstance(o, SessionUpdate)} == {"employee"}


def test_unknown_library_is_other_match():
    assert Processor(ProcessorConfig()).handle(m("9"), T0) == [OtherMatch("9", T0)]


def test_line_requires_mapping():
    p = Processor(ProcessorConfig())
    assert p.handle(LineCrossing("1", None), T0) == []
    p.cfg.line_directions = {"1": "in"}
    assert p.handle(LineCrossing("1", None), T0) == [LineCounted("in", T0)]


def test_clock_going_backwards_opens_new_session():
    s = sessions(Processor(ProcessorConfig()), [(m(), T0), (m(), T0 - timedelta(minutes=1))])
    assert [o.new for o in s] == [True, True]
