"""Slice 7: the reasoning call - what's running + the price curve -> advice (spec 6).

The AI centerpiece. Nemotron on Nebius Token Factory receives the situation in
language/JSON and returns one console line plus a JSON action:

    PRICE HIGH. DEFER EV -> 01:00. EST 6.1 C/KWH CHEAPER.
    {"action": "suggest_defer", "appliance": "ev_charger", "defer_until": "01:00",
     "est_saving_c_kwh": 6.1, "reason": "..."}

What goes up (spec 11 privacy line): the running appliances with confidence,
unknown loads by size and phase, the Nordpool curve, temperatures, darkness,
the usage-pattern text and the time. Never the raw power stream.

What the edge does first, so the model does not have to: the price arithmetic.
Cheapest 1 h / 3 h windows, the curve's range, negative slots and per-load
running cost now are computed locally and handed over as facts. The model's job
is the judgement - is it worth it, what about deadlines and comfort, and how
sure are we about what's running.

Output handling (spec 2): answer may be in `content` or `reasoning_content`;
no tool calling - the JSON is prompted for and parsed, validated, and the verdict
line is rebuilt from the JSON if the model forgot it. Advice only: the action is
a SUGGESTION. Nothing here ever switches anything.

Run on its own (one real call each):
    python -m virta.advisor                  # from the live loop's var/state.json
    python -m virta.advisor --scenario ev    # EV charging now, real price curve
    python -m virta.advisor --scenario kitchen
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ConfigError, CostConfig, load_cost_config, load_ha_config, load_nebius_config
from .cost_control import CloudCallGate, price_tier
from .ha_client import HAClient, HAError, parse_price_curve
from .guardrails import about_a_person, known_numbers, ungrounded_numbers
from .nebius_client import NebiusError, chat, extract_json
from .profiles import Profile, load_profiles

USAGE_PROFILE = "usage_profile.txt"
# Measured: with the observation voice the model found a good line early, then
# kept polishing it past 8000 tokens (finish_reason=length). The prompt now says
# commit to the first good line; this is the safety net. Runs in the background.
ADVICE_MAX_TOKENS = 16000
VERDICT_MAX_CHARS = 72
ACTIONS = ("none", "suggest_defer")

SYSTEM_PROMPT = """\
You are the household energy advisor inside a home energy console in Finland. \
Given the appliances currently running (detected from a 3-phase meter, each \
with a confidence), the Nordpool electricity price curve (c/kWh, VAT included, \
15-minute slots, local time; a NEGATIVE price means you are paid to consume), \
the outdoor/indoor temperature, whether it is dark, and the household's usage \
patterns, decide whether any running or imminent load should be deferred to \
save money without hurting comfort or hard deadlines (e.g. EV charged by 07:00).

Rules:
- Read the slot timestamps. Price facts (cheapest windows, range) are computed \
for you in price.facts - use them rather than re-deriving.
- Be specific and brief. Never nag about trivial savings: short loads (kettle, \
toaster, lights) are never worth deferring. If nothing is worth changing, say so.
- Detections marked unusual, low-confidence (<60%) or unreliable_ev_load_control \
may be mentioned but never be the sole basis for a defer.
- If a negative or very cheap price is NOW, say run it now - never defer into a \
more expensive slot.
- If tomorrow's prices are not published yet, only today's remaining curve is \
known; do not assume tomorrow is cheaper.
- Keep your reasoning short. Decide the action, write the FIRST good console line that is true, and stop - do not iterate on the wording.

