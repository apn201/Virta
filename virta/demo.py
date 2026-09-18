"""Replay a real recorded day - Virta without Home Assistant, for anyone to run.

    python -m virta.demo                          # from 07:20, 10x speed, Nemotron if a key is set
    python -m virta.demo --from 08:40 --speed 30  # jump to the EV charge
    python -m virta.demo --no-llm                 # no Token Factory key needed
    python -m virta.console                       # in a second terminal: the instrument

The sample (samples/) is ONE REAL DAY measured by a Shelly 3EM in the author's
house, with the real Nordpool prices of that day - dates shifted by 44 weeks,
nothing else changed. It contains deliberate test runs: the halogens, a kettle,
a toaster, and an EV charge at ~11 kW.

The replay drives the SAME code as the live loop: stacking detector, base-load
calibration, context scoring, rhythm finder, and the Tier 1 advisor on
Nemotron (Nebius Token Factory), gated by the same cost gate. The detector runs
in the sample's own clock, so settle windows and costs are exactly as they were;
only the display is mapped onto real time (T below), compressed by --speed.
Nothing here talks to Home Assistant, and no action is ever taken.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .baseline import BaselineTracker
from .config import ConfigError, load_cost_config
from .cost_control import CloudCallGate, Situation, price_tier
from .detector import LoadState, StackingDetector
from .ev_signature import rough_baseline
from .fsutil import atomic_write_text
from .history import _finish, _to_float, _to_local, align_phases
from .live import session_summary
from .prices import PriceCurve
from .profiles import load_profiles
from .rhythm import strong_rhythms
from .scoring import Context, score_state

SAMPLE_POWER = Path("samples/sample_day_power.csv.gz")
SAMPLE_PRICES = Path("samples/sample_day_prices.csv.gz")
SAMPLE_INSIGHTS = Path("samples/sample_insights.json")
PHASE_IDS = ("sensor.3em_channel_a_power", "sensor.3em_channel_b_power", "sensor.3em_channel_c_power")
TOTAL_ID = "sensor.3em_total_power"
TICK_S = 0.5  # real seconds between console snapshots


def load_sample() -> tuple[dict, list[tuple[datetime, float]]]:
    raw: dict[str, list] = {}
    with gzip.open(SAMPLE_POWER, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            when, value = _to_local(row["last_updated"]), _to_float(row["state"])
            if when is not None and value is not None:
                raw.setdefault(row["entity_id"], []).append((when, value))
    prices = []
    with gzip.open(SAMPLE_PRICES, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            when, value = _to_local(row["last_updated"]), _to_float(row["state"])
            if when is not None and value is not None:
                prices.append((when, value))
    return _finish(raw), sorted(prices)


def nordpool_attrs(prices: list[tuple[datetime, float]]) -> dict:
    """The sample's prices in the shape of the Nordpool sensor's attributes."""
    slots = []
    for i, (start, value) in enumerate(prices):
        end = prices[i + 1][0] if i + 1 < len(prices) else start + timedelta(minutes=15)
        slots.append({"start": start.isoformat(), "end": end.isoformat(), "value": value})
    return {"raw_today": slots, "raw_tomorrow": [], "tomorrow_valid": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", default="07:20", help="sample clock to start at (HH:MM)")
    parser.add_argument("--speed", type=float, default=10.0, help="sample minutes per real minute")
    parser.add_argument("--no-llm", action="store_true", help="skip Nemotron (no Token Factory key needed)")
    parser.add_argument("--state-out", default="var/state.json")
    args = parser.parse_args(argv)

    if not SAMPLE_POWER.is_file():
        print(f"No sample at {SAMPLE_POWER}.", file=sys.stderr)
        return 1
    series, prices = load_sample()
    frames = align_phases(series, TOTAL_ID, PHASE_IDS)
    day = frames[0].when.date()
    hh, mm = (int(x) for x in args.start.split(":"))
    sample_start = datetime.combine(day, datetime.min.time()).astimezone().replace(hour=hh, minute=mm)
    curve = PriceCurve(prices)
    attrs = nordpool_attrs(prices)
    profiles = [p for p in load_profiles("profiles")]
    cost_cfg = load_cost_config()
    insights = json.loads(SAMPLE_INSIGHTS.read_text(encoding="utf-8")) if SAMPLE_INSIGHTS.is_file() else {}

    # warm up: the detector and base load see the day up to the start point instantly
    seed = rough_baseline([f for f in frames if f.when.hour < 6])
    tracker = BaselineTracker(seed)
    detector = StackingDetector(profiles, seed)
    idx = 0
    while idx < len(frames) and frames[idx].when < sample_start:
        detector.update(frames[idx])
        floor = tracker.observe(detector.calibration_frame(frames[idx]), stack_empty=detector.calibration_ready)
        if floor:
            detector.set_baseline(floor)
        idx += 1

    use_llm = not args.no_llm
    gate = CloudCallGate(cost_cfg) if use_llm else None
    advise = None
    if use_llm:
        try:
            from .advisor import advise as _advise
            from .config import load_nebius_config

            load_nebius_config()  # fail early and clearly if there is no key
            advise = _advise
        except ConfigError as exc:
            print(f"Nemotron off: {exc.args[0].splitlines()[0]} (use --no-llm to silence this)")
            use_llm = False
    executor = ThreadPoolExecutor(max_workers=1)
    in_flight: Future | None = None
    verdict, last_advice = "", {}

    real_start = datetime.now().astimezone()

    def T(when: datetime) -> datetime:  # sample time -> display time
        return real_start + (when - sample_start) / args.speed

    print(f"VIRTA DEMO  replaying a real recorded day ({day:%a %d.%m.%Y}, dates shifted) from {args.start} "
          f"at {args.speed:g}x  - Nemotron {'on' if use_llm else 'off'}.  Ctrl+C to stop.")
    print(f"  run the console in another terminal:  python -m virta.console")
    recent: list = []
    rhythms: list = []
    rhythm_due = sample_start
    try:
        while idx < len(frames):
            real_now = datetime.now().astimezone()
            sample_now = sample_start + (real_now - real_start) * args.speed
            before = {l.key: l for l in detector.active}
            while idx < len(frames) and frames[idx].when <= sample_now:
                frame = frames[idx]
                for event in detector.update(frame):
                    print(f"  sample {event.when:%H:%M:%S}  {event.describe()[27:110]}")
                    ended = before.get(event.load_key)
                    if ended and event.kind in ("OFF", "OFF_RECONCILED", "BASE_ON", "BASE_SHIFT"):
                        summary = session_summary(ended, event.when, curve, finished=True)
                        summary["since"], summary["ended"] = T(ended.since).isoformat(), T(event.when).isoformat()
                        recent.insert(0, summary)
                        del recent[4:]
                    before = {l.key: l for l in detector.active}
                floor = tracker.observe(detector.calibration_frame(frame), stack_empty=detector.calibration_ready)
                if floor:
                    detector.set_baseline(floor)
                idx += 1
            last = frames[max(0, idx - 1)]

            if sample_now >= rhythm_due:  # rhythms from the full-resolution sample, last 2 h
                window = {ph: [(t, v) for t, v in series[eid] if sample_now - timedelta(hours=2) <= t <= sample_now]
                          for ph, eid in zip("ABC", PHASE_IDS)}
                rhythms = [r.summary() for r in strong_rhythms(window)]
                for r in rhythms:
                    r["recent_pulses"] = [round(T(datetime.fromtimestamp(t).astimezone()).timestamp())
                                          for t in r.get("recent_pulses", [])]
                rhythm_due = sample_now + timedelta(minutes=5)

            dark = sample_now.hour < 7 or sample_now.hour >= 17  # November in Finland (approximate)
            state = detector.state()
            scored = score_state(state, Context(sample_now, dark))
            price_now = curve.at(sample_now)
            tier = price_tier(price_now, cost_cfg) if price_now is not None else None
            loads = []
            for s in scored:
                summary = session_summary(s.load, sample_now, curve)
                loads.append({"id": s.load.load_id, "name": s.load.display_name, "state": str(s.load.state),
                              "phases": s.load.phases, "draw_w": s.load.draw, "confidence": round(s.confidence, 3),
                              "reason": s.reason, "unusual": s.unusual, "unreliable": s.unreliable,
                              "base_part_off": s.load.base_part_off, **summary,
                              "since": T(s.load.since).isoformat(), "label": s.label()})
            trace_from = sample_now - timedelta(minutes=20 * args.speed)
            snapshot = {
                "when": real_now.isoformat(),
                "replay": {"speed": args.speed, "sample_clock": sample_now.strftime("%H:%M"),
                           "note": "real recorded day, dates shifted"},
                "power_w": {"A": last.a, "B": last.b, "C": last.c, "total": last.total},
                "trace": [[round(T(f.when).timestamp(), 1), round(f.total), round(f.a or 0), round(f.b or 0),
                           round(f.c or 0)] for f in frames[max(0, idx - 2000):idx] if f.when >= trace_from],
                "price_curve": [[round(T(s).timestamp()), round(T(e).timestamp()), v] for s, e, v in (
                    (prices[i][0], prices[i + 1][0] if i + 1 < len(prices) else prices[i][0] + timedelta(minutes=15),
                     prices[i][1]) for i in range(len(prices)))],
                "price_bounds_c_kwh": {"cheap": cost_cfg.price_cheap_max_c, "normal": cost_cfg.price_normal_max_c,
                                       "expensive": cost_cfg.price_expensive_max_c},
                "base_load_w": tracker.current.__dict__,
                "residual_w": state.residual_w,
                "loads": loads,
                "recent": recent,
                "rhythms": rhythms,
                "ev_active": state.ev_active,
                "others_unreliable": state.others_unreliable,
                "context": {"part_of_day": f"REPLAY {sample_now:%a %H:%M}, {'dark' if dark else 'light'}".upper(),
                            "is_dark": dark},
                "price": {"c_kwh": price_now, "tier": str(tier) if tier else None, "tomorrow_valid": False},
            }

            # Tier 1 on Nemotron, on situation change - same gate, same advisor
            if in_flight is not None and in_flight.done():
                advice, _ = in_flight.result()
                gate.add_usage(advice.usage)
                in_flight = None
                if advice.ok:
                    verdict = advice.verdict
                    last_advice = {**advice.to_json(), "at": real_now.isoformat(),
                                   "tokens": advice.usage.get("completion_tokens")}
                    print(f"  NEMOTRON >> {verdict}")
            if use_llm and in_flight is None and price_now is not None:
                ids = sorted(s.load.load_id if s.load.state is LoadState.MATCHED else f"unknown_{s.load.phases}"
                             for s in scored if not s.load.base_part_off)
                situation = Situation.build(ids, price_now, dark, cost_cfg)
                decision = gate.evaluate(situation, now=sample_now)
                if decision.should_call:
                    gate.record_call({}, situation=situation, now=real_now)
                    advisor_view = {**snapshot, "base_load_w": tracker.current.__dict__}
                    in_flight = executor.submit(advise, json.loads(json.dumps(advisor_view, default=str)), attrs,
                                                now=sample_now)
            snapshot.update({
                "verdict": verdict, "advice": last_advice,
                "reasoning": {"in_flight": in_flight is not None, "since": real_now.isoformat() if in_flight else None,
                              "why": "situation changed" if in_flight else None},
                "gate": {"status": gate.status() if gate else "calls: 0/0 this hour | 0/0 today | 0 lifetime"},
                "commentary": ({**insights["insights"][0], "index": 1, "count": len(insights["insights"])}
                               if insights.get("insights") else None),
            })
            atomic_write_text(args.state_out, json.dumps(snapshot, indent=1, default=str))
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    print("end of the sample day")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
