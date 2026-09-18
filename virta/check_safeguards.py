"""Slice 2 check: prove the cloud-call safeguards behave, without spending a cent.

Run:  python -m virta.check_safeguards

Simulates a realistic day at the real 15s poll rate and reports how many cloud
calls the gate would actually allow, then runs the failure scenarios that the
guardrails exist for: a flapping sensor, the kill switch, and a restart trying
to get a fresh budget.

No network calls of any kind. The clock is fake, the appliances are scripted,
and the ledger is written to a scratch file.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .config import ConfigError, load_cost_config
from .cost_control import CloudCallGate, PriceTier, Situation, price_tier

TICK = timedelta(seconds=15)  # spec 11: the real 3EM poll rate
DAY_START = datetime(2026, 9, 17, 0, 0, 0).astimezone()


def scripted_day(now: datetime) -> tuple[list[str], float, bool]:
    """A plausible day: what is on, the price, and whether it is dark.

    Times are the ones from the spec's real captures - EV overnight (section 4),
    kitchen loads in the morning, sauna in the evening (section 3.4).
    """
    minutes = now.hour * 60 + now.minute
    active: list[str] = []

    if minutes >= 23 * 60 or minutes < 6 * 60:  # EV charges overnight
        active.append("ev_charger")
    if 7 * 60 + 12 <= minutes < 7 * 60 + 15:
        active.append("kettle")
    if 7 * 60 + 15 <= minutes < 7 * 60 + 17:
        active.append("toaster")
    if 17 * 60 <= minutes < 22 * 60:
        active.append("halogen1")
    if 20 * 60 <= minutes < 21 * 60 + 30:
        active.append("sauna")

    # Price curve shaped like a real Finnish day: cheap at night, peak late
    # afternoon. Values in c/kWh, crossing the 5/8/12 tier boundaries.
    if minutes < 6 * 60:
        price = 1.2
    elif minutes < 9 * 60:
        price = 6.4
    elif minutes < 16 * 60:
        price = 7.9
    elif minutes < 19 * 60:
        price = 13.5
    elif minutes < 22 * 60:
        price = 9.1
    else:
        price = 3.0

    is_dark = minutes < 6 * 60 + 30 or minutes >= 20 * 60
    return active, price, is_dark


def simulate_day(gate: CloudCallGate, config) -> tuple[int, Counter, list[str]]:
    """Walk a full day at 15s ticks. Returns (calls, triggers, a short call log)."""
    triggers: Counter = Counter()
    log: list[str] = []
    calls = 0
    now = DAY_START
    end = DAY_START + timedelta(days=1)

    while now < end:
        active, price, is_dark = scripted_day(now)
        tomorrow_valid = now.hour >= 14  # prices publish ~13:00 CET
        situation = Situation.build(active, price, is_dark, config, tomorrow_valid=tomorrow_valid)

        decision = gate.evaluate(situation, now=now)
        if decision.should_call:
            calls += 1
            triggers[decision.trigger] += 1
            gate.record_call({"prompt_tokens": 900, "completion_tokens": 300},
                             situation=situation, now=now)
            gate.set_verdict(f"verdict for {situation.describe()}", now=now)
            if len(log) < 14:
                log.append(f"  {now:%H:%M:%S}  {decision.trigger:17s} {decision.reason}")
        now += TICK

    return calls, triggers, log


def flapping_sensor(gate: CloudCallGate, config, *, ticks_per_flip: int) -> tuple[int, Counter]:
    """A sensor toggling a load for an hour - the budget-drain case.

    Two speeds matter and they are stopped by different layers:
      ticks_per_flip=1 - flips faster than the settle window, so DEBOUNCE never
                         lets it through and it never reaches the cap.
      ticks_per_flip=4 - flips every 60s, settles each time, so every flip is a
                         legitimate-looking change and only the HOURLY CAP stops it.
    """
    calls = 0
    blocks: Counter = Counter()
    now = DAY_START
    for i in range(240):  # one hour of 15s ticks
        active = ["ghost_load"] if (i // ticks_per_flip) % 2 else []
        situation = Situation.build(active, 7.0, False, config)
        decision = gate.evaluate(situation, now=now)
        if decision.should_call:
            calls += 1
            gate.record_call({"prompt_tokens": 900, "completion_tokens": 300},
                             situation=situation, now=now)
        else:
            blocks[decision.blocked_by] += 1
        now += TICK
    return calls, blocks


def main() -> int:
    try:
        config = load_cost_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="virta_safeguards_"))
    try:
        sim_config = replace(
            config,
            usage_state_path=str(scratch / "usage.json"),
            kill_switch_path=str(scratch / "CLOUD_OFF"),
        )
        print(sim_config.describe())

        # --- tier boundaries ------------------------------------------------
        print("\n== price tiers (spec 8; boundaries from config) ==")
        for price in (-1.5, 0.665, 4.9, 5.0, 7.9, 8.0, 11.9, 12.0, 25.0):
            tier = price_tier(price, sim_config)
            note = "  <- paid to consume" if tier is PriceTier.NEGATIVE else ""
            print(f"  {price:7.3f} c/kWh -> {str(tier).upper()}{note}")

        # --- a normal day ---------------------------------------------------
        print("\n== simulated day: 24h at 15s ticks ==")
        gate = CloudCallGate(sim_config)
        calls, triggers, log = simulate_day(gate, sim_config)
        ticks = 5760
        print(f"  local ticks (free)   : {ticks}")
        print(f"  cloud calls (paid)   : {calls}")
        print(f"  reduction vs per-tick: {100 * (1 - calls / ticks):.1f}%")
        print(f"  triggers             : {dict(triggers)}")
        print("  first calls:")
        for line in log:
            print(line)
        print(f"  ledger: {gate.status(DAY_START + timedelta(hours=23, minutes=59))}")

        # --- a flapping sensor ----------------------------------------------
        ok = True
        for label, per_flip, ledger in (
            ("every 15s (faster than the 8s settle window)", 1, "flap_fast.json"),
            ("every 60s (settles each time - only the cap can stop it)", 4, "flap_slow.json"),
        ):
            print(f"\n== flapping sensor, {label} ==")
            flap_gate = CloudCallGate(replace(sim_config, usage_state_path=str(scratch / ledger)))
            flap_calls, blocks = flapping_sensor(flap_gate, sim_config, ticks_per_flip=per_flip)
            held = flap_calls <= sim_config.max_calls_per_hour
            ok = ok and held
            print(f"  ticks                : 240 (one hour)")
            print(f"  cloud calls allowed  : {flap_calls}, cap is {sim_config.max_calls_per_hour}/hour")
            print(f"  holds by layer       : {dict(blocks)}")
            print(f"  within cap           : {held}")

        # --- restart does not reset the budget ------------------------------
        print("\n== restart with the hour's budget already spent ==")
        reborn = CloudCallGate(replace(sim_config, usage_state_path=str(scratch / "flap_slow.json")))
        after = reborn.evaluate(
            Situation.build(["something_new"], 7.0, False, sim_config),
            now=DAY_START + timedelta(minutes=59),
        )
        print(f"  ledger survived      : {reborn.usage.total_calls} calls loaded from disk")
        print(f"  next call            : {after}")
        ok = ok and not after.should_call

        # --- kill switch ----------------------------------------------------
        print("\n== kill switch ==")
        kill_gate = CloudCallGate(replace(sim_config, usage_state_path=str(scratch / "kill.json")))
        situation = Situation.build(["ev_charger"], 13.5, True, sim_config)
        print(f"  before : {kill_gate.evaluate(situation, now=DAY_START)}")
        Path(sim_config.kill_switch_path).write_text("stop\n", encoding="utf-8")
        killed = kill_gate.evaluate(situation, now=DAY_START)
        print(f"  after  : {killed}")
        Path(sim_config.kill_switch_path).unlink()
        ok = ok and not killed.should_call

        print(f"\nAll safeguards exercised, no cloud calls made. Guardrails held: {ok}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
