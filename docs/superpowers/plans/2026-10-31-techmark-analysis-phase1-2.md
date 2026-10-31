# Techmark Analysis — Implementation Plan, Phases 1–2

Spec: `docs/superpowers/specs/2026-10-31-techmark-analysis-design.md` (commit 0c7914c).
Scope: core library (phase 1) and HA integration (phase 2). No camera writes, no enrollment code, no GitHub push. Each task = test first → implement → green → local commit.

Graphify: no graph exists (repo had no code). After T8, run `graphify update .` once (local, no LLM) to check module boundaries; no further Graphify work.

## Ground rules (apply to every task)

- Test first; a task is done only when its listed tests pass and the full suite stays green.
- Fixtures: only `tests/fixtures/*.json` (sanitized) plus synthetic data built in tests. JPEG bodies in tests are a constant 1×1 grey JPEG (non-biometric). No real images, `modelData`, names, or certificate numbers.
- Phase 1 modules import no `homeassistant` code and do no network I/O.
- Never log host, username, password, auth headers, or URLs containing credentials.
- Safety invariants enforced by tests: unmatched events never create people/sessions; employees never appear in customer stats; quality gate never applies to matches; no code path writes camera configuration.

## Test environments

- Phase 1: native Python 3.13, `pytest`.
- Phase 2: Docker image matching HA 2026.10's Python, `pytest-homeassistant-custom-component` pinned to the HA 2026.10 release.

---

## Phase 0 — Scaffold

**T0. Repository skeleton**
- Files: `pyproject.toml` (pytest config, ruff), `requirements_test.txt`, `custom_components/techmark_analysis/{__init__.py,manifest.json,const.py}`, `hacs.json`, `tests/conftest.py`, `tests/helpers.py` (multipart builder + 1×1 JPEG constant).
- `manifest.json`: `domain`, `name`, `version 0.1.0`, `config_flow: true`, `iot_class: local_push`, `integration_type: device`, `requirements: []` (Pillow added in phase 5, see D-2).
- Accept: `pytest` collects and runs; `tests/test_manifest.py` asserts required keys and that `requirements` contains no unpinned entry.

## Phase 1 — Core library (pure Python)

**T1. Multipart parser** — `stream_parser.py`
- `MultipartParser(boundary, max_part_bytes=8 MiB).feed(chunk) -> list[Part]`; `Part(headers: dict[str,str], body: bytes)`. Uses `Content-Length` when present, otherwise boundary scan; resyncs on the next boundary after any error.
- Tests: well-formed stream split into 1-byte and random-size chunks yields identical parts; missing `Content-Length`; truncated final part is not emitted; garbage between parts skipped; malformed header block → part dropped, next part parsed; oversize part dropped and counted; boundary string appearing inside a JPEG body with correct `Content-Length` does not split it.
- Accept: all tests pass; parser exposes counters `parts_ok`, `parts_dropped`.

**T2. Event decoder** — `models.py`, `decoder.py`
- Dataclasses: `FaceMatch(fdid, lib_name, human_id, name, similarity, score, camera_time, ids)`, `FaceUnmatched(score, max_similarity, face_rect, rec_rect, camera_time, ids)`, `FaceCapture`, `LineCrossing(line_id, direction, camera_time)`, `Heartbeat`, `RawEvent(event_type)`. `ids` = every `pId`/`contentID` value found in the event.
- `decode(part) -> Event | None` for JSON and XML parts.
- Tests: `alarm_match_employee.json` → `FaceMatch(fdid="1", lib_name="Employee", human_id="1", similarity=0.97, score=87)`; `alarm_unmatched.json` → `FaceUnmatched(score=84)`; `face_capture.json` → `FaceCapture`; synthetic XML `videoloss` → `Heartbeat`; synthetic `linedetection` XML → `LineCrossing` (format marked assumed, A-2); unknown type → `RawEvent`; invalid JSON, missing `alarmResult`, empty `candidate` list → `None`, no exception; `certificateNumber`/`modelData` never present on decoded objects.

