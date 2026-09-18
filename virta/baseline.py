"""Per-phase static baseline, calibrated from QUIET, not from the clock (spec 5.1).

The house has a ~1 kW always-on floor (servers, miners, fridge, standby) that
every detection is measured against, so it has to be known and current.

- Quiet is the signal, not the hour. The EV often charges at night, so a fixed
  01-05 window would calibrate against a 10 kW car. Instead: find stretches that
  are LOW (total under a ceiling), STABLE (small per-phase spread) and SUSTAINED
  (at least QUIET_MIN_DURATION), with nothing on the detector's stack.
- Per phase. C carries more always-on load than A and B.
- Rolling, not reset. Each day's best quiet stretch contributes one per-phase
  median; the baseline is the median of the last ROLLING_DAYS of those. One
  weird night cannot throw the reference.

Streaming by design: `observe()` takes one reading at a time, so the same code
calibrates a replay of history and the live Pi loop. State persists to JSON so a
restarted Pi comes back with a known floor instead of guessing.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median, pstdev

from .ev_signature import Baseline
from .history import Frame

# TUNABLE
QUIET_MIN_DURATION = timedelta(minutes=20)
QUIET_MAX_STD_W = 40.0  # per-phase spread allowed inside a quiet stretch
QUIET_CEILING_OVER_BASE_W = 350.0  # total must stay within this of the current floor
ROLLING_DAYS = 5


@dataclass(frozen=True)
class QuietStretch:
    start: datetime
    end: datetime
    baseline: Baseline
    spread_w: float  # worst per-phase standard deviation

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class BaselineTracker:
    def __init__(self, seed: Baseline, *, state_path: str | Path | None = None) -> None:
        self.current = seed
        self._state_path = Path(state_path) if state_path else None
        self._window: deque[Frame] = deque()
        self._sums = [0.0, 0.0, 0.0]  # running sums keep the spread check O(1)
        self._sumsq = [0.0, 0.0, 0.0]
        self._day_best: dict[date, QuietStretch] = {}
        self.updates: list[tuple[datetime, Baseline, QuietStretch]] = []
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.current = Baseline(**data["current"])
        except (ValueError, KeyError, TypeError):
            pass  # unreadable state: keep the seed rather than crash

    def persist(self) -> None:
        """Write the current floor now (e.g. a freshly seeded one)."""
        self._save()

    def _save(self) -> None:
        if not self._state_path:
            return
        from .fsutil import atomic_write_text

        atomic_write_text(self._state_path, json.dumps({"current": self.current.__dict__}, indent=2))

    # --- streaming ----------------------------------------------------------
    def _is_candidate(self, frame: Frame) -> bool:
        if None in frame.phases:
            return False
        return frame.total <= self.current.total + QUIET_CEILING_OVER_BASE_W

    def observe(self, frame: Frame, *, stack_empty: bool) -> Baseline | None:
        """Feed one reading. Returns the new baseline when it changed, else None.

        `stack_empty` comes from the detector: a stretch only counts as quiet if
        nothing is believed to be running on top of the floor.
        """
        if not stack_empty or not self._is_candidate(frame):
            # usually a load switching on is what ends a quiet stretch - the
            # stretch itself is still a valid calibration, so hand it back
            return self._close_window()

        self._push(frame)
        # a stable stretch must stay stable: if the newest reading breaks the
        # spread, the stretch ends just before it and a new one starts with it
        if len(self._window) >= 4 and self._spread() > QUIET_MAX_STD_W:
            self._pop_last()
            changed = self._close_window()
            self._push(frame)
            return changed
        return None

    def flush(self) -> Baseline | None:
        """Close any open stretch (end of a replay)."""
        return self._close_window()

    def _push(self, frame: Frame) -> None:
        self._window.append(frame)
        for i, value in enumerate(frame.phases):
            self._sums[i] += value
            self._sumsq[i] += value * value

    def _pop_last(self) -> None:
        frame = self._window.pop()
        for i, value in enumerate(frame.phases):
            self._sums[i] -= value
            self._sumsq[i] -= value * value

    def _spread(self) -> float:
        n = len(self._window)
        worst = 0.0
        for s, sq in zip(self._sums, self._sumsq):
            mean = s / n
            worst = max(worst, max(0.0, sq / n - mean * mean) ** 0.5)
        return worst

    def _close_window(self) -> Baseline | None:
        window = list(self._window)
        self._window.clear()
        self._sums = [0.0, 0.0, 0.0]
        self._sumsq = [0.0, 0.0, 0.0]
        if len(window) < 4 or window[-1].when - window[0].when < QUIET_MIN_DURATION:
            return None
        cols = list(zip(*(f.phases for f in window)))
        stretch = QuietStretch(
            start=window[0].when,
            end=window[-1].when,
            baseline=Baseline(
                total=median(f.total for f in window),
                a=median(cols[0]),
                b=median(cols[1]),
                c=median(cols[2]),
            ),
            spread_w=max(pstdev(col) for col in cols),
        )
        day = stretch.start.date()
        best = self._day_best.get(day)
        # the day's best stretch: longest wins, lowest spread breaks ties
        if best and (best.duration, -best.spread_w) >= (stretch.duration, -stretch.spread_w):
            return None
        self._day_best[day] = stretch
        return self._roll(stretch)

    def _roll(self, stretch: QuietStretch) -> Baseline:
        recent = [self._day_best[d] for d in sorted(self._day_best)[-ROLLING_DAYS:]]
        self.current = Baseline(
            total=median(s.baseline.total for s in recent),
            a=median(s.baseline.a for s in recent),
            b=median(s.baseline.b for s in recent),
            c=median(s.baseline.c for s in recent),
        )
        self.updates.append((stretch.end, self.current, stretch))
        self._save()
        return self.current
