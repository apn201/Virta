"""The real Nordpool price as a step function over time (c/kWh) - one place.

Used by the nightly pass (costing a fortnight of runs) and by the live loop
(costing the session that is running right now). Every cost is energy x the
price actually in force during that energy - slot by slot, never a start-slot
shortcut, never an average standing in for a real price.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

from .ha_client import parse_price_curve


class PriceCurve:
    def __init__(self, prices: list[tuple[datetime, float]]) -> None:
        self.times = [t for t, _ in prices]
        self.values = [v for _, v in prices]

    @classmethod
    def from_nordpool(cls, attrs: dict) -> "PriceCurve":
        """From the Nordpool sensor's attributes: yesterday is not in there, so a
        session that started before today's first slot is costed from 00:00."""
        slots = parse_price_curve(attrs, "raw_today") + parse_price_curve(attrs, "raw_tomorrow")
        return cls(sorted((s["start"], s["value"]) for s in slots))

    def at(self, when: datetime) -> float | None:
        i = bisect_right(self.times, when) - 1
        return self.values[i] if i >= 0 else None  # before the first known price: unknown, not guessed

    def cost_eur(self, start: datetime, end: datetime, watts: float) -> float | None:
        """Exact cost of a constant draw over [start, end), segment by real price."""
        if not self.times:
            return None
        start = max(start, self.times[0])
        if end <= start:
            return 0.0
        eur, t = 0.0, start
        i = bisect_right(self.times, t) - 1
        while t < end:
            seg_end = min(self.times[i + 1], end) if i + 1 < len(self.times) else end
            eur += watts * (seg_end - t).total_seconds() / 3.6e6 * self.values[i] / 100
            t, i = seg_end, i + 1
        return eur

    def cheapest_window(self, day_start: datetime, day_end: datetime, duration: timedelta,
                        watts: float) -> tuple[datetime, float] | None:
        """The cheapest REAL window of the same length inside [day_start, day_end).

        Candidate starts are the price change points (slot boundaries) - a window
        starting mid-slot is never cheaper than one starting at a boundary.
        Only windows fully covered by known prices count.
        """
        last_known = self.times[-1] + timedelta(minutes=15) if self.times else day_start
        latest_start = min(day_end, last_known) - duration
        candidates = [day_start] + [t for t in self.times if day_start <= t <= latest_start]
        best = None
        for s in candidates:
            if s > latest_start or (self.times and s < self.times[0]):
                continue
            c = self.cost_eur(s, s + duration, watts)
            if c is not None and (best is None or c < best[1]):
                best = (s, c)
        return best
