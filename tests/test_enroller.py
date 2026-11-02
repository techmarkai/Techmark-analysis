import asyncio
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from core.correlator import image_ids
from core.enroller import (
    CameraRejected,
    CameraTransient,
    EnrollConfig,
    Enroller,
    EnrollPersistence,
    FaceUnusable,
    camera_name,
    summarize,
)
from core.faces import exact_pids, normalize_modeling
from core.models import FaceMatch, FaceUnmatched
from core.processor import Processor, ProcessorConfig
from core.store import Store
from core.visitors import visitor_report

RECT = {"x": 0.4, "y": 0.3, "width": 0.1, "height": 0.2}
REC = {"x": 0.35, "y": 0.25, "width": 0.2, "height": 0.35}
SALT = "ab12cd34"


def ev(pid, score=90):
    return FaceUnmatched(score, 0.0, RECT, REC, None, frozenset({pid}))


class Fake:
    def __init__(self, add_results=None, found=None):
        self.next_vno, self.links, self.added, self.add_results, self.found = 0, [], [], list(add_results or []), found or []
        self.status_results, self.checked, self.log = [], [], []
        self.persist = EnrollPersistence(
            verifying=self._p("verifying"), finished=self._p("finished"), review=self._p("review")
        )

    def _p(self, kind):
        async def rec(*args):
            self.log.append((kind, *args))

        return rec

    async def reserve(self):
        self.next_vno += 1
        return self.next_vno

    async def link(self, pid, vno):
        self.links.append((pid, vno))

    async def add_face(self, name, jpeg):
        self.added.append(name)
        r = self.add_results.pop(0) if self.add_results else "900"
        if isinstance(r, Exception):
            raise r
        return r

    async def find(self, name):
        return self.found

    async def status(self, name):
        self.checked.append(name)
        r = self.status_results.pop(0) if self.status_results else [("900", "success")]
        if isinstance(r, Exception):
            raise r
        return r


def make(fake, **cfg):
    cfg = {"verify_first_seconds": 0, **cfg}
    return Enroller(EnrollConfig(enabled=True, image_wait_seconds=10, retry_base_seconds=30, salt=SALT, **cfg),
                    fake.reserve, fake.link, fake.add_face, fake.find, fake.status, fake.persist)


def run(coro):
    return asyncio.run(coro)


def queue(e, pid, t, score=90, crop=b"jpeg"):
    e.offer(ev(pid, score), t)
    if crop is not None and e.wants(ev(pid, score)):
        e.attach_crop(ev(pid, score), crop)


def name(n):
    return camera_name(SALT, n)


def test_disabled_by_default_never_enrolls():
    f = Fake()
    e = Enroller(EnrollConfig(), f.reserve, f.link, f.add_face, f.find)
    queue(e, "p1", 0)
    assert run(e.tick(100)) == [] and f.added == [] and e.pending == 0


def test_two_people_simultaneously_both_enrolled():
    f = Fake(add_results=["901", "902"])
    e = make(f)
    queue(e, "a", 0)
    queue(e, "b", 0)
    assert run(e.tick(1)) == [1, 2] and f.added == [name(1), name(2)]


def test_three_arrive_during_active_enrollment_none_lost():
    f = Fake()
    e = make(f)
    queue(e, "first", 0)
    started, release = asyncio.Event(), asyncio.Event()
    orig = f.add_face

    async def slow_add(n, jpeg):
        started.set()
        await release.wait()
        return await orig(n, jpeg)

    e.add_face = slow_add

    async def scenario():
        worker = asyncio.create_task(e.tick(1))
        await started.wait()
        for pid in ("x", "y", "z"):
            queue(e, pid, 2)
        assert await e.tick(2) == []  # serialized: no parallel enrollment
        release.set()
        return await worker, await e.tick(3)

    first, rest = run(scenario())
    assert first == [1] and rest == [2, 3, 4] and e.pending == 0 and e.drain_losses() == {}


def test_employee_with_visitor_does_not_block_visitor():
    e = make(Fake())
    assert not hasattr(e, "note_employee")  # no employee suppression exists
    queue(e, "visitor", 0)
    assert run(e.tick(1)) == [1]