**T3. Image correlation** — `correlator.py`
- `Correlator(ttl=2.0)`: `add_event(event, t)`, `add_image(part, t) -> AttachResult`, `expire(t) -> list[Event]`. An image is attached only when an identifier from its headers (`image_ids(headers)`: `Content-ID`, `filename`, `name`) equals an id in a pending event's `ids`. Otherwise it is discarded and `uncorrelated_images` increments. Events are released after TTL with or without images.
- Tests: matching `Content-ID` attaches; image without identifiers is **not** attached even when it immediately follows its event; identifier matching an older pending event attaches to that event, not the newest; identifier matching nothing → discarded; image after TTL → discarded; two interleaved events each get only their own image; events always released (images optional).
- Accept: no code path attaches by order (also asserted by a grep-style test that `correlator.py` has no index-based pairing); `image_ids` is the single place to adapt after T-3.

**T4. Processor** — `processor.py`
- `ProcessorConfig(min_face_quality=50, employee_window_min=10, customer_window_min=30, roles={"1":"employee","2":"customer"})`. `Processor(cfg, sessions: dict).handle(event, now_utc) -> list[Outcome]`.
- Outcomes: `AttendanceSeen(human_id, name, ts)`, `SessionOpened/SessionExtended(role, human_id, session_id, ts)`, `UnmatchedCounted(ts)`, `LowQualityCounted(ts)`, `OtherMatch(fdid, ts)`, `LineCounted(direction, ts)`.
- Tests: match with score 20 still processed (gate not applied to matches); unmatched score 49 → `LowQualityCounted`, 50 → `UnmatchedCounted`; unmatched never yields session/attendance outcomes; 3 identical employee matches 3 s apart → 1 session, 3 `AttendanceSeen`; employee gap 9 min → extend, 11 min → new; customer gap 29 → extend, 31 → new; customer seen every 20 min for 3 h → 1 session; unknown FDID → `OtherMatch` only; windows read from config (changing config changes behaviour); employee FDID never produces customer outcomes.

**T5. SQLite store** — `store.py`
- Schema v1 + `schema_version` table: `events(ts_utc, cam_ts, kind, role, fdid, human_id, name, similarity, score)`, `sessions(id, role, human_id, start_utc, last_utc, open)`, `attendance(day, human_id, name, first_utc, last_utc)`, `counters(day, metric, value)`, `gaps(start_utc, end_utc, reason)`, `meta(key, value)` (holds `last_alive_utc`). WAL mode; synchronous API (HA layer calls it in the executor).
- `apply(outcomes)`, `load_open_sessions()`, `start_gap/end_gap`, `touch_alive(ts)`, `purge(older_than_days)`.
- Tests: apply → close → reopen → `load_open_sessions()` equals pre-close state; processor built from restored sessions extends the same session after "restart"; attendance first/last correct under out-of-order inserts; schema init idempotent; column list contains no image/model/certificate columns; purge removes only old rows; a write error (read-only file) raises a typed `StoreError`.

**T6. Statistics** — `stats.py`
- `daily(store, date, tz)`, `monthly(store, year, month, tz)` → dict: employees present + attendance rows, known customers unique, returning customers (session on an earlier local day), customer visits, unmatched events, low-quality faces, other matches, gaps overlapping the period.
- Tests: employee rows never counted in any customer metric; unmatched never in unique/returning customers; returning logic across days; UTC+3 zone boundary (events at 23:59 and 00:01 local fall on different days); midnight-crossing session counted on its start day; gap overlapping the day is reported.

**T7. Reports** — `report.py`
- `render_markdown(stats)`, `render_csv(stats)`; pure functions.
- Tests: labels exact: "Unmatched face events (not unique people)", "Known customers (unique)"; the word "footfall" never applied to face metrics; door metrics shown as "not configured" when absent; gap warning line present when gaps exist; CSV header stable.

**T8. Phase 1 checkpoint**
- Full suite green; `ruff check`; `graphify update .` once; fix any boundary violation it surfaces (phase 1 importing HA, cycles). Local commit tagged `phase1`.

## Phase 2 — Home Assistant integration (Docker test env)

**T9. ISAPI client (read-only)** — `isapi.py`
- `HikClient(httpx_client, host, user, password)`: `device_info()`, `get_time()`, `list_libraries()`, `search_faces(fdid, start, limit)`, `app_states()`, `alert_stream()` (async context manager yielding `(boundary, chunk_iter)`). Digest auth via `httpx.DigestAuth`. No write methods in phases 1–2.
- Tests (`httpx.MockTransport`): parse sample XML responses; 401 → `AuthError`; timeout → `CameraUnavailable`; exception messages and `caplog` never contain password or auth header; boundary extracted from response `Content-Type`.

