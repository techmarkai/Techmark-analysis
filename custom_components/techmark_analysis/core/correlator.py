"""Identifier-based image/event correlation. Never pairs by arrival order."""

from __future__ import annotations

import re
from collections import OrderedDict

from .stream_parser import Part

_DISPOSITION_NAME = re.compile(r'(?:file)?name="?([^";]+)"?', re.I)


def image_ids(headers: dict[str, str]) -> set[str]:
    """Identifiers an image part declares. Single place to adapt after test T-3."""
    ids: set[str] = set()
    cid = headers.get("content-id", "").strip().strip("<>")
    if cid:
        ids.add(cid)
    ids.update(m.strip() for m in _DISPOSITION_NAME.findall(headers.get("content-disposition", "")) if m.strip())
    return ids


class Correlator:
    """Remembers recent events' identifiers; attaches an image only on an exact id match."""

    def __init__(self, ttl: float = 10.0, max_events: int = 256) -> None:
        self._ttl = ttl
        self._max = max_events
        self._events: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.attached = 0
        self.uncorrelated_images = 0

    def add_event(self, event, ids, t: float) -> None:
        for i in ids:
            self._events[i] = (t, event)
            self._events.move_to_end(i)
        while len(self._events) > self._max:
            self._events.popitem(last=False)

    def add_image(self, part: Part, t: float):
        """Return the event this image belongs to, or None (image must be discarded)."""
        self._expire(t)
        for i in image_ids(part.headers):
            hit = self._events.get(i)
            if hit is not None:
                self.attached += 1
                return hit[1]
        self.uncorrelated_images += 1
        return None

    def _expire(self, t: float) -> None:
        for key in [k for k, (ts, _) in self._events.items() if t - ts > self._ttl]:
            del self._events[key]