def test_same_detection_id_enrolled_once():
    f = Fake()
    e = make(f)
    queue(e, "same", 0)
    queue(e, "same", 0.5)
    run(e.tick(1))
    queue(e, "same", 5)
    assert run(e.tick(6)) == [] and f.added == [name(1)] and e.stats["duplicate_detection"] == 2


def test_different_ids_close_together_both_retained():
    e = make(Fake())
    queue(e, "d1", 0)
    queue(e, "d2", 0.1)
    assert e.pending == 2 and len(run(e.tick(1))) == 2


def test_failed_enrollment_retried_with_same_number_no_duplicate_record():
    f = Fake(add_results=[CameraTransient("timeout")])
    e = make(f)
    queue(e, "a", 0)
    assert run(e.tick(1)) == [] and e.pending == 1
    assert run(e.tick(10)) == []  # backing off
    f.found = ["950"]  # the first upload had actually succeeded
    assert run(e.tick(31)) == [1]
    assert f.added == [name(1)] and ("950", 1) in f.links and f.next_vno == 1


def test_retry_gives_up_counted_as_loss_number_retired():
    f = Fake(add_results=[CameraTransient("x")] * 2)
    e = make(f, max_attempts=2)
    queue(e, "a", 0)
    run(e.tick(1))
    run(e.tick(100))
    assert e.pending == 0 and e.drain_losses() == {"upload_failed": 1}
    queue(e, "b", 200)
    assert run(e.tick(201)) == [2]


def test_rejections_and_missing_image():
    f = Fake(add_results=[CameraRejected("no face")])
    e = make(f)
    queue(e, "rej", 0)
    queue(e, "noimg", 0, crop=None)
    run(e.tick(1))
    assert e.pending == 1
    run(e.tick(11))
    assert e.pending == 0 and e.stats["camera_rejected"] == 1 and e.drain_losses() == {"no_image": 1}
    e.offer(ev("bad"), 20)
    e.reject_image(ev("bad"))  # unusable face: quality decision, not a loss
    assert e.pending == 0 and e.stats["dropped_unusable_face"] == 1 and e.drain_losses() == {}


def test_overflow_never_silent_and_budget_by_bytes():
    e = make(Fake(), max_pending_bytes=100)
    queue(e, "a", 0, crop=b"x" * 60)
    queue(e, "b", 0, crop=b"x" * 60)  # would exceed 100 bytes
    assert e.pending == 1 and e.pending_bytes == 60
    e2 = make(Fake(), max_pending=1)
    queue(e2, "c", 0)
    queue(e2, "d", 0)
    assert e.drain_losses() == {"overflow": 1} and e2.drain_losses() == {"overflow": 1}
    run(e.tick(1))
    assert e.pending_bytes == 0  # memory released after enrollment


def test_low_quality_not_queued():
    e = make(Fake())
    queue(e, "low", 0, score=60)
    assert e.pending == 0 and e.stats["skipped_quality"] == 1


def test_restart_loss_is_recorded_not_silent(tmp_path):
    tz = ZoneInfo("Etc/GMT-3")
    t = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    s = Store(str(tmp_path / "q.db"), tz)
    salt = s.enroll_salt()

    async def reserve():
        return s.create_visitor(t)

    f = Fake(add_results=[CameraTransient("down")])
    e = Enroller(EnrollConfig(enabled=True, salt=salt), reserve, f.link, f.add_face, f.find, f.status, f.persist)
    queue(e, "a", 0)
    queue(e, "b", 0, crop=None)
    run(e.tick(1))  # "a" holds number 1 after a failed upload; "b" awaits its image
    s.save_pending(e.snapshot(), t)
    s.close()
    s2 = Store(str(tmp_path / "q.db"), tz)  # restart
    left = s2.take_pending()
    assert [r[0] for r in left] == ["a", "b"] and left[0][3] == 1
    assert s2.take_pending() == []  # reported once
    s2.record_enroll_losses({"restart": len(left)}, t)
    assert s2.enroll_loss_totals() == {"restart": 2}
    assert s2.enroll_salt() == salt and s2.create_visitor(t) == 2  # same salt, number 1 retired
    assert not any(isinstance(v, bytes) for row in left for v in row)  # metadata only, no image data
    s2.close()


