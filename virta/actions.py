"""Virta acts in the room - voice, lamp, and confirmed control through Home Assistant.

The Physical AI track asks for agents that "sense and act in the real world".
Virta senses with the 3EM and reasons with Nemotron; this module is the ACT:

  VOICE    - Virta says its line out loud through HA's tts.speak (the same
             speaker the house already uses for its 5 kW alarm). Actionable
             advice is always spoken; an observation only when Nemotron itself
             flags it worth saying (JSON "speak": true). Quiet hours and a rate
             limit stop it nagging.
  LAMP     - one RGB light shows Virta's JUDGEMENT (not just the price - the
             house already has a price lamp): red = something should wait,
             green = run things now, purple = noticed something, blue = calm.
  CONTROL  - Nemotron may PROPOSE turning OFF an allowlisted device. Default
             policy is suggest-and-confirm (spec 7, 9): a human presses Y.
             Lights may instead be "auto": announced out loud, a grace period
             to cancel, then done - for a console with no keyboard. An optional
             swap turns on a cheaper light in the same room. Never turns on
             anything but a configured swap target, never touches the EV.

Every action is appended to var/actions.jsonl - what was done, when, and why.
All three are opt-in from the environment; with nothing configured Virta acts
on nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from .ha_client import HAClient, HAError

ACTION_LOG = Path("var/actions.jsonl")
ANSWER_FILE = Path("var/answer.json")  # the console writes the human's Y/N here
PROPOSAL_TTL = timedelta(minutes=10)
ALLOWED_SERVICES = {("light", "turn_off"), ("switch", "turn_off")}  # off only, by design

MOODS = {  # rgb, and what it means - shown on the console legend too
    "wait": ((255, 40, 0), "something should wait"),
    "run": ((0, 255, 60), "run things now"),
    "noticed": ((150, 0, 255), "Virta noticed something"),
    "calm": ((0, 60, 255), "nothing to act on"),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in _env(name).split(",") if item.strip()]


def _log(kind: str, **fields) -> None:
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"when": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind, **fields}
    with open(ACTION_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def spoken(line: str) -> str:
    """Console shorthand -> something a TTS voice reads naturally."""
    text = line.strip()
    for pattern, repl in (
        (r"C/KWH", " cents per kilowatt hour"), (r"\bKWH\b", " kilowatt hours"), (r"->", " until "),
        (r"\bEST\b", "estimated"), (r"(\d)\s*KW\b", r"\1 kilowatts"), (r"(\d)\s*W\b", r"\1 watts"),
        (r"\bEV\b", "E V"), (r"\bEUR/MO(?:NTH)?\b", "euros a month"), (r"\bEUR\b", "euros"),
        (r"~", "about "), (r"/MO(?:NTH)?\b", " a month"),
    ):
        text = re.sub(pattern, repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:1].upper() + text[1:].lower()  # sentence case reads better than shouting
    return re.sub(r"\bi\b", "I", text)  # the pronoun only


@dataclass
class Voice:
    client: HAClient
    tts_entity: str = field(default_factory=lambda: _env("VIRTA_TTS_ENTITY", "tts.google_en_com"))
    player: str = field(default_factory=lambda: _env("VIRTA_TTS_PLAYER"))
    language: str = field(default_factory=lambda: _env("VIRTA_TTS_LANG", "en"))
    quiet: str = field(default_factory=lambda: _env("VIRTA_QUIET", "23:00-08:00"))
    max_per_hour: int = field(default_factory=lambda: int(_env("VIRTA_SPEAK_PER_HOUR", "4")))
    min_gap_s: float = field(default_factory=lambda: float(_env("VIRTA_SPEAK_MIN_GAP_S", "600")))
    spoken_at: list[float] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.player)

    def _quiet_now(self) -> bool:
        try:
            a, b = (dtime(*map(int, part.split(":"))) for part in self.quiet.split("-"))
        except ValueError:
            return False
        now = datetime.now().time()
        return (a <= now or now < b) if a > b else (a <= now < b)

    def say(self, line: str, *, why: str, urgent: bool = False) -> str:
        """Speak if allowed. Returns what happened, for the log and the console."""
        if not self.enabled:
            return "voice off (VIRTA_TTS_PLAYER not set)"
        if self._quiet_now():
            return f"quiet hours {self.quiet}"
        now = time.time()
        self.spoken_at = [t for t in self.spoken_at if now - t < 3600]
        if not urgent:
            if len(self.spoken_at) >= self.max_per_hour:
                return f"already spoke {self.max_per_hour} times this hour"
            if self.spoken_at and now - self.spoken_at[-1] < self.min_gap_s:
                return "spoke less than 10 min ago"
        text = spoken(line)
        try:
            self.client.call_service("tts", "speak", {
                "entity_id": self.tts_entity, "media_player_entity_id": self.player,
                "message": text, "language": self.language, "cache": False,
            })
        except HAError as exc:
            _log("speak_failed", text=text, error=str(exc)[:200])
            return f"failed: {str(exc).splitlines()[0]}"
        self.spoken_at.append(now)
        _log("spoke", text=text, why=why, player=self.player)
        return "spoken"


@dataclass
class Lamp:
    client: HAClient
    entity: str = field(default_factory=lambda: _env("VIRTA_LAMP"))
    brightness_pct: int = field(default_factory=lambda: int(_env("VIRTA_LAMP_BRIGHTNESS", "20")))
    mood: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.entity)

    def show(self, mood: str, *, why: str) -> str:
        if not self.enabled:
            return "lamp off (VIRTA_LAMP not set)"
        if mood == self.mood or mood not in MOODS:
            return "unchanged"
        rgb, meaning = MOODS[mood]
        try:
            self.client.call_service("light", "turn_on", {
                "entity_id": self.entity, "rgb_color": list(rgb), "brightness_pct": self.brightness_pct,
            })
        except HAError as exc:
            _log("lamp_failed", mood=mood, error=str(exc)[:200])
            return f"failed: {str(exc).splitlines()[0]}"
        self.mood = mood
        _log("lamp", mood=mood, meaning=meaning, why=why, entity=self.entity)
        return f"lamp {mood}"


@dataclass
class Controls:
    """Control of allowlisted devices - turn OFF only, plus an optional cheaper swap.

    Two policies, per device, both from .env:
      confirm (VIRTA_CONTROL) - Nemotron proposes; nothing happens until a human
                                presses Y on the console.
      auto    (VIRTA_AUTO)    - for lights only: Virta ANNOUNCES the action out
                                loud, waits a grace period (N on the console
                                cancels), then acts. For a console with no
                                keyboard (the Pi), and for a live demo.
    Swap (VIRTA_SWAP "light.big=light.small"): after turning a device off, turn
    on its cheaper partner in the same room. turn_on is allowed ONLY for a
    configured swap target, ONLY right after its partner was turned off.
    Never anything else; never the EV.
    """

    client: HAClient
    allow: list[str] = field(default_factory=lambda: _env_list("VIRTA_CONTROL"))
    auto: list[str] = field(default_factory=lambda: _env_list("VIRTA_AUTO"))
    swap: dict[str, str] = field(default_factory=lambda: dict(
        pair.split("=", 1) for pair in _env_list("VIRTA_SWAP") if "=" in pair))
    grace_s: float = field(default_factory=lambda: float(_env("VIRTA_AUTO_GRACE_S", "15")))
    pending: dict | None = None

    def __post_init__(self) -> None:
        # auto implies allowed; auto is for lights only, by design
        self.auto = [e for e in self.auto if e.startswith("light.")]
        self.allow = sorted(set(self.allow) | set(self.auto))

    def _name(self, entity_id: str) -> str:
        try:
            return (self.client.get_state(entity_id).attributes or {}).get("friendly_name", entity_id)
        except HAError:
            return entity_id

    def devices(self) -> list[dict]:
        """What Nemotron may propose to switch off, with its state and any cheaper swap."""
        found = []
        for entity_id in self.allow:
            try:
                s = self.client.get_state(entity_id)
            except HAError:
                continue
            entry = {"entity_id": entity_id, "name": (s.attributes or {}).get("friendly_name", entity_id),
                     "state": s.state, "policy": "auto" if entity_id in self.auto else "confirm",
                     "on_since": s.last_updated.strftime("%H:%M") if s.last_updated and s.state == "on" else None}
            if entity_id in self.swap:
                entry["swap_to"] = {"entity_id": self.swap[entity_id], "name": self._name(self.swap[entity_id]),
                                    "note": "a cheaper light in the same room - turned on after this one is off"}
            found.append(entry)
        return found

    def propose(self, action: dict | None, *, why: str) -> dict | None:
        """Validate Nemotron's proposal. Anything off the allowlist or not a turn-off is refused."""
        if not action:
            return None
        entity = str(action.get("entity_id", ""))
        service = str(action.get("service", ""))
        domain, _, verb = service.partition(".")
        if entity not in self.allow or (domain, verb) not in ALLOWED_SERVICES or entity.split(".")[0] != domain:
            _log("proposal_refused", action=action, why="not an allowlisted turn-off")
            return None
        now = datetime.now().astimezone()
        auto = entity in self.auto
        swap_to = self.swap.get(entity)
        self.pending = {
            "id": uuid.uuid4().hex[:8], "entity_id": entity, "service": service,
            "name": str(action.get("name") or self._name(entity)), "why": why[:200],
            "policy": "auto" if auto else "confirm",
            "act_at": (now + timedelta(seconds=self.grace_s)).isoformat(timespec="seconds") if auto else None,
            "swap_to": swap_to, "swap_name": self._name(swap_to) if swap_to else None,
            "expires": (now + PROPOSAL_TTL).isoformat(timespec="seconds"),
        }
        _log("proposed", **self.pending)
        return self.pending

    def _execute(self, done: dict) -> str:
        domain, _, verb = done["service"].partition(".")
        try:
            self.client.call_service(domain, verb, {"entity_id": done["entity_id"]})
            if done.get("swap_to"):  # the cheaper light takes over - the only turn_on Virta ever does
                self.client.call_service("light", "turn_on", {"entity_id": done["swap_to"]})
        except HAError as exc:
            _log("action_failed", id=done["id"], error=str(exc)[:200])
            return "failed"
        _log("done", id=done["id"], entity_id=done["entity_id"], service=done["service"],
             swap_to=done.get("swap_to"), policy=done["policy"], why=done["why"])
        return "done"

    def check_answer(self) -> tuple[str, dict | None]:
        """Every tick: act on the human's answer - or, for auto, when the grace period is over."""
        if not self.pending:
            return "", None
        now = datetime.now().astimezone()
        try:
            answer = json.loads(ANSWER_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            answer = {}
        if answer.get("id") == self.pending["id"]:
            ANSWER_FILE.unlink(missing_ok=True)
            done, self.pending = self.pending, None
            if answer.get("answer") != "yes":
                _log("declined", id=done["id"], entity_id=done["entity_id"])
                return "declined", done
            return self._execute(done), done
        if self.pending["policy"] == "auto" and now >= datetime.fromisoformat(self.pending["act_at"]):
            done, self.pending = self.pending, None
            return self._execute(done), done
        if now > datetime.fromisoformat(self.pending["expires"]):
            _log("proposal_expired", id=self.pending["id"])
            self.pending = None
            return "expired", None
        return "", None


def mood_for(advice: dict, price_tier: str | None, has_unusual: bool) -> str:
    """Virta's judgement as a colour."""
    action = advice.get("action") or {}
    if action.get("action") == "suggest_defer":
        return "wait"
    if action.get("ha_action") or has_unusual or action.get("speak"):
        return "noticed"
    if price_tier in ("negative", "cheap"):
        return "run"
    return "calm"
