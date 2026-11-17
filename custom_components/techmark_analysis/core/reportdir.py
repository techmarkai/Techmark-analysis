"""Resolve and validate a user-chosen report folder. Pure logic (no Home Assistant imports)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

DEFAULT_REPORT_DIR = "techmark_reports"


def resolve_report_dir(value: str | None, config_dir: str, is_allowed: Callable[[str], bool]) -> Path | None:
    """Folder for generated reports, or None when it is not allowed.

    Relative values are inside the Home Assistant config folder. Anything else must be accepted
    by `is_allowed` (HA's allowlist_external_dirs, e.g. /media), so reports cannot be written
    to arbitrary places on the host. Symlinks are resolved before the check.
    """
    raw = (value or "").strip() or DEFAULT_REPORT_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config_dir) / path
    real = os.path.realpath(path)
    cfg = os.path.realpath(config_dir)
    if real == cfg or real.startswith(cfg + os.sep) or is_allowed(real):
        return Path(real)
    return None
