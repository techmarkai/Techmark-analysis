# Techmark Analysis — Design

Home Assistant custom integration for the Hikvision iDS-2CD7146G2-IZHSY (LAN address `<CAMERA_IP>`, e.g. `192.0.2.30`; firmware V5.9.10 build 250228).

**Principle:** the camera performs all AI (face capture, face modelling, face comparison, line crossing). Home Assistant only receives, stores, correlates, calculates, reports, and manages face-library records through the camera API. No CompreFace, Double Take, or any other recognition in HA.

## 1. Evidence base

### 1.1 Confirmed on the device (2026-10-31)

| Capability | Evidence |
|---|---|
| Face Capture + Comparison and Modeling + Smart Event + AI Open Platform run concurrently | `GET /ISAPI/Custom/OpenPlatform/App` → all `runStatus=true` |
| multidimensional PDC cannot start alongside them | `PUT .../App/7/start` → `noMemory. app start failed` |
| No native counter without PDC | `isSupportCounting`, `isSupportFramesPeopleCounting`, `isSupportFaceCaptureStatistics` = false |
| Face libraries | FDID 1 `Employee` (threshold 90, 1 record); FDID 2 `Customers` (threshold 80, empty). Max 10 libs × 30 000 faces |
| Library management API | `POST /ISAPI/Intelligent/FDLib/FDSearch` (namespaced XML) works; capabilities advertise append/edit/delete |
| Face comparison enabled | `faceContrast enable=true`, `contrastFailureAlarmUpload=true` |
| All event types on one `alertStream` connection | heartbeat (`videoloss`, ~10 s), `faceCapture`, `alarmResult` multiplexed, ~15 min, no drop |
| Comparison results delivered without trigger linkage | `alarmResult` arrived with `faceContrast-1` linkage empty (linkage PUT is rejected: `unknown event type:69`) |
| Match payload | `errorMsg:"ok"`, `candidate.blacklist_id` (FDID), `FDLibName`, `human_id`, `reserve_field.name`, `similarity`, `faces[].score.value`, `dateTime` |
| Unmatched payload | `errorMsg:"contrastFailed"`, `maxsimilarity`, `score.value`, `targetAttrs.pId` (unique per event), `recommendFaceRect` |
| Images | match: scene 2560×1504 + library face 800×1000; unmatched: scene only |
| Repeats | same person → separate events seconds apart, new `faceId`/`pId`, same `human_id` |
| Low-quality captures | unmatched events with `score` 10–15 observed; cannot be attributed to a person |
| Line crossing | 4 lines, direction `any/left-right/right-left`, target `human`, `center` linkage present; all rules **disabled** |
| Push alternative | 3 HTTP-host slots, all unconfigured (not used in v1) |

### 1.2 Assumptions (not yet observed)

- A-1: Matches against `Customers` carry the same structure as `Employee` (`blacklist_id: "2"`, `FDLibName: "Customers"`).
- A-2: Line-crossing events arrive on the same `alertStream` as `eventType: "linedetection"`.
- A-3: The camera accepts a second concurrent `alertStream` client (HA + diagnostics).
- A-4: Face enrollment accepts a cropped JPEG via `POST /ISAPI/Intelligent/FDLib/FaceDataRecord` multipart.
- A-5: The camera can reach an NTP server once DNS or an IP server is configured.
- A-6: Resolved — Pillow declared in `manifest.json` requirements.

### 1.3 Remaining tests

| ID | Test | Needs approval for |
|---|---|---|
| T-1 | Customer match payload (A-1) | Enrolling one consenting adult |
| T-2 | Line-crossing accuracy vs manual count, concurrent with face apps (A-2) | Drawing/enabling lines |
| T-3 | Events reach HA via integration; part headers / image pairing; reconnect after camera reboot | — |
| T-4 | Two concurrent stream clients (A-3) | — |
| T-5 | NTP sync (A-5) | NTP change |
| T-6 | Enrollment round-trip and subsequent `Customers` match (A-4) | Enrollment |
| T-7 | Enrollment from event `modelData` (no crop) | Enrollment |
| T-8 | SD card present; `FCSearch` backfill feasibility (read-only) | — |

