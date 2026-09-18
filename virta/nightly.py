"""Tier 2 NIGHTLY - understand the household, once, while nothing competes (spec 6A, 12/7b).

Run on demand (the live loop also runs it once a night):
    python -m virta.nightly                 # discovery + insights, up to two calls
    python -m virta.nightly --no-llm        # build and show the digest only, no cloud
    python -m virta.nightly --show-digest   # print exactly what leaves the house

This is where "cheap sensor, expensive brain" (spec 0) happens. A detector knows
what is on NOW. It has no memory and no notion of "usual", so it can never say
"your always-on base load costs more than everything else combined" or "the EV
charged in the most expensive hours of the day". This pass can, because it
reasons over the whole event history with world knowledge and the real prices.

Pipeline:
  1. Pull the recorder window from HA (power + the Nordpool price history).
  1b. Archive it locally first (archive.py) - HA purges after 14 days, we don't.
  2. Replay it through the same detector/baseline -> events + daily base loads.
  3. Discovery (spec 5A): the labelling pass - clusters, answers, proposals.
  4. The DIGEST, computed at the edge: energy split (base load / identified /
     unknown / other), what each run cost at the real prices while it ran, what
     the same run would have cost in that day's cheapest real window, base-load
     drift, habits by part of day, how long since each load last ran, discovery
     status, and one row per archived day (kept forever).
     Numbers only - no raw stream (spec 11). The digest is saved to
     var/digest.json so it is always inspectable what was sent.
  5. ONE deep Nemotron call -> insights in the dry register (spec 6A voices).
  6. Validate at the edge, cache to var/insights.json. The console serves them
     all day for free (Tier 3).

The guardrail is enforced in code, not only asked for: comment on the METER,
never the PERSON. Insights that talk about people - presence, sleep, guests,
health - are dropped before they reach the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from .advisor import read_usage_profile
from .baseline import BaselineTracker
from .config import ConfigError, load_cost_config, load_ha_config, load_nebius_config
from .cost_control import CloudCallGate
from .detector import Event, StackingDetector
from .ev_signature import rough_baseline
from .fsutil import atomic_write_text
from .guardrails import about_a_person, known_numbers, ungrounded_numbers
from . import archive
from .ha_client import HAClient, HAError
from .history import Frame, align_phases
from .nebius_client import NebiusError, chat, extract_json
from .prices import PriceCurve
from .profiles import load_profiles
from .scoring import part_of_day

INSIGHTS_OUT = "var/insights.json"
DIGEST_OUT = "var/digest.json"
EVENTS_OUT = "var/events.jsonl"
WINDOW_DAYS = 14  # detailed window; the archive (and history_by_day) reaches further back
DAILY_HISTORY = Path("data/archive/daily.json")  # one row per day, kept forever
MAX_GAP_S = 600.0  # longer gaps are not integrated as held power
NIGHTLY_MAX_TOKENS = 16000  # latency does not matter at night; truncation does
MAX_INSIGHTS = 6
LINE_MAX = 72
KINDS = ("behavior", "efficiency", "anomaly", "carbon", "discovery")

SYSTEM_PROMPT = """\
You are the reflective voice of a home energy console in Finland. Once a night \
you read a DIGEST of the household's electricity use - computed from a single \
cheap 3-phase meter plus the real Nordpool prices - and write short insights \
that a load detector alone could never produce.

Kinds of insight (use the ones the evidence supports; skip the rest):
- behavior: how devices are used - habits by time of day, what runs together, \
changes in the base load.
- efficiency: where money goes and what would cost less - use appliance physics \
you know (e.g. kettle vs microwave, standby loads) and the real prices given.
- anomaly: something missing, new, or odd about a DEVICE - a routine load that \
stopped, a base load that jumped.
The BASE LOAD is the static always-on load (ventilation, fridge, freezer, \
servers, cryptominers - about 1 kW), measured as one bundle. It is NOT floor \
heating; floor heating is a separate, minor appliance.
- carbon: only as a factual side note - in the Nordic grid, cheap hours often \
coincide with high wind output. Say "often", never invent numbers.
- discovery: a recurring unknown load and what it might be.
- base_parts_switched_off lists parts of the base load that were turned OFF for a while (negative watts: a PC, a server, ventilation stepping down) and what that saved at real prices - evidence of which always-on devices can be switched off, and what keeping them off would save.

