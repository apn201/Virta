"""Slice 6: the live loop - poll HA, detect, score, gate (spec 8, 11, 12).

Run:
    python -m virta.live                 # forever, status line every tick
    python -m virta.live --minutes 5     # bounded run
    python -m virta.live --once          # a single tick (smoke test)
    python -m virta.live --quiet         # only events, base-load changes and gate triggers

The LOCAL loop (spec 8): every HA_POLL_INTERVAL_S read the three phase sensors
over the LAN, feed the same streaming detector + baseline the replay uses, score
by context, and write var/state.json for the console. Free: HTTP + arithmetic.

The CLOUD loop is gated but DRY in slice 6: when the gate says "call", the loop
logs WOULD CALL and moves on. The advice prompt is slice 7. Nothing leaves the
house in this slice, and nothing is spent.

Built for the Pi3: stdlib HTTP, a few comparisons per tick, no numpy, no plots.
HA hiccups are logged and backed off, never fatal - it has to run for days.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from .advisor import Advice, advise
from .baseline import BaselineTracker
from .config import ConfigError, load_cost_config, load_ha_config
from .cost_control import CloudCallGate, Situation, price_tier
from .detector import LoadState, StackingDetector
from .ev_signature import rough_baseline
from .fsutil import atomic_write_text
from .ha_client import HAClient, HAError, parse_price_curve
from .history import Frame, align_phases, fetch_history
from .prices import PriceCurve
from .profiles import load_profiles
from .scoring import Context, score_state

STATE_OUT = "var/state.json"
EVENTS_OUT = "var/events_live.jsonl"
BASELINE_STATE = "var/baseline.json"
INSIGHTS_IN = "var/insights.json"  # Tier 2 output, served here for free (Tier 3)
NIGHTLY_LAST = "var/nightly_last_run.txt"
COMMENTARY_ROTATE_MIN = 3  # one cached insight at a time, unobtrusive (spec 9)
TRACE_MINUTES = 20  # history the console scrolls through
RECENT_MINUTES = 30  # finished sessions stay on the console this long...
RECENT_MAX = 4  # ...up to this many


class InsightFeed:
    """Tier 3 (spec 6A): serve last night's conclusions all day, at zero cost.

    Reloads var/insights.json only when it changes; rotates one line every
    COMMENTARY_ROTATE_MIN minutes, deterministically by the clock.
    """

    def __init__(self, path: str = INSIGHTS_IN) -> None:
        self.path = Path(path)
        self._mtime = 0.0
        self.data: dict = {}

    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self._mtime = mtime
            except (ValueError, OSError):
                pass  # mid-write or unreadable: keep what we have

    def current(self, now: datetime) -> dict | None:
        self._reload()
        items = self.data.get("insights") or []
        if not items:
            return None
        index = int(now.timestamp() // (COMMENTARY_ROTATE_MIN * 60)) % len(items)
        item = items[index]
        return {"line": item.get("line"), "kind": item.get("kind"), "evidence": item.get("evidence"),
                "index": index + 1, "count": len(items), "generated_at": self.data.get("generated_at")}


CONTEXT_REFRESH_S = 60.0  # price/sun/temps change slowly; power is read every tick
SITUATION_MIN_CONFIDENCE = 0.5  # a load must be at least this sure to shape advice
MAX_BACKOFF_S = 120.0


class LiveReader:
    """Everything the loop reads from HA, with slow context cached."""

    def __init__(self, client: HAClient) -> None:
        self.client = client
        cfg = client.config
        self.phase_ids = [cfg.entity("SENSOR_PHASE_A"), cfg.entity("SENSOR_PHASE_B"), cfg.entity("SENSOR_PHASE_C")]
        self._context_at: float = 0.0
        self.price: float | None = None
        self.price_attrs: dict = {}
        self.curve = PriceCurve([])  # the real slots, for costing sessions as they run
        self.tomorrow_valid = False
        self.is_dark: bool | None = None
        self.outdoor: float | None = None
        self.indoor: float | None = None

    def frame(self, now: datetime) -> Frame | None:
        """One reading. Total is A+B+C computed here - the HA total sensor is the
        same sum, but read separately it can be a mixed-epoch partial (slice 3)."""
        readings = [self.client.get_state(eid) for eid in self.phase_ids]
        if not all(r.usable for r in readings):
            return None  # spec 3.5: skip, never float() an 'unavailable'
        a, b, c = (r.value for r in readings)
        ages = [(now - r.last_updated).total_seconds() if r.last_updated else None for r in readings]
        return Frame(now, a + b + c, a, b, c, *ages)

    def refresh_context(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._context_at < CONTEXT_REFRESH_S:
            return
        cfg = self.client.config
        price, attrs = self.client.price_now()
        self.price = price
        self.price_attrs = attrs  # the curve, for the advice call
        self.curve = PriceCurve.from_nordpool(attrs)
        self.tomorrow_valid = bool(attrs.get("tomorrow_valid"))
        self.is_dark = self.client.is_dark()
        self.outdoor = self.client.get_state(cfg.entity("SENSOR_TEMP_OUT")).value
        self.indoor = self.client.get_state(cfg.entity("SENSOR_TEMP_IN")).value
        self._context_at = time.monotonic()


def seed_floor(client: HAClient, profiles: list) -> tuple[object, str]:
    """First-ever start: calibrate from the last 24 h the same way the live loop
    will - quiet stretches, via a detector replay - so the very first floor is
    the real one. A rough p10 is only the fallback when no quiet stretch exists.
    Every later start uses the persisted floor instead."""
    cfg = client.config
    end = datetime.now().astimezone()
    series, _ = fetch_history(client, cfg.power_entities, end - timedelta(hours=24), end)
    frames = align_phases(series, cfg.entity("SENSOR_TOTAL"),
                          (cfg.entity("SENSOR_PHASE_A"), cfg.entity("SENSOR_PHASE_B"), cfg.entity("SENSOR_PHASE_C")))
    rough = rough_baseline(frames)
    tracker = BaselineTracker(rough)  # in memory only; the caller persists
    detector = StackingDetector(profiles, rough)
    for frame in frames:
        detector.update(frame)
        floor = tracker.observe(detector.calibration_frame(frame), stack_empty=detector.calibration_ready)
        if floor:
            detector.set_baseline(floor)
    tracker.flush()
    if tracker.updates:
        _, floor, stretch = tracker.updates[-1]
        return floor, (f"quiet stretches in the last 24 h (latest {stretch.start:%H:%M}-{stretch.end:%H:%M}, "
                       f"{len(frames)} readings)")
    return rough, f"p10 of the last 24 h - no quiet stretch found ({len(frames)} readings)"


def session_summary(load, until: datetime, curve: PriceCurve, *, finished: bool = False) -> dict:
    """How long a load has run, at what draw, and what it cost at the REAL prices
    of every slot it spanned. For a base part switched off it is money NOT spent."""
    watts = abs(load.total_w)
    cost = curve.cost_eur(load.since, until, watts)
    summary = {
        "minutes": round((until - load.since).total_seconds() / 60, 1),
        "watts": round(watts),
        "kwh": round(watts * (until - load.since).total_seconds() / 3.6e6, 3),
        "cost_eur": None if cost is None else round(cost, 4),
        "saving": load.base_part_off,  # True: the cost is money NOT spent
    }
    if finished:
        summary.update({"label": load.label(), "id": load.load_id, "state": str(load.state),
                        "name": load.display_name, "confidence": round(load.confidence, 3), "phases": load.phases,
                        "base_part_off": load.base_part_off, "since": load.since.isoformat(),
                        "ended": until.isoformat()})
    return summary


def price_curve_for_console(attrs: dict) -> list[list]:
    """Today's (and tomorrow's, once published) real slots: [[start_epoch_s, end_epoch_s, c_kwh], ...]."""
    slots = parse_price_curve(attrs, "raw_today")
    if attrs.get("tomorrow_valid"):
        slots += parse_price_curve(attrs, "raw_tomorrow")
    return [[round(s["start"].timestamp()),
             round((s["end"] or s["start"] + timedelta(minutes=15)).timestamp()),
             round(s["value"], 3)] for s in sorted(slots, key=lambda s: s["start"])]


def _atomic_json(path: str, payload: dict) -> None:
    """The console snapshot. Missing one tick is harmless, crashing the loop is not."""
    try:
        atomic_write_text(path, json.dumps(payload, indent=1, default=str))
    except OSError as exc:
        print(f"  (state.json not written this tick: {exc})")


def run(args: argparse.Namespace) -> int:
    try:
        ha_cfg = load_ha_config()
        cost_cfg = load_cost_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    if args.demo:  # reasoning is the show: more often, the same daily ceiling
        from dataclasses import replace

        from .config import DEMO_DEBOUNCE_S, DEMO_HEARTBEAT_MINUTES, DEMO_MAX_CALLS_PER_HOUR

        cost_cfg = replace(cost_cfg, max_calls_per_hour=DEMO_MAX_CALLS_PER_HOUR,
                           heartbeat_minutes=DEMO_HEARTBEAT_MINUTES, debounce_s=DEMO_DEBOUNCE_S)
    interval = args.interval or ha_cfg.poll_interval_s
    client = HAClient(ha_cfg, timeout_s=min(10.0, interval))
    reader = LiveReader(client)
    profiles = load_profiles("profiles")

    try:
        if Path(BASELINE_STATE).is_file():
            tracker = BaselineTracker(rough_baseline([]), state_path=BASELINE_STATE)
            origin = f"persisted quiet-stretch floor ({BASELINE_STATE})"
        else:
            seed, origin = seed_floor(client, profiles)
            tracker = BaselineTracker(seed, state_path=BASELINE_STATE)
            tracker.persist()
        reader.refresh_context(force=True)
    except HAError as exc:
        print(f"HA ERROR at startup\n{exc}", file=sys.stderr)
        return 1

    detector = StackingDetector(profiles, tracker.current)
    gate = CloudCallGate(cost_cfg)
    # One worker: at most one advice call in flight. A reasoning call takes 10-60 s;
    # the 15 s local loop must never wait for it.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nemotron")
    in_flight: Future | None = None
    asking: dict = {}  # what the call in flight was asked about - shown on the console while it thinks
    last_advice: dict = {}

    feed = InsightFeed()
    trace: deque = deque()
    recent: deque = deque(maxlen=RECENT_MAX)  # finished sessions, with their final real cost
    try:  # pre-fill the console trace from HA so a restart doesn't blank it
        since = datetime.now().astimezone() - timedelta(minutes=TRACE_MINUTES)
        series, _ = fetch_history(client, ha_cfg.power_entities, since)
        for f in align_phases(series, ha_cfg.entity("SENSOR_TOTAL"), tuple(reader.phase_ids)):
            if None not in f.phases:
                trace.append([round(f.when.timestamp(), 1), round(f.total), round(f.a), round(f.b), round(f.c)])
    except HAError:
        pass  # the trace simply starts empty
    last_commentary: str | None = None
    nightly_thread: threading.Thread | None = None
    try:
        hh, mm = (int(x) for x in os.environ.get("NIGHTLY_AT", "03:30").split(":"))
        nightly_at = dtime(hh, mm)
    except ValueError:
        print("NIGHTLY_AT must be HH:MM - using 03:30")
        nightly_at = dtime(3, 30)

    def nightly_last() -> str:
        try:
            return Path(NIGHTLY_LAST).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def run_nightly_in_background(started: datetime) -> None:
        # mark first: a crash mid-pass must not re-run (and re-spend) every tick
        atomic_write_text(NIGHTLY_LAST, started.date().isoformat())
        print(f"{started:%H:%M:%S}  NIGHTLY  deep pass started (archive, discovery, insights)")
        from .nightly import run_nightly

        try:
            code = run_nightly(use_llm=not args.dry, gate=gate, verbose=False)
        except Exception as exc:  # the live loop must survive a nightly bug
            print(f"{datetime.now():%H:%M:%S}  NIGHTLY  crashed: {exc!r} - live loop continues")
            return
        try:
            cached = len(json.loads(Path(INSIGHTS_IN).read_text(encoding="utf-8")).get("insights") or [])
        except (OSError, ValueError):
            cached = 0
        status = "ok" if code == 0 else f"code {code}, previous insights kept"
        print(f"{datetime.now():%H:%M:%S}  NIGHTLY  finished ({status}) - {cached} insights cached")

    def harvest(*, block: bool = False) -> None:
        nonlocal in_flight, last_advice
        if in_flight is None or (not block and not in_flight.done()):
            return
        stamp = datetime.now().astimezone()
        try:
            advice, _payload = in_flight.result()
        except Exception as exc:  # a bug in the advice path must not kill the loop
            advice = Advice("", {}, False, f"advice crashed: {exc!r}")
        in_flight = None
        gate.add_usage(advice.usage, now=stamp)
        if advice.ok:
            gate.set_verdict(advice.verdict, now=stamp)
            last_advice = {**advice.to_json(), "at": stamp.isoformat(),
                           "tokens": advice.usage.get("completion_tokens"), "asked": dict(asking)}
            print(f"{stamp:%H:%M:%S}  NEMOTRON  >> {advice.verdict}")
            print(f"{'':10s}action {json.dumps(advice.action, ensure_ascii=False)}"
                  f"  ({advice.usage.get('completion_tokens', '?')} tokens, from {advice.source})")
            for note in advice.warnings:
                print(f"{'':10s}note: {note}")
        else:
            print(f"{stamp:%H:%M:%S}  NEMOTRON  no new advice ({advice.status}) - previous verdict stays")

    print(f"VIRTA LIVE  polling {ha_cfg.api_url} every {interval:g}s  (Ctrl+C to stop)")
    print(f"  base     {tracker.current.describe()}  <- {origin}")
    print(f"  profiles {', '.join(p.id for p in detector.profiles)}")
    if args.dry:
        print("  cloud    DRY RUN - logs when it would call Nemotron; nothing is sent or spent")
    else:
        print(f"  cloud    Nemotron on situation change, gated: {cost_cfg.max_calls_per_hour}/h, "
              f"{cost_cfg.max_calls_per_day}/day, kill switch {cost_cfg.kill_switch_path}  ({gate.status()})")
    print(f"  output   {STATE_OUT} (for the console), {EVENTS_OUT} (event log)\n")

    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    failures = 0
    ticks = 0
    while True:
        started = time.monotonic()
        now = datetime.now().astimezone()
        harvest()
        try:
            reader.refresh_context()
            frame = reader.frame(now)
            failures = 0
        except HAError as exc:
            failures += 1
            backoff = min(MAX_BACKOFF_S, interval * 2 ** min(failures, 4))
            print(f"{now:%H:%M:%S}  HA unreachable ({str(exc).splitlines()[0]}) - retry in {backoff:.0f}s")
            if args.once:
                return 1
            time.sleep(backoff)
            continue

        if frame is None:
            print(f"{now:%H:%M:%S}  a phase reported unavailable - tick skipped")
        else:
            ticks += 1
            before = {l.key: l for l in detector.active}  # to cost a session the moment it ends
            for event in detector.update(frame):
                ended = before.get(event.load_key)
                if ended is not None and event.kind in ("OFF", "OFF_RECONCILED", "BASE_ON", "BASE_SHIFT"):
                    recent.appendleft(session_summary(ended, event.when, reader.curve, finished=True))
                print(f"{now:%H:%M:%S}  EVENT  {event.describe()[18:]}")
                with open(EVENTS_OUT, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_json()) + "\n")
            new_floor = tracker.observe(detector.calibration_frame(frame), stack_empty=detector.calibration_ready)
            if new_floor:
                detector.set_baseline(new_floor)
                print(f"{now:%H:%M:%S}  BASE   base load recalibrated from a quiet stretch -> {new_floor.describe()}")

            state = detector.state()
            context = Context(now, reader.is_dark, reader.outdoor, reader.indoor)
            scored = score_state(state, context)

            tier = price_tier(reader.price, cost_cfg) if reader.price is not None else None
            # the console's scrolling trace: the last TRACE_MINUTES of readings
            trace.append([round(now.timestamp(), 1), round(frame.total), round(frame.a), round(frame.b), round(frame.c)])
            while trace and trace[0][0] < now.timestamp() - TRACE_MINUTES * 60:
                trace.popleft()
            snapshot = {
                "when": now.isoformat(),
                "power_w": {"A": frame.a, "B": frame.b, "C": frame.c, "total": frame.total},
                "trace": list(trace),  # [epoch_s, total, A, B, C]
                "price_curve": price_curve_for_console(reader.price_attrs),
                "price_bounds_c_kwh": {"cheap": cost_cfg.price_cheap_max_c, "normal": cost_cfg.price_normal_max_c,
                                       "expensive": cost_cfg.price_expensive_max_c},
                "base_load_w": tracker.current.__dict__,
                "residual_w": state.residual_w,
                "loads": [
                    {"id": s.load.load_id, "name": s.load.display_name, "state": str(s.load.state),
                     "phases": s.load.phases, "draw_w": s.load.draw, "confidence": round(s.confidence, 3),
                     "reason": s.reason, "unusual": s.unusual, "unreliable": s.unreliable,
                     "base_part_off": s.load.base_part_off,
                     **session_summary(s.load, now, reader.curve),
                     "since": s.load.since.isoformat(), "label": s.label()}
                    for s in scored
                ],
                "ev_active": state.ev_active,
                "others_unreliable": state.others_unreliable,
                "context": {"part_of_day": context.describe(), "is_dark": reader.is_dark,
                            "outdoor_c": reader.outdoor, "indoor_c": reader.indoor},
                "price": {"c_kwh": reader.price, "tier": str(tier) if tier else None,
                          "tomorrow_valid": reader.tomorrow_valid},
            }

            # --- the cloud loop: gated, event-driven, never blocking the tick ---
            decision_txt = "no price - gate skipped (fail closed)"
            if reader.price is not None:
                ids = sorted(
                    s.load.load_id if s.load.state is LoadState.MATCHED else f"unknown_{s.load.phases}"
                    for s in scored
                    if not s.load.base_part_off
                    and (s.load.state is LoadState.UNKNOWN or s.confidence >= SITUATION_MIN_CONFIDENCE)
                )
                situation = Situation.build(ids, reader.price, bool(reader.is_dark), cost_cfg,
                                            tomorrow_valid=reader.tomorrow_valid)
                if in_flight is not None:
                    decision_txt = "advice call in flight - waiting for it"
                else:
                    decision = gate.evaluate(situation, now=now)
                    decision_txt = str(decision)
                    if decision.should_call and args.dry:
                        print(f"{now:%H:%M:%S}  WOULD CALL NEMOTRON  [{decision.trigger}] {decision.reason}")
                        gate.mark_would_call(situation, now=now)
                    elif decision.should_call:
                        print(f"{now:%H:%M:%S}  NEMOTRON  asking  [{decision.trigger}] {decision.reason}")
                        asking = {"since": now.isoformat(), "trigger": decision.trigger, "why": decision.reason}
                        gate.record_call({}, situation=situation, now=now)  # counted when SENT
                        in_flight = executor.submit(advise, json.loads(json.dumps(snapshot)),
                                                    dict(reader.price_attrs), now=now)

            loads_txt = ", ".join(s.label() + (" (EV-unreliable)" if s.unreliable else "") for s in scored) or "nothing above the base load"
            if not args.quiet:
                price_txt = f"{reader.price:.2f} c {str(tier).upper()}" if reader.price is not None else "price ?"
                dark_txt = "dark" if reader.is_dark else "light"
                print(f"{now:%H:%M:%S}  A {frame.a:5.0f}  B {frame.b:5.0f}  C {frame.c:5.0f}  = {frame.total:6.0f} W"
                      f" | {price_txt} | {dark_txt} | {loads_txt}")

            snapshot["gate"] = {"decision": decision_txt, "status": gate.status(now)}
            snapshot["verdict"] = gate.cached_verdict  # live advice - the instrument voice
            while recent and now - datetime.fromisoformat(recent[-1]["ended"]) > timedelta(minutes=RECENT_MINUTES):
                recent.pop()
            snapshot["recent"] = list(recent)
            snapshot["advice"] = last_advice
            snapshot["reasoning"] = {"in_flight": in_flight is not None, "demo": bool(args.demo),
                                     **(asking if in_flight is not None else {})}
            commentary = feed.current(now)  # last night's understanding - the dry voice
            snapshot["commentary"] = commentary
            if commentary and commentary["line"] != last_commentary and not args.quiet:
                print(f"{now:%H:%M:%S}  INSIGHT  {commentary['line']}  ({commentary['index']}/{commentary['count']})")
            last_commentary = commentary["line"] if commentary else None

            # Tier 2: once a night, the deep pass - in the background, same gate
            if not args.no_nightly and now.time() >= nightly_at and nightly_last() != now.date().isoformat() \
                    and (nightly_thread is None or not nightly_thread.is_alive()):
                nightly_thread = threading.Thread(target=run_nightly_in_background, args=(now,),
                                                  name="nightly", daemon=True)
                nightly_thread.start()
            _atomic_json(args.state_out, snapshot)

        if args.once or (deadline and time.monotonic() >= deadline):
            break
        time.sleep(max(0.0, interval - (time.monotonic() - started)))

    if in_flight is not None:
        print("waiting for the advice call in flight...")
        harvest(block=True)
    executor.shutdown(wait=True)
    print(f"\nstopped after {ticks} ticks. base load {tracker.current.describe()}; "
          f"{len(detector.events)} events this run -> {EVENTS_OUT}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--minutes", type=float, default=0, help="stop after this long (default: run forever)")
    parser.add_argument("--interval", type=float, default=0, help="override HA_POLL_INTERVAL_S")
    parser.add_argument("--quiet", action="store_true", help="only print events, base-load changes and gate triggers")
    parser.add_argument("--dry", action="store_true", help="never call Nemotron; log when it would")
    parser.add_argument("--demo", action="store_true",
                        help="demo mode: a fresh Nemotron line every 5 min, 60 calls/h, same daily cap")
    parser.add_argument("--state-out", default=STATE_OUT,
                        help=f"where to write the console snapshot (default {STATE_OUT}; use another path "
                             "for a test run next to a running loop)")
    parser.add_argument("--no-nightly", action="store_true",
                        help="don't run the nightly deep pass (default: once a night at NIGHTLY_AT, 03:30)")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nstopped (Ctrl+C)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
