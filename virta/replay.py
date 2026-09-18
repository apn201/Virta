"""Slice 4: replay recorded history through the baseline + stacking detector.

Run:
    python -m virta.replay                      # cached HA pull (data/power_history.csv)
    python -m virta.replay --fetch              # pull fresh history from HA first
    python -m virta.replay --day 2026-09-16     # print events for one day only

Same streaming code as the live Pi loop - one reading at a time, no look-ahead
except the seed floor. Read-only against HA; no cloud calls.

Writes the event log to var/events.jsonl - the compact history spec 5A mines
later, and the only power-derived thing that is ever allowed to leave the house.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .baseline import BaselineTracker
from .config import ConfigError, load_ha_config
from .detector import StackingDetector
from .ev_signature import rough_baseline
from .ha_client import HAClient, HAError
from .history import align_phases, fetch_history, load_csv, save_csv
from .profiles import load_profiles

CACHE = "data/power_history.csv"
SEED_HOURS = 6  # the seed floor looks at this much history before the first quiet stretch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch", action="store_true", help="pull fresh history from HA before replaying")
    parser.add_argument("--csv", default=CACHE, help=f"history file to replay (default {CACHE})")
    parser.add_argument("--days", type=float, default=14, help="history to request with --fetch")
    parser.add_argument("--day", help="only print events for this date (YYYY-MM-DD)")
    parser.add_argument("--events-out", default="var/events.jsonl")
    args = parser.parse_args(argv)

    try:
        ha_config = load_ha_config(require_token=args.fetch)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    if args.fetch:
        end = datetime.now().astimezone()
        try:
            series, _ = fetch_history(
                HAClient(ha_config, timeout_s=120), ha_config.power_entities, end - timedelta(days=args.days), end
            )
        except HAError as exc:
            print(f"HA ERROR\n{exc}", file=sys.stderr)
            return 1
        save_csv(series, args.csv)
    if not Path(args.csv).is_file():
        print(f"No history at {args.csv} - run with --fetch first.", file=sys.stderr)
        return 1
    series, _ = load_csv(args.csv, ha_config.power_entities)

    frames = align_phases(
        series,
        ha_config.entity("SENSOR_TOTAL"),
        (ha_config.entity("SENSOR_PHASE_A"), ha_config.entity("SENSOR_PHASE_B"), ha_config.entity("SENSOR_PHASE_C")),
    )
    if not frames:
        print("No usable readings.", file=sys.stderr)
        return 1

    profiles = load_profiles("profiles")
    matchable = [p.id for p in profiles if p.matchable]
    ignored = [p.id for p in profiles if not p.matchable]

    seed_until = frames[0].when + timedelta(hours=SEED_HOURS)
    seed = rough_baseline([f for f in frames if f.when <= seed_until])
    tracker = BaselineTracker(seed)  # no state_path: a replay must not overwrite the live floor
    detector = StackingDetector(profiles, seed)

    print(f"REPLAY   {frames[0].when:%a %d.%m %H:%M} -> {frames[-1].when:%a %d.%m %H:%M}, {len(frames)} readings")
    print(f"PROFILES matched against: {', '.join(matchable)}")
    if ignored:
        print(f"         loaded, never matched (not observed by the 3EM): {', '.join(ignored)}")
    print(f"SEED     {seed.describe()}  (p10 of the first {SEED_HOURS} h)")

    for frame in frames:
        detector.update(frame)
        new_floor = tracker.observe(detector.calibration_frame(frame), stack_empty=detector.calibration_ready)
        if new_floor:
            detector.set_baseline(new_floor)
    final_floor = tracker.flush()
    if final_floor:
        detector.set_baseline(final_floor)

    print("\nBASELINE UPDATES (quiet stretches, spec 5.1)")
    for when, floor, stretch in tracker.updates:
        mins = stretch.duration.total_seconds() / 60
        print(f"  {stretch.start:%a %d.%m %H:%M}-{stretch.end:%H:%M} ({mins:.0f} min, spread {stretch.spread_w:.0f} W)"
              f"  -> floor {floor.describe()}")

    wanted_day = date.fromisoformat(args.day) if args.day else None
    shown = [e for e in detector.events if wanted_day is None or e.when.date() == wanted_day]
    print(f"\nEVENTS ({len(shown)}{' on ' + args.day if args.day else ''})")
    for event in shown:
        print(f"  {event.describe()}")

    kinds = Counter(e.kind for e in detector.events)
    matched_on = Counter(e.load_id for e in detector.events if e.kind == "ON" and e.state == "MATCHED")
    unknown_on = sum(1 for e in detector.events if e.kind == "ON" and e.state == "UNKNOWN")
    print("\nSUMMARY")
    print(f"  events by kind   : {dict(kinds)}")
    print(f"  matched switch-on: {dict(matched_on)}")
    print(f"  unknown switch-on: {unknown_on}")
    print(f"  unreliable (EV)  : {sum(1 for e in detector.events if e.unreliable)}")

    print("\nSTATE AT END OF REPLAY (spec 5.3)")
    for line in detector.state().describe().splitlines():
        print(f"  {line}")

    out = Path(args.events_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for event in detector.events:
            fh.write(json.dumps(event.to_json()) + "\n")
    print(f"\nevent log -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
