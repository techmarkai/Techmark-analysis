"""Typed camera events and processor outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FaceMatch:
    fdid: str
    lib_name: str
    human_id: str
    name: str
    similarity: float
    score: int | None
    camera_time: datetime | None
    ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FaceUnmatched:
    score: int | None
    max_similarity: float | None
    face_rect: dict | None
    rec_rect: dict | None
    camera_time: datetime | None
    ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FaceCapture:
    scores: tuple[int, ...]
    camera_time: datetime | None
    ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class LineCrossing:
    line_id: str
    camera_time: datetime | None
    ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    camera_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RawEvent:
    event_type: str


Event = FaceMatch | FaceUnmatched | FaceCapture | LineCrossing | Heartbeat | RawEvent


# ---- processor outcomes -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    role: str
    human_id: str
    name: str
    start: datetime
    last: datetime


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    session: Session
    new: bool
    similarity: float
    score: int | None
    fdid: str


@dataclass(frozen=True, slots=True)
class AttendanceSeen:
    human_id: str
    name: str
    ts: datetime


@dataclass(frozen=True, slots=True)
class UnmatchedCounted:
    ts: datetime
    score: int | None


@dataclass(frozen=True, slots=True)
class LowQualityCounted:
    ts: datetime
    score: int | None


@dataclass(frozen=True, slots=True)
class OtherMatch:
    fdid: str
    ts: datetime


@dataclass(frozen=True, slots=True)
class LineCounted:
    direction: str  # "in" | "out"
    ts: datetime


Outcome = SessionUpdate | AttendanceSeen | UnmatchedCounted | LowQualityCounted | OtherMatch | LineCounted
