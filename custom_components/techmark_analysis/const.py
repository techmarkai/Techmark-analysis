"""Constants for Techmark Analysis."""

DOMAIN = "techmark_analysis"

CONF_SERIAL = "serial"
CONF_MODEL = "model"
CONF_MIN_FACE_QUALITY = "min_face_quality"
CONF_EMPLOYEE_WINDOW = "employee_window_min"
CONF_CUSTOMER_WINDOW = "customer_window_min"
CONF_EMPLOYEE_FDID = "employee_fdid"
CONF_CUSTOMER_FDID = "customer_fdid"
CONF_REPORT_TIME = "daily_report_time"
CONF_RETENTION_DAYS = "retention_days"

DEFAULT_OPTIONS = {
    CONF_MIN_FACE_QUALITY: 50,
    CONF_EMPLOYEE_WINDOW: 10,
    CONF_CUSTOMER_WINDOW: 30,
    CONF_EMPLOYEE_FDID: "1",
    CONF_CUSTOMER_FDID: "2",
    CONF_REPORT_TIME: "23:55:00",
    CONF_RETENTION_DAYS: 400,
}

# Camera apps that must stay running; the integration only reports drift, never changes it.
EXPECTED_APPS = {"Face Capture": True, "Comparison and Modeling": True, "Smart Event": True}

WATCHDOG_SECONDS = 60
BACKOFF_MAX_SECONDS = 60
CLOCK_SKEW_LIMIT_SECONDS = 120
DB_FILENAME = "techmark_analysis.db"
REPORT_DIR = "techmark_reports"

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"
EVENT_EMPLOYEE_SEEN = f"{DOMAIN}_employee_seen"
EVENT_CUSTOMER_SEEN = f"{DOMAIN}_customer_seen"
EVENT_UNMATCHED_FACE = f"{DOMAIN}_unmatched_face"
