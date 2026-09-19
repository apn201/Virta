"""Teach by gesture: a new load, a guess out loud, a flick of the switch, a new profile.

Builder decision 2026-09-19: the demo needs one moment where the house visibly
LEARNS. The discovery loop of spec 0, live and without a keyboard:

  1. Something unknown switches on (a step the detector can't match).
  2. After IDENTIFY_AFTER_S of it running, Nemotron guesses what it is from the
     step's size, phase, the time and what else runs - and the butler says so,
     asking: if I'm right, switch it off and on again.
  3. The gesture - the same step going and coming back within GESTURE_WINDOW_S -
     is the "yes". The physical switch is the user interface.
  4. Virta writes profiles/<id>.json from the measured step and adopts the running
     load under its new name. Next time it is recognised like any other profile.

No gesture within GESTURE_WAIT_S: nothing is learned; the load stays unknown and
the ordinary labelling worklist (spec 5A) still gets it. A guess is a hypothesis
until a human confirms it - the same rule as everywhere else in Virta.

The call is Super (labelling tier), one per new unknown load at most, through
the same gate: kill switch and caps apply.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .guardrails import about_a_person, known_numbers, ungrounded_numbers
from .nebius_client import chat, extract_json
from .profiles import Profile, _from_json

IDENTIFY_AFTER_S = 20  # let it settle: a kettle-length blip isn't worth a guess
GESTURE_WINDOW_S = 20  # off and on again within this = "yes, that's it"
GESTURE_MIN_DIP_S = 0.5  # a dip must be real, not one mixed-epoch reading
GESTURE_WAIT_S = 180  # how long a guess waits for its gesture
GESTURE_SIZE_TOLERANCE = 0.25  # the step back on must match the one that went off
GESTURE_MIN_TOL_W = 60.0
ASK_COOLDOWN_S = 600  # the same size on the same phase is not asked about twice in 10 min
IDENTIFY_MAX_TOKENS = 12000
PROFILES_DIR = Path("profiles")

IDENTIFY_PROMPT = """\
You are Virta, the butler of a home in Finland, watching it through a cheap \
3-phase electricity meter. Something NEW has just switched on: one step on one \
phase that matches none of the house's known appliances. Guess what it is.

You get: the step (watts, phase), how long it has run so far, the time of day, \
whether it is dark, the outdoor temperature, what else is running, and the \
house's known appliances with their steps - it is NOT one of those. Think about \
typical power draws of common household appliances (hair dryer, vacuum cleaner, \
space heater, iron, blender, microwave, drill, fan heater, dehumidifier, rice \
cooker, heat gun, ...) and pick the most likely.

