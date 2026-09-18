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

from .actions import Controls, Lamp, Voice, mood_for
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
from .rhythm import strong_rhythms
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
RHYTHM_EVERY_S = 300  # rhythms re-measured every 5 min...
RHYTHM_WINDOW_H = 2  # ...over the last 2 h of full-resolution history


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
        self.price_override: float | None = None
        self.curve = PriceCurve([])  # the real slots, for costing sessions as they run
        self.tomorrow_valid = False
        self.is_dark: bool | None = None
        self.outdoor: float | None = None
        self.indoor: float | None = None
        self.linked: dict[str, str] = {}  # HA state of devices profiles link to (cross-check)
        self.linked_ids: list[str] = []

    def refresh_linked(self) -> None:
        """Every tick: HA's own on/off for linked devices - fresh enough to confirm a
        switch-on the moment the detector reports it (a 60 s cache would lag)."""
        for entity_id in self.linked_ids:
            try:
                self.linked[entity_id] = self.client.get_state(entity_id).state
            except HAError:
                self.linked.pop(entity_id, None)

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
        self.price_override = demo_price()
        if self.price_override is not None:  # demo scene: a price spike on cue, clearly flagged
            self.price = self.price_override
            self.price_attrs = override_current_slot(attrs, self.price_override)
        self.curve = PriceCurve.from_nordpool(self.price_attrs)
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


def name_rhythms(summaries: list[dict], profiles: list) -> list[dict]:
    """Give a rhythm its name if a profile describes it (signature type "rhythm")."""
    named = [p for p in profiles if (p.raw.get("signature") or {}).get("type") == "rhythm"]
    for r in summaries:
        for p in named:
            sig = p.raw["signature"]
            same_kind = sig.get("kind") == r["kind"] and sig.get("phase") == r["phase"]
            size_ok = abs(r["amplitude_w"] - sig.get("amplitude_w", 0)) <= 0.3 * max(sig.get("amplitude_w", 1), 1)
            if same_kind and size_ok:
                r["name"] = p.display_name
                r["profile"] = p.id
                break
    return summaries


DEMO_PRICE_FILE = Path("var/demo_price")


def demo_price() -> float | None:
    """A demo price override in c/kWh, read from var/demo_price (empty/absent = off).
    For filming the action scene on cue; the console flags it in amber - never silent."""
    try:
        text = DEMO_PRICE_FILE.read_text(encoding="utf-8").strip()
        return float(text) if text else None
    except (OSError, ValueError):
        return None


DEMO_PRICE_MINUTES = 60  # a real price spike lasts; one expensive slot followed by a cheap one
                         # just teaches the model to wait it out (seen live 18.09, 14:08)