**T10. Config and options flow** — `config_flow.py`, `strings.json`, `translations/en.json`
- User step: host, username, password → `device_info()`; unique_id = serial number. Reauth step. Options: `min_face_quality` (50, 0–100), `employee_window_min` (10, 5–240), `customer_window_min` (30, 5–240), employee/customer library FDID (pre-selected by name `Employee`/`Customers` from `list_libraries()`), `daily_report_time` (23:55), `retention_days` (400). No enrollment option in phases 1–2.
- Tests: success creates entry; `cannot_connect`; `invalid_auth`; `already_configured`; reauth updates password; options persist and validate ranges.

**T11. Stream runner and coordinator** — `coordinator.py`, `__init__.py`
- Setup: open store (executor), restore sessions, record restart gap `last_alive_utc → now`, read-only `app_states()` check (drift → repair issue), start runner task. Runner: connect → parser → decoder → correlator → processor → store (executor) → dispatcher signal. Heartbeat watchdog 60 s; backoff 1→2→4…→60 s, reset after 60 s healthy; disconnect opens a gap, reconnect closes it; `touch_alive` every 60 s; clock skew per event, > 120 s → repair issue; 401 → `entry.async_start_reauth`, runner stops. Unload cancels runner and closes store.
- Tests (fake stream source): malformed part mid-stream → later events still processed; connection drop → reconnect with backoff, one gap recorded; silence > 60 s → watchdog reconnect; HA restart (unload, setup again on same DB) → open session restored and restart gap recorded; 401 → reauth flow started, no reconnect loop; app-state drift → repair issue, no camera write calls (MockTransport asserts only GET requests issued, except the alertStream GET); unload leaves no running task.

**T12. Entities** — `sensor.py`, `binary_sensor.py`
- Spec §4.4 entities. Values computed from store on setup, updated via dispatcher. Daily/monthly counters: `state_class: total` with `last_reset` at local midnight / month start (HA time zone). Door sensors unavailable until a line mapping exists.
- Tests: fixture event sequence → expected states; employee match never changes customer sensors; unmatched changes only the unmatched sensor; midnight rollover (freezer) resets daily sensors and sets `last_reset`; `stream_connected` follows runner state.

**T13. Bus events** — in coordinator
- `techmark_analysis_employee_seen`, `_customer_seen`, `_unmatched_face`; payload: role, human_id, name, similarity, score, session_new, ts.
- Tests: fired once per outcome; payload has no image, `modelData`, or certificate fields; unmatched payload has no person fields.

**T14. Services and scheduled reports** — `services.yaml`, `services.py`
- `generate_report(period, date)` → writes `<config>/techmark_reports/…{md,csv}` and returns response data; `list_faces(library)` read-only. Daily report at `daily_report_time`, monthly on day 1 at 00:10 (`async_track_time_change`), written via executor.
- Tests: service returns stats and writes both files; invalid date rejected; scheduled callback produces files for the previous day/month; no enroll/delete service registered.

**T15. Diagnostics** — `diagnostics.py`
- Config entry diagnostics: options, runner state, parser/correlator counters, last gap; `async_redact_data` on host/username/password.
- Test: output contains counters and no host, username, or password.

**T16. Phase 2 checkpoint**
- Full suite green in Docker; `ruff check`; manifest/strings consistency test; `graphify update .`; local commit tagged `phase2`. Nothing pushed.

---

## Blocking assumptions and decisions

| ID | Item | Blocks | Proposal |
|---|---|---|---|
| D-1 | Image part header format is unknown (raw capture deleted). T3 is built strictly; if headers carry no identifier, all images are dropped. | Nothing in phases 1–2 (images only matter for phase-5 enrollment). | Proceed; confirm in T-3. |
| D-2 | Pillow pin: phases 1–2 never import Pillow. | Nothing now. | Add the pinned requirement in phase 5 with the enrollment code (avoids an unused install dependency). Needs your OK as a deviation from spec §4. |
| D-3 | Line-crossing payload format (A-2) assumed. | Nothing; door sensors stay unavailable. | Proceed; verify in T-2. |
| D-4 | Phase 2 tests require Docker (HA test harness does not run on Windows). | T9–T16 test execution. | Approve use of a local Python container for tests. |