def test_new_database_gets_new_salt(tmp_path):
    tz = ZoneInfo("Etc/GMT-3")
    a, b = Store(str(tmp_path / "1.db"), tz), Store(str(tmp_path / "2.db"), tz)
    assert a.enroll_salt() != b.enroll_salt()  # a reset DB never reuses old camera names
    a.close()
    b.close()


def test_lookup_requires_exact_identity():
    me = name(7)
    recs = [
        {"name": me, "person_id": "10", "custom_human_id": me},  # ours
        {"name": me, "person_id": "11", "custom_human_id": ""},  # firmware does not report it
        {"name": me + "0", "person_id": "12", "custom_human_id": ""},  # fuzzy search hit
        {"name": me, "person_id": "13", "custom_human_id": "OTHER"},  # someone else's id
        {"name": name(70), "person_id": "14", "custom_human_id": ""},
        {"name": me, "person_id": None, "custom_human_id": me},  # malformed PID
    ]
    assert exact_pids(recs, me) == ["10", "11"]


def test_role_labels_never_identifiers():
    h = {"content-id": "backgroundImage", "content-disposition": 'form-data; name="PID123"; filename="backgroundImage"'}
    assert image_ids(h) == {"PID123"}


def test_erase_visitor(tmp_path):
    s = Store(str(tmp_path / "e.db"), ZoneInfo("Etc/GMT-3"))
    p = Processor(ProcessorConfig())
    t = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    s.apply(p.handle(FaceMatch("2", "Customers", "55", "", 0.9, 80, None), t))
    assert s.erase_visitor(1, t) is True and s.erase_visitor(42, t) is False
    assert visitor_report(s, 1) == {"visitor_id": 1, "erased": True, "visits": []}
    assert s.query("SELECT COUNT(*) FROM events WHERE human_id='55'")[0][0] == 0
    s.apply(Processor(ProcessorConfig()).handle(FaceMatch("2", "Customers", "55", "", 0.9, 80, None), t))
    assert s.query("SELECT COUNT(*) FROM visit_days")[0][0] == 0  # face still on camera: ignored
    assert s.create_visitor(t) == 2  # erased number never reused
    s.close()


def test_face_crop_validates_and_fits_upload_limit():
    pil = pytest.importorskip("PIL.Image")
    from core.facecrop import MAX_UPLOAD_BYTES, prepare_face

    buf = io.BytesIO()
    pil.new("RGB", (2560, 1504), (120, 130, 140)).save(buf, "JPEG")  # synthetic, no face
    scene = buf.getvalue()
    out = prepare_face(scene, ev("a"), 120)
    assert len(out) <= MAX_UPLOAD_BYTES and pil.open(io.BytesIO(out)).size == (512, 526)
    with pytest.raises(FaceUnusable):
        prepare_face(scene, ev("a"), 300)  # 0.1 * 2560 = 256 px < 300
    with pytest.raises(FaceUnusable):
        prepare_face(scene, FaceUnmatched(90, 0, {"x": 0.95, "y": 0, "width": 0.1, "height": 0.1}, REC, None), 10)
    with pytest.raises(FaceUnusable):
        prepare_face(b"not a jpeg", ev("a"), 10)


# ---- modeling verification -------------------------------------------------


def test_upload_is_not_enrollment_until_modeling_succeeds():
    f = Fake()
    f.status_results = [[("900", "pending")], [("900", "success")]]
    e = make(f, verify_first_seconds=10)
    queue(e, "a", 0)
    assert run(e.tick(1)) == [] and e.stats["uploaded"] == 1 and e.stats["enrolled"] == 0
    assert e.verifying == 1 and f.log[0] == ("verifying", 1, name(1), ["900"])
    assert run(e.tick(5)) == [] and f.checked == []  # first check not due yet
    assert run(e.tick(11)) == [] and e.verifying == 1  # pending -> backoff
    assert run(e.tick(30)) == [] and len(f.checked) == 1  # next check at 11 + 20
    assert run(e.tick(31)) == [1] and e.stats["enrolled"] == 1 and e.verifying == 0
    assert ("finished", 1) in f.log and not any(x[0] == "review" for x in f.log)


