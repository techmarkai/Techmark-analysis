"""SQLite persistence. Synchronous API; the HA layer calls it from the executor.

Stores no images, face model data or certificate numbers.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import (
    AttendanceSeen,
    LineCounted,
    LowQualityCounted,
    OtherMatch,
    Outcome,
    Session,
    SessionUpdate,
    UnmatchedCounted,
)

SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
  role TEXT, fdid TEXT, human_id TEXT, name TEXT, similarity REAL, score INTEGER);
CREATE INDEX IF NOT EXISTS ix_events_day ON events(day, kind);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, role TEXT NOT NULL, human_id TEXT NOT NULL, name TEXT,
  start REAL NOT NULL, last REAL NOT NULL, day TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sessions_day ON sessions(day, role);
CREATE INDEX IF NOT EXISTS ix_sessions_person ON sessions(role, human_id, last);
CREATE TABLE IF NOT EXISTS attendance(
  day TEXT NOT NULL, human_id TEXT NOT NULL, name TEXT, first REAL NOT NULL, last REAL NOT NULL,
  PRIMARY KEY(day, human_id));
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL, reason TEXT);
-- Permanent visitor history (v2). Never purged. AUTOINCREMENT: visitor numbers are never reused.
CREATE TABLE IF NOT EXISTS visitors(visitor_no INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS visitor_camera_ids(
  camera_pid TEXT PRIMARY KEY, visitor_no INTEGER NOT NULL REFERENCES visitors(visitor_no));
CREATE TABLE IF NOT EXISTS visit_days(
  visitor_no INTEGER NOT NULL REFERENCES visitors(visitor_no), day TEXT NOT NULL,
  first REAL NOT NULL, last REAL NOT NULL, events INTEGER NOT NULL,
  PRIMARY KEY(visitor_no, day));
CREATE INDEX IF NOT EXISTS ix_visit_days_day ON visit_days(day);
"""


class StoreError(Exception):
    """Persistent storage failure."""


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


