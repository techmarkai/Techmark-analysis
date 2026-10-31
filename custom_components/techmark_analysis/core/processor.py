"""Classification, face-quality gate and gap-based visit sessions. Pure logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    AttendanceSeen,
    FaceMatch,
    FaceUnmatched,
    LineCounted,
    LineCrossing,
    LowQualityCounted,
    OtherMatch,
    Outcome,
    Session,
    SessionUpdate,
    UnmatchedCounted,
)

EMPLOYEE = "employee"
CUSTOMER = "customer"


@dataclass
class ProcessorConfig:
    min_face_quality: int = 50
    employee_window_min: int = 10
    customer_window_min: int = 30
    roles: dict[str, str] = field(default_factory=lambda: {"1": EMPLOYEE, "2": CUSTOMER})
    line_directions: dict[str, str] = field(default_factory=dict)  # line_id -> "in"/"out"

    def window(self, role: str) -> timedelta:
        return timedelta(minutes=self.employee_window_min if role == EMPLOYEE else self.customer_window_min)


class Processor:
    def __init__(self, cfg: ProcessorConfig, sessions: dict[tuple[str, str], Session] | None = None) -> None:
        self.cfg = cfg
        self.sessions: dict[tuple[str, str], Session] = dict(sessions or {})

    def handle(self, event, now: datetime) -> list[Outcome]:
        if isinstance(event, FaceMatch):
            return self._match(event, now)
        if isinstance(event, FaceUnmatched):
            # Quality gate applies to unmatched events only. Unmatched events are
            # counted raw: no persistent identifier exists, so never deduplicated
            # and never turned into people or sessions.
            if event.score is None or event.score < self.cfg.min_face_quality:
                return [LowQualityCounted(now, event.score)]
            return [UnmatchedCounted(now, event.score)]
        if isinstance(event, LineCrossing):
            direction = self.cfg.line_directions.get(event.line_id)
            return [LineCounted(direction, now)] if direction in ("in", "out") else []
        return []

    def _match(self, ev: FaceMatch, now: datetime) -> list[Outcome]:
        role = self.cfg.roles.get(ev.fdid)
        if role not in (EMPLOYEE, CUSTOMER):
            return [OtherMatch(ev.fdid, now)]
        key = (role, ev.human_id)
        prev = self.sessions.get(key)
        if prev is not None and now - prev.last <= self.cfg.window(role) and now >= prev.last:
            sess = Session(prev.id, role, ev.human_id, ev.name or prev.name, prev.start, now)
            new = False
        else:
            sess = Session(f"{role}:{ev.human_id}:{int(now.timestamp())}", role, ev.human_id, ev.name, now, now)
            new = True
        self.sessions[key] = sess
        out: list[Outcome] = [SessionUpdate(sess, new, ev.similarity, ev.score, ev.fdid)]
        if role == EMPLOYEE:
            out.append(AttendanceSeen(ev.human_id, ev.name, now))
        return out