Voice: dry, observant, a little HAL. Precise about the device, never preachy, \
never cute. Example lines: "YOU BOILED THE KETTLE 47 TIMES THIS FORTNIGHT. A \
MICROWAVE SAVES ~5 EUR/MO." / "THE CAR HAS NOT CHARGED IN THREE WEEKS. SOLD?" / \
"THE BASE LOAD ROSE 200 W ON THE 3RD AND STAYED. SOMETHING NEW IS ALWAYS ON."

Hard rules:
- Personal is fine, creepy is not. You may guess playfully what the household \
is up to FROM A DEVICE ("THE EV CHARGED AT 08:51. SOMEONE HAD PLACES TO BE."). \
Never anything about health, mood, relationships, visitors, sleep or the \
bathroom, and never say or imply the house is empty.
- Only suggest moving a load in time if the digest marks it shiftable: true. \
Loads marked shiftable: false are used on demand (lights, kettle, toaster) - \
never suggest running them at another time.
- An efficiency insight must name the money in EUR per month (use the \
per-month figures given) and must be worth at least 0.50 EUR/month. Skip \
anything smaller - never nag about trivial savings.
- An anomaly about something NOT happening needs an established routine in the \
digest: at least 3 earlier runs at a regular interval. With less history, do \
not claim anything stopped or is overdue.
- A deadline such as "charged by 07:00" says when a charge must FINISH; it does \
not mean the device must run every day.
- Every number must come from the digest or be simple arithmetic on it. The \
detailed window covers window.days days; history_by_day holds one row per day \
for every day ever archived - use it for longer patterns, and never claim a \
trend longer than the days it actually contains. Say so when data is thin.
- Every price and cost in the digest is real: each run is costed slot by slot \
at the Nordpool price in force while it ran, and "cheapest same-length window" \
is a real window on that day's real curve. Per-month figures are the only \
estimates (scaled from the window) - label them "EST" or "~".
- Prices are c/kWh incl. VAT; costs in EUR are given where computed.
- At most 6 insights, strongest first. Fewer good ones beat many weak ones.