The console line has TWO VOICES - pick by what you have to say:
- ADVICE, when a load is genuinely worth moving: the clean instrument voice, \
trustworthy, no jokes. "PRICE <TIER>. DEFER EV -> 01:00. EST 6.1 C/KWH CHEAPER." \
/ "NEGATIVE PRICE NOW. RUN LOADS." (<TIER> is a placeholder for price.tier - \
never copy it literally.)
- OBSERVATION, when nothing is worth moving (most of the time): do NOT say \
"nothing to shift". Say something true and noticing about what the meter shows \
right now, dry and observant, a little HAL - using today_so_far (what already \
ran today), usual (the house's habits from the nightly analysis), what is on, \
the base load and the price. You may be playfully personal, guessing what the \
household is up to FROM A DEVICE, like a computer scanning the room: "EV AT \
FULL TILT. DRIVING FERRARI TODAY?" The register, not text to reuse - never copy \
an example; write your own line about THIS data.
Hard rules for both voices: personal is fine, creepy is not - nothing about \
health, mood, relationships, visitors, sleep or the bathroom, and never say or \
imply the house is empty. Every number must come from the data given. Never nag.

Output exactly two things and nothing else:
Line 1: the console line, at most 60 characters, UPPERCASE.
Then a JSON object:
{"voice": "advice" or "observation", "action": "none" or "suggest_defer", \
"appliance": "<running id>" or null, "defer_until": "HH:MM" or null, \
"est_saving_c_kwh": number or null, "reason": "one sentence: why this line"}"""


# --- inputs -----------------------------------------------------------------
def read_usage_profile(path: str = USAGE_PROFILE) -> str:
    file = Path(path)
    if not file.is_file():
        return ""
    lines = [l.strip() for l in file.read_text(encoding="utf-8").splitlines()]
    return " ".join(l for l in lines if l and not l.startswith("#"))


EVENT_LOGS = ("var/events_live.jsonl", "var/events.jsonl")


def today_so_far(now: datetime) -> dict:
    """What already ran today, per load: how many times, first and last start.

    From the local event logs (the live loop's, and the nightly replay's) - so it
    survives a restart of the loop. Compact: counts and clock times only."""
    starts: list[tuple[str, str]] = []  # (HH:MM:SS, load name)
    today = now.date().isoformat()
    for path in EVENT_LOGS:
        file = Path(path)
        if not file.is_file():
            continue
        for line in file.read_text(encoding="utf-8").splitlines():
            if today not in line[:40]:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") not in ("ON", "BASE_OFF") or not e.get("when", "").startswith(today):
                continue
            name = e["load_id"] if e.get("state") == "MATCHED" else f"unknown_{e.get('phases')}"
            if e["kind"] == "BASE_OFF":
                name = f"base_part_off_{e.get('phases')}"
            starts.append((e["when"][11:19], name))

    # The live log and the nightly replay both saw the same switches, seconds
    # apart (the loop polls every 15 s; the replay sees every HA row). Starts of
    # the same load within 90 s are one run.
    def secs(hms: str) -> int:
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s

    loads: dict[str, dict] = {}
    last_start: dict[str, int] = {}
    for hms, name in sorted(starts):
        t = secs(hms)
        if name in last_start and t - last_start[name] <= 90:
            continue
        last_start[name] = t
        entry = loads.setdefault(name, {"runs": 0, "first": hms[:5], "last": hms[:5]})
        entry["runs"] += 1
        entry["last"] = hms[:5]
    return loads


def usual_habits() -> dict:
    """The house's habits, as last night's analysis measured them (var/digest.json)."""
    try:
        digest = json.loads(Path("var/digest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"note": "no nightly analysis yet"}
    habits = {
        aid: {"runs_per_day": a.get("runs_per_day"), "part_of_day": a.get("starts_by_part_of_day"),
              "median_run_min": a.get("median_run_min")}
        for aid, a in (digest.get("appliances") or {}).items()
    }
    return {
        "measured_over_days": (digest.get("window") or {}).get("days"),
        "appliances": habits,
        "base_load_w": (digest.get("base_load") or {}).get("now_w"),
        "base_load_share_of_energy_pct": (digest.get("energy") or {}).get("base_load_share_pct"),
    }


def _slot_label(when: datetime, now: datetime) -> str:
    return when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%a %H:%M")


def price_facts(slots: list[dict], now: datetime) -> dict[str, Any]:
    """Deterministic price arithmetic, done at the edge (cheap, exact, auditable)."""
    ahead = [s for s in slots if (s["end"] or s["start"] + timedelta(minutes=15)) > now]
    if not ahead:
        return {"note": "no price curve available"}
    minutes = [((s["end"] or s["start"] + timedelta(minutes=15)) - s["start"]).total_seconds() / 60 for s in ahead]
    slot_min = round(min(minutes)) or 15

    def cheapest(hours: float) -> dict | None:
        n = max(1, round(hours * 60 / slot_min))
        if len(ahead) < n:
            return None
        best = min(range(len(ahead) - n + 1), key=lambda i: sum(s["value"] for s in ahead[i:i + n]))
        window = ahead[best:best + n]
        return {"start": _slot_label(window[0]["start"], now),
                "avg_c_kwh": round(sum(s["value"] for s in window) / n, 3)}

    values = [s["value"] for s in ahead]
    negatives = [_slot_label(s["start"], now) for s in ahead if s["value"] < 0]
    return {
        "now_c_kwh": ahead[0]["value"],
        "horizon_ends": _slot_label(ahead[-1]["end"] or ahead[-1]["start"], now),
        "min_c_kwh": min(values),
        "max_c_kwh": max(values),
        "cheapest_1h": cheapest(1),
        "cheapest_3h": cheapest(3),
        "negative_slots": negatives[:12],
        "slot_minutes": slot_min,
    }


def curve_for_prompt(slots: list[dict], now: datetime) -> list[list]:
    """[["10:15", 9.8], ...] from the current slot on - compact, timestamps kept."""
    return [[_slot_label(s["start"], now), round(s["value"], 3)]
            for s in slots if (s["end"] or s["start"] + timedelta(minutes=15)) > now]


def build_payload(
    snapshot: dict,
    price_attrs: dict,
    profiles: dict[str, Profile],
    cost: CostConfig,
    *,
    now: datetime,
) -> dict:
    """The whole situation, abstracted. Built from the same snapshot the console shows."""
    slots = parse_price_curve(price_attrs, "raw_today")
    tomorrow_ok = bool(price_attrs.get("tomorrow_valid"))
    if tomorrow_ok:
        slots += parse_price_curve(price_attrs, "raw_tomorrow")
    slots.sort(key=lambda s: s["start"])
    facts = price_facts(slots, now)
    price_now = snapshot.get("price", {}).get("c_kwh")
    if price_now is None:
        price_now = facts.get("now_c_kwh")

    running, unknown = [], []
    for load in snapshot.get("loads", []):
        draw = sum((load.get("draw_w") or {}).values())
        if draw < 0:
            continue  # a base part switched off - irrelevant to what to defer now
        if load.get("state") == "UNKNOWN":
            unknown.append({"phases": load.get("phases"), "draw_w": round(draw),
                            "since": load.get("since", "")[11:16]})
            continue
        entry = {
            "id": load["id"],
            "name": load.get("name", load["id"]),
            "confidence": load.get("confidence"),
            "draw_w": round(draw),
            "since": load.get("since", "")[11:16],
            "unusual": bool(load.get("unusual")),
            "unreliable_ev_load_control": bool(load.get("unreliable")),
        }
        if price_now is not None:
            entry["cost_now_c_per_hour"] = round(draw / 1000 * price_now, 1)
        profile = profiles.get(load["id"])
        if profile:
            deadlines = [f.get("note") for f in profile.raw.get("context_factors", []) if f.get("factor") == "deadline"]
            if deadlines:
                entry["deadline"] = "; ".join(d for d in deadlines if d)
            duration = (profile.raw.get("measured") or {}).get("duration_s")
            if duration:
                entry["typical_run_min"] = round(duration / 60, 1)
        running.append(entry)

    return {
        "now": now.strftime("%a %d.%m.%Y %H:%M (local time)"),
        "running": running,
        "unknown_loads": unknown,
        "ev_charging": bool(snapshot.get("ev_active")),
        "ev_note": ("EV charger has dynamic load control: while it charges, other detections are unreliable"
                    if snapshot.get("ev_active") else None),
        "house_total_w": round((snapshot.get("power_w") or {}).get("total", 0)),
        "base_load_w": round((snapshot.get("base_load_w") or {}).get("total", 0)),
        "base_load_is": "static always-on load: ventilation, fridge, freezer, servers, cryptominers - about 1 kW, bundled, not itemised",
        "price": {
            "now_c_kwh": price_now,
            "tier": str(price_tier(price_now, cost)) if price_now is not None else None,
            "tier_bounds_c_kwh": {"cheap_below": cost.price_cheap_max_c, "normal_below": cost.price_normal_max_c,
                                  "expensive_below": cost.price_expensive_max_c},
            "tomorrow_published": tomorrow_ok,
            "facts": facts,
            "curve": curve_for_prompt(slots, now),
        },
        "context": {
            "is_dark": (snapshot.get("context") or {}).get("is_dark"),
            "outdoor_c": (snapshot.get("context") or {}).get("outdoor_c"),
            "indoor_c": (snapshot.get("context") or {}).get("indoor_c"),
        },
        "usage_patterns": read_usage_profile() or "not provided",
        "today_so_far": today_so_far(now),
        "usual": usual_habits(),
    }


# --- output -----------------------------------------------------------------
@dataclass
class Advice:
    verdict: str
    action: dict[str, Any]
    ok: bool
    status: str
    source: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"verdict": self.verdict, "action": self.action, "ok": self.ok, "status": self.status,
                "source": self.source, "warnings": self.warnings}


def _verdict_line(text: str) -> str:
    """First line that is not JSON, a code fence or empty."""
    for line in (text or "").splitlines():
        clean = line.strip().strip("`").strip()
        if not clean or clean.startswith(("{", "[", "json")):
            continue
        return clean
    return ""


# Price words a verdict may use, and the tiers each one is TRUE for.
TIER_WORDS = {
    "NEGATIVE": {"negative"}, "CHEAP": {"cheap"}, "LOW": {"negative", "cheap"},
    "NORMAL": {"normal"}, "EXPENSIVE": {"expensive"}, "HIGH": {"expensive", "peak"}, "PEAK": {"peak"},
}
TIER_PHRASE = re.compile(r"(PRICE\s+(?:IS\s+)?)(" + "|".join(TIER_WORDS) + r")\b")


def correct_tier_words(verdict: str, tier: str | None) -> tuple[str, bool]:
    """The edge knows the tier exactly; the model only paraphrases it.

    Measured: with a literal example in the prompt the model wrote "PRICE NORMAL"
    at 10.1 c/kWh (EXPENSIVE). A wrong fact on the console is worse than none, so
    any price word that is false for the real tier is replaced by the real tier.
    """
    if not tier:
        return verdict, False
    fixed = TIER_PHRASE.sub(
        lambda m: m.group(0) if tier in TIER_WORDS[m.group(2)] else f"{m.group(1)}{tier.upper()}", verdict
    )
    fixed = fixed.replace("<TIER>", tier.upper())
    return fixed, fixed != verdict


def parse_advice(result, running_ids: set[str], tier: str | None = None, payload: dict | None = None) -> Advice:
    parsed = extract_json(result)
    warnings: list[str] = []
    if not isinstance(parsed, dict) or "action" not in parsed:
        return Advice("", {}, False, "no usable JSON action in the reply", result.source, result.usage)

    action = str(parsed.get("action", "none")).strip().lower()
    if action not in ACTIONS:
        warnings.append(f"unknown action {action!r} treated as none")
        action = "none"
    appliance = parsed.get("appliance")
    if appliance is not None and str(appliance) not in running_ids:
        warnings.append(f"appliance {appliance!r} is not running - dropped")
        appliance = None
    if action == "suggest_defer" and appliance is None:
        warnings.append("defer without a running appliance - downgraded to none")
        action = "none"
    defer_until = parsed.get("defer_until")
    if defer_until is not None and not re.fullmatch(r"\d{1,2}:\d{2}", str(defer_until)):
        warnings.append(f"defer_until {defer_until!r} is not HH:MM - dropped")
        defer_until = None
    saving = parsed.get("est_saving_c_kwh")
    if not isinstance(saving, (int, float)):
        saving = None
    voice = "advice" if action == "suggest_defer" else "observation"
    clean = {"voice": voice, "action": action, "appliance": appliance, "defer_until": defer_until,
             "est_saving_c_kwh": saving, "reason": str(parsed.get("reason", ""))[:300]}

    verdict = _verdict_line(result.content) if result.source == "content" else ""
    if not verdict:  # rebuild from the JSON rather than show nothing
        if action == "suggest_defer":
            verdict = f"DEFER {str(appliance).upper()} -> {defer_until or 'LATER'}."
            if saving is not None:
                verdict += f" EST {saving:.1f} C/KWH CHEAPER."
        else:
            verdict = "NOTHING TO SHIFT."
        warnings.append("verdict line rebuilt from the JSON")
    verdict, corrected = correct_tier_words(verdict.upper(), tier)
    if corrected:
        warnings.append(f"price word in the verdict corrected to the real tier ({tier})")
    # The same guardrails as the nightly voice: the meter, not the person, and no
    # invented numbers. An observation that fails either is replaced by a plain
    # instrument line - a dull true line beats a witty wrong one.
    problem = ""
    if about_a_person(verdict, clean["reason"]):
        problem = "talked about a person, not the meter"
    elif payload is not None and ungrounded_numbers(verdict, known_numbers(payload)):
        problem = f"number(s) {', '.join(ungrounded_numbers(verdict, known_numbers(payload)))} not in the data"
    if problem:
        warnings.append(f"line dropped ({problem}): {verdict[:60]!r}")
        verdict = f"NOTHING TO SHIFT. PRICE {tier.upper()}." if tier else "NOTHING TO SHIFT."
        clean["voice"] = "advice" if action == "suggest_defer" else "plain"
    verdict = verdict[:VERDICT_MAX_CHARS]
    return Advice(verdict, clean, True, "ok", result.source, result.usage, warnings)


def advise(snapshot: dict, price_attrs: dict, *, now: datetime | None = None) -> tuple[Advice, dict]:
    """One Nemotron call. The caller gates it and records it (fail closed)."""
    now = now or datetime.now().astimezone()
    cost = load_cost_config()
    profiles = {p.id: p for p in load_profiles("profiles")}
    payload = build_payload(snapshot, price_attrs, profiles, cost, now=now)
    running_ids = {r["id"] for r in payload["running"]}
    try:
        config = load_nebius_config()
        result = chat(config, SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), max_tokens=ADVICE_MAX_TOKENS)
    except (ConfigError, NebiusError) as exc:
        return Advice("", {}, False, f"call failed: {str(exc).splitlines()[0]}"), payload

    dump = Path("var/last_advice_reply.txt")
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(f"finish_reason: {result.finish_reason}\nusage: {result.usage}\n\n--- content ---\n"
                    f"{result.content}\n\n--- reasoning_content ---\n{result.reasoning_content}\n", encoding="utf-8")
    if result.truncated:
        return Advice("", {}, False, "reply cut off at max_tokens", result.source, result.usage), payload
    return parse_advice(result, running_ids, payload["price"]["tier"], payload), payload


# --- standalone -------------------------------------------------------------
def _scenario(name: str, base: dict, now: datetime) -> dict:
    """Synthetic 'what's running' on top of the REAL price curve and context."""
    since = (now - timedelta(minutes=10)).isoformat()
    loads = {
        "ev": [{"id": "ev_charger", "name": "EV charger (Eve)", "state": "MATCHED", "phases": "ABC",
                "draw_w": {"A": 3450, "B": 3450, "C": 3500}, "confidence": 0.98, "since": since}],
        "kitchen": [
            {"id": "kettle", "name": "Water kettle", "state": "MATCHED", "phases": "B",
             "draw_w": {"B": 2000}, "confidence": 0.95, "since": since},
            {"id": "toaster", "name": "Toaster", "state": "MATCHED", "phases": "B",
             "draw_w": {"B": 900}, "confidence": 0.89, "since": since}],
        "idle": [],
    }[name]
    snap = json.loads(json.dumps(base))
    snap["loads"] = loads
    snap["ev_active"] = name == "ev"
    floor = (snap.get("base_load_w") or {}).get("total", 985)
    snap["power_w"] = {"total": floor + sum(sum(l["draw_w"].values()) for l in loads)}
    return snap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=("ev", "kitchen", "idle"),
                        help="synthetic running loads on the real price curve")
    parser.add_argument("--show-payload", action="store_true", help="print exactly what is sent")
    args = parser.parse_args(argv)

    try:
        ha_cfg = load_ha_config()
        cost = load_cost_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2
    state_path = Path("var/state.json")
    base = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    now = datetime.now().astimezone()
    snapshot = _scenario(args.scenario, base, now) if args.scenario else base
    if not snapshot:
        print("No var/state.json yet - run python -m virta.live --once, or use --scenario.", file=sys.stderr)
        return 1
    try:
        client = HAClient(ha_cfg)
        price, attrs = client.price_now()
    except HAError as exc:
        print(f"HA ERROR\n{exc}", file=sys.stderr)
        return 1
    snapshot.setdefault("price", {})["c_kwh"] = price

    gate = CloudCallGate(cost)
    decision = gate.allow_housekeeping(f"advisor CLI ({args.scenario or 'state.json'})")
    if not decision.should_call:
        print(f"NOT CALLING: {decision.reason}")
        return 1
    gate.record_call({})  # counted when sent - fail closed
    advice, payload = advise(snapshot, attrs, now=now)
    gate.add_usage(advice.usage)

    if args.show_payload:
        shown = json.loads(json.dumps(payload))
        curve = shown["price"]["curve"]
        shown["price"]["curve"] = curve[:6] + [["...", f"{len(curve)} slots total"]]
        print("PAYLOAD (curve abbreviated here; sent in full)")
        print(json.dumps(shown, indent=1, ensure_ascii=False))
    print(f"\nSITUATION  {args.scenario or 'live state.json'}  |  price now {price} c/kWh "
          f"{str(price_tier(price, cost)).upper()}  |  tomorrow published: {bool(attrs.get('tomorrow_valid'))}")
    facts = payload["price"]["facts"]
    print(f"PRICE      cheapest 1h {facts.get('cheapest_1h')}  cheapest 3h {facts.get('cheapest_3h')}  "
          f"range {facts.get('min_c_kwh')}..{facts.get('max_c_kwh')} until {facts.get('horizon_ends')}")
    print(f"NEMOTRON   {advice.status}  (answer from {advice.source or '-'}, "
          f"{advice.usage.get('completion_tokens', '?')} completion tokens)")
    if advice.ok:
        print(f"\n  >> {advice.verdict}\n")
        print(f"  action: {json.dumps(advice.action, ensure_ascii=False)}")
    for w in advice.warnings:
        print(f"  note: {w}")
    print(f"\nledger: {gate.status()}")
    return 0 if advice.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
