"""Power history: one loader for CSV exports and live HA history (spec 3.5, slice 3).

Both sources produce the same thing - `Series`, a dict of entity_id -> sorted
list of (local datetime, float) - so everything downstream (baseline, stacking
detector, labelling) is source-agnostic.

The spec 3.5 parsing rules live here and nowhere else:
  - `unavailable` / `unknown` / empty states are SKIPPED, never float()-ed.
  - timestamps are irregular; they are kept exactly as recorded, never resampled
    onto a fixed grid. Anything that needs elapsed time uses the real gap.
  - `_energy_cost` entities are dropped outright (dummy data, spec 3.5).
  - light-control Shellys (`switch_*`) are dropped - lights, not loads.

Two facts about the real data that shape this module:
  - HA history records state CHANGES. A flat stretch produces no rows, so a
    long gap means "value held", not "data missing".
  - `sensor.3em_total_power` is a template that updates whenever ANY phase
    updates, so it has ~3x the rows of each channel and its timestamps do not
    line up with theirs. `align_phases` handles that by carrying the last-known
    value of each phase forward, together with how stale it is.
"""

from __future__ import annotations

import csv
import urllib.parse
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .ha_client import UNUSABLE_STATES, HAClient

Sample = tuple[datetime, float]
Series = dict[str, list[Sample]]

DROP_SUFFIXES = ("_energy_cost",)  # spec 3.5
DROP_PREFIXES = ("switch.",)  # spec 3.5: light-control Shellys


def _keep_entity(entity_id: str) -> bool:
    if any(entity_id.endswith(suffix) for suffix in DROP_SUFFIXES):
        return False
    if any(entity_id.startswith(prefix) for prefix in DROP_PREFIXES):
        return False
    return True


def _to_float(state: object) -> float | None:
    text = str(state if state is not None else "").strip()
    if text.lower() in UNUSABLE_STATES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_local(stamp: str) -> datetime | None:
    """Parse any HA timestamp to a timezone-aware LOCAL datetime.

    The HA API returns UTC (`+00:00`); the CSV exports are naive local time.
    Naive stamps are therefore interpreted as local, aware ones converted to
    local - prices and advice talk in local clock time (spec 3.2).
    """
    text = (stamp or "").strip().strip('"')
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone()  # naive -> assumed local; aware -> converted


@dataclass
class LoadReport:
    """What the loader kept and what it skipped - surfaced, not hidden."""

    kept: dict[str, int]
    unusable: dict[str, int]
    dropped_entities: set[str]

    def describe(self) -> str:
        lines = []
        for entity_id in sorted(self.kept):
            skipped = self.unusable.get(entity_id, 0)
            note = f", {skipped} unusable skipped" if skipped else ""
            lines.append(f"  {entity_id:32s} {self.kept[entity_id]:7d} samples{note}")
        if self.dropped_entities:
            lines.append(f"  dropped (spec 3.5): {', '.join(sorted(self.dropped_entities))}")
        return "\n".join(lines)


def _finish(raw: dict[str, list[Sample]]) -> Series:
    """Sort each series and remove exact duplicate timestamps (keep the last)."""
    series: Series = {}
    for entity_id, samples in raw.items():
        samples.sort(key=lambda s: s[0])
        deduped: list[Sample] = []
        for sample in samples:
            if deduped and deduped[-1][0] == sample[0]:
                deduped[-1] = sample
            else:
                deduped.append(sample)
        series[entity_id] = deduped
    return series


# --- sources ----------------------------------------------------------------
def load_csv(path: str | Path, entity_ids: Iterable[str] | None = None) -> tuple[Series, LoadReport]:
    """Load a long-format states export: `entity_id,last_updated,state`."""
    wanted = set(entity_ids) if entity_ids else None
    raw: dict[str, list[Sample]] = {}
    unusable: dict[str, int] = {}
    dropped: set[str] = set()

    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            entity_id = (row.get("entity_id") or "").strip()
            if not entity_id:
                continue
            if not _keep_entity(entity_id):
                dropped.add(entity_id)
                continue
            if wanted is not None and entity_id not in wanted:
                continue
            when = _to_local(row.get("last_updated") or row.get("last_changed") or "")
            value = _to_float(row.get("state"))
            if when is None or value is None:
                unusable[entity_id] = unusable.get(entity_id, 0) + 1
                continue
            raw.setdefault(entity_id, []).append((when, value))

    series = _finish(raw)
    return series, LoadReport({k: len(v) for k, v in series.items()}, unusable, dropped)


