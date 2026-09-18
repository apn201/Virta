"""Local history archive - kept forever, because HA only keeps 14 days (spec 3.1).

Patterns worth reasoning about ("the base load crept up three weeks ago", "the
car stopped charging a month ago") are longer than the recorder's window. So
every day of raw power and real Nordpool price history is copied out of HA into
local files and never deleted. It stays on this machine: raw power data never
leaves the house (spec 11), and data/ is git-ignored.

Layout (gzip CSV, the same long format as the HA exports):
    data/archive/power/2026-09-16.csv.gz    entity_id,last_updated,state
    data/archive/prices/2026-09-16.csv.gz   last_updated,state   (c/kWh)
    data/archive/manifest.json              what is stored, what is frozen

A day is re-fetched until it is at least two days old (late recorder rows,
yesterday's tail), then frozen and never touched again - even after HA purges
it, the archive keeps it. Everything downstream reads from the archive.
Compressed, a full day is ~400 KB (measured): ~150 MB a year, fine on the Pi's SD card.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .fsutil import atomic_write_text
from .ha_client import HAClient
from .history import Sample, Series, _finish, _to_float, _to_local, fetch_history

ROOT = Path("data/archive")
POWER_DIR = ROOT / "power"
PRICE_DIR = ROOT / "prices"
MANIFEST = ROOT / "manifest.json"
HA_WINDOW_DAYS = 14  # the recorder's purge_keep_days
FREEZE_AFTER_DAYS = 2  # re-fetch recent days to catch late rows, then freeze


def _manifest() -> dict:
    if MANIFEST.is_file():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"days": {}}


def _write_gz(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Atomic: write the gzip to a temp name, then replace (a crash leaves the old file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    tmp.replace(path)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min).astimezone()
    return start, datetime.combine(day + timedelta(days=1), time.min).astimezone()


def sync(client: HAClient, *, verbose: bool = False) -> dict:
    """Copy every day HA still has into the archive; freeze days once settled."""
    cfg = client.config
    power_ids = cfg.power_entities
    price_id = cfg.entity("SENSOR_NORDPOOL")
    manifest = _manifest()
    today = datetime.now().astimezone().date()
    written, frozen_skipped = [], 0

    for back in range(HA_WINDOW_DAYS, -1, -1):
        day = today - timedelta(days=back)
        key = day.isoformat()
        entry = manifest["days"].get(key, {})
        if entry.get("frozen"):
            frozen_skipped += 1
            continue
        start, end = _day_bounds(day)
        end = min(end, datetime.now().astimezone())
        series, _ = fetch_history(client, [*power_ids, price_id], start, end)
        power_rows = [[eid, when.isoformat(), f"{value:g}"]
                      for eid in power_ids for when, value in series.get(eid, [])]
        price_rows = [[when.isoformat(), f"{value:g}"] for when, value in series.get(price_id, [])]
        if not power_rows:
            continue  # nothing recorded that day (before power recording began)
        power_rows.sort(key=lambda r: r[1])
        _write_gz(POWER_DIR / f"{key}.csv.gz", ["entity_id", "last_updated", "state"], power_rows)
        _write_gz(PRICE_DIR / f"{key}.csv.gz", ["last_updated", "state"], price_rows)
        manifest["days"][key] = {
            "power_rows": len(power_rows),
            "price_rows": len(price_rows),
            "first": power_rows[0][1],
            "last": power_rows[-1][1],
            "written": datetime.now().astimezone().isoformat(timespec="seconds"),
            "frozen": back >= FREEZE_AFTER_DAYS,
        }
        written.append(key)

    manifest["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
    if verbose:
        days = sorted(manifest["days"])
        print(f"ARCHIVE    {len(days)} days stored ({days[0]} .. {days[-1]}); refreshed {len(written)}, "
              f"{frozen_skipped} frozen and untouched  -> {ROOT}")
    return manifest


def days_available() -> list[str]:
    return sorted(_manifest()["days"])


def load(start: date, end: date, entity_ids: list[str], price: bool = True) -> tuple[Series, list[Sample]]:
    """Read archived days [start, end] -> (power series, price samples). Spec 3.5 rules apply."""
    wanted = set(entity_ids)
    raw: dict[str, list[Sample]] = {}
    prices: list[Sample] = []
    day = start
    while day <= end:
        power_file = POWER_DIR / f"{day.isoformat()}.csv.gz"
        if power_file.is_file():
            with gzip.open(power_file, "rt", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    if row["entity_id"] not in wanted:
                        continue
                    when, value = _to_local(row["last_updated"]), _to_float(row["state"])
                    if when is not None and value is not None:
                        raw.setdefault(row["entity_id"], []).append((when, value))
        price_file = PRICE_DIR / f"{day.isoformat()}.csv.gz"
        if price and price_file.is_file():
            with gzip.open(price_file, "rt", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    when, value = _to_local(row["last_updated"]), _to_float(row["state"])
                    if when is not None and value is not None:
                        prices.append((when, value))
        day += timedelta(days=1)
    return _finish(raw), sorted(prices)
