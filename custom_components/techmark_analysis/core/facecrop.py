"""Crop the camera-reported face area from an event scene image (plain image processing, no AI).

Needs Pillow. Pillow is NOT declared in manifest.json yet: approved only if test T-D shows
the camera cannot enroll from face model data. Until then, the import fails gracefully and
enrollment reports FaceUnusable.
"""

from __future__ import annotations

import io

from .enroller import FaceUnusable
from .models import FaceUnmatched

MAX_UPLOAD_BYTES = 300 * 1024  # camera importImageSize limit


def _valid_rect(r) -> bool:
    try:
        x, y, w, h = (float(r[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1 and y + h <= 1


def prepare_face(scene: bytes, event: FaceUnmatched, min_face_px: int) -> bytes:
    try:
        from PIL import Image  # noqa: PLC0415 - optional, see module docstring
    except ImportError as err:
        raise FaceUnusable("image library unavailable") from err
    face, crop = event.face_rect, event.rec_rect
    if not (_valid_rect(face) and _valid_rect(crop)):
        raise FaceUnusable("invalid face coordinates")
    try:
        img = Image.open(io.BytesIO(scene))
        img.load()
    except Exception as err:  # noqa: BLE001 - any decode failure means unusable
        raise FaceUnusable("undecodable image") from err
    w, h = img.size  # actual pixels, never assumed
    if float(face["width"]) * w < min_face_px:
        raise FaceUnusable("face too small")
    box = (
        int(float(crop["x"]) * w),
        int(float(crop["y"]) * h),
        int((float(crop["x"]) + float(crop["width"])) * w),
        int((float(crop["y"]) + float(crop["height"])) * h),
    )
    out = img.convert("RGB").crop(box)
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_UPLOAD_BYTES:
            return buf.getvalue()
    raise FaceUnusable("crop too large")
