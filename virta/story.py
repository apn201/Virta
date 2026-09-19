"""The house's story so far - what the butler talks about when nothing needs doing.

Builder decision 2026-09-19: "It's not about saving money. When something can be
shifted he says or acts, but else he makes observations of the data in general,
history vs today, trends, weather."

So the live payload carries a small, computed-at-the-edge story:
  - today so far (kWh, what it cost) vs the same clock time yesterday
  - recent whole days from the local archive (kWh, base load) - the trend
  - the weather: outdoor now vs 24 h ago, today's low/high, indoor alongside

Arithmetic only, all local; the model gets conclusions to talk about, never the
raw power stream (spec 11). Cached for STORY_TTL_S - one HA pull per 10 minutes
at most, cheap enough for the Pi.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import archive
from .ha_client import HAClient, HAError
from .history import fetch_history

STORY_TTL_S = 600
DAILY = Path("data/archive/daily.json")
_cache: dict[str, Any] = {"at": 0.0, "story": None}


def _kwh(samples: list[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    """Energy under a step-held power series between start and end, in kWh."""
    points = [(t, w) for t, w in samples if t <= end]
    if not points:
        return None
    total, last_t, last_w = 0.0, start, None
    for t, w in points:
        if t <= start:
            last_w = w
            continue
        if last_w is not None:
            total += last_w * (t - last_t).total_seconds()
        last_t, last_w = t, w
    if last_w is not None:
        total += last_w * (end - last_t).total_seconds()
    return round(total / 3.6e6, 1)


def _temps(client: HAClient, now: datetime) -> dict[str, Any]:
    cfg = client.config
    out_id, in_id = cfg.entity("SENSOR_TEMP_OUT"), cfg.entity("SENSOR_TEMP_IN")
    series, _ = fetch_history(client, [out_id, in_id], now - timedelta(hours=24), now)
    out, indoor = series.get(out_id, []), series.get(in_id, [])
    weather: dict[str, Any] = {}
    if out:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [v for t, v in out if t >= midnight] or [out[-1][1]]
        weather.update({"outdoor_now_c": round(out[-1][1], 1), "outdoor_24h_ago_c": round(out[0][1], 1),
                        "outdoor_today_low_c": round(min(today), 1), "outdoor_today_high_c": round(max(today), 1)})
    if indoor:
        weather.update({"indoor_now_c": round(indoor[-1][1], 1), "indoor_24h_ago_c": round(indoor[0][1], 1)})
    return weather


def house_story(client: HAClient, now: datetime | None = None) -> dict[str, Any]:
    """The story, cached. Never raises: a missing piece is simply left out."""
    if _cache["story"] is not None and time.monotonic() - _cache["at"] < STORY_TTL_S:
        return _cache["story"]
    now = now or datetime.now().astimezone()
    story: dict[str, Any] = {"as_of": now.strftime("%H:%M")}
    total_id = client.config.entity("SENSOR_TOTAL")
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        series, _ = fetch_history(client, [total_id], midnight, now)
        story["today_so_far_kwh"] = _kwh(series.get(total_id, []), midnight, now)
    except HAError:
        pass
    try:  # the same clock time yesterday, from the local archive (HA as the fallback)
        y = (now - timedelta(days=1)).date()
        series, _ = archive.load(y, y, [total_id], price=False)
        samples = series.get(total_id, [])
        if not samples:
            series, _ = fetch_history(client, [total_id], midnight - timedelta(days=1), now - timedelta(days=1))
            samples = series.get(total_id, [])
        story["yesterday_same_time_kwh"] = _kwh(samples, midnight - timedelta(days=1), now - timedelta(days=1))
    except (HAError, OSError, ValueError):
        pass
    try:  # whole recent days: the trend
        daily = json.loads(DAILY.read_text(encoding="utf-8"))
        days = [(d, v) for d, v in sorted(daily.items()) if not v.get("partial_day")][-7:]
        story["recent_days"] = [{"day": date.fromisoformat(d).strftime("%a %d.%m"), "kwh": v.get("kwh"),
                                 "base_load_w": v.get("base_load_w"),
                                 "biggest": max((v.get("loads_kwh") or {"-": 0}).items(), key=lambda kv: kv[1])[0]}
                                for d, v in days]
        if days:
            story["recent_daily_avg_kwh"] = round(sum(v.get("kwh") or 0 for _, v in days) / len(days), 1)
    except (OSError, ValueError, TypeError):
        pass
    try:
        story["weather"] = _temps(client, now)
    except HAError:
        pass
    _cache.update(at=time.monotonic(), story=story)
    return story