def test_modeling_failure_retires_flags_and_never_reuploads():
    f = Fake()
    f.status_results = [[("900", "failed")]]
    e = make(f)
    queue(e, "a", 0)
    assert run(e.tick(1)) == [] and e.stats["enrolled"] == 0
    assert ("review", 1, name(1), ["900"], "failed") in f.log and ("finished", 1) in f.log
    assert e.drain_losses() == {"modeling_failed": 1}
    run(e.tick(500))
    assert f.added == [name(1)]  # no second upload for the failed identity
    queue(e, "b", 600)  # same person returns unmatched: new detection, new number (camera cannot link them)
    assert run(e.tick(601)) == [2] and f.added == [name(1), name(2)]


def test_unknown_and_unavailable_states_retry_then_unverified():
    f = Fake()
    f.status_results = [[("900", "unknown")], CameraTransient("down"), [], [("900", "pending")]]
    e = make(f, verify_first_seconds=1, verify_max_checks=4)
    queue(e, "a", 0)
    for t in (1, 3, 5, 9, 17, 50):
        run(e.tick(t))
    assert [k for k in e.stats if k.startswith("verify_")] == ["verify_unknown", "verify_unavailable", "verify_pending"]
    assert e.stats["verify_unavailable"] == 2 and e.verifying == 0
    assert ("review", 1, name(1), ["900"], "unverified") in f.log
    assert e.drain_losses() == {"modeling_unverified": 1} and f.added == [name(1)]


def test_status_summary_rules():
    assert summarize(None) == "unavailable" and summarize([]) == "unavailable"
    assert summarize([("1", "failed"), ("2", "success")]) == "success"  # one good record suffices
    assert summarize([("1", "failed"), ("2", "failed")]) == "failed"
    assert summarize([("1", "failed"), ("2", "pending")]) == "pending"
    assert summarize([("1", "unknown")]) == "unknown"
    assert [normalize_modeling(s) for s in ("success", "FAILED", "modeling", "", "weird")] == [
        "success", "failed", "pending", "unknown", "unknown"]


def test_upload_retry_then_verification_single_record():
    f = Fake(add_results=[CameraTransient("timeout")], found=["950"])
    f.status_results = [[("950", "success")]]
    e = make(f)
    queue(e, "a", 0)
    run(e.tick(1))
    assert run(e.tick(31)) == [1] and f.added == [name(1)] and f.checked == [name(1)]


def test_restart_resumes_verification_without_image(tmp_path):
    tz = ZoneInfo("Etc/GMT-3")
    t = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    s = Store(str(tmp_path / "v.db"), tz)
    s.create_visitor(t)
    s.save_verifying(1, name(1), ["900"], t)
    s.close()
    s2 = Store(str(tmp_path / "v.db"), tz)  # restart
    rows = s2.load_verifying()
    assert rows == [(1, name(1), ["900"])]
    f = Fake()
    e = make(f)
    e.resume_verification(rows, 0)
    assert run(e.tick(0)) == [1] and f.added == [] and f.checked == [name(1)]  # checked, never re-uploaded
    s2.drop_verifying(1)
    assert s2.load_verifying() == []
    s2.close()


def test_failed_review_retires_visitor_and_preserves_history(tmp_path):
    tz = ZoneInfo("Etc/GMT-3")
    t = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    s = Store(str(tmp_path / "r.db"), tz)
    p = Processor(ProcessorConfig())
    s.apply(p.handle(FaceMatch("2", "Customers", "77", "", 0.9, 80, None), t))  # visitor 1 (existing history)
    v2 = s.create_visitor(t)
    s.link_camera_id("900", v2)
    s.add_review(v2, name(v2), ["900"], "failed", t)
    s.apply(Processor(ProcessorConfig()).handle(FaceMatch("2", "Customers", "900", "", 0.9, 80, None), t))
    assert visitor_report(s, v2)["visit_days"] == 0  # retired: never counted
    assert visitor_report(s, 1)["visit_days"] == 1  # existing history untouched
    assert s.reviews() == [{"visitor_no": v2, "camera_name": name(v2), "camera_ids": ["900"], "status": "failed"}]
    s.add_review(v2, name(v2), ["900"], "failed", t)  # idempotent
    assert len(s.reviews()) == 1
    s.close()
