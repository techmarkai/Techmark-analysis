"""Automatic customer enrollment queue. Disabled by default.

The camera does all face work. HA queues every distinct, good-quality unmatched detection,
then asks the camera, one candidate at a time, to add that face to Customers under the next
permanent visitor number. Later visits resolve through the camera's own matching.

Evidence-based limits (live captures, capability reads):
- Each unmatched detection has a unique pId; the same pId twice is the same detection and is
  enrolled at most once. Nothing links separate detections of one unknown person and the
  camera has no search-by-photo, so repeated detections before the first record exists can
  become separate visitors (no distinct person is ever dropped instead).
- Employee matches and unmatched events are different detections; employees never block visitors.
- The alertStream has no replay. Candidates are held in memory only (a small face crop, never
  the scene, never on disk). A restart loses pending candidates; the coordinator persists their
  non-biometric metadata so the loss is counted and reported, not silent.

Losses are never silent: every dropped candidate increments `losses` (drained by the
coordinator into persistent records and a repair issue).

Retry identity: names are `TMV-<db salt>-<visitor no>`; the salt is random per HA database, so a
reset database cannot reuse an old name. Retries look the record up by that exact name (and
customHumanID when the camera reports it) before re-uploading.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .models import FaceUnmatched


class FaceUnusable(Exception):
    """Image cannot produce an acceptable single-face crop."""


class CameraTransient(Exception):
    """Temporary camera failure: safe to retry (after a lookup)."""


class CameraRejected(Exception):
    """Camera refused the face: do not retry."""


@dataclass
class EnrollConfig:
    enabled: bool = False
    min_score: int = 70
    min_face_px: int = 120
    image_wait_seconds: float = 10.0  # image normally follows its event within ~1 s
    max_attempts: int = 5
    retry_base_seconds: float = 30.0
    max_pending: int = 1000  # metadata entries
    max_pending_bytes: int = 20 * 1024 * 1024  # memory budget for queued face crops (~30 KB each)
    salt: str = "000000"


@dataclass
class _Candidate:
    event: FaceUnmatched
    t: float
    jpeg: bytes | None = None
    visitor_no: int | None = None
    attempts: int = 0
    next_try: float = 0.0


def camera_name(salt: str, visitor_no: int) -> str:
    """Name and customHumanID on the camera; unique per HA database, never a bare number."""
    return f"TMV-{salt}-{visitor_no:06d}"


@dataclass
class Enroller:
    cfg: EnrollConfig
    reserve_visitor: Callable[[], Awaitable[int]]
    link: Callable[[str, int], Awaitable[None]]
    add_face: Callable[[str, bytes], Awaitable[str]]  # (name, jpeg) -> camera PID
    # Must return only PIDs whose record has exactly this name (and customHumanID, when reported).
    find_by_name: Callable[[str], Awaitable[list[str]]]
    stats: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self._queue: OrderedDict[str, _Candidate] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()  # retransmission guard
        self._bytes = 0
        self._lock = asyncio.Lock()
        self._losses: Counter = Counter()

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def pending_bytes(self) -> int:
        return self._bytes

    def _lost(self, reason: str) -> None:
        self.stats[f"lost_{reason}"] += 1
        self._losses[reason] += 1

    def drain_losses(self) -> dict[str, int]:
        out, self._losses = dict(self._losses), Counter()
        return out

    def snapshot(self) -> list[dict]:
        """Non-biometric metadata of pending candidates (for loss accounting across restarts)."""
        return [
            {"key": k, "score": c.event.score, "visitor_no": c.visitor_no, "has_crop": c.jpeg is not None}
            for k, c in self._queue.items()
        ]

    def offer(self, event: FaceUnmatched, t: float) -> None:
        """Queue one unmatched detection. Never blocked by other enrollments or employees."""
        if not self.cfg.enabled:
            return
        if event.score is None or event.score < self.cfg.min_score:
            self.stats["skipped_quality"] += 1
            return
        if not event.ids:
            self.stats["skipped_no_id"] += 1
            return
        key = min(event.ids)
        if key in self._seen:
            self.stats["duplicate_detection"] += 1
            return
        self._seen[key] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        if len(self._queue) >= self.cfg.max_pending:
            self._lost("overflow")
            return
        self._queue[key] = _Candidate(event, t)
        self.stats["queued"] += 1

    def wants(self, event: FaceUnmatched) -> bool:
        return any((c := self._queue.get(k)) is not None and c.jpeg is None for k in event.ids)

    def attach_crop(self, event: FaceUnmatched, jpeg: bytes) -> None:
        """Face crop (from the image correlated to `event` by identifier). Scene is not kept."""
        for key in event.ids:
            cand = self._queue.get(key)
            if cand is None or cand.jpeg is not None:
                continue
            if self._bytes + len(jpeg) > self.cfg.max_pending_bytes:
                del self._queue[key]
                self._lost("overflow")
                return
            cand.jpeg = jpeg
            self._bytes += len(jpeg)
            return

    def reject_image(self, event: FaceUnmatched) -> None:
        """Correlated image had no usable face (quality decision, not a loss)."""
        for key in event.ids:
            if self._queue.pop(key, None) is not None:
                self.stats["dropped_unusable_face"] += 1

    def _remove(self, key: str) -> None:
        cand = self._queue.pop(key, None)
        if cand is not None and cand.jpeg is not None:
            self._bytes -= len(cand.jpeg)

    async def tick(self, t: float) -> list[int]:
        """Process ready candidates one at a time, in arrival order. Returns new visitor numbers."""
        if self._lock.locked():
            return []
        done: list[int] = []
        async with self._lock:
            for key in list(self._queue):
                cand = self._queue.get(key)
                if cand is None or cand.next_try > t:
                    continue
                if cand.jpeg is None:
                    if t - cand.t >= self.cfg.image_wait_seconds:
                        self._remove(key)
                        self._lost("no_image")
                    continue
                result = await self._process(cand, t)
                if result != "retry":
                    self._remove(key)
                if result == "done":
                    done.append(cand.visitor_no)
        return done

    async def _process(self, cand: _Candidate, t: float) -> str:
        if cand.visitor_no is None:
            cand.visitor_no = await self.reserve_visitor()
        name = camera_name(self.cfg.salt, cand.visitor_no)
        try:
            pids = await self.find_by_name(name) if cand.attempts else []
            if not pids:
                pids = [await self.add_face(name, cand.jpeg)]
        except CameraTransient:
            cand.attempts += 1
            if cand.attempts >= self.cfg.max_attempts:
                self._lost("upload_failed")  # reserved number stays retired, never reused
                return "drop"
            cand.next_try = t + self.cfg.retry_base_seconds * 2 ** (cand.attempts - 1)
            self.stats["retries"] += 1
            return "retry"
        except CameraRejected:
            self.stats["camera_rejected"] += 1
            return "drop"
        for pid in pids:
            await self.link(pid, cand.visitor_no)
        await self.link(name, cand.visitor_no)  # in case events report customHumanID instead of PID
        self.stats["enrolled"] += 1
        return "done"