Output exactly two things and nothing else:
Line 1: what you say in the chat, one or two short sentences, sentence case, in \
a discreet butler's manner with a dry wit: name the step (phase and watts), give \
your guess, and ask the household, if you are right, to switch it off for a few seconds and back on. \
At most 150 characters. The manner (placeholders in angle brackets - never an \
appliance of their own): "Something new on <phase>, <watts> W - <your guess>, \
unless I am much mistaken. Off for a moment and back on, if I have it right." \
Vary the wording; the guess must come from the step, not from this sentence.
Then a JSON object:
{"guess": "<1-3 lowercase words, the appliance>", "confidence": <0..1>, \
"alternatives": ["<next guess>", "<third>"], "reason": "<one sentence>"}
Hard rules: every number you write must come from the data; nothing about \
health, mood, relationships, visitors, sleep or the bathroom."""


@dataclass
class Candidate:
    load_key: int
    phase: str
    step_w: float
    since: datetime
    asked: bool = False
    asked_at: datetime | None = None
    guess: str | None = None
    confidence: float | None = None
    line: str | None = None
    off_at: datetime | None = None
    off_w: float | None = None
    level: float | None = None  # the phase reading while it runs - what a dip is measured from
    dip_from: datetime | None = None
    dip_low: float | None = None
    done: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "learned"


class Teacher:
    """Tracks unknown loads from switch-on to guess to gesture to profile."""

    def __init__(self, profiles_dir: Path = PROFILES_DIR) -> None:
        self.profiles_dir = profiles_dir
        self.candidates: dict[int, Candidate] = {}
        self.asked_recently: list[tuple[datetime, str, float]] = []  # (when, phase, W)

    # --- what the detector saw ---------------------------------------------
    def observe(self, event, now: datetime) -> Candidate | None:
        """Feed every detector event. Returns a candidate when its gesture confirms it."""
        delta = sum(event.delta_w.values())
        if event.kind == "ON" and event.state == "UNKNOWN" and len(event.phases) == 1 and delta > 0:
            # the gesture's "on again": a pending candidate on this phase, just off, same size
            for c in self.candidates.values():
                if c.asked and not c.done and c.off_at and c.phase == event.phases \
                        and (event.when - c.off_at).total_seconds() <= GESTURE_WINDOW_S \
                        and abs(delta - c.step_w) <= max(GESTURE_MIN_TOL_W, GESTURE_SIZE_TOLERANCE * c.step_w):
                    c.done = True
                    c.extra["confirmed_key"] = event.load_key
                    c.extra["confirmed_w"] = delta
                    return c
            self.candidates[event.load_key] = Candidate(event.load_key, event.phases, delta, event.when)
        elif event.kind in ("OFF", "OFF_RECONCILED") and event.load_key in self.candidates:
            c = self.candidates[event.load_key]
            if c.asked and not c.done:
                c.off_at, c.off_w = event.when, -delta  # maybe the first half of the gesture
            else:
                del self.candidates[event.load_key]  # ended before we even asked
        return None

    def watch(self, frame) -> Candidate | None:
        """Every reading. The flick itself - off for a second or three, then on again - is
        shorter than the detector's settle time (10 s), so it is caught here, on the raw
        phase value: a dip of about the step's size that recovers within the window."""
        for c in self.candidates.values():
            if not c.asked or c.done or not c.guess:
                continue
            value = {"A": frame.a, "B": frame.b, "C": frame.c}.get(c.phase)
            if value is None:
                continue
            tol = max(GESTURE_MIN_TOL_W, GESTURE_SIZE_TOLERANCE * c.step_w)
            if c.dip_from is None:
                if c.level is not None and value <= c.level - (c.step_w - tol):
                    c.dip_from, c.dip_low = frame.when, value  # it went off
                else:  # still running: follow its level slowly (a heater's draw wanders)
                    c.level = value if c.level is None else 0.8 * c.level + 0.2 * value
                continue
            c.dip_low = min(c.dip_low, value)
            held = (frame.when - c.dip_from).total_seconds()
            if value >= c.level - tol:  # back on
                if held >= GESTURE_MIN_DIP_S:
                    c.done, c.off_at, c.off_w = True, c.dip_from, c.level - c.dip_low
                    c.extra.update(confirmed_key=c.load_key, confirmed_w=value - c.dip_low)
                    return c
                c.dip_from = None
            elif held > GESTURE_WINDOW_S:
                c.dip_from = None  # it stayed off: the detector will report a normal OFF
        return None

    def due(self, now: datetime, active_keys: set[int]) -> Candidate | None:
        """The one candidate worth a guess now, if any."""
        self._expire(now, active_keys)
        self.asked_recently = [a for a in self.asked_recently if (now - a[0]).total_seconds() < ASK_COOLDOWN_S]
        for c in self.candidates.values():
            if c.asked or c.load_key not in active_keys:
                continue
            if (now - c.since).total_seconds() < IDENTIFY_AFTER_S:
                continue
            if any(p == c.phase and abs(w - c.step_w) <= max(GESTURE_MIN_TOL_W, 0.2 * c.step_w)
                   for _, p, w in self.asked_recently):
                continue
            return c
        return None

    def _expire(self, now: datetime, active_keys: set[int]) -> None:
        for key, c in list(self.candidates.items()):
            if c.done:
                del self.candidates[key]
            elif c.asked and c.asked_at and (now - c.asked_at).total_seconds() > GESTURE_WAIT_S:
                del self.candidates[key]  # no gesture: stays unknown, the worklist has it
            elif c.off_at and (now - c.off_at).total_seconds() > GESTURE_WINDOW_S:
                del self.candidates[key]  # switched off, not back on: just a run that ended
            elif not c.asked and key not in active_keys:
                del self.candidates[key]

    def mark_asked(self, c: Candidate, now: datetime) -> None:
        c.asked, c.asked_at = True, now
        self.asked_recently.append((now, c.phase, c.step_w))

    def guess_for(self, load_key: int) -> str | None:
        c = self.candidates.get(load_key)
        return c.guess if c and c.guess else None

    # --- the guess ------------------------------------------------------------
    @staticmethod
    def build_payload(c: Candidate, snapshot: dict, profiles: list[Profile], now: datetime) -> dict:
        context = snapshot.get("context") or {}
        return {
            "new_step": {"phase": c.phase, "watts": round(c.step_w),
                         "running_for_s": round((now - c.since).total_seconds())},
            "time": now.strftime("%a %H:%M"),
            "is_dark": context.get("is_dark"),
            "outdoor_c": context.get("outdoor_c"),
            "also_running": [{"name": l.get("name"), "watts": round(sum((l.get("draw_w") or {}).values()))}
                             for l in snapshot.get("loads") or []
                             if l.get("state") == "MATCHED" and not l.get("base_part_off")],
            "known_appliances_not_this": [{"name": p.display_name, "phase": p.phase, "step_w": round(p.step_w)}
                                          for p in profiles if p.matchable],
        }

    @staticmethod
    def identify(config, payload: dict) -> dict:
        """One Nemotron call -> {"ok", "guess", "confidence", "line", "usage"}. Runs in a worker."""
        result = chat(config, IDENTIFY_PROMPT, json.dumps(payload, ensure_ascii=False),
                      max_tokens=IDENTIFY_MAX_TOKENS, model=config.model_labels)
        Path("var").mkdir(exist_ok=True)
        Path("var/last_identify_reply.txt").write_text(
            f"finish_reason: {result.finish_reason}\nusage: {result.usage}\n\n--- content ---\n{result.content}\n"
            f"\n--- reasoning_content ---\n{result.reasoning_content}\n", encoding="utf-8")
        parsed = extract_json(result)
        if result.truncated or not isinstance(parsed, dict) or not str(parsed.get("guess") or "").strip():
            return {"ok": False, "usage": result.usage, "why": "no usable guess"}
        guess = str(parsed["guess"]).strip().lower()[:40]
        step = payload["new_step"]
        line = ""
        for raw in (result.content or "").splitlines():
            raw = raw.strip().strip("`").strip()
            if raw and not raw.startswith(("{", "[", "json")):
                line = raw
                break
        # the same guardrails as every other line; a failing line is rebuilt plainly
        if not line or about_a_person(line) or ungrounded_numbers(line, known_numbers(payload)) \
                or len(line) > 170:
            line = (f"Something new on {step['phase']}, about {step['watts']} W. A {guess}, I'd wager - "
                    "off for a few seconds and back on, if I have it right.")
        elif "back on" not in line.lower() and "off and on" not in line.lower():
            # the gesture must be asked for completely: off AND back on
            if any(w in line.lower() for w in (" off", "toggle", "flick")):
                line = line.rstrip(" .?") + ", then back on."
            else:
                line = line.rstrip(" .") + ". Off for a few seconds and back on, if I have it right."
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        return {"ok": True, "guess": guess, "confidence": confidence, "line": line,
                "alternatives": [str(a) for a in (parsed.get("alternatives") or [])][:2],
                "reason": str(parsed.get("reason", ""))[:200], "usage": result.usage}

    # --- the lesson -------------------------------------------------------------
    def learn(self, c: Candidate, now: datetime) -> Profile:
        """Write the new profile from the measured steps and return it."""
        self.profiles_dir.mkdir(exist_ok=True)
        base = slug(c.guess or "learned")
        pid, n = base, 2
        while (self.profiles_dir / f"{pid}.json").exists():
            pid, n = f"{base}_{n}", n + 1
        steps = [c.step_w, c.off_w or c.step_w, c.extra.get("confirmed_w", c.step_w)]
        step = sum(steps) / len(steps)
        spread = max(steps) - min(steps)
        tolerance = round(max(35.0, 0.15 * step, spread * 1.5))
        data = {
            "id": pid,
            "display_name": (c.guess or pid).capitalize(),
            "tier": 1,
            "shiftable": False,
            "signature": {"type": "step", "phase": c.phase, "step_w": round(step), "step_tolerance_w": tolerance,
                          "cycling": None},
            "measured": {
                "on": f"{c.since:%H:%M:%S} +{c.step_w:.0f} W on {c.phase}",
                "gesture": f"off {c.off_at:%H:%M:%S} -{(c.off_w or 0):.0f} W, on again "
                           f"+{c.extra.get('confirmed_w', 0):.0f} W",
                "source": f"taught by gesture {now:%a %d.%m.%Y %H:%M}: Nemotron guessed "
                          f"'{c.guess}' ({c.confidence}), the household confirmed by switching it off and on",
            },
            "context_factors": [],
            "learned": True,
            "notes": "Learned live (virta/teach.py). Edit the name freely; the signature is measured.",
        }
        path = self.profiles_dir / f"{pid}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return _from_json(data)
