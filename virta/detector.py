"""Stacking detector - the core engine (spec 5.2, 5.3).

The floor is not static during the day: it is the static baseline PLUS
everything currently believed to be on. Each phase keeps

    accounted[phase] = baseline[phase] + drift[phase] + sum(active loads on phase)

and every sustained deviation of the actual phase power from `accounted` is an
event, measured against the CURRENT accounted level, not the cold floor:

  step UP   -> a load switched on. Match its size+phase to a profile -> MATCHED;
               clearly above the base load but matching nothing -> UNKNOWN. Either
               way it joins the stack by its measured magnitude, so the NEXT
               event is measured on top of it. Unknowns stack without names.
  step DOWN -> a load ended. Match the drop to an active load's draw, remove it.

Rules layered on top (spec 4, 5.2, builder decisions):

- Settle before committing. A deviation must hold for SETTLE_MIN_READINGS and
  SETTLE_MIN_S before it becomes an event, so a single spiky reading is noise.
- EV = balanced three-phase step, detected by signature alone (the charger sends
  nothing). WHILE IT CHARGES: its load control makes other readings unreliable,
  so every other event is flagged `unreliable`, drops that match no other load
  are absorbed as the charger throttling, and balanced shifts adjust its draw.
- A drop that no running load explains means part of the BASE LOAD switched
  off (a PC, a server, ventilation stepping down). It joins the stack as a
  NEGATIVE load (BASE_OFF), pairs with the matching step back up (BASE_ON), and
  is clustered and labelled like any unknown. Off for over 24 h, it is a lasting
  change instead (BASE_SHIFT): moved to drift, which the next quiet-stretch
  recalibration (baseline.py) clears by measuring the new base load.
- Simultaneous switches in one reading look like one combined step. They are
  reported honestly as UNKNOWN +X W, never force-split into a guess.
- Loads the 3EM does not see do not exist here (builder decision 2026-09-18).

Streaming: `update(frame)` per reading, so replay and the live Pi loop share it.
Light enough for a Pi3: a handful of comparisons per phase per reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from itertools import count
from statistics import median

from .ev_signature import Baseline
from .history import Frame
from .profiles import PHASES, Profile

# TUNABLE
STEP_MIN_W = 120.0  # smallest step we act on; halogen 2 is ~195 W, phase noise ~50 W
SETTLE_MIN_READINGS = 2
SETTLE_MIN_S = 10.0
EV_MIN_PHASE_STEP_W = 1500.0  # each phase must move this much for a 3-phase event
EV_BALANCE_MIN_RATIO = 0.6
# An OFF step is matched only against loads that are ON - a far smaller candidate
# set than every profile - so it can be looser: real draws drift while running
# (a halogen measured +230 on, -194 off on 17.09).
OFF_TOLERANCE_FRACTION = 0.25
OFF_TOLERANCE_MIN_W = 80.0
EV_RELIABILITY_FACTOR = 0.5  # other detections' confidence while the EV charges
EV_OFF_FRACTION = 0.5  # all three phases dropping this share of the EV draw = EV off
BASE_PART_MAX_OFF = timedelta(hours=24)  # off longer than this = the base load really changed


class LoadState(str, Enum):
    MATCHED = "MATCHED"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


@dataclass
class ActiveLoad:
    """One thing believed to be running above the base load - or, with a negative
    draw, a part of the base load believed to be switched off."""

    key: int
    load_id: str  # profile id, or "unknown"
    display_name: str
    state: LoadState
    draw: dict[str, float]  # phase -> W currently attributed to this load
    since: datetime
    confidence: float
    reason: str
    unreliable: bool = False
    profile: Profile | None = field(default=None, repr=False)

    @property
    def total_w(self) -> float:
        return sum(self.draw.values())

    @property
    def phases(self) -> str:
        return "".join(p for p in PHASES if self.draw.get(p))

    @property
    def base_part_off(self) -> bool:
        """A negative load: part of the always-on base load is switched OFF."""
        return self.total_w < 0

    def label(self) -> str:
        if self.base_part_off:
            name = "BASE PART" if self.state is LoadState.UNKNOWN else self.display_name.upper()
            return f"{name} OFF {self.total_w:+.0f}W {self.phases}"
        if self.state is LoadState.UNKNOWN:
            return f"UNKNOWN +{self.total_w:.0f}W {self.phases}"
        return f"{self.display_name} {self.confidence:.0%}"


@dataclass(frozen=True)
class Event:
    """One detected switch - the event log that spec 5A mines later."""

    when: datetime
    # ON | OFF | OFF_RECONCILED | EV_ADJUST | BASE_OFF | BASE_ON | BASE_SHIFT
    # (BASE_* = part of the always-on base load switched off / back on / off for good)
    kind: str
    load_id: str
    display_name: str
    state: str
    phases: str
    delta_w: dict[str, float]
    confidence: float
    reason: str
    unreliable: bool = False
    duration_s: float | None = None
    load_key: int = 0  # pairs an ON with its OFF in the event log; 0 = no load

    def describe(self) -> str:
        deltas = " ".join(f"{p}{v:+.0f}" for p, v in self.delta_w.items())
        extra = f"  ran {self.duration_s / 60:.1f} min" if self.duration_s is not None else ""
        flag = "  [UNRELIABLE: EV load control]" if self.unreliable else ""
        who = f"UNKNOWN {self.phases}" if self.state == "UNKNOWN" else self.display_name
        conf = f" {self.confidence:.0%}" if self.kind == "ON" and self.state == "MATCHED" else ""
        return f"{self.when:%a %d.%m %H:%M:%S}  {self.kind:16s} {who}{conf}  ({deltas} W){extra}{flag}  - {self.reason}"

    def to_json(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "kind": self.kind,
            "load_id": self.load_id,
            "state": self.state,
            "phases": self.phases,
            "delta_w": {k: round(v, 1) for k, v in self.delta_w.items()},
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "unreliable": self.unreliable,
            "duration_s": None if self.duration_s is None else round(self.duration_s, 1),
            "load_key": self.load_key,
        }


@dataclass
class _Pending:
    direction: int  # +1 up, -1 down
    since: datetime
    residuals: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class DetectionState:
    """Every bit of load, in one of three states, shown honestly (spec 5.3)."""

    when: datetime | None
    baseline: Baseline
    loads: tuple[ActiveLoad, ...]
    residual_w: dict[str, float]  # actual - accounted, per phase
    ev_active: bool

    @property
    def others_unreliable(self) -> bool:
        return self.ev_active

    def describe(self) -> str:
        lines = [f"BASELINE  {self.baseline.describe()}"]
        for load in self.loads:
            flag = "  (unreliable - EV load control)" if load.unreliable else ""
            draw = " ".join(f"{p}{w:+.0f}" for p, w in load.draw.items() if w)
            lines.append(f"{load.state!s:9s} {load.label():28s} {draw:22s} since {load.since:%H:%M:%S}{flag}")
        res = " ".join(f"{p}{v:+.0f}" for p, v in self.residual_w.items())
        lines.append(f"RESIDUAL  {res} W (actual minus accounted)")
        if self.ev_active:
            lines.append("NOTE      EV charging - load control active, other detections unreliable")
        return "\n".join(lines)


def _signature_confidence(error_w: float, tolerance_w: float) -> float:
    """Magnitude fit only - context scoring is slice 6. 95% at a perfect fit, 50% at the edge."""
    if tolerance_w <= 0:
        return 0.5
    return max(0.5, min(0.95, 0.95 - 0.45 * (error_w / tolerance_w)))


class StackingDetector:
    def __init__(self, profiles: list[Profile], baseline: Baseline) -> None:
        self.profiles = [p for p in profiles if p.matchable]
        self.baseline = baseline
        self.drift: dict[str, float] = {p: 0.0 for p in PHASES}
        self.active: list[ActiveLoad] = []
        self.events: list[Event] = []
        self._pending: dict[str, _Pending | None] = {p: None for p in PHASES}
        self._keys = count(1)
        self._last: Frame | None = None
        self._ev_off_leftovers: list[Event] = []

    # --- accounting ---------------------------------------------------------
    def _base(self, phase: str) -> float:
        return {"A": self.baseline.a, "B": self.baseline.b, "C": self.baseline.c}[phase]

    def accounted(self, phase: str) -> float:
        return self._base(phase) + self.drift[phase] + sum(l.draw.get(phase, 0.0) for l in self.active)

    def residual(self, frame: Frame, phase: str) -> float | None:
        value = {"A": frame.a, "B": frame.b, "C": frame.c}[phase]
        return None if value is None else value - self.accounted(phase)

    @property
    def ev(self) -> ActiveLoad | None:
        for load in self.active:
            if load.profile and load.profile.others_unreliable_while_active:
                return load
        return None

    @property
    def stack_empty(self) -> bool:
        return not self.active

    @property
    def calibration_ready(self) -> bool:
        """Quiet enough to calibrate: nothing running ABOVE the base load.

        Parts of the base load that are switched off do not block calibration -
        a PC off every night must not stop the nightly recalibration.
        """
        return all(l.base_part_off for l in self.active)

    def calibration_frame(self, frame: Frame) -> Frame:
        """The reading as if every switched-off base part were back on.

        The base load means "everything normally on". Calibrating on raw
        readings while the PC is off would quietly lower it by the PC and the
        PC's return would then look like a new load. Adding back the parts known
        to be off keeps the base load's meaning stable.
        """
        missing = {p: -sum(l.draw.get(p, 0.0) for l in self.active if l.base_part_off) for p in PHASES}
        if not any(missing.values()):
            return frame
        a = frame.a + missing["A"] if frame.a is not None else None
        b = frame.b + missing["B"] if frame.b is not None else None
        c = frame.c + missing["C"] if frame.c is not None else None
        return Frame(frame.when, frame.total + sum(missing.values()), a, b, c,
                     frame.age_a, frame.age_b, frame.age_c)

    def set_baseline(self, baseline: Baseline) -> None:
        """New base load from a quiet stretch. Nothing runs above it, so drift is cleared."""
        self.baseline = baseline
        if self.calibration_ready:
            self.drift = {p: 0.0 for p in PHASES}

    def state(self) -> DetectionState:
        frame = self._last
        residual = {p: (self.residual(frame, p) or 0.0) if frame else 0.0 for p in PHASES}
        return DetectionState(
            when=frame.when if frame else None,
            baseline=self.baseline,
            loads=tuple(self.active),
            residual_w=residual,
            ev_active=self.ev is not None,
        )

    # --- streaming ----------------------------------------------------------
    def update(self, frame: Frame) -> list[Event]:
        """Feed one reading; returns the events it completed."""
        self._last = frame
        new_events: list[Event] = self._retire_long_off(frame.when)

        matured: dict[str, float] = {}
        for phase in PHASES:
            r = self.residual(frame, phase)
            if r is None:
                continue
            direction = 1 if r >= STEP_MIN_W else -1 if r <= -STEP_MIN_W else 0
            pending = self._pending[phase]
            if direction == 0:
                self._pending[phase] = None  # back within the band: it was noise
                continue
            if pending is None or pending.direction != direction:
                pending = self._pending[phase] = _Pending(direction, frame.when)
            pending.residuals.append(r)
            held = (frame.when - pending.since).total_seconds()
            if len(pending.residuals) >= SETTLE_MIN_READINGS and held >= SETTLE_MIN_S:
                matured[phase] = median(pending.residuals[-3:])

        if not matured:
            self.events.extend(new_events)
            return new_events

        # A three-phase event needs all three phases moving the same way. Pull in
        # phases still settling so the EV's staggered ramp commits as one event.
        for phase in PHASES:
            if phase not in matured and self._pending[phase] is not None:
                matured_dir = 1 if next(iter(matured.values())) > 0 else -1
                if self._pending[phase].direction == matured_dir:
                    matured[phase] = median(self._pending[phase].residuals[-3:])

        current = {p: self.residual(frame, p) or 0.0 for p in PHASES}
        new_events.extend(self._commit(frame.when, matured, current))
        for phase in matured:
            self._pending[phase] = None
        self.events.extend(new_events)
        return new_events

    # --- committing ---------------------------------------------------------
    def _commit(self, when: datetime, steps: dict[str, float], current: dict[str, float]) -> list[Event]:
        ups = {p: v for p, v in steps.items() if v > 0}
        downs = {p: v for p, v in steps.items() if v < 0}
        events: list[Event] = []

        ev = self.ev
        if ev is not None:
            # While charging, the charger moves all three phases together as its
            # load control acts. Any balanced shift is the charger, whatever its size.
            # A phase that crossed the threshold while the other two moved the same
            # way by at least EV_BALANCE_MIN_RATIO of it is the charger too - the
            # threshold just happened to catch one phase first (16.09 09:32).
            for group, sign in ((ups, 1), (downs, -1)):
                if group and len(group) < 3:
                    everyone = {p: current[p] for p in PHASES}
                    if all(sign * v > 0 for v in everyone.values()) and self._ratio_ok(everyone):
                        group.clear()
                        group.update(everyone)
            if len(ups) == 3 and self._ratio_ok(ups):
                for p, v in ups.items():
                    ev.draw[p] = ev.draw.get(p, 0.0) + v
                events.append(self._event(when, "EV_ADJUST", ev, ups, "charger ramped up (load control)"))
                ups = {}
            if len(downs) == 3:
                if self._ev_off(downs):
                    events.append(self._off_ev(when, downs))
                    events.extend(self._ev_off_leftovers)
                    downs = {}
                elif self._ratio_ok(downs):
                    for p, v in downs.items():
                        ev.draw[p] = max(0.0, ev.draw.get(p, 0.0) + v)
                    events.append(self._event(when, "EV_ADJUST", ev, downs, "charger throttled (load control)"))
                    downs = {}
        elif len(ups) == 3 and self._balanced(ups):
            events.append(self._on_three_phase(when, ups))
            ups = {}

        for phase, delta in ups.items():
            events.extend(self._on_single(when, phase, delta))
        for phase, delta in downs.items():
            event = self._off_single(when, phase, delta)
            if event:
                events.append(event)
        return events

    @staticmethod
    def _ratio_ok(deltas: dict[str, float]) -> bool:
        values = [abs(v) for v in deltas.values()]
        return max(values) > 0 and min(values) / max(values) >= EV_BALANCE_MIN_RATIO

    @classmethod
    def _balanced(cls, deltas: dict[str, float]) -> bool:
        """Big enough and even enough to be a three-phase switch-on."""
        return min(abs(v) for v in deltas.values()) >= EV_MIN_PHASE_STEP_W and cls._ratio_ok(deltas)

    def _on_three_phase(self, when: datetime, ups: dict[str, float]) -> Event:
        candidates = [p for p in self.profiles if p.kind == "balanced_3phase"]
        best, best_conf, why = None, 0.0, ""
        for profile in candidates:
            errors = [abs(v - profile.per_phase_step_w) for v in ups.values()]
            worst = max(errors)
            if worst <= profile.per_phase_tolerance_w:
                conf = _signature_confidence(worst, profile.per_phase_tolerance_w)
                balance = min(ups.values()) / max(ups.values())
                conf = min(0.98, conf + 0.1 * balance)  # balance is the EV's strongest tell
                if conf > best_conf:
                    best, best_conf = profile, conf
                    why = f"balanced 3-phase step, {balance:.0%} balance, per-phase fit within {worst:.0f} W"
        if best is None:
            return self._add(when, ups, None, 0.0, "balanced 3-phase step matching no profile", unreliable=False)
        return self._add(when, ups, best, best_conf, why, unreliable=False)

    def _retire_long_off(self, now: datetime) -> list[Event]:
        """A base part off for over BASE_PART_MAX_OFF is a lasting change, not a pause.

        It leaves the stack; its draw moves into drift so the books stay balanced
        until the next quiet recalibration measures the new, lower base load.
        That lasting drop is itself an insight for the nightly pass.
        """
        retired = []
        for load in [l for l in self.active if l.base_part_off]:
            if now - load.since >= BASE_PART_MAX_OFF:
                self.active.remove(load)
                for p, w in load.draw.items():
                    self.drift[p] += w
                retired.append(self._event(
                    now, "BASE_SHIFT", load, dict(load.draw),
                    f"off for over {BASE_PART_MAX_OFF.total_seconds() / 3600:.0f} h - "
                    "treated as a lasting drop of the base load",
                    duration_s=(now - load.since).total_seconds(),
                ))
        return retired

    def _on_single(self, when: datetime, phase: str, delta: float) -> list[Event]:
        ev = self.ev
        # A switched-off base part coming back (the PC turned on again) closes
        # its negative load before anything is matched against profiles.
        back, back_err = None, float("inf")
        for load in self.active:
            off = -load.draw.get(phase, 0.0)
            if not load.base_part_off or off <= 0:
                continue
            tolerance = max(OFF_TOLERANCE_MIN_W, OFF_TOLERANCE_FRACTION * off)
            if load.profile is not None:
                tolerance = max(tolerance, load.profile.tolerance_w)
            err = abs(delta - off)
            if err <= tolerance and err < back_err:
                back, back_err = load, err
        if back is not None:
            return [self._remove(when, back, {phase: delta}, f"+{delta:.0f} W on {phase}: the base part is back on",
                                 kind="BASE_ON")]

        best, best_err = None, float("inf")
        for profile in self.profiles:
            if profile.kind != "step" or profile.phase != phase:
                continue
            err = abs(delta - profile.step_w)
            if err <= profile.tolerance_w and err < best_err:
                best, best_err = profile, err
        if best is None:
            reason = f"+{delta:.0f} W on {phase}, above the base load but matches no profile"
            return [self._add(when, {phase: delta}, None, 0.0, reason, unreliable=ev is not None)]
        conf = _signature_confidence(best_err, best.tolerance_w)
        reason = f"phase {phase} + size match ({delta:.0f} vs {best.step_w:.0f}±{best.tolerance_w:.0f} W)"
        if ev is not None:
            conf *= EV_RELIABILITY_FACTOR
            reason += "; EV charging so confidence halved"
        # One device cannot be on twice. If it is already on the stack, its OFF
        # was missed; retire the stale instance so it cannot haunt the stack.
        events: list[Event] = []
        stale = next((l for l in self.active if l.profile is best), None)
        if stale is not None:
            self.active.remove(stale)
            for p, w in stale.draw.items():
                self.drift[p] += w  # its draw was already gone from the actual reading
            events.append(self._event(
                when, "OFF_RECONCILED", stale, {p: -w for p, w in stale.draw.items()},
                "switched on again while believed on - earlier OFF was missed",
            ))
        events.append(self._add(when, {phase: delta}, best, conf, reason, unreliable=ev is not None))
        return events

    def _ev_off(self, downs: dict[str, float]) -> bool:
        ev = self.ev
        if ev is None:
            return False
        return all(-downs[p] >= EV_OFF_FRACTION * ev.draw.get(p, 0.0) for p in PHASES)

    def _off_ev(self, when: datetime, downs: dict[str, float]) -> Event:
        ev = self.ev
        assert ev is not None
        event = self._remove(when, ev, downs, "all three phases dropped by the EV's draw")
        # Anything that dropped beyond the EV's own draw is another load ending
        # in the same reading: match it like any OFF (falls back to drift).
        self._ev_off_leftovers = []
        for p in PHASES:
            leftover = -downs[p] - ev.draw.get(p, 0.0)
            if leftover >= STEP_MIN_W:
                extra = self._off_single(when, p, -leftover)
                if extra:
                    self._ev_off_leftovers.append(extra)
            elif leftover <= -STEP_MIN_W:
                self.drift[p] -= leftover  # dropped less than its draw: charger had throttled
        return event

    def _off_single(self, when: datetime, phase: str, delta: float) -> Event | None:
        drop = -delta
        best, best_err = None, float("inf")
        for load in self.active:
            if load is self.ev:
                continue
            draw = load.draw.get(phase, 0.0)
            if draw <= 0:
                continue  # only loads running ABOVE the base load can switch off here
            tolerance = max(OFF_TOLERANCE_MIN_W, OFF_TOLERANCE_FRACTION * draw)
            if load.profile is not None:
                tolerance = max(tolerance, load.profile.tolerance_w)
            err = abs(drop - draw)
            if err <= tolerance and err < best_err:
                best, best_err = load, err
        if best is not None:
            return self._remove(when, best, {phase: delta}, f"-{drop:.0f} W on {phase} matches its draw")

        ev = self.ev
        if ev is not None and ev.draw.get(phase):
            # charger throttling: its draw on this phase shrinks, no event
            ev.draw[phase] = max(0.0, ev.draw[phase] - drop)
            return self._event(when, "EV_ADJUST", ev, {phase: delta}, "charger throttled (load control)")

        # Nothing running above the base load explains the drop, so part of the
        # base load itself switched off (a PC, a server, ventilation stepping
        # down). It joins the stack as a NEGATIVE load - measured, paired with
        # its return, clustered and labelled like any unknown.
        best_profile, best_err = None, float("inf")
        for profile in self.profiles:
            if not profile.raw.get("base_component") or profile.phase != phase:
                continue
            err = abs(drop - profile.step_w)
            if err <= profile.tolerance_w and err < best_err:
                best_profile, best_err = profile, err
        if best_profile is not None:
            conf = _signature_confidence(best_err, best_profile.tolerance_w)
            reason = f"-{drop:.0f} W on {phase}: {best_profile.display_name} (part of the base load) switched off"
        else:
            conf = 0.0
            reason = f"-{drop:.0f} W on {phase} below the base load: part of it switched off"
        return self._add(when, {phase: -drop}, best_profile, conf, reason, unreliable=False, kind="BASE_OFF")

    # --- stack edits --------------------------------------------------------
    def _add(
        self,
        when: datetime,
        draw: dict[str, float],
        profile: Profile | None,
        confidence: float,
        reason: str,
        *,
        unreliable: bool,
        kind: str = "ON",
    ) -> Event:
        load = ActiveLoad(
            key=next(self._keys),
            load_id=profile.id if profile else "unknown",
            display_name=profile.display_name if profile else "unknown",
            state=LoadState.MATCHED if profile else LoadState.UNKNOWN,
            draw=dict(draw),
            since=when,
            confidence=confidence,
            reason=reason,
            unreliable=unreliable,
            profile=profile,
        )
        self.active.append(load)
        return self._event(when, kind, load, draw, reason)

    def _remove(self, when: datetime, load: ActiveLoad, delta: dict[str, float], reason: str,
                *, kind: str = "OFF") -> Event:
        self.active.remove(load)
        return self._event(when, kind, load, delta, reason, duration_s=(when - load.since).total_seconds())

    @staticmethod
    def _event(
        when: datetime,
        kind: str,
        load: ActiveLoad,
        delta: dict[str, float],
        reason: str,
        *,
        duration_s: float | None = None,
    ) -> Event:
        return Event(
            when=when,
            kind=kind,
            load_id=load.load_id,
            display_name=load.display_name,
            state=str(load.state),
            phases="".join(p for p in PHASES if p in delta),
            delta_w=dict(delta),
            confidence=load.confidence,
            reason=reason,
            unreliable=load.unreliable,
            duration_s=duration_s,
            load_key=load.key,
        )
