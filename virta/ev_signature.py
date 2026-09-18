"""EV charging detection by signature alone (spec 4, builder decision 2026-09-18).

The Eve charger sends NO data to Home Assistant, so the EV is identified purely
from its power signature: the only balanced three-phase load in the house,
~8-12 kW total, each phase rising by roughly the same amount.

The charger also has dynamic load control: when other household load rises it
throttles itself to protect the main fuses, so the total can stay flat while
another appliance switches on underneath. Consequence, stated as a rule:

    WHILE THE EV IS CHARGING, OTHER DETECTIONS ARE UNRELIABLE.

Every window this module returns carries that flag, and downstream code (the
stacking detector, the reasoning payload, the console) must honour it rather
than report other loads with normal confidence.

Hysteresis is what makes throttling survivable: entry needs the full signature,
but staying in the EV state only needs a reduced balanced draw, because a
throttled charger is still a charging charger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .history import Frame

# TUNABLE - seeded from the spec 4 capture: +8-11 kW, each phase +3000-3900 W.
ENTER_TOTAL_DELTA_W = 7000.0  # total rise over baseline to START an EV window
ENTER_PHASE_DELTA_W = 1800.0  # every phase must rise at least this much
STAY_TOTAL_DELTA_W = 3500.0  # while charging, a throttled draw this high keeps it on
STAY_PHASE_DELTA_W = 800.0
BALANCE_MIN_RATIO = 0.45  # smallest phase rise / largest phase rise
MIN_DURATION = timedelta(minutes=2)  # shorter blips are not a charge session
MERGE_GAP = timedelta(minutes=5)  # a brief dropout inside a session is still one session

UNRELIABLE_NOTE = "EV charging - load control active, other detections unreliable"


@dataclass(frozen=True)
class Baseline:
    """Per-phase floor the rises are measured against (spec 5.1 is the real one)."""

    total: float
    a: float
    b: float
    c: float

    def describe(self) -> str:
        return f"total {self.total:.0f} W  (A {self.a:.0f} / B {self.b:.0f} / C {self.c:.0f})"


@dataclass(frozen=True)
class EVWindow:
    start: datetime
    end: datetime
    peak_total_w: float
    mean_total_w: float
    mean_phase_rise_w: tuple[float, float, float]
    others_unreliable: bool = True  # always - that is the point
    note: str = UNRELIABLE_NOTE

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def describe(self) -> str:
        a, b, c = self.mean_phase_rise_w
        hours, rem = divmod(int(self.duration.total_seconds()), 3600)
        return (
            f"{self.start:%a %d.%m %H:%M} -> {self.end:%H:%M}  ({hours}h{rem // 60:02d}m)  "
            f"mean {self.mean_total_w / 1000:.1f} kW, peak {self.peak_total_w / 1000:.1f} kW, "
            f"rise A/B/C +{a:.0f}/+{b:.0f}/+{c:.0f} W"
        )


def rough_baseline(frames: list[Frame], quantile: float = 0.10) -> Baseline:
    """A low-percentile floor per phase - good enough for inspection.

    NOT the spec 5.1 baseline (quiet-stretch, rolling, per phase) - that is
    slice 4. This works as long as the EV is on for well under 90% of the window.
    """

    def q(values: list[float]) -> float:
        values = sorted(values)
        return values[min(len(values) - 1, int(len(values) * quantile))] if values else 0.0

    return Baseline(
        total=q([f.total for f in frames]),
        a=q([f.a for f in frames if f.a is not None]),
        b=q([f.b for f in frames if f.b is not None]),
        c=q([f.c for f in frames if f.c is not None]),
    )


def _rises(frame: Frame, base: Baseline) -> tuple[float, float, float] | None:
    if frame.a is None or frame.b is None or frame.c is None:
        return None
    return (frame.a - base.a, frame.b - base.b, frame.c - base.c)


def _balanced(rises: tuple[float, float, float]) -> bool:
    high = max(rises)
    return high > 0 and min(rises) / high >= BALANCE_MIN_RATIO


def looks_like_ev(frame: Frame, base: Baseline, *, charging: bool) -> bool:
    """Entry test when not charging; the looser stay test when already charging."""
    rises = _rises(frame, base)
    if rises is None:
        return charging  # no phase data yet: hold the current state, don't flip
    total_rise = frame.total - base.total
    if charging:
        return total_rise >= STAY_TOTAL_DELTA_W and min(rises) >= STAY_PHASE_DELTA_W and _balanced(rises)
    return total_rise >= ENTER_TOTAL_DELTA_W and min(rises) >= ENTER_PHASE_DELTA_W and _balanced(rises)


def find_ev_windows(frames: list[Frame], base: Baseline) -> list[EVWindow]:
    """Charge sessions in a history, with hysteresis, merging and a minimum length."""
    spans: list[tuple[int, int]] = []
    charging = False
    start_idx = 0
    for idx, frame in enumerate(frames):
        now_ev = looks_like_ev(frame, base, charging=charging)
        if now_ev and not charging:
            start_idx = idx
        elif charging and not now_ev:
            spans.append((start_idx, idx))
        charging = now_ev
    if charging:
        spans.append((start_idx, len(frames) - 1))

    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and frames[s].when - frames[merged[-1][1]].when <= MERGE_GAP:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    windows: list[EVWindow] = []
    for s, e in merged:
        chunk = frames[s : e + 1]
        if chunk[-1].when - chunk[0].when < MIN_DURATION:
            continue
        rises = [r for r in (_rises(f, base) for f in chunk) if r is not None]
        mean_rise = tuple(sum(r[i] for r in rises) / len(rises) for i in range(3)) if rises else (0.0, 0.0, 0.0)
        windows.append(
            EVWindow(
                start=chunk[0].when,
                end=chunk[-1].when,
                peak_total_w=max(f.total for f in chunk),
                mean_total_w=sum(f.total for f in chunk) / len(chunk),
                mean_phase_rise_w=mean_rise,  # type: ignore[arg-type]
            )
        )
    return windows


def ev_active_at(windows: list[EVWindow], when: datetime) -> EVWindow | None:
    for window in windows:
        if window.start <= when <= window.end:
            return window
    return None