class Store:
    def __init__(self, path: str, tz: ZoneInfo) -> None:
        self.tz = tz
        self._lock = threading.Lock()
        try:
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        except sqlite3.Error as err:
            raise StoreError(str(err)) from err

    def day(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).date().isoformat()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, fn) -> None:
        with self._lock:
            try:
                self._db.execute("BEGIN")
                fn(self._db)
                self._db.execute("COMMIT")
            except (sqlite3.Error, StoreError) as err:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(err, StoreError):
                    raise
                raise StoreError(str(err)) from err

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---- writes ---------------------------------------------------------

    def apply(self, outcomes: list[Outcome]) -> None:
        def run(db: sqlite3.Connection) -> None:
            for o in outcomes:
                if isinstance(o, SessionUpdate):
                    s = o.session
                    db.execute(
                        "INSERT INTO events(ts, day, kind, role, fdid, human_id, name, similarity, score)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (_ts(s.last), self.day(s.last), "match", s.role, o.fdid, s.human_id, s.name, o.similarity, o.score),
                    )
                    db.execute(
                        "INSERT INTO sessions(id, role, human_id, name, start, last, day) VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(id) DO UPDATE SET last=excluded.last, name=excluded.name",
                        (s.id, s.role, s.human_id, s.name, _ts(s.start), _ts(s.last), self.day(s.start)),
                    )
                    if s.role == "customer":
                        self._visit(db, s.human_id, s.last)
                elif isinstance(o, AttendanceSeen):
                    db.execute(
                        "INSERT INTO attendance(day, human_id, name, first, last) VALUES(?,?,?,?,?)"
                        " ON CONFLICT(day, human_id) DO UPDATE SET"
                        " first=MIN(first, excluded.first), last=MAX(last, excluded.last), name=excluded.name",
                        (self.day(o.ts), o.human_id, o.name, _ts(o.ts), _ts(o.ts)),
                    )
                elif isinstance(o, (UnmatchedCounted, LowQualityCounted)):
                    kind = "unmatched" if isinstance(o, UnmatchedCounted) else "low_quality"
                    db.execute(
                        "INSERT INTO events(ts, day, kind, score) VALUES(?,?,?,?)",
                        (_ts(o.ts), self.day(o.ts), kind, o.score),
                    )
                elif isinstance(o, OtherMatch):
                    db.execute(
                        "INSERT INTO events(ts, day, kind, fdid) VALUES(?,?,?,?)",
                        (_ts(o.ts), self.day(o.ts), "other_match", o.fdid),
                    )
                elif isinstance(o, LineCounted):
                    db.execute(
                        "INSERT INTO events(ts, day, kind) VALUES(?,?,?)",
                        (_ts(o.ts), self.day(o.ts), f"line_{o.direction}"),
                    )

        self._write(run)

    # ---- permanent visitors -----------------------------------------------

    @staticmethod
    def _new_visitor(db: sqlite3.Connection, ts: datetime) -> int:
        return db.execute("INSERT INTO visitors(created) VALUES(?)", (_ts(ts),)).lastrowid

    def _visit(self, db: sqlite3.Connection, camera_pid: str, ts: datetime) -> None:
        """One visit-day per visitor per local day; repeats only widen first/last and count events."""
        row = db.execute("SELECT visitor_no FROM visitor_camera_ids WHERE camera_pid=?", (camera_pid,)).fetchone()
        if row is None:
            vno = self._new_visitor(db, ts)
            db.execute("INSERT INTO visitor_camera_ids(camera_pid, visitor_no) VALUES(?,?)", (camera_pid, vno))
        else:
            vno = row[0]
        db.execute(
            "INSERT INTO visit_days(visitor_no, day, first, last, events) VALUES(?,?,?,?,1)"
            " ON CONFLICT(visitor_no, day) DO UPDATE SET"
            " first=MIN(first, excluded.first), last=MAX(last, excluded.last), events=events+1",
            (vno, self.day(ts), _ts(ts), _ts(ts)),
        )

    def create_visitor(self, now: datetime) -> int:
        """Reserve the next permanent visitor number (used by enrollment)."""
        out: list[int] = []
        self._write(lambda db: out.append(self._new_visitor(db, now)))
        return out[0]

    def link_camera_id(self, camera_pid: str, visitor_no: int) -> None:
        """Map a camera person ID to a visitor. Never silently remaps an existing link."""

        def run(db):
            if db.execute("SELECT 1 FROM visitors WHERE visitor_no=?", (visitor_no,)).fetchone() is None:
                raise StoreError(f"unknown visitor {visitor_no}")
            row = db.execute("SELECT visitor_no FROM visitor_camera_ids WHERE camera_pid=?", (camera_pid,)).fetchone()
            if row is not None and row[0] != visitor_no:
                raise StoreError(f"camera id already linked to visitor {row[0]}")
            db.execute("INSERT OR IGNORE INTO visitor_camera_ids(camera_pid, visitor_no) VALUES(?,?)", (camera_pid, visitor_no))

        self._write(run)

    def start_gap(self, start: datetime, reason: str) -> None:
        def run(db):
            if db.execute("SELECT 1 FROM gaps WHERE end IS NULL").fetchone() is None:
                db.execute("INSERT INTO gaps(start, reason) VALUES(?,?)", (_ts(start), reason))

        self._write(run)

    def end_gap(self, end: datetime) -> None:
        self._write(lambda db: db.execute("UPDATE gaps SET end=? WHERE end IS NULL", (_ts(end),)))

    def touch_alive(self, now: datetime) -> None:
        self._write(
            lambda db: db.execute(
                "INSERT INTO meta(key, value) VALUES('last_alive', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(_ts(now)),),
            )
        )

    def last_alive(self) -> datetime | None:
        row = self.query("SELECT value FROM meta WHERE key='last_alive'")
        return _dt(float(row[0][0])) if row else None

    def purge(self, now: datetime, older_than_days: int) -> None:
        cutoff = _ts(now - timedelta(days=older_than_days))

        def run(db):
            db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            db.execute("DELETE FROM sessions WHERE last < ?", (cutoff,))
            db.execute("DELETE FROM attendance WHERE last < ?", (cutoff,))
            db.execute("DELETE FROM gaps WHERE end IS NOT NULL AND end < ?", (cutoff,))

        self._write(run)

    # ---- reads ----------------------------------------------------------

    def load_latest_sessions(self) -> dict[tuple[str, str], Session]:
        rows = self.query(
            "SELECT s.id, s.role, s.human_id, s.name, s.start, s.last FROM sessions s"
            " JOIN (SELECT role, human_id, MAX(last) AS m FROM sessions GROUP BY role, human_id) x"
            " ON s.role = x.role AND s.human_id = x.human_id AND s.last = x.m"
        )
        return {(r[1], r[2]): Session(r[0], r[1], r[2], r[3] or "", _dt(r[4]), _dt(r[5])) for r in rows}

    def gaps_between(self, start: datetime, end: datetime, now: datetime) -> list[tuple[datetime, datetime | None, str]]:
        rows = self.query(
            "SELECT start, end, reason FROM gaps WHERE start < ? AND COALESCE(end, ?) > ? ORDER BY start",
            (_ts(end), _ts(now), _ts(start)),
        )
        return [(_dt(a), _dt(b) if b is not None else None, r) for a, b, r in rows]
