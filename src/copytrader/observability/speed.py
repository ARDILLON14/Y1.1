"""Bounded, in-memory speed statistics for the dashboard.

Prometheus keeps the long-term history; this keeps the last few hundred
samples so the dashboard can show, without extra infrastructure:

* detection delay (source transaction → we saw it), per stream/source;
* which stream delivered each transaction first and by how much;
* how each send route (RPC, Jito, extra RPCs) accepted our transactions.

Everything is lost on restart by design: it describes the current setup.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..1); None without samples."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def summary(values: list[float], digits: int = 3) -> dict[str, Any]:
    p50, p90 = percentile(values, 0.5), percentile(values, 0.9)
    return {
        "n": len(values),
        "p50": None if p50 is None else round(p50, digits),
        "p90": None if p90 is None else round(p90, digits),
    }


class SpeedStats:
    def __init__(self, window: int = 500) -> None:
        self.window = window
        self._detection: dict[str, deque[float]] = {}
        self._stream_notices: Counter[str] = Counter()
        self._stream_first: Counter[str] = Counter()
        self._stream_lead: dict[str, deque[float]] = {}
        self._send_ok: Counter[str] = Counter()
        self._send_failed: Counter[str] = Counter()
        self._send_ms: dict[str, deque[float]] = {}

    def _series(self, store: dict[str, deque[float]], key: str) -> deque[float]:
        series = store.get(key)
        if series is None:
            series = store[key] = deque(maxlen=self.window)
        return series

    def record_detection(self, source: str, seconds: float) -> None:
        self._series(self._detection, source).append(max(0.0, seconds))

    def record_notice(self, stream: str, *, first: bool) -> None:
        """A stream delivered a transaction; ``first`` if no other stream had delivered it yet."""
        self._stream_notices[stream] += 1
        if first:
            self._stream_first[stream] += 1

    def record_lead(self, winner: str, lead_ms: float) -> None:
        """When a slower stream delivers the same transaction: how far ahead the winner was."""
        self._series(self._stream_lead, winner).append(max(0.0, lead_ms))

    def record_send(self, route: str, ok: bool, elapsed_ms: float | None = None) -> None:
        (self._send_ok if ok else self._send_failed)[route] += 1
        if ok and elapsed_ms is not None:
            self._series(self._send_ms, route).append(elapsed_ms)

    def snapshot(self) -> dict[str, Any]:
        streams = sorted(set(self._stream_notices) | set(self._stream_first))
        total_first = sum(self._stream_first.values())
        routes = sorted(set(self._send_ok) | set(self._send_failed))
        return {
            "detection": {k: summary(list(v)) for k, v in sorted(self._detection.items())},
            "streams": [
                {
                    "stream": s,
                    "notices": self._stream_notices[s],
                    "first": self._stream_first[s],
                    "first_share": round(self._stream_first[s] / total_first, 3) if total_first else None,
                    "lead_ms": summary(list(self._stream_lead.get(s, ())), 1),
                }
                for s in streams
            ],
            "send_routes": [
                {
                    "route": r,
                    "accepted": self._send_ok[r],
                    "failed": self._send_failed[r],
                    "ms": summary(list(self._send_ms.get(r, ())), 1),
                }
                for r in routes
            ],
        }
