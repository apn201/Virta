"""Confirm the live Home Assistant feed: every entity in the spec 11 config block.

Run:  python -m virta.check_ha

Read-only. Touches nothing in HA, changes nothing, and makes no cloud calls.
Prints the four power sensors, the Nordpool price plus a summary of its curve,
both temperatures and the darkness flag - i.e. exactly the inputs the detector
and the reasoning layer will consume.
"""

from __future__ import annotations

import sys

from .config import ConfigError, load_cost_config, load_ha_config
from .cost_control import price_tier
from .ha_client import HAClient, HAError, parse_price_curve


def main() -> int:
    try:
        ha_config = load_ha_config()
        cost_config = load_cost_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}", file=sys.stderr)
        return 2

    print(ha_config.describe())
    client = HAClient(ha_config)

    try:
        print(f"\nAPI       : {client.ping()}")

        print("\nPOWER (spec 3.1)")
        for entity_id, reading in client.power_now().items():
            stamp = reading.last_updated.strftime("%H:%M:%S") if reading.last_updated else "?"
            print(f"  {entity_id:32s} {str(reading).split(' = ')[1]:>16s}   updated {stamp}")

        print("\nPRICE (spec 3.2)")
        price, attributes = client.price_now()
        if price is None:
            print("  current price unusable - sensor reported a non-numeric state")
        else:
            tier = price_tier(price, cost_config)
            print(f"  now              {price:g} c/kWh -> {str(tier).upper()}")
        today = parse_price_curve(attributes, "raw_today")
        tomorrow = parse_price_curve(attributes, "raw_tomorrow")
        print(f"  raw_today        {len(today)} slots")
        if today:
            first, last = today[0], today[-1]
            span_min = (first["end"] - first["start"]).total_seconds() / 60 if first["end"] else None
            values = [slot["value"] for slot in today]
            print(f"    slot length    {span_min:g} min" if span_min else "    slot length    unknown")
            print(f"    first slot     {first['start']:%H:%M %z}  {first['value']:g} c/kWh")
            print(f"    last slot      {last['start']:%H:%M %z}  {last['value']:g} c/kWh")
            print(f"    range          {min(values):g} .. {max(values):g} c/kWh")
            if min(values) < 0:
                print("    NEGATIVE prices present - you are paid to consume in some slots")
        valid = attributes.get("tomorrow_valid")
        print(f"  raw_tomorrow     {len(tomorrow)} slots (tomorrow_valid={valid})")
        if not tomorrow:
            print("    empty - normal before ~13:00 CET; the reasoning gets a short horizon")

        print("\nCONTEXT (spec 3.3)")
        for key in ("SENSOR_TEMP_OUT", "SENSOR_TEMP_IN"):
            reading = client.get_state(ha_config.entity(key))
            label = "outdoor" if key == "SENSOR_TEMP_OUT" else "indoor"
            print(f"  {label:8s} {reading}")
        sun = client.get_state(ha_config.entity("SENSOR_SUN"))
        print(f"  sun      {sun.entity_id} = {sun.state}  -> is_dark={sun.state == 'below_horizon'}")

    except HAError as exc:
        print(f"\nHA ERROR\n{exc}", file=sys.stderr)
        return 1

    print("\nAll spec 11 entities read successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
