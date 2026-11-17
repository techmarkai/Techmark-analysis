"""Resolve and validate a user-chosen report folder. Pure logic (no Home Assistant imports)."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

DEFAULT_REPORT_DIR = "techmark_reports"
MEDIA_PREFIX = "media-source://media_source/"


def _real_inside(real: str, root: str) -> bool:
    root = os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def resolve_report_dir(
    value: str | None,
    config_dir: str,
    is_allowed: Callable[[str], bool],
    media_dirs: Mapping[str, str] | None = None,
) -> Path | None:
    """Folder for generated reports, or None when it is not allowed.

    Accepted values: a path relative to the Home Assistant config folder, a media-source URI
    (media-source://media_source/local/sub/dir, mapped through `media_dirs`), or an absolute path.
    Absolute paths must be inside a media directory or accepted by `is_allowed` (HA's
    allowlist_external_dirs), so reports cannot be written to arbitrary places on the host.
    Symlinks are resolved before the check.
    """
    media_dirs = media_dirs or {}
    raw = (value or "").strip() or DEFAULT_REPORT_DIR
    if raw.startswith(MEDIA_PREFIX):
        source, _, rel = raw[len(MEDIA_PREFIX):].partition("/")
        base = media_dirs.get(source)
        if base is None:
            return None
        path = Path(base) / rel
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = Path(config_dir) / path
    real = os.path.realpath(path)
    if _real_inside(real, config_dir) or any(_real_inside(real, d) for d in media_dirs.values()) or is_allowed(real):
        return Path(real)
    return None


def write_readonly(path: Path, text: str, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Write a report so that it ends up read-only (mode 0444, the read-only flag on SMB shares).

    A temporary file is written, locked, then moved over the target, so regenerating a report with
    the same name replaces the old read-only file instead of failing, and a reader never sees a
    half-written file.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding=encoding, newline=newline)
    try:
        tmp.chmod(0o444)
        try:
            os.replace(tmp, path)
        except PermissionError:  # Windows and some network mounts refuse to replace a read-only file
            remove_report(path)
            os.replace(tmp, path)
    finally:
        remove_report(tmp)


def remove_report(path: Path) -> None:
    """Delete a report even though it is read-only (unlock, then unlink)."""
    try:
        path.chmod(0o666)
    except OSError:
        pass
    path.unlink(missing_ok=True)


def list_media_folders(media_dirs: Mapping[str, str], max_depth: int = 2, limit: int = 200) -> list[str]:
    """media-source URIs of the folders under each media directory (for the options dropdown)."""
    out: list[str] = []
    for source, base in sorted(media_dirs.items()):
        stack = [(base, "", 0)]
        while stack and len(out) < limit:
            folder, rel, depth = stack.pop(0)
            out.append(f"{MEDIA_PREFIX}{source}/{rel}")
            if depth >= max_depth:
                continue
            try:
                subs = sorted(e.name for e in os.scandir(folder) if e.is_dir(follow_symlinks=False) and not e.name.startswith("."))
            except OSError:
                continue
            stack += [(os.path.join(folder, s), f"{rel}{s}/", depth + 1) for s in subs]
    return out