def override_current_slot(attrs: dict, price: float) -> dict:
    """The Nordpool attributes with the current slot and the next hour replaced by the demo price."""
    now = datetime.now().astimezone()
    until = now + timedelta(minutes=DEMO_PRICE_MINUTES)
    out = dict(attrs)
    for key in ("raw_today", "raw_tomorrow"):
        slots = []
        for slot in attrs.get(key) or []:
            start, end = datetime.fromisoformat(str(slot["start"])), datetime.fromisoformat(str(slot["end"]))
            slots.append({**slot, "value": price} if start < until and end > now else slot)
        out[key] = slots
    return out


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
    # ACT (actions.py): voice, the Virta lamp, and suggest-and-confirm control.
    # Each is opt-in from .env; unset means Virta acts on nothing.
    voice, lamp, controls = Voice(client), Lamp(client), Controls(client)
    reader.linked_ids = sorted({p.raw["ha_entity"] for p in profiles if p.raw.get("ha_entity")} | set(controls.allow))
    controllable: list[dict] = []
    last_action: dict = {}
    print(f"  act      voice {voice.player or 'off'} | lamp {lamp.entity or 'off'} | "
          f"control {', '.join(controls.allow) or 'off'} | HA-linked {', '.join(reader.linked_ids) or 'none'}")
    # One worker: at most one advice call in flight. A reasoning call takes 10-60 s;
    # the 15 s local loop must never wait for it.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nemotron")
    in_flight: Future | None = None
    asking: dict = {}  # what the call in flight was asked about - shown on the console while it thinks
    last_advice: dict = {}
    last_state: dict = {}
    expensive_since: datetime | None = None  # the house rule's clock
    rule_cooldown: dict[str, datetime] = {}  # entity -> last answered/acted; a "no" is respected

    feed = InsightFeed()
    trace: deque = deque()
    recent: deque = deque(maxlen=RECENT_MAX)  # finished sessions, with their final real cost
    rhythms: dict = {"list": [], "at": None}  # small cyclic loads, found by pattern (rhythm.py)
    rhythm_thread: threading.Thread | None = None
    rhythm_due = 0.0

    def measure_rhythms() -> None:
        """Full-resolution history: a 2 s dip is invisible to 15 s polling but not to HA."""
        try:
            since = datetime.now().astimezone() - timedelta(hours=RHYTHM_WINDOW_H)
            series, _ = fetch_history(client, list(reader.phase_ids), since)
            by_phase = dict(zip("ABC", (series.get(eid, []) for eid in reader.phase_ids)))
            found = [r.summary() for r in strong_rhythms(by_phase)]
            rhythms["list"] = name_rhythms(found, profiles)
            rhythms["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        except Exception as exc:  # a rhythm hiccup must never touch the live loop
            print(f"{datetime.now():%H:%M:%S}  RHYTHM   measurement skipped: {exc!r}")
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

    controllable_at = [0.0]

    def act_on(advice: Advice, stamp: datetime) -> None:
        """Turn Nemotron's answer into action: lamp mood, voice, proposal."""
        action = advice.action or {}
        unusual = any(l.get("unusual") for l in (last_state.get("loads") or []))
        tier = (last_state.get("price") or {}).get("tier")
        result = lamp.show(mood_for({"action": action}, tier, unusual), why=action.get("reason", ""))
        if result.startswith(("lamp", "failed")):
            print(f"{stamp:%H:%M:%S}  ACT    {result}")
        proposal = controls.propose(action.get("ha_action"), why=action.get("reason", ""))
        if proposal and proposal["policy"] == "auto":
            swap = f" and {proposal['swap_name']} on" if proposal.get("swap_name") else ""
            print(f"{stamp:%H:%M:%S}  ACT    AUTO in {controls.grace_s:g}s: {proposal['name']} off{swap}")
            voice.say(f"{advice.verdict}. Switching {proposal['name']} off{swap}.", why="auto action", urgent=True)
        elif proposal:
            print(f"{stamp:%H:%M:%S}  ACT    PROPOSED: turn off {proposal['name']} - confirm on the console")
            voice.say(f"{advice.verdict}. Shall I turn off {proposal['name']}? Confirm on the console.",
                      why="proposal", urgent=True)
        elif action.get("voice") == "advice" or action.get("speak"):
            said = voice.say(advice.verdict, why=action.get("reason", ""))
            print(f"{stamp:%H:%M:%S}  ACT    voice: {said}")

    def house_rule(now: datetime) -> None:
        """The standing rule behind VIRTA_AUTO + VIRTA_SWAP: at an expensive or peak price,
        an auto light that is on gets swapped for its cheaper partner. Nemotron gets the
        first word - its own ha_action wins - but a light the owner asked to be managed
        this way must not depend on the model choosing to (18.09: Nano saw PEAK and
        decided to wait). Announced out loud, N cancels, a "no" holds for 30 min."""
        nonlocal expensive_since
        price = last_state.get("price") or {}
        if price.get("tier") not in ("expensive", "peak"):
            expensive_since = None
            return
        expensive_since = expensive_since or now
        if controls.pending or in_flight is not None or not controls.auto:
            return
        asked_after = last_advice.get("at") and datetime.fromisoformat(last_advice["at"]) >= expensive_since
        if not asked_after and now - expensive_since < timedelta(seconds=90):
            return  # Nemotron hasn't answered about this price yet
        for dev in controls.devices():
            entity = dev["entity_id"]
            if dev["policy"] != "auto" or dev["state"] != "on":
                continue
            if entity in rule_cooldown and now - rule_cooldown[entity] < timedelta(minutes=30):
                continue
            why = f"house rule: price {price.get('tier')} ({price.get('c_kwh')} c/kWh), {dev['name']} on"
            proposal = controls.propose({"entity_id": entity, "service": "light.turn_off"}, why=why)
            if not proposal:
                continue
            rule_cooldown[entity] = now
            swap = f" and {proposal['swap_name']} on" if proposal.get("swap_name") else ""
            tier_word = "at its peak" if price.get("tier") == "peak" else "expensive"
            c = price.get("c_kwh")
            cents = f", {c:.0f} cents" if isinstance(c, (int, float)) else ""
            print(f"{now:%H:%M:%S}  ACT    HOUSE RULE, AUTO in {controls.grace_s:g}s: {proposal['name']} off{swap}")
            lamp.show("noticed", why=why)
            voice.say(f"Electricity is {tier_word}{cents}. Switching {proposal['name']} off{swap}.",
                      why="house rule", urgent=True)
            return

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
            act_on(advice, stamp)
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
        if demo_price() != reader.price_override:  # the demo price changed: apply it now, not in 60 s
            reader.refresh_context(force=True)
        answer, done = controls.check_answer()  # the human's Y/N from the console
        if answer:
            last_action = {"at": now.isoformat(timespec="seconds"), "result": answer,
                           "name": (done or {}).get("name"), "entity_id": (done or {}).get("entity_id")}
            print(f"{now:%H:%M:%S}  ACT    proposal {answer}: {(done or {}).get('name', '')}")
            if answer == "done" and (done or {}).get("policy") == "confirm":  # auto was announced already
                voice.say(f"Done. {(done or {}).get('name', 'It')} is off.", why="confirmed action", urgent=True)
            if (done or {}).get("entity_id"):
                rule_cooldown[done["entity_id"]] = now
        house_rule(now)
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
            reader.refresh_linked()
            scored = score_state(state, context, reader.linked)

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
                          "tomorrow_valid": reader.tomorrow_valid,
                          "demo_override": reader.price_override is not None},
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
            if time.monotonic() >= rhythm_due and (rhythm_thread is None or not rhythm_thread.is_alive()):
                rhythm_due = time.monotonic() + RHYTHM_EVERY_S
                rhythm_thread = threading.Thread(target=measure_rhythms, name="rhythm", daemon=True)
                rhythm_thread.start()
            snapshot["rhythms"] = rhythms["list"]
            if controls.allow and (not controllable or reader._context_at != controllable_at[0]):
                controllable[:] = controls.devices()  # refreshed with the slow context
                controllable_at[0] = reader._context_at
            snapshot["controllable"] = controllable
            snapshot["proposal"] = controls.pending
            snapshot["last_action"] = last_action
            snapshot["lamp"] = {"mood": lamp.mood, "entity": lamp.entity} if lamp.enabled else None
            snapshot["voice_on"] = voice.enabled
            snapshot["rhythms_at"] = rhythms["at"]
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
            last_state.clear()
            last_state.update(snapshot)
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