Answer ONLY with JSON:
{"insights": [{"kind": "behavior|efficiency|anomaly|carbon|discovery", \
"line": "console line, UPPERCASE, max 70 characters", "detail": "one or two \
plain sentences", "evidence": "the digest numbers it rests on", "confidence": 0.0}]}"""


# --- 1-2. window + replay ---------------------------------------------------
def fetch_window(client: HAClient, days: float, *, verbose: bool = False) -> tuple[list[Frame], list[tuple[datetime, float]]]:
    """Sync the local archive from HA, then read the window FROM THE ARCHIVE.

    The archive outlives HA's 14-day purge, so the window can be longer than
    the recorder's. Prices are read from one extra day before the window so the
    price in force at the window's first minute is known, not guessed.
    """
    cfg = client.config
    archive.sync(client, verbose=verbose)
    end = datetime.now().astimezone()
    start = end - timedelta(days=days)
    series, prices = archive.load(start.date() - timedelta(days=1), end.date(), cfg.power_entities)
    frames = align_phases(series, cfg.entity("SENSOR_TOTAL"),
                          (cfg.entity("SENSOR_PHASE_A"), cfg.entity("SENSOR_PHASE_B"), cfg.entity("SENSOR_PHASE_C")))
    return [f for f in frames if f.when >= start], prices


def replay(frames: list[Frame], profiles: list) -> tuple[list[Event], list]:
    seed = rough_baseline([f for f in frames if f.when <= frames[0].when + timedelta(hours=6)])
    tracker = BaselineTracker(seed)
    detector = StackingDetector(profiles, seed)
    for frame in frames:
        detector.update(frame)
        floor = tracker.observe(detector.calibration_frame(frame), stack_empty=detector.calibration_ready)
        if floor:
            detector.set_baseline(floor)
    tracker.flush()
    return detector.events, tracker.updates


# --- 4. the digest ----------------------------------------------------------
def _runs(events: list[Event], window_end: datetime) -> list[dict]:
    """Pair ON with OFF by load key -> one run per switch-on."""
    open_runs: dict[int, dict] = {}
    runs = []
    for e in events:
        if e.kind in ("ON", "BASE_OFF", "RELABEL") and e.load_key:
            open_runs[e.load_key] = {"id": e.load_id, "name": e.display_name, "state": e.state,
                                     "phases": e.phases, "watts": sum(e.delta_w.values()), "start": e.when}
        elif e.kind in ("OFF", "OFF_RECONCILED", "BASE_ON", "BASE_SHIFT") and e.load_key in open_runs:
            run = open_runs.pop(e.load_key)
            run["end"] = e.when
            runs.append(run)
    for run in open_runs.values():  # still on at the end of the window
        run["end"] = window_end
        run["still_on"] = True
        runs.append(run)
    return sorted(runs, key=lambda r: r["start"])


def build_digest(frames: list[Frame], events: list[Event], floor_updates: list,
                 prices: list[tuple[datetime, float]], profiles: list, registry: dict) -> dict:
    start, end = frames[0].when, frames[-1].when
    days = (end - start).total_seconds() / 86400
    curve = PriceCurve(prices)
    price_at = curve.at
    names = {p.id: p.display_name for p in profiles}

    # whole-house energy and what it actually cost, at the price of the moment
    total_kwh = cost_eur = 0.0
    daily: dict[str, dict] = defaultdict(lambda: {"kwh": 0.0, "eur": 0.0})
    for a, b in zip(frames, frames[1:]):
        dt = min((b.when - a.when).total_seconds(), MAX_GAP_S)
        kwh = a.total * dt / 3.6e6
        total_kwh += kwh
        day = daily[a.when.date().isoformat()]
        day["kwh"] += kwh
        p = price_at(a.when)
        if p is not None:
            cost_eur += kwh * p / 100
            day["eur"] += kwh * p / 100

    # the floor, day by day (quiet stretches, spec 5.1)
    per_day: dict[str, int] = {}
    for _, floor, stretch in floor_updates:  # the day's last (best) quiet-stretch floor
        per_day[stretch.start.date().isoformat()] = round(floor.total)
    floors = sorted(per_day.items())
    floor_now = floor_updates[-1][1].total if floor_updates else rough_baseline(frames).total
    window_prices = [v for t, v in prices if start <= t <= end] or [v for _, v in prices]
    avg_price = sum(window_prices) / len(window_prices) if window_prices else None
    floor_kwh = floor_now * days * 24 / 1000

    shiftable = {p.id: p.raw.get("shiftable") for p in profiles}
    runs = _runs(events, end)
    appliances: dict[str, dict] = {}
    unknown_kwh = 0.0
    base_parts: dict[str, dict] = {}
    for run in runs:
        minutes = (run["end"] - run["start"]).total_seconds() / 60
        kwh = run["watts"] * minutes / 60 / 1000
        if run["watts"] < 0:
            # part of the base load switched OFF: energy NOT used, valued at real prices
            key = run["id"] if run["state"] != "UNKNOWN" else f"{run['phases']}{round(run['watts'] / 10) * 10:+.0f}W"
            part = base_parts.setdefault(key, {"name": names.get(run["id"], "unknown base part"),
                                               "phase": run["phases"], "watts": [], "times_off": 0,
                                               "hours_off": 0.0, "kwh_not_used": 0.0, "eur_not_spent": 0.0,
                                               "off_at": [], "still_off": False})
            part["watts"].append(run["watts"])
            part["times_off"] += 1
            part["hours_off"] += minutes / 60
            part["kwh_not_used"] += -kwh
            saved = curve.cost_eur(run["start"], run["end"], -run["watts"])
            part["eur_not_spent"] += saved or 0.0
            part["off_at"].append(f"{run['start']:%a %H:%M}")
            part["still_off"] = part["still_off"] or bool(run.get("still_on"))
            continue
        if run["state"] == "UNKNOWN":
            unknown_kwh += kwh
            continue
        a = appliances.setdefault(run["id"], {
            "name": names.get(run["id"], run["name"]), "runs": 0, "minutes": 0.0, "kwh": 0.0,
            "cost_eur": 0.0, "cost_at_cheapest_same_day_eur": 0.0, "alternatives": [],
            "part_of_day": Counter(), "weekdays": Counter(), "durations": [], "phases": run["phases"],
            "typical_w": [], "starts": [],
        })
        # the run's real cost: every slot it spanned, at that slot's price
        paid = curve.cost_eur(run["start"], run["end"], run["watts"])
        a["runs"] += 1
        a["minutes"] += minutes
        a["kwh"] += kwh
        if paid is not None:
            a["cost_eur"] += paid
            # the same run, same length, placed in the cheapest REAL window of its day
            d0 = datetime.combine(run["start"].date(), datetime.min.time(), tzinfo=run["start"].tzinfo)
            alt = curve.cheapest_window(d0, d0 + timedelta(days=1), run["end"] - run["start"], run["watts"])
            cheapest = alt[1] if alt else paid
            a["cost_at_cheapest_same_day_eur"] += cheapest
            if alt and shiftable.get(run["id"]):
                a["alternatives"].append({
                    "ran": f"{run['start']:%a %d.%m %H:%M}-{run['end']:%H:%M}",
                    "paid_eur": round(paid, 3),
                    "cheapest_same_length_window": f"{alt[0]:%H:%M}-{alt[0] + (run['end'] - run['start']):%H:%M}",
                    "would_have_cost_eur": round(alt[1], 3),
                })
        a["part_of_day"][part_of_day(run["start"])] += 1
        a["weekdays"][run["start"].strftime("%a")] += 1
        a["durations"].append(minutes)
        a["typical_w"].append(run["watts"])
        a["starts"].append(run["start"])

    # one compact row per day - merged into the long-term daily history
    day_loads: dict[str, Counter] = defaultdict(Counter)
    for run in runs:
        kwh = run["watts"] * (run["end"] - run["start"]).total_seconds() / 3.6e6
        day_loads[run["start"].date().isoformat()]["unknown" if run["state"] == "UNKNOWN" else run["id"]] += kwh
    base_by_day = dict(floors)
    history_rows = {}
    for d, v in sorted(daily.items()):
        partial = (d == start.date().isoformat() and start.time() > datetime.min.time().replace(minute=10)) \
            or d == end.date().isoformat()
        history_rows[d] = {
            "kwh": round(v["kwh"], 1),
            "eur": round(v["eur"], 2),
            "avg_price_paid_c_kwh": round(v["eur"] * 100 / v["kwh"], 2) if v["kwh"] else None,
            "base_load_w": base_by_day.get(d),
            "loads_kwh": {k: round(x, 2) for k, x in sorted(day_loads[d].items())},
            "partial_day": partial,
        }

    identified_kwh = sum(a["kwh"] for a in appliances.values())
    per_month = 30 / max(days, 0.01)  # scale the window to a month, stated as such
    appliance_out = {}
    for aid, a in appliances.items():
        last = max(a["starts"])
        can_shift = shiftable.get(aid)
        appliance_out[aid] = {
            "shiftable": can_shift,  # from the profile: False = used on demand, never suggest moving it
            "cost_per_month_eur": round(a["cost_eur"] * per_month, 2),
            "saving_per_month_if_run_at_cheapest_eur": (
                round((a["cost_eur"] - a["cost_at_cheapest_same_day_eur"]) * per_month, 2) if can_shift else None
            ),
            "name": a["name"],
            "phase": a["phases"],
            "typical_w": round(median(a["typical_w"])),
            "runs": a["runs"],
            "runs_per_day": round(a["runs"] / max(days, 0.01), 2),
            "median_run_min": round(median(a["durations"]), 1),
            "total_kwh": round(a["kwh"], 2),
            "cost_eur": round(a["cost_eur"], 3),
            "cost_if_each_run_in_cheapest_same_length_window_eur": round(a["cost_at_cheapest_same_day_eur"], 3),
            "runs_vs_cheapest_window": a["alternatives"][-5:] if a["alternatives"] else None,
            "avg_price_paid_c_kwh": round(a["cost_eur"] * 100 / a["kwh"], 2) if a["kwh"] else None,
            "starts_by_part_of_day": dict(a["part_of_day"]),
            "starts_by_weekday": dict(a["weekdays"]),
            "last_run": last.strftime("%a %d.%m %H:%M"),
            "days_since_last_run": round((end - last).total_seconds() / 86400, 1),
        }

    known_ids = {p.id: p for p in profiles}
    never_seen = sorted(
        pid for pid, p in known_ids.items()
        if pid not in appliances and p.matchable
    )
    not_observable = sorted(pid for pid, p in known_ids.items() if not p.matchable)

    discovery = []
    for cid, entry in (registry.get("clusters") or {}).items():
        s = entry.get("summary", {})
        discovery.append({
            "id": cid, "watts": s.get("watts"), "phase": s.get("phases"),
            "run_min": s.get("duration_min"), "occurrences": s.get("occurrences"),
            "time_of_day": s.get("time_of_day"), "pulsing": s.get("pulsing"),
            "status": entry.get("status"), "llm_guess": entry.get("llm_guess"),
            "named_as": Path(entry["profile"]).stem if entry.get("profile") else None,
        })

    by_part: dict[str, list[float]] = defaultdict(list)
    for t, v in prices:
        if start <= t <= end:
            by_part[part_of_day(t)].append(v)

    other_kwh = total_kwh - floor_kwh - identified_kwh - unknown_kwh
    return {
        "window": {"from": start.strftime("%a %d.%m %H:%M"), "to": end.strftime("%a %d.%m %H:%M"),
                   "days": round(days, 1),
                   "note": (f"only {days:.1f} days of history exist so far - treat patterns as early"
                            if days < 7 else "")},
        "energy": {
            "total_kwh": round(total_kwh, 1),
            "base_load_kwh": round(floor_kwh, 1),
            "identified_loads_kwh": round(identified_kwh, 1),
            "unknown_loads_kwh": round(unknown_kwh, 1),
            "other_small_or_unseen_kwh": round(max(other_kwh, 0.0), 1),
            "base_load_share_pct": round(100 * floor_kwh / total_kwh) if total_kwh else None,
        },
        "cost": {
            "total_eur": round(cost_eur, 2),
            "per_day_eur": round(cost_eur / max(days, 0.01), 2),
            "base_load_per_day_eur": round(floor_now * 24 / 1000 * avg_price / 100, 2) if avg_price else None,
            "base_load_per_month_eur": round(floor_now * 24 * 30 / 1000 * avg_price / 100, 2) if avg_price else None,
            "per_month_eur_at_this_rate": round(cost_eur * per_month, 2),
            "monthly_figures_note": f"scaled from {days:.1f} days at this week's prices - an estimate",
            "avg_price_paid_c_kwh": round(cost_eur * 100 / total_kwh, 2) if total_kwh else None,
            "avg_market_price_c_kwh": round(avg_price, 2) if avg_price is not None else None,
        },
        "base_load": {
            "now_w": round(floor_now),
            "by_day_w": floors,
            "change_w": (floors[-1][1] - floors[0][1]) if len(floors) > 1 else 0,
            "what_it_is": "static always-on load: ventilation, fridge, freezer, servers, cryptominers - about 1 kW, bundled, not itemised",
            "not": "NOT floor heating - floor heating is a separate, minor appliance",
        },
        "prices": {
            "min_c_kwh": round(min(window_prices), 2) if window_prices else None,
            "max_c_kwh": round(max(window_prices), 2) if window_prices else None,
            "negative_slots": sum(1 for v in window_prices if v < 0),
            "avg_by_part_of_day_c_kwh": {k: round(sum(v) / len(v), 2) for k, v in by_part.items()},
        },
        "appliances": appliance_out,
        "base_parts_switched_off": {
            k: {"name": v["name"], "phase": v["phase"], "watts": round(median(v["watts"])),
                "times_off": v["times_off"], "hours_off": round(v["hours_off"], 1),
                "kwh_not_used": round(v["kwh_not_used"], 2), "eur_not_spent": round(v["eur_not_spent"], 3),
                "switched_off_at": v["off_at"][-6:], "off_right_now": v["still_off"]}
            for k, v in base_parts.items()
        },
        "known_but_not_seen_in_window": never_seen,
        "not_observable_by_the_meter": not_observable,
        "discovery": discovery,
        "history_by_day": history_rows,  # replaced in run_nightly by the full archive history
        "usage_patterns": read_usage_profile() or "not provided",
        "detection_limit_note": "steps under ~120 W are not individually visible; the base load is measured as one bundle",
    }


# --- 5-6. the deep call + edge validation -----------------------------------
def thin_routine(line: str, digest: dict) -> bool:
    """True if the line is about an appliance seen fewer than 3 times.

    "It stopped" / "it's overdue" needs a routine to break. The prompt says so,
    and Ultra still wrote "EV NOT RUN SINCE WED" after ONE observed charge - so
    it is enforced here too.
    """
    upper = line.upper()
    for aid, a in (digest.get("appliances") or {}).items():
        words = {aid.upper(), aid.replace("_", " ").upper(), str(a.get("name", "")).upper()}
        words |= {str(a.get("name", "")).split(" ")[0].upper()}  # "EV" from "EV charger (Eve)"
        if any(w and len(w) >= 2 and re.search(rf"\b{re.escape(w)}\b", upper) for w in words):
            if (a.get("runs") or 0) < 3:
                return True
    return False


def validate(raw: Any, digest: dict | None = None) -> tuple[list[dict], list[str]]:
    """Keep only well-formed insights about devices whose console numbers are real.

    The console line is what people read and act on, so every number on it must
    trace to the digest. The detail sentence is the model's explanation - kept
    in the file, labelled as such, but not shown as fact.
    """
    grounded = known_numbers(digest) if digest is not None else None
    kept, dropped, seen = [], [], set()
    items = raw.get("insights") if isinstance(raw, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        line = " ".join(str(item.get("line", "")).split()).upper()
        detail = " ".join(str(item.get("detail", "")).split())
        evidence = " ".join(str(item.get("evidence", "")).split())
        kind = str(item.get("kind", "")).lower()
        if not line or not evidence:
            dropped.append(f"no line or no evidence: {line[:50]!r}")
            continue
        if kind not in KINDS:
            kind = "behavior"
        if about_a_person(line, detail):
            dropped.append(f"about a person, not the meter: {line[:60]!r}")
            continue
        if kind == "anomaly" and digest is not None and thin_routine(line, digest):
            dropped.append(f"anomaly about a device with under 3 runs - no routine to break: {line[:60]!r}")
            continue
        if grounded is not None:
            missing = ungrounded_numbers(line, grounded)
            if missing:
                dropped.append(f"number(s) {', '.join(missing)} not in the digest: {line[:60]!r}")
                continue
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX].rsplit(" ", 1)[0].rstrip(",;:-") + "."
        if line in seen:
            continue
        seen.add(line)
        conf = item.get("confidence")
        kept.append({"kind": kind, "line": line, "model_explanation": detail, "evidence": evidence,
                     "confidence": float(conf) if isinstance(conf, (int, float)) else 0.5})
    return kept[:MAX_INSIGHTS], dropped


def ask_for_insights(digest: dict, gate: CloudCallGate) -> tuple[list[dict], str, list[str]]:
    decision = gate.allow_housekeeping("nightly insights")
    if not decision.should_call:
        return [], f"skipped: {decision.reason}", []
    try:
        config = load_nebius_config()
    except ConfigError as exc:
        return [], f"skipped: {exc.args[0].splitlines()[0]}", []
    gate.record_call({})  # counted when SENT (fail closed)
    try:
        result = chat(config, SYSTEM_PROMPT, json.dumps(digest, ensure_ascii=False), max_tokens=NIGHTLY_MAX_TOKENS,
                      model=config.model_nightly)  # Tier 2: the deepest, once a night
    except NebiusError as exc:
        return [], f"failed: {str(exc).splitlines()[0]}", []
    gate.add_usage(result.usage)
    atomic_write_text("var/last_nightly_reply.txt",
                      f"finish_reason: {result.finish_reason}\nusage: {result.usage}\n\n--- content ---\n"
                      f"{result.content}\n\n--- reasoning_content ---\n{result.reasoning_content}\n")
    if result.truncated:
        return [], "reply cut off at max_tokens (raw reply in var/last_nightly_reply.txt)", []
    insights, dropped = validate(extract_json(result), digest)
    tokens = result.usage.get("completion_tokens", "?")
    return insights, f"one call, {tokens} completion tokens, answer from {result.source}", dropped


def run_nightly(*, use_llm: bool = True, days: float = WINDOW_DAYS, gate: CloudCallGate | None = None,
                discovery: bool = True, verbose: bool = True, show_digest: bool = False) -> int:
    say = print if verbose else (lambda *a, **k: None)
    try:
        ha_cfg = load_ha_config()
        cost_cfg = load_cost_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2
    gate = gate or CloudCallGate(cost_cfg)
    profiles = load_profiles("profiles")

    try:
        frames, prices = fetch_window(HAClient(ha_cfg, timeout_s=120), days, verbose=verbose)
    except HAError as exc:
        print(f"HA ERROR\n{exc}", file=sys.stderr)
        return 1
    if len(frames) < 100:
        say("NIGHTLY    not enough history yet")
        return 1
    events, floor_updates = replay(frames, profiles)
    atomic_write_text(EVENTS_OUT, "".join(json.dumps(e.to_json()) + "\n" for e in events))
    say(f"NIGHTLY    {frames[0].when:%a %d.%m %H:%M} -> {frames[-1].when:%a %d.%m %H:%M}: "
        f"{len(frames)} readings, {len(events)} events, {len(prices)} price points")

    if discovery:  # spec 5A is part of the nightly job list (6A)
        from . import labeller

        say("DISCOVERY  (the labelling pass)")
        labeller.run(use_llm=use_llm, verbose=verbose, gate=gate)

    from .labeller import load_registry

    digest = build_digest(frames, events, floor_updates, prices, profiles, load_registry())
    # long-term memory: complete days are kept forever, the window's days refreshed
    history = json.loads(DAILY_HISTORY.read_text(encoding="utf-8")) if DAILY_HISTORY.is_file() else {}
    for day, row in digest["history_by_day"].items():
        if not row["partial_day"] or day not in history:
            history[day] = row
    atomic_write_text(DAILY_HISTORY, json.dumps(dict(sorted(history.items())), indent=1))
    digest["history_by_day"] = dict(sorted(history.items()))
    digest["window"]["history_days_archived"] = len(history)
    atomic_write_text(DIGEST_OUT, json.dumps(digest, indent=1, ensure_ascii=False))
    e, c = digest["energy"], digest["cost"]
    say(f"DIGEST     {e['total_kwh']} kWh / {c['total_eur']} EUR over {digest['window']['days']} d; "
        f"base load {e['base_load_kwh']} kWh ({e['base_load_share_pct']}%), identified {e['identified_loads_kwh']}, "
        f"unknown {e['unknown_loads_kwh']}  -> {DIGEST_OUT}")
    if show_digest:
        print(json.dumps(digest, indent=1, ensure_ascii=False))

    if not use_llm:
        say("INSIGHTS   --no-llm: digest built, no call made")
        return 0
    insights, status, dropped = ask_for_insights(digest, gate)
    say(f"INSIGHTS   {status}")
    for reason in dropped:
        say(f"           dropped: {reason}")
    if not insights:
        say("           keeping the previous insights (if any)")
        return 1
    atomic_write_text(INSIGHTS_OUT, json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": digest["window"],
        "insights": insights,
    }, indent=1, ensure_ascii=False))
    for i in insights:
        say(f"  [{i['kind']:10s}] {i['line']}")
        say(f"               (model) {i["model_explanation"]}")
    say(f"           -> {INSIGHTS_OUT}  ({gate.status()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-llm", action="store_true", help="build the digest, no cloud calls")
    parser.add_argument("--no-discovery", action="store_true", help="skip the labelling pass")
    parser.add_argument("--days", type=float, default=WINDOW_DAYS)
    parser.add_argument("--show-digest", action="store_true", help="print exactly what is sent")
    args = parser.parse_args(argv)
    return run_nightly(use_llm=not args.no_llm, days=args.days, discovery=not args.no_discovery,
                       show_digest=args.show_digest)


if __name__ == "__main__":
    raise SystemExit(main())
