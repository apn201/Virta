"""Slice 4 check: the stacking detector behaves as spec 5.2/5.3 says it must.

Run:  python -m virta.check_detector

Part 1 is synthetic and always runs: the spec 5.2 worked example (a known load
detected on top of an UNKNOWN one), spike rejection, and the EV rule (other
detections flagged unreliable while it charges).

Part 2 runs only when the cached real history exists (data/, git-ignored): the
builder's labelled test run on Wed 16.09 must come out as labelled.

No network, no cloud.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .ev_signature import Baseline
from .history import Frame
from .profiles import load_profiles

from .detector import StackingDetector

T0 = datetime(2026, 9, 18, 12, 0, 0).astimezone()
FLOOR = Baseline(total=1000.0, a=350.0, b=200.0, c=450.0)


def _frames(script: list[tuple[int, int, float, float, float]]) -> list[Frame]:
    """(start_s, end_s, extra_a, extra_b, extra_c) segments at a 15 s cadence."""
    frames = []
    for start, end, a, b, c in script:
        for s in range(start, end, 15):
            pa, pb, pc = FLOOR.a + a, FLOOR.b + b, FLOOR.c + c
            frames.append(Frame(T0 + timedelta(seconds=s), pa + pb + pc, pa, pb, pc, 0.0, 0.0, 0.0))
    return frames


def _run(frames: list[Frame]) -> StackingDetector:
    det = StackingDetector(load_profiles("profiles"), FLOOR)
    for frame in frames:
        det.update(frame)
    return det


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  - ' + detail) if detail else ''}")
    return ok


def synthetic() -> bool:
    ok = True
    print("SYNTHETIC")

    # spec 5.2 worked example: an unknown load is already on phase B; a toaster
    # lands on top of it and must still be recognised - measured against the
    # accounted total, not the cold floor. 1500 W matches no B profile (toaster
    # 890+-120, kettle 2000+-200) - the size of the real unknown B pulses on 16.09.
    det = _run(_frames([
        (0, 120, 0, 0, 0),
        (120, 600, 0, 1500, 0),  # unknown 1.5 kW on B
        (600, 660, 0, 1500 + 890, 0),  # toaster on top
        (660, 900, 0, 1500, 0),  # toaster off
        (900, 1200, 0, 0, 0),  # unknown off
    ]))
    ons = [e for e in det.events if e.kind == "ON"]
    offs = [e for e in det.events if e.kind == "OFF"]
    ok &= check("unknown 1.5 kW on B reported as UNKNOWN", bool(ons) and ons[0].state == "UNKNOWN",
                ons[0].describe()[20:80] if ons else "no event")
    ok &= check("toaster recognised on top of the unknown",
                len(ons) > 1 and ons[1].load_id == "toaster", ons[1].describe()[20:80] if len(ons) > 1 else "")
    ok &= check("both switch-offs paired, stack empty at the end",
                len(offs) == 2 and det.stack_empty, f"{len(offs)} OFF events")

    # a single spiky reading is noise, not a load
    det = _run(_frames([(0, 150, 0, 0, 0), (150, 165, 0, 2000, 0), (165, 400, 0, 0, 0)]))
    ok &= check("one-reading +2 kW spike rejected", not det.events, f"{len(det.events)} events")

    # EV: balanced 3-phase step; a kettle during charging is flagged unreliable
    det = _run(_frames([
        (0, 120, 0, 0, 0),
        (120, 600, 3450, 3450, 3450),
        (600, 690, 3450, 3450 + 2000, 3450),  # kettle on B while charging
        (690, 900, 3450, 3450, 3450),
        (900, 1100, 0, 0, 0),
    ]))
    ev_on = [e for e in det.events if e.kind == "ON" and e.load_id == "ev_charger"]
    kettle = [e for e in det.events if e.kind == "ON" and e.load_id == "kettle"]
    ok &= check("EV detected from the balanced 3-phase signature", bool(ev_on),
                f"{ev_on[0].confidence:.0%}" if ev_on else "missed")
    ok &= check("kettle during charging flagged unreliable, confidence halved",
                bool(kettle) and kettle[0].unreliable and kettle[0].confidence <= 0.5,
                f"{kettle[0].confidence:.0%}, unreliable={kettle[0].unreliable}" if kettle else "missed")
    ok &= check("EV and kettle both switched off, stack empty", det.stack_empty)
    return ok


# The builder's test log for Wed 16.09 (times approximate, from memory).
LABELLED_16_09 = [
    ("halogen1", "07:25", "07:28"),
    ("halogen2", "07:29", "07:31"),
    ("kettle", "07:42", "07:44"),
    ("toaster", "07:45", "07:46"),
    ("ev_charger", "08:50", "10:42"),
]
SLACK = timedelta(minutes=2)


def labelled() -> bool | None:
    path = Path("data/power_history.csv")
    if not path.is_file():
        print("\nLABELLED TEST RUN  skipped - no cached history (run: python -m virta.replay --fetch)")
        return None

    from .config import load_ha_config
    from .baseline import BaselineTracker
    from .ev_signature import rough_baseline
    from .history import align_phases, load_csv

    cfg = load_ha_config(require_token=False)
    series, _ = load_csv(path, cfg.power_entities)
    frames = align_phases(series, cfg.entity("SENSOR_TOTAL"),
                          tuple(cfg.entity(k) for k in ("SENSOR_PHASE_A", "SENSOR_PHASE_B", "SENSOR_PHASE_C")))
    seed = rough_baseline([f for f in frames if f.when <= frames[0].when + timedelta(hours=6)])
    tracker = BaselineTracker(seed)
    det = StackingDetector(load_profiles("profiles"), seed)
    for frame in frames:
        det.update(frame)
        floor = tracker.observe(det.calibration_frame(frame), stack_empty=det.calibration_ready)
        if floor:
            det.set_baseline(floor)

    print("\nLABELLED TEST RUN (Wed 16.09)")
    tz = frames[0].when.tzinfo
    ok = True
    for load_id, on, off in LABELLED_16_09:
        t_on = datetime.combine(datetime(2026, 9, 16).date(), datetime.strptime(on, "%H:%M").time(), tzinfo=tz)
        t_off = datetime.combine(datetime(2026, 9, 16).date(), datetime.strptime(off, "%H:%M").time(), tzinfo=tz)
        hit_on = next((e for e in det.events if e.kind == "ON" and e.load_id == load_id
                       and abs(e.when - t_on) <= SLACK), None)
        hit_off = next((e for e in det.events if e.kind == "OFF" and e.load_id == load_id
                        and abs(e.when - t_off) <= SLACK), None)
        detail = (f"on {hit_on.when:%H:%M:%S} {hit_on.confidence:.0%}, off {hit_off.when:%H:%M:%S}"
                  if hit_on and hit_off else f"on={bool(hit_on)} off={bool(hit_off)}")
        ok &= check(f"{load_id:11s} labelled {on}-{off}", bool(hit_on and hit_off), detail)

    unexplained = [e for e in det.events if e.kind == "UNEXPLAINED_DROP"]
    ok &= check("no unexplained drops across the whole history", not unexplained, f"{len(unexplained)}")
    ok &= check("stack empty at the end (nothing stuck on)", det.stack_empty,
                ", ".join(l.label() for l in det.active))
    return ok


def scoring() -> bool:
    """Context scoring (spec 7): usual hour boosts, odd hour demotes, dark doubles a correlation."""
    from .scoring import Context, score_state

    ok = True
    print("\nCONTEXT SCORING")

    def kettle_at(hour: int) -> tuple[float, str, bool]:
        det = _run(_frames([(0, 120, 0, 0, 0), (120, 400, 0, 2000, 0)]))
        when = T0.replace(hour=hour, minute=30)
        scored = score_state(det.state(), Context(when, is_dark=False))
        s = scored[0]
        return s.confidence, s.reason, s.unusual

    base = _run(_frames([(0, 120, 0, 0, 0), (120, 400, 0, 2000, 0)])).active[0].confidence
    morning, why_m, _ = kettle_at(7)
    night, why_n, _ = kettle_at(3)
    ok &= check("kettle at 07:30 boosted by its usual morning", morning > base, f"{base:.0%} -> {morning:.0%}")
    ok &= check("kettle at 03:30 demoted, reason says why", night < base and "outside its usual" in why_n,
                f"{base:.0%} -> {night:.0%}")

    # halogen with the kettle on: kitchen cluster, stronger when dark. An imperfect
    # +240 W match (not the exact 265) so the boosts are not hidden by the 98% ceiling.
    det = _run(_frames([(0, 120, 0, 0, 0), (120, 300, 0, 2000, 0), (300, 600, 0, 2000, 240)]))
    evening = T0.replace(hour=19, minute=0)
    light = next(s for s in score_state(det.state(), Context(evening, is_dark=False)) if s.load.load_id == "halogen1")
    dark = next(s for s in score_state(det.state(), Context(evening, is_dark=True)) if s.load.load_id == "halogen1")
    ok &= check("halogen + kettle on: correlation boost, doubled in the dark",
                dark.confidence > light.confidence and "stronger in the dark" in dark.reason,
                f"light {light.confidence:.0%}, dark {dark.confidence:.0%}")
    return ok


def base_parts() -> bool:
    """A part of the base load switching OFF is a first-class negative load."""
    from .baseline import BaselineTracker

    ok = True
    print("\nBASE PARTS SWITCHED OFF")

    # a PC (150 W on A) off for 30 min, a kettle while it's off, then the PC back
    det = _run(_frames([
        (0, 120, 0, 0, 0),
        (120, 900, -150, 0, 0),  # PC off
        (900, 990, -150, 2000, 0),  # kettle while the PC is off
        (990, 1920, -150, 0, 0),
        (1920, 2200, 0, 0, 0),  # PC back on
    ]))
    off = [e for e in det.events if e.kind == "BASE_OFF"]
    back = [e for e in det.events if e.kind == "BASE_ON"]
    kettle = [e for e in det.events if e.kind == "ON" and e.load_id == "kettle"]
    ok &= check("PC switching off -> BASE_OFF, a negative unknown load", bool(off) and off[0].delta_w.get("A", 0) < -100,
                off[0].describe()[20:90] if off else "missed")
    ok &= check("kettle still recognised while the PC is off", bool(kettle))
    ok &= check("PC back on -> BASE_ON, paired, with its off-time", bool(back) and back[0].duration_s is not None,
                f"off {back[0].duration_s / 60:.0f} min" if back else "missed")
    ok &= check("stack empty afterwards", det.stack_empty, ", ".join(l.label() for l in det.active))

    # the base load keeps meaning "everything normally on" while the PC is off
    det = StackingDetector(load_profiles("profiles"), FLOOR)
    tracker = BaselineTracker(FLOOR)
    for frame in _frames([(0, 120, 0, 0, 0), (120, 3600 * 2, -150, 0, 0)]):
        det.update(frame)
        floor = tracker.observe(det.calibration_frame(frame), stack_empty=det.calibration_ready)
        if floor:
            det.set_baseline(floor)
    tracker.flush()
    ok &= check("recalibrating during a 2 h PC-off stretch keeps phase A at ~350 W, not ~200",
                abs(tracker.current.a - FLOOR.a) < 20, f"A = {tracker.current.a:.0f} W")

    # off for more than a day is a lasting change, not a pause
    det = _run(_frames([(0, 120, 0, 0, 0), (120, 3600 * 25, -150, 0, 0)]))
    shift = [e for e in det.events if e.kind == "BASE_SHIFT"]
    ok &= check("off for over 24 h -> BASE_SHIFT (the base load really dropped)", bool(shift) and det.stack_empty,
                shift[0].reason if shift else "missed")
    return ok


def main() -> int:
    results = [synthetic(), scoring(), base_parts(), labelled()]
    passed = all(r is not False for r in results)
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SOME CHECKS FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
