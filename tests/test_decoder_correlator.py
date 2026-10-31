import json
from pathlib import Path

from conftest import FAKE_JPEG, fixture_bytes, fixture_json

from core.correlator import Correlator, image_ids
from core.decoder import decode
from core.models import FaceCapture, FaceMatch, FaceUnmatched, Heartbeat, LineCrossing, RawEvent
from core.stream_parser import Part

J = {"content-type": "application/json"}
X = {"content-type": "application/xml"}


def test_match_decoded():
    ev = decode(Part(J, fixture_bytes("alarm_match_employee.json")))
    assert isinstance(ev, FaceMatch)
    assert (ev.fdid, ev.lib_name, ev.human_id, ev.similarity, ev.score) == ("1", "Employee", "1", 0.97, 87)
    assert ev.camera_time is not None and ev.camera_time.utcoffset() is not None
    assert "TESTPID-0001" in ev.ids
    assert "backgroundImage" not in ev.ids and "faceLibImage" not in ev.ids
    assert not hasattr(ev, "modelData") and "certificate" not in repr(ev)


def test_unmatched_and_capture_decoded():
    ev = decode(Part(J, fixture_bytes("alarm_unmatched.json")))
    assert isinstance(ev, FaceUnmatched) and ev.score == 84 and ev.rec_rect["width"] > 0
    cap = decode(Part(J, fixture_bytes("face_capture.json")))
    assert isinstance(cap, FaceCapture) and cap.scores == (15,)


def test_xml_heartbeat_and_line():
    hb = b"<EventNotificationAlert><eventType>videoloss</eventType><eventState>inactive</eventState></EventNotificationAlert>"
    assert isinstance(decode(Part(X, hb)), Heartbeat)
    ln = (
        b'<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema"><eventType>linedetection</eventType>'
        b"<eventState>active</eventState><DetectionRegionList><DetectionRegionEntry><regionID>2</regionID>"
        b"</DetectionRegionEntry></DetectionRegionList></EventNotificationAlert>"
    )
    ev = decode(Part(X, ln))
    assert isinstance(ev, LineCrossing) and ev.line_id == "2"


def test_bad_inputs_never_raise():
    match = fixture_json("alarm_match_employee.json")
    no_cand = json.loads(json.dumps(match))
    no_cand["alarmResult"][0]["faces"][0]["identify"][0]["candidate"] = []
    assert decode(Part(J, b"{not json")) is None
    assert decode(Part(J, b'{"eventType":"alarmResult"}')) is None
    assert decode(Part(J, json.dumps(no_cand).encode())) is None
    assert decode(Part(X, b"<unclosed")) is None
    assert decode(Part(X, b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')) is None
    assert decode(Part({"content-type": "image/jpeg"}, FAKE_JPEG)) is None
    assert isinstance(decode(Part(J, b'{"eventType":"somethingNew"}')), RawEvent)


def img(**headers) -> Part:
    return Part({"content-type": "image/jpeg", **{k.lower().replace("_", "-"): v for k, v in headers.items()}}, FAKE_JPEG)


def test_image_attached_only_by_identifier():
    c = Correlator(ttl=10)
    c.add_event("evA", {"pidA"}, 0)
    assert c.add_image(img(content_id="<pidA>"), 1) == "evA"
    # Immediately following its event but without an identifier: discarded.
    c.add_event("evB", {"pidB"}, 2)
    assert c.add_image(img(), 2.1) is None
    assert c.uncorrelated_images == 1


def test_identifier_selects_older_event_not_newest():
    c = Correlator(ttl=10)
    c.add_event("old", {"p1"}, 0)
    c.add_event("new", {"p2"}, 1)
    assert c.add_image(img(content_disposition='form-data; name="x"; filename="p1"'), 2) == "old"


def test_unknown_or_expired_identifier_discarded():
    c = Correlator(ttl=2)
    c.add_event("ev", {"p1"}, 0)
    assert c.add_image(img(content_id="zzz"), 1) is None
    assert c.add_image(img(content_id="p1"), 5) is None
    assert c.uncorrelated_images == 2 and c.attached == 0


def test_image_ids_parsing():
    assert image_ids({"content-id": "<abc>", "content-disposition": 'form-data; name="n1"; filename="f1"'}) == {"abc", "n1", "f1"}
    assert image_ids({}) == set()


def test_correlator_has_no_positional_pairing():
    src = (Path(__file__).parents[1] / "custom_components/techmark_analysis/core/correlator.py").read_text()
    assert "[-1]" not in src and "last_event" not in src


def test_hostile_inputs_rejected():
    assert decode(Part(J, b"[1, 2]")) is None
    assert decode(Part(J, b"[" * 100000 + b"]" * 100000)) is None
    utf16_dtd = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'.encode("utf-16")
    assert decode(Part(X, utf16_dtd)) is None