## 2. Camera time (recommendation, not applied)

Current: `timeMode=manual`, `localTime=2026-10-31T21:23+03:00`, `timeZone=CST-3:00:00`, NTP `time.windows.com`, DNS `0.0.0.0/0.0.0.0`. The hostname cannot resolve, so NTP cannot work as configured; manual time drifts.

Recommended (adapt to the site's IANA time zone):

- `timeMode=NTP`
- `timeZone` matching the site (Hikvision POSIX sign is inverted: `CST-3:00:00` means UTC+3)
- DNS: primary `<GATEWAY_IP>` (e.g. `192.0.2.254`), secondary `1.1.1.1` — or an IP-addressed NTP server if DNS is not wanted
- NTP server: `pool.ntp.org` (or the gateway, if it serves NTP)
- `synchronizeInterval=60` minutes

Set Home Assistant's time zone to the site's IANA zone; reports use it for day boundaries.

The integration treats **HA receive time as authoritative** and stores camera time alongside. Skew > 120 s raises a HA repair issue.

## 3. Metrics and their meaning

| Metric | Definition | Is NOT |
|---|---|---|
| Door entries / exits | Line-crossing events per direction (after T-2) | Exact footfall — merged crossings and alarm cooldowns undercount |
| Employees present | Distinct `human_id` from the employee library today | — |
| Attendance | Per employee per day: first seen, last seen, number of visits | Time clock of record |
| Known customers (unique) | Distinct `human_id` from the customer library today/month | All customers |
| Returning customers | Known customers with a visit on an earlier day | — |
| Customer visits | Deduplicated sessions of known customers | — |
| Unmatched face events | Quality-passing `contrastFailed` events, counted raw | **Not** unique people, **not** new customers |
| Low-quality faces | Unmatched events with `score` below threshold | Excluded from all customer statistics |
| Face coverage | Quality-passing face events ÷ door entries (after T-2) | — |

Reports always print door entries and face-based figures side by side and never label face counts as footfall.
Unmatched face events are stored, counted, and reported separately from known/unique customer counts and are never added to them.

## 4. Architecture

```
camera alertStream ──► stream.py (multipart parse) ──► processor.py (classify, quality, dedupe)
                                                          │
                                ┌─────────────────────────┼──────────────────────┐
                                ▼                         ▼                      ▼
                          store.py (SQLite)        HA bus events          coordinator state
                                │                                                │
                          stats.py / report.py ──────────────────────────► sensor.py
isapi.py (library mgmt, deviceInfo, time check) ◄── services
```

Repository `Techmark-analysis` (GitHub `techmarkai`), HACS-installable:

```
custom_components/techmark_analysis/
  manifest.json  const.py            (hacs.json lives at repo root)
  isapi.py        async ISAPI client: digest auth via HA's httpx client; deviceInfo, time, FDLib search/append/delete
  stream.py       alertStream reader + pure multipart parser → event dataclasses
  models.py       FaceMatch, FaceUnmatched, FaceCapture, LineCrossing, Heartbeat
  processor.py    pure logic: library→role mapping, quality gate, dedupe windows
  store.py        sqlite3 (stdlib) in <config>/techmark_analysis.db, executor-run
  stats.py        daily/monthly aggregation queries
  report.py       Markdown + CSV report writer
  coordinator.py  owns stream task, processor, store; pushes state to entities
  config_flow.py  host/user/password + options
  sensor.py  binary_sensor.py  services.yaml  strings.json  translations/en.json
tests/            pytest + pytest-homeassistant-custom-component, sanitized fixtures
```

Runtime dependency: `"requirements": ["Pillow==X.Y.Z"]` in `manifest.json`, explicitly pinned, used only for enrollment cropping. The stdlib has no JPEG codec, so cropping without Pillow is not feasible. Presence in HA is not assumed. The pin is set at implementation to the Pillow version in HA 2026.10 `package_constraints.txt`, because a different pin conflicts with HA core and blocks installation.

### 4.1 Units

- **isapi.py** — `HikClient(host, user, password)`: `device_info()`, `get_time()`, `search_faces(fdid)`, `add_face(fdid, name, jpeg)`, `delete_face(fdid, fpid)`, `open_alert_stream()`. Raises `AuthError`, `CameraError`. No business logic.
- **stream.py** — `parse_parts(bytes_iter) -> Iterator[Part]` (pure, boundary-agnostic) and `to_event(part, next_images) -> Event | None`. Unknown event types become `RawEvent` and are logged at debug.
- **processor.py** — `Processor(config).handle(event, now) -> list[Outcome]`. Pure; no I/O; fully unit-tested.
- **store.py** — tables `events` (normalized, no images, no model data), `sessions` (deduped visits), `attendance` (day, human_id, first, last, visits). Retention configurable (default 400 days).
- **stats.py / report.py** — read-only queries; reports written to `<config>/techmark_reports/YYYY-MM[-DD].{md,csv}`.

### 4.2 Classification rules

1. `alarmResult` + `errorMsg == "ok"` → look up `blacklist_id` in the library-role map (set in options, auto-detected by name `Employee` / `Customers`). Unknown FDID → logged, counted as `other_match`, not in customer stats. **The face-quality gate does not apply to matches**; the camera's library threshold decides (a genuine employee match scored 52).
2. `alarmResult` + `contrastFailed`:
   - `score < min_face_quality` (option, default **50**) → `low_quality` counter only.
   - else → `unmatched` event, counted raw with **no deduplication** (no persistent camera identifier exists; merging by time would imply identity). Never increments a people count.
3. `faceCapture` → stored for coverage diagnostics only; the paired `alarmResult` carries the decision.
4. `linedetection` → entry/exit counter by configured line ID → direction mapping (inactive until T-2).
5. Heartbeat → watchdog only.

### 4.3 Duplicate suppression

Per `(role, human_id)`: a match within `window` of the **previous match** (gap-based) extends the current session; otherwise opens a new session. Options (minutes, range 5–240): employee **10**, customer **30**. A long stay stays one visit; two visits closer than the window merge (known undercount). A session crossing midnight belongs to its start day. Sessions, not raw events, feed visit counts.

Attendance first/last-seen are updated on every match regardless of windows. "Last seen" is not "departed": staff leaving without facing the camera show an early last-seen.

Day boundaries and all report periods use HA's configured time zone (`hass.config.time_zone`). Sessions use HA receive time; camera time is stored for reference and skew checks only.

Session state (role, human_id, session start, last match, open/closed) is written to the SQLite `sessions` table on every match and restored on startup, so a restart neither opens spurious sessions nor loses open ones.

### 4.4 Entities

- `binary_sensor.techmark_stream_connected`
- `sensor.techmark_employees_present_today` (attrs: names, first/last seen)
- `sensor.techmark_known_customers_today`, `sensor.techmark_returning_customers_today`, `sensor.techmark_customer_visits_today`
- `sensor.techmark_unmatched_face_events_today`, `sensor.techmark_low_quality_faces_today`
- `sensor.techmark_door_entries_today`, `sensor.techmark_door_exits_today` (unavailable until line mapping configured)
- `sensor.techmark_camera_clock_skew`
- Month-to-date variants for customer metrics.

Bus events: `techmark_analysis_employee_seen`, `techmark_analysis_customer_seen`, `techmark_analysis_unmatched_face` (payload: role, human_id, name, similarity, score, session_new).

### 4.5 Services

- `techmark_analysis.generate_report` (period: day|month, date) → writes files, returns response data.
- `techmark_analysis.list_faces` (library) → response data.
- `techmark_analysis.enroll_customer` / `delete_face` — **registered only when option `enrollment_enabled` is on (default off; enabling requires explicit owner authorization)**. Scene images are retained only while enrollment is enabled, in memory, last 20, max 24 h; never written to disk.
  - Method: Pillow decodes the scene JPEG and reads its **actual** width/height (never assumes 2560×1504 or the payload resolution). Rect values must be within 0–1 with `x+width ≤ 1` and `y+height ≤ 1`; otherwise reject. Crop `recommendFaceRect` in actual pixels, re-encode JPEG q90, all in the executor.
  - Rejects: face width < `min_enroll_face_px` (option, default **120** — a proposed limit, not a verified camera requirement; tuned in T-6), score < 70, or an Employee match within ±10 s of the event. Camera rejection messages are surfaced verbatim.
  - Limitation: observed unknown faces were ~87 px wide, so many events will not be enrollable. Alternative without cropping — upload the event `modelData` — is untested (T-7).

Daily report generated automatically at a configurable time (default 23:55); monthly on day 1 at 00:10.

### 4.6 Error handling

- Stream: reconnect with exponential backoff 1 s → 60 s; watchdog reconnects if no bytes for 60 s; `stream_connected` reflects state.
- 401 → HA reauth flow. Other HTTP errors → retry, logged once per outage.
- Malformed part → logged with byte offset, skipped; never stops the stream.
- DB writes batched in executor; a failed write is logged and the event retried once.
- Integration never writes camera configuration except enrollment/delete services.

### 4.7 Privacy and credentials

- No images, face model data, or certificate numbers stored in SQLite; only `human_id`, name, library, similarity, score, timestamps.
- Test fixtures are sanitized JSON only (`modelData` redacted, fake names); no image bytes.
- `camera-backup/`, raw captures, and credentials are never committed.
- Credentials live in the HA config entry, which HA stores **unencrypted** in `.storage/core.config_entries` (platform norm, protected by host access). Diagnostics use `async_redact_data` for host/username/password; URLs and auth headers are never logged.

### 4.8 Event continuity

- Events during HA or network downtime are **lost** in v1. Each outage (start, end) is stored in a `gaps` table and flagged in reports for affected periods.
- Image correlation: the parser reads multipart boundaries and each part's headers (`Content-Type`, `Content-Length`, `Content-ID`, `filename`). A JPEG is attached to an event only when a header identifier matches an identifier in that event (e.g. `targetAttrs.contentID`/`pId`, `human_data[].contentID`/`pId`). Arrival order alone is **never** used.
- If T-3 shows the firmware provides no reliable correlation, images are discarded: events are processed without images, a `image_correlation_unavailable` diagnostic is set, and enrollment stays unavailable. Uncertain images are never attached to an event.
- v1.1 candidate: backfill via camera `FCSearch` (requires SD card and correct camera clock; T-8).
- Sensors use `state_class: total` with `last_reset` at local midnight / month start.

## 5. Testing

- Unit: parser on sanitized fixtures from the 2026-10-31 capture; processor rules (quality gate, dedupe edges, unknown FDID); stats aggregation.
- Integration: config flow, coordinator with a fake stream, sensor updates.
- Physical (with approval): T-1…T-6; install via HACS custom repository, verify entities through Home Assistant MCP.

## 6. Delivery phases

1. Library + parser + processor + store (pure, tested).
2. HA integration: config flow, coordinator, sensors, events, reports.
3. Publish to GitHub `techmarkai/Techmark-analysis`, install via HACS, validate T-3/T-4 live.
4. (Approval) NTP change, T-5.
5. (Approval) Enrollment feature + T-1/T-6 with a consenting adult.
6. (Approval) Line crossing + T-2 calibration; door metrics go live.

## 7. Camera state invariants

Apps 2–5 running, 1/6/7 stopped; Employee library and threshold untouched. The integration verifies app states at startup and reports drift as a repair issue; it never starts or stops apps.
