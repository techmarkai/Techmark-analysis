"""Automatic customer enrollment queue with camera face-modeling verification. Disabled by default.

Pipeline per candidate (one at a time, arrival order):
  queued -> (face crop attached) -> uploaded to Customers -> VERIFYING -> success | failed | unverified
Upload success is NOT enrollment success: the camera must report its face model was built
(ModelingStatus) for the exact record identity (name and customHumanID).

- success: counted as enrolled.
- failed: never counted; visitor number retired; loss "modeling_failed"; record flagged for
  admin review. Camera records are never deleted or overwritten automatically.
- pending / unknown status / camera unavailable: re-checked with bounded backoff; when checks
  run out: loss "modeling_unverified", flagged for review. The link is kept so that, if the
  camera later finishes modeling, matches still count for the right visitor.
Verification never re-uploads, so a failed identity cannot spawn more camera records.

Evidence-based limits (live captures, capability reads):
- Each unmatched detection has a unique pId; the same pId twice is enrolled at most once.
  Nothing links separate detections of one unknown person and the camera has no
  search-by-photo, so repeated detections before a record exists can become separate visitors.
- Employees never block visitors (different detections, nothing correlates them).
- Candidates waiting for upload hold a small crop in memory only; a restart loses them (counted).
  Verifications need no image: they are persisted (name and IDs only) and resumed after restart.

Names are `TMV-<db salt>-<visitor no>`; the salt is random per HA database.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .models import FaceUnmatched

SUCCESS, FAILED, PENDING, UNKNOWN, UNAVAILABLE = "success", "failed", "pending", "unknown", "unavailable"


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
    verify_first_seconds: float = 10.0  # first modeling-status check after upload
    verify_max_checks: int = 6  # backoff doubles: ~10 min total by default
    salt: str = "000000"


@dataclass
class _Candidate:
    event: FaceUnmatched
    t: float
    jpeg: bytes | None = None
    visitor_no: int | None = None
    attempts: int = 0
    next_try: float = 0.0
    tried: bool = False  # an upload was attempted: always look the name up before uploading again


@dataclass
class _Verify:
    visitor_no: int
    name: str
    pids: list[str]
    checks: int = 0
    next_try: float = 0.0


@dataclass
class EnrollPersistence:
    """Durable bookkeeping (names/IDs only, never images)."""

    verifying: Callable[[int, str, list[str]], Awaitable[None]]
    finished: Callable[[int], Awaitable[None]]
    review: Callable[[int, str, list[str], str], Awaitable[None]]


def camera_name(salt: str, visitor_no: int) -> str:
    """Name and customHumanID on the camera; unique per HA database, never a bare number."""
    return f"TMV-{salt}-{visitor_no:06d}"


def summarize(results: list[tuple[str, str]] | None) -> str:
    """Overall modeling state for one identity from [(pid, normalized status)]."""
    if not results:
        return UNAVAILABLE  # camera unreachable, or record not (yet) visible
    states = {s for _, s in results}
    if SUCCESS in states:
        return SUCCESS
    if states == {FAILED}:
        return FAILED
    return PENDING if PENDING in states else UNKNOWN


async def _noop(*_args) -> None:
    return None


@dataclass
class Enroller:
    cfg: EnrollConfig
    reserve_visitor: Callable[[], Awaitable[int]]
    link: Callable[[str, int], Awaitable[None]]
    add_face: Callable[[str, bytes], Awaitable[str]]  # (name, jpeg) -> camera PID
    # Must return only PIDs whose record has exactly this name (and customHumanID, when reported).
    find_by_name: Callable[[str], Awaitable[list[str]]]
    # Exact-identity modeling status: name -> [(pid, success|failed|pending|unknown)].
    check_status: Callable[[str], Awaitable[list[tuple[str, str]]]] | None = None
    persist: EnrollPersistence | None = None
    stats: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self._queue: OrderedDict[str, _Candidate] = OrderedDict()
        self._verify: OrderedDict[int, _Verify] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()  # retransmission guard
        self._bytes = 0
        self._lock = asyncio.Lock()
        self._losses: Counter = Counter()
        if self.persist is None:
            self.persist = EnrollPersistence(_noop, _noop, _noop)

    # ---- state ------------------------------------------------------------

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def verifying(self) -> int:
        return len(self._verify)

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
        """Non-biometric metadata of candidates not yet uploaded (restart loss accounting)."""
        return [
            {"key": k, "score": c.event.score, "visitor_no": c.visitor_no, "has_crop": c.jpeg is not None}
            for k, c in self._queue.items()
        ]

    def resume_verification(self, rows: list[tuple[int, str, list[str]]], t: float) -> None:
        """Re-arm modeling checks persisted before a restart (no image needed)."""
        for vno, name, pids in rows:
            self._verify.setdefault(vno, _Verify(vno, name, list(pids), next_try=t))
            self.stats["verify_resumed"] += 1

    # ---- intake -----------------------------------------------------------

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

    # ---- processing ---------------------------------------------------------

    async def tick(self, t: float) -> list[int]:
        """Upload ready candidates, then check modeling. Returns visitor numbers whose modeling succeeded."""
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
                try:
                    result = await self._upload(cand, t)
                except Exception:  # noqa: BLE001 - unexpected failure: keep candidate, never re-upload blindly
                    self.stats["errors"] += 1
                    cand.attempts += 1
                    if cand.attempts >= self.cfg.max_attempts:
                        self._remove(key)
                        self._lost("upload_failed")
                    else:
                        cand.next_try = t + self.cfg.retry_base_seconds
                    continue
                if result != "retry":
                    self._remove(key)
            for vno in list(self._verify):
                v = self._verify[vno]
                if v.next_try > t:
                    continue
                try:
                    if await self._check(v, t):
                        done.append(vno)
                except Exception:  # noqa: BLE001 - bookkeeping failure: re-check later, never re-upload
                    self.stats["errors"] += 1
                    v.next_try = t + self.cfg.verify_first_seconds
        return done

    async def _upload(self, cand: _Candidate, t: float) -> str:
        if cand.visitor_no is None:
            cand.visitor_no = await self.reserve_visitor()
        name = camera_name(self.cfg.salt, cand.visitor_no)
        try:
            pids = await self.find_by_name(name) if cand.tried else []
            if not pids:
                cand.tried = True
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
        # Identity is the unique name (match events carry it); camera PIDs are reused after deletion,
        # so they are kept only as verification bookkeeping, never linked as identity.
        await self.link(name, cand.visitor_no)
        self.stats["uploaded"] += 1
        await self.persist.verifying(cand.visitor_no, name, pids)
        self._verify[cand.visitor_no] = _Verify(cand.visitor_no, name, pids, next_try=t + self.cfg.verify_first_seconds)
        return "uploaded"

    async def _check(self, v: _Verify, t: float) -> bool:
        """One modeling-status check. Returns True only on confirmed success. Never re-uploads."""
        try:
            results = await self.check_status(v.name) if self.check_status else None
        except CameraTransient:
            results = None
        state = summarize(results)
        self.stats[f"verify_{state}"] += 1
        if results:
            v.pids = sorted({*v.pids, *(p for p, _ in results)})
        if state == SUCCESS:
            del self._verify[v.visitor_no]
            await self.persist.finished(v.visitor_no)
            self.stats["enrolled"] += 1
            return True
        if state == FAILED:
            del self._verify[v.visitor_no]
            await self.persist.review(v.visitor_no, v.name, v.pids, FAILED)  # retires the number
            await self.persist.finished(v.visitor_no)
            self._lost("modeling_failed")
            return False
        v.checks += 1
        if v.checks >= self.cfg.verify_max_checks:
            del self._verify[v.visitor_no]
            await self.persist.review(v.visitor_no, v.name, v.pids, "unverified")
            await self.persist.finished(v.visitor_no)
            self._lost("modeling_unverified")
            return False
        v.next_try = t + self.cfg.verify_first_seconds * 2 ** v.checks
        return False
