# Techmark Analysis

Home Assistant integration for the Hikvision iDS-2CD7146G2-IZHSY. **All AI runs on the camera** (face capture, face comparison); Home Assistant only receives events over ISAPI `alertStream`, stores them in SQLite, and calculates statistics.

- Employee attendance (first/last seen) from the camera's `Employee` face library
- Known and returning customers from the `Customers` face library; employees never counted as customers
- Unmatched faces reported as *unmatched face events* — never as unique people
- Configurable face-quality threshold (default 50) and visit windows
- Daily/monthly Markdown + CSV reports (`generate_report` service, admin only)
- Data gaps recorded for camera or Home Assistant outages

The integration never changes camera configuration and never starts or stops camera apps. Customer enrollment is not included in this version.

Install: HACS → Custom repositories → this repo (Integration), then add **Techmark Analysis** under Settings → Devices & services.

Design: `docs/superpowers/specs/`.
