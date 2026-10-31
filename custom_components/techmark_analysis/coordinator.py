"""Runtime: owns the alertStream task, processor, store, and derived statistics."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    BACKOFF_MAX_SECONDS,
    CLOCK_SKEW_LIMIT_SECONDS,
    CONF_CUSTOMER_FDID,
    CONF_CUSTOMER_WINDOW,
    CONF_EMPLOYEE_FDID,
    CONF_EMPLOYEE_WINDOW,
    CONF_MIN_FACE_QUALITY,
    CONF_REPORT_TIME,
    CONF_RETENTION_DAYS,
    DB_FILENAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    EVENT_CUSTOMER_SEEN,
    EVENT_EMPLOYEE_SEEN,
    EVENT_UNMATCHED_FACE,
    EXPECTED_APPS,
    REPORT_DIR,
    SIGNAL_UPDATE,
    WATCHDOG_SECONDS,
)
from .core.attendance import attendance_rows, render_attendance_csv, resolve_range
from .core.correlator import Correlator
from .core.decoder import decode
from .core.models import (
    FaceCapture,
    FaceMatch,
    FaceUnmatched,
    LineCrossing,
    SessionUpdate,
    UnmatchedCounted,
)
from .core.processor import CUSTOMER, EMPLOYEE, Processor, ProcessorConfig
from .core.report import render_csv, render_markdown
from .core.stats import daily, monthly
from .core.store import Store, StoreError
from .core.stream_parser import MultipartParser, Part
from .isapi import AuthError, CameraError, HikClient

_LOGGER = logging.getLogger(__name__)


def build_config(options: dict) -> ProcessorConfig:
    o = {**DEFAULT_OPTIONS, **options}
    return ProcessorConfig(
        min_face_quality=int(o[CONF_MIN_FACE_QUALITY]),
        employee_window_min=int(o[CONF_EMPLOYEE_WINDOW]),
        customer_window_min=int(o[CONF_CUSTOMER_WINDOW]),
        roles={str(o[CONF_EMPLOYEE_FDID]): EMPLOYEE, str(o[CONF_CUSTOMER_FDID]): CUSTOMER},
    )


class TechmarkRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HikClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.store: Store | None = None
        self.processor = Processor(build_config(entry.options))
        self.correlator = Correlator()
        self.connected = False
        self.today: dict = {}
        self.month: dict = {}
        self.clock_skew: int | None = None
        self.counters = {"parts_ok": 0, "parts_dropped": 0, "undecodable": 0, "reconnects": 0, "handler_errors": 0}
        self.last_error: str | None = None
        self._skew_issue = False
        self._unsubs: list = []
        self._task: asyncio.Task | None = None

    @property
    def signal(self) -> str:
        return SIGNAL_UPDATE.format(self.entry.entry_id)

    async def _job(self, func, *args):
        return await self.hass.async_add_executor_job(partial(func, *args))

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        tz = ZoneInfo(self.hass.config.time_zone)
        self.store = await self._job(Store, self.hass.config.path(DB_FILENAME), tz)
        self.processor.sessions = await self._job(self.store.load_latest_sessions)
        now = dt_util.utcnow()
        last = await self._job(self.store.last_alive)
        if last is not None and now - last > timedelta(seconds=5):
            await self._job(self.store.start_gap, last, "home_assistant_offline")
        await self._check_apps()
        await self._refresh()
        self._task = self.entry.async_create_background_task(self.hass, self._run(), f"{DOMAIN}_stream")
        rt = dt_util.parse_time(str(self.entry.options.get(CONF_REPORT_TIME, DEFAULT_OPTIONS[CONF_REPORT_TIME])))
        self._unsubs += [
            async_track_time_interval(self.hass, self._alive, timedelta(seconds=60)),
            async_track_time_change(self.hass, self._midnight, hour=0, minute=0, second=5),
            async_track_time_change(self.hass, self._daily_report, hour=rt.hour, minute=rt.minute, second=0),
            async_track_time_change(self.hass, self._monthly_report, hour=0, minute=10, second=0),
        ]

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        if self._task:
            self._task.cancel()
        if self.store:
            try:
                await self._job(self.store.touch_alive, dt_util.utcnow())
            finally:
                await self._job(self.store.close)

    # ---- stream -----------------------------------------------------------

    async def _run(self) -> None:
        backoff = 1
        while True:
            started: float | None = None
            reason = "stream_closed"
            try:
                async with self.client.alert_stream(read_timeout=WATCHDOG_SECONDS) as (boundary, chunks):
                    started = monotonic()
                    self._set_connected(True)
                    await self._job(self.store.end_gap, dt_util.utcnow())
                    parser = MultipartParser(boundary)
                    async for chunk in chunks:
                        for part in parser.feed(chunk):
                            await self._handle_part(part)
                        self.counters["parts_ok"] = parser.parts_ok
                        self.counters["parts_dropped"] = parser.parts_dropped
            except AuthError:
                self._set_connected(False)
                self.last_error = "authentication failed"
                await self._safe_gap("authentication_failed")
                self.entry.async_start_reauth(self.hass)
                return
            except CameraError as err:
                reason = str(err)
            except StoreError as err:
                reason = "store_error"
                _LOGGER.error("Techmark store error: %s", err)
            self.last_error = reason
            self._set_connected(False)
            await self._safe_gap(reason)
            if started is not None and monotonic() - started > 60:
                backoff = 1
            self.counters["reconnects"] += 1
            _LOGGER.debug("alertStream ended (%s); reconnecting in %ss", reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

    async def _safe_gap(self, reason: str) -> None:
        try:
            await self._job(self.store.start_gap, dt_util.utcnow(), reason)
        except StoreError as err:
            _LOGGER.error("Could not record data gap: %s", err)

    @callback
    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            async_dispatcher_send(self.hass, self.signal)

    async def _handle_part(self, part: Part) -> None:
        try:
            await self._process_part(part)
        except StoreError:
            raise
        except Exception:  # never let one bad event stop the stream
            self.counters["handler_errors"] += 1
            _LOGGER.exception("Unexpected error handling camera event")

    async def _process_part(self, part: Part) -> None:
        now = dt_util.utcnow()
        if part.content_type.startswith("image/"):
            # Correlated by identifier only. Images are not used before enrollment (phase 5)
            # and are discarded here; never stored.
            self.correlator.add_image(part, monotonic())
            return
        event = decode(part)
        if event is None:
            self.counters["undecodable"] += 1
            return
        if isinstance(event, (FaceMatch, FaceUnmatched, FaceCapture, LineCrossing)) and event.ids:
            self.correlator.add_event(event, event.ids, monotonic())
        self._check_skew(getattr(event, "camera_time", None), now)
        outcomes = self.processor.handle(event, now)
        if not outcomes:
            return
        await self._job(self.store.apply, outcomes)
        self._fire(outcomes, now)
        await self._refresh()

    @callback
    def _fire(self, outcomes, now: datetime) -> None:
        for o in outcomes:
            if isinstance(o, SessionUpdate):
                s = o.session
                self.hass.bus.async_fire(
                    EVENT_EMPLOYEE_SEEN if s.role == EMPLOYEE else EVENT_CUSTOMER_SEEN,
                    {
                        "role": s.role,
                        "human_id": s.human_id,
                        "name": s.name,
                        "similarity": o.similarity,
                        "score": o.score,
                        "session_new": o.new,
                        "ts": now.isoformat(),
                    },
                )
            elif isinstance(o, UnmatchedCounted):
                self.hass.bus.async_fire(EVENT_UNMATCHED_FACE, {"score": o.score, "ts": now.isoformat()})

    @callback
    def _check_skew(self, camera_time: datetime | None, now: datetime) -> None:
        if camera_time is None or camera_time.utcoffset() is None:
            return
        self.clock_skew = round((now - camera_time).total_seconds())
        bad = abs(self.clock_skew) > CLOCK_SKEW_LIMIT_SECONDS
        if bad and not self._skew_issue:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "clock_skew",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="clock_skew",
                translation_placeholders={"seconds": str(self.clock_skew)},
            )
        elif not bad and self._skew_issue:
            ir.async_delete_issue(self.hass, DOMAIN, "clock_skew")
        self._skew_issue = bad

    async def _check_apps(self) -> None:
        try:
            states = await self.client.app_states()
        except CameraError as err:
            _LOGGER.debug("App state check skipped: %s", err)
            return
        drift = [name for name, running in EXPECTED_APPS.items() if states.get(name) is not running]
        if drift:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "app_state_drift",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="app_state_drift",
                translation_placeholders={"apps": ", ".join(drift)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "app_state_drift")

    # ---- stats / reports ----------------------------------------------------

    async def _refresh(self) -> None:
        now = dt_util.utcnow()
        local = dt_util.as_local(now)
        self.today = await self._job(daily, self.store, local.date(), now)
        self.month = await self._job(monthly, self.store, local.year, local.month, now)
        async_dispatcher_send(self.hass, self.signal)

    async def _alive(self, _now=None) -> None:
        await self._job(self.store.touch_alive, dt_util.utcnow())

    async def _midnight(self, _now=None) -> None:
        days = int(self.entry.options.get(CONF_RETENTION_DAYS, DEFAULT_OPTIONS[CONF_RETENTION_DAYS]))
        await self._job(self.store.purge, dt_util.utcnow(), days)
        await self._refresh()

    async def _daily_report(self, _now=None) -> None:
        await self.async_generate_report("day", dt_util.now().date())

    async def _monthly_report(self, _now=None) -> None:
        today = dt_util.now().date()
        if today.day == 1:
            await self.async_generate_report("month", today - timedelta(days=1))

    async def async_export_attendance(self, kind: str, start: date | None, end: date | None) -> dict:
        """Write the attendance CSV for a range (HA time zone). Raises ValueError on a bad range."""
        first, last = resolve_range(kind, dt_util.now().date(), start, end)
        rows = await self._job(attendance_rows, self.store, first, last)
        text = render_attendance_csv(rows)
        name = f"attendance_{first.isoformat()}_{last.isoformat()}.csv"
        folder = Path(self.hass.config.path(REPORT_DIR))

        def write() -> str:
            folder.mkdir(exist_ok=True)
            path = folder / name
            path.write_text(text, encoding="utf-8-sig", newline="")  # BOM: Excel opens Unicode names correctly
            return str(path)

        return {
            "range": kind,
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
            "rows": len(rows),
            "file": await self._job(write),
            "filename": name,
        }

    async def async_generate_report(self, period: str, day: date) -> dict:
        now = dt_util.utcnow()
        if period == "month":
            stats = await self._job(monthly, self.store, day.year, day.month, now)
            name, title = f"{day:%Y-%m}", f"Techmark monthly report {day:%Y-%m}"
        else:
            stats = await self._job(daily, self.store, day, now)
            name, title = day.isoformat(), f"Techmark daily report {day.isoformat()}"
        folder = Path(self.hass.config.path(REPORT_DIR))
        md, csv_text = render_markdown(stats, title), render_csv(stats)

        def write() -> list[str]:
            folder.mkdir(exist_ok=True)
            paths = [folder / f"{name}.md", folder / f"{name}.csv"]
            paths[0].write_text(md, encoding="utf-8")
            paths[1].write_text(csv_text, encoding="utf-8")
            return [str(p) for p in paths]

        files = await self._job(write)
        return {"stats": stats, "files": files}