def fetch_history(
    client: HAClient,
    entity_ids: Iterable[str],
    start: datetime,
    end: datetime | None = None,
) -> tuple[Series, LoadReport]:
    """Pull recorder history straight from HA's REST API (read-only).

    Uses `minimal_response` + `no_attributes`: after the first row of each
    entity, rows carry only `state` and `last_changed`, which is all we need
    and keeps a multi-day pull to a few seconds on the LAN.
    """
    ids = list(entity_ids)
    end = end or datetime.now().astimezone()
    query = urllib.parse.urlencode(
        {
            "filter_entity_id": ",".join(ids),
            "end_time": end.isoformat(),
            "minimal_response": "",
            "no_attributes": "",
        }
    )
    payload = client._get(f"history/period/{urllib.parse.quote(start.isoformat())}?{query}")

    raw: dict[str, list[Sample]] = {}
    unusable: dict[str, int] = {}
    for rows in payload or []:
        if not rows:
            continue
        entity_id = rows[0].get("entity_id", "")
        for row in rows:
            when = _to_local(row.get("last_changed") or row.get("last_updated") or "")
            value = _to_float(row.get("state"))
            if when is None or value is None:
                unusable[entity_id] = unusable.get(entity_id, 0) + 1
                continue
            raw.setdefault(entity_id, []).append((when, value))

    series = _finish(raw)
    return series, LoadReport({k: len(v) for k, v in series.items()}, unusable, set())


def save_csv(series: Series, path: str | Path) -> None:
    """Cache a Series in the same long format as the HA exports."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [(when, entity_id, value) for entity_id, samples in series.items() for when, value in samples]
    rows.sort(key=lambda r: r[0])
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["entity_id", "last_updated", "state"])
        for when, entity_id, value in rows:
            writer.writerow([entity_id, when.isoformat(), f"{value:g}"])


# --- derived ----------------------------------------------------------------
def power_from_energy(samples: list[Sample], *, max_gap_s: float = 600.0) -> list[Sample]:
    """Spec 3.1 fallback: W from cumulative kWh, using ACTUAL elapsed time.

    W = (E[i] - E[i-1]) / dt_seconds * 3600 * 1000, guarding 0 < dt < 600 and
    dE >= 0 (a meter reset or a backwards step is skipped, not negated). Only
    for windows that have energy but no power - prefer the real power sensors.
    """
    out: list[Sample] = []
    for (t0, e0), (t1, e1) in zip(samples, samples[1:]):
        dt = (t1 - t0).total_seconds()
        de = e1 - e0
        if not (0 < dt < max_gap_s) or de < 0:
            continue
        out.append((t1, de / dt * 3600 * 1000))
    return out


@dataclass(frozen=True)
class Frame:
    """One instant of house state, stamped at a TOTAL update.

    Phases are the last-known value at or before `when`, with their age in
    seconds, because the channels update independently of the total. A stale
    phase value is honest information; a pretended-aligned one is not.
    """

    when: datetime
    total: float
    a: float | None
    b: float | None
    c: float | None
    age_a: float | None
    age_b: float | None
    age_c: float | None

    @property
    def phases(self) -> tuple[float | None, float | None, float | None]:
        return (self.a, self.b, self.c)


def _last_known(samples: list[Sample], times: list[datetime], when: datetime) -> tuple[float | None, float | None]:
    idx = bisect_right(times, when) - 1
    if idx < 0:
        return None, None
    at, value = samples[idx]
    return value, (when - at).total_seconds()


# The 3EM pushes all three phases within a few ms; the total template then fires
# once per phase update. Updates closer together than this are one push.
BURST_WINDOW_S = 1.0


def collapse_bursts(samples: list[Sample], window_s: float = BURST_WINDOW_S) -> list[Sample]:
    """Keep only the LAST sample of each burst.

    Measured on the real data: `sensor.3em_total_power` is exactly A+B+C
    (|error| 0.00 W at p95), recomputed after EACH phase update. A 3EM push
    therefore yields three totals within ~10 ms, and the first two are
    mixed-epoch partial sums (new A + stale B + stale C). Only the last one is
    a real reading; keeping the others fabricates stair-steps and triple-counts
    every event.
    """
    out: list[Sample] = []
    for sample in samples:
        if out and (sample[0] - out[-1][0]).total_seconds() < window_s:
            out[-1] = sample
        else:
            out.append(sample)
    return out


def align_phases(series: Series, total_id: str, phase_ids: tuple[str, str, str]) -> list[Frame]:
    """One Frame per 3EM push, with each phase's last-known value carried forward."""
    total = collapse_bursts(series.get(total_id, []))
    phase_samples = [series.get(pid, []) for pid in phase_ids]
    phase_times = [[t for t, _ in samples] for samples in phase_samples]

    frames: list[Frame] = []
    for when, value in total:
        (a, age_a), (b, age_b), (c, age_c) = (
            _last_known(samples, times, when) for samples, times in zip(phase_samples, phase_times)
        )
        frames.append(Frame(when, value, a, b, c, age_a, age_b, age_c))
    return frames


def interval_stats(samples: list[Sample]) -> dict[str, float]:
    """Real sample spacing - spec 3.1 says ~15s steady, sub-second on change."""
    gaps = sorted(
        (b - a).total_seconds() for (a, _), (b, _) in zip(samples, samples[1:]) if b > a
    )
    if not gaps:
        return {}
    pick = lambda q: gaps[min(len(gaps) - 1, int(len(gaps) * q))]  # noqa: E731
    return {"p05": pick(0.05), "median": pick(0.5), "p95": pick(0.95), "max": gaps[-1]}


def span(samples: list[Sample]) -> timedelta:
    return samples[-1][0] - samples[0][0] if len(samples) > 1 else timedelta(0)
