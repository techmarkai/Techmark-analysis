"""Decode alertStream parts into typed events. Never raises on bad input."""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

from .models import (
    Event,
    FaceCapture,
    FaceMatch,
    FaceUnmatched,
    Heartbeat,
    LineCrossing,
    RawEvent,
)
from .stream_parser import Part
from .xmlsafe import safe_fromstring

_LOGGER = logging.getLogger(__name__)


def decode(part: Part) -> Event | None:
    ct = part.content_type
    try:
        if "json" in ct:
            data = json.loads(part.body)
            return _from_json(data) if isinstance(data, dict) else None
        if "xml" in ct:
            return _from_xml(part.body)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, ET.ParseError) as err:
        _LOGGER.debug("Undecodable %s part (%d bytes): %s", ct, len(part.body), type(err).__name__)
    return None


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _ids(*values) -> frozenset[str]:
    # Only per-event unique identifiers (pId). contentID values such as
    # "backgroundImage" are role labels, not identifiers, and are excluded.
    return frozenset(v for v in values if isinstance(v, str) and v)


def _from_json(d: dict) -> Event | None:
    etype = d.get("eventType", "")
    when = _time(d.get("dateTime"))
    if etype == "alarmResult":
        res = d["alarmResult"][0]
        face = res["faces"][0]
        score = (face.get("score") or {}).get("value")
        target_pid = (res.get("targetAttrs") or {}).get("pId")
        identify = (face.get("identify") or [{}])[0]
        msg = res.get("errorMsg", "")
        if msg == "ok":
            cand = identify["candidate"][0]
            human_pids = [h.get("pId") for h in cand.get("human_data", [])]
            return FaceMatch(
                fdid=str(cand["blacklist_id"]),
                lib_name=cand.get("FDLibName", ""),
                human_id=str(cand["human_id"]),
                name=(cand.get("reserve_field") or {}).get("name", ""),
                similarity=float(cand.get("similarity", identify.get("maxsimilarity", 0.0))),
                score=score,
                camera_time=when,
                ids=_ids(target_pid, *human_pids),
            )
        if msg == "contrastFailed":
            return FaceUnmatched(
                score=score,
                max_similarity=identify.get("maxsimilarity"),
                face_rect=face.get("faceRect"),
                rec_rect=face.get("recommendFaceRect"),
                camera_time=when,
                ids=_ids(target_pid),
            )
        return RawEvent(f"alarmResult:{msg}")
    if etype == "faceCapture":
        cap = d["faceCapture"][0]
        return FaceCapture(
            scores=tuple(f.get("faceScore") for f in cap.get("faces", [])),
            camera_time=when,
            ids=_ids((cap.get("targetAttrs") or {}).get("pId")),
        )
    return RawEvent(etype or "unknown")


def _from_xml(body: bytes) -> Event | None:
    root = safe_fromstring(body)  # raises UnsafeXML (a ValueError) on any DTD/entity
    fields = {el.tag: (el.text or "").strip() for el in root.iter()}
    etype = fields.get("eventType", "")
    when = _time(fields.get("dateTime"))
    if etype == "videoloss" and fields.get("eventState") == "inactive":
        return Heartbeat(when)
    if etype == "linedetection":
        # Payload format assumed (spec A-2); verified in test T-2.
        if fields.get("eventState", "active") != "active":
            return None
        return LineCrossing(line_id=fields.get("regionID") or fields.get("lineID") or "1", camera_time=when)
    return RawEvent(etype or "unknown")
