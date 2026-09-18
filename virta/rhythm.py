"""Rhythm detection - loads recognised by their PATTERN, whatever their size (spec 5, Tier 2).

The stacking detector (detector.py) finds loads by a sustained STEP of 120 W or
more. Some loads never make one: a fridge compressor, a fan controller, a PSU
that drops out for two seconds every two minutes. Each switch is small and
brief, so the step detector - rightly - calls it noise. But noise is not
regular. A 35 W dip that comes back 54 times in two hours at the same size is a
device, and its rhythm is its fingerprint (spec 5: "cycling is both a
fingerprint and the overlap-separator").

Method, on full-resolution history (the live loop's 15 s polling would miss a
2 s pulse; HA's recorder has every change):
  1. Small steps: consecutive readings on one phase that move by MIN_W..MAX_W.
  2. Pulses: a step up followed by a matching step down (something briefly ON),
     or down then up (something briefly OFF) - matching within PAIR_TOLERANCE.
  3. Rhythms: pulses of the same direction and similar size, repeating at least
     MIN_PULSES times. Described by amplitude, pulse width, period, regularity.

No thresholds on watts beyond the noise floor: a 20 W rhythm counts as much as
a 300 W one. Size is recorded, not required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median, pstdev

# TUNABLE
MIN_W = 15.0  # below this, phase readings wobble on their own
MAX_W = 400.0  # above this the stacking detector handles it as a step
PAIR_TOLERANCE = 0.35  # the step back must match the step out within 35%
MAX_PULSE_S = 30 * 60  # a pulse longer than this is a load, not a rhythm
AMPLITUDE_GROUP = 0.30  # pulses within 30% of each other's size are one rhythm
MIN_PULSES = 6  # fewer is not a rhythm yet
STRONG_PER_HOUR = 4.0  # reported only if it repeats at least this often...
STRONG_MIN_PULSES = 8  # ...and at least this many times in the window


@dataclass(frozen=True)
class Pulse:
    start: datetime
    width_s: float
    amplitude_w: float  # positive = something briefly ON, negative = briefly OFF


@dataclass
class Rhythm:
    phase: str
    direction: int  # +1 briefly on, -1 briefly off
    pulses: list[Pulse]
    window_s: float

    @property
    def amplitude_w(self) -> float:
        return median(abs(p.amplitude_w) for p in self.pulses)

    @property
    def amplitude_spread_w(self) -> float:
        return pstdev([abs(p.amplitude_w) for p in self.pulses]) if len(self.pulses) > 1 else 0.0

    @property
    def width_s(self) -> float:
        return median(p.width_s for p in self.pulses)

    @property
    def period_s(self) -> float | None:
        starts = sorted(p.start for p in self.pulses)
        gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
        return median(gaps) if gaps else None

    @property
    def regularity(self) -> float:
        """1 = clockwork, 0 = random: 1 - (median absolute deviation / median) of the
        gaps. Robust on purpose: one long quiet spell must not make a steady
        rhythm look random (the mean-based version scored everything ~0)."""
        starts = sorted(p.start for p in self.pulses)
        gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
        if len(gaps) < 2:
            return 0.0
        mid = median(gaps)
        if not mid:
            return 0.0
        return max(0.0, 1.0 - median(abs(g - mid) for g in gaps) / mid)

    @property
    def strong(self) -> bool:
        return self.per_hour >= STRONG_PER_HOUR and len(self.pulses) >= STRONG_MIN_PULSES

    @property
    def per_hour(self) -> float:
        return len(self.pulses) / max(self.window_s / 3600, 1e-6)

    @property
    def id(self) -> str:
        """Stable-ish signature: phase, direction, size, pulse width - for naming it later."""
        kind = "ON" if self.direction > 0 else "DIP"
        width = f"{self.width_s:.0f}s" if self.width_s < 90 else f"{self.width_s / 60:.0f}m"
        return f"{self.phase}-{kind}{round(self.amplitude_w / 5) * 5:.0f}W-{width}"

    def summary(self) -> dict:
        return {
            "id": self.id,
            "phase": self.phase,
            "kind": "briefly_on" if self.direction > 0 else "briefly_off",
            "amplitude_w": round(self.amplitude_w),
            "amplitude_spread_w": round(self.amplitude_spread_w),
            "pulse_s": round(self.width_s, 1),
            "every_s": round(self.period_s) if self.period_s else None,
            "per_hour": round(self.per_hour, 1),
            "regularity": round(self.regularity, 2),
            "pulses": len(self.pulses),
            "last": max(p.start for p in self.pulses).isoformat(),
            # the last 25 min of pulse times, so the console can tick them on the trace
            "recent_pulses": [round(p.start.timestamp()) for p in sorted(self.pulses, key=lambda p: p.start)
                              if (max(q.start for q in self.pulses) - p.start).total_seconds() <= 25 * 60][-60:],
        }


def pulses(samples: list[tuple[datetime, float]]) -> list[Pulse]:
    """Pair small steps into pulses: out and back again, at a matching size."""
    steps = []
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        d = v1 - v0
        if MIN_W <= abs(d) <= MAX_W:
            steps.append((t1, d))
    found, open_step = [], None
    for t, d in steps:
        if open_step is None:
            open_step = (t, d)
            continue
        t0, d0 = open_step
        opposite = (d > 0) != (d0 > 0)
        matches = abs(abs(d) - abs(d0)) <= PAIR_TOLERANCE * max(abs(d), abs(d0))
        if opposite and matches and (t - t0).total_seconds() <= MAX_PULSE_S:
            found.append(Pulse(t0, (t - t0).total_seconds(), (abs(d0) + abs(d)) / 2 * (1 if d0 > 0 else -1)))
            open_step = None
        else:
            open_step = (t, d)  # an unmatched step starts a new candidate
    return found


def find_rhythms(samples: list[tuple[datetime, float]], phase: str) -> list[Rhythm]:
    """All rhythms on one phase's full-resolution history."""
    if len(samples) < 3:
        return []
    window = (samples[-1][0] - samples[0][0]).total_seconds()
    groups: list[Rhythm] = []
    for p in sorted(pulses(samples), key=lambda p: abs(p.amplitude_w)):
        direction = 1 if p.amplitude_w > 0 else -1
        home = next((g for g in groups if g.direction == direction
                     and abs(abs(p.amplitude_w) - g.amplitude_w) <= AMPLITUDE_GROUP * g.amplitude_w), None)
        if home is None:
            home = Rhythm(phase, direction, [], window)
            groups.append(home)
        home.pulses.append(p)
    return sorted((g for g in groups if len(g.pulses) >= MIN_PULSES), key=lambda g: -len(g.pulses))


def strong_rhythms(series_by_phase: dict[str, list[tuple[datetime, float]]], limit: int = 6) -> list[Rhythm]:
    """The rhythms worth showing, strongest first, across all phases."""
    found = [r for phase, samples in series_by_phase.items() for r in find_rhythms(samples, phase) if r.strong]
    return sorted(found, key=lambda r: -r.per_hour)[:limit]
