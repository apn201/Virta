"""Read-only Home Assistant REST client (spec 11).

Local LAN only - `http://<haos-ip>:8123/api/`, long-lived token from env. The
runtime READS; every HA config change or bulk history export goes through
OpenClaw, never through this code.

Standard library only (urllib), deliberately: this runs on a Pi3 alongside the
detector and the console, and a dependency-free HTTP GET is all it needs.

Spec 3.5 parsing rules are enforced at the edge of the system, here, so nothing
downstream ever sees a string where it expects a number:
  - `unavailable` / `unknown` / empty states are SKIPPED, never float()-ed.
  - timestamps are parsed as timezone-aware local datetimes and kept that way.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import HAConfig

# spec 3.5: the three state strings that must never reach float()
UNUSABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})


class HAError(RuntimeError):
    """A Home Assistant read failed, with a message aimed at fixing the setup."""


@dataclass(frozen=True)
class SensorReading:
    """One HA state, parsed. `value` is None when the state was unusable."""

    entity_id: str
    state: str
    value: float | None
    last_updated: datetime | None
    unit: str = ""
    attributes: dict[str, Any] = None  # type: ignore[assignment]

    @property
    def usable(self) -> bool:
        return self.value is not None

    def __str__(self) -> str:
        if self.usable:
            return f"{self.entity_id} = {self.value:g}{(' ' + self.unit) if self.unit else ''}"
        return f"{self.entity_id} = {self.state!r} (unusable)"


def _parse_float(state: str) -> float | None:
    """Spec 3.5: skip unusable states rather than crashing on them."""
    cleaned = (state or "").strip()
    if cleaned.lower() in UNUSABLE_STATES:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_time(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


class HAClient:
    """Minimal read-only client. One GET per entity, or /api/states for all."""

    def __init__(self, config: HAConfig, *, timeout_s: float = 10.0) -> None:
        self.config = config
        self.timeout_s = timeout_s

    def _get(self, path: str) -> Any:
        url = self.config.api_url + path.lstrip("/")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise HAError(
                    f"Home Assistant rejected the token (HTTP 401) at {url}.\n"
                    f"  token: {self.config.redacted_token}\n"
                    "  Create a fresh one in HA: Profile -> Security -> Long-Lived\n"
                    "  Access Tokens, and put it in HA_TOKEN."
                ) from exc
            if exc.code == 404:
                raise HAError(
                    f"Home Assistant has no such endpoint or entity: {url} (HTTP 404).\n"
                    "  Check the entity id against the spec 11 config block - an\n"
                    "  entity renamed in HA shows up exactly like this."
                ) from exc
            raise HAError(f"Home Assistant returned HTTP {exc.code} for {url}: {exc}") from exc
        except urllib.error.URLError as exc:
            raise HAError(
                f"Could not reach Home Assistant at {url}.\n"
                f"  reason: {exc.reason}\n"
                "  Check HA_BASE_URL, that this machine is on the same LAN as HAOS,\n"
                "  and that the REST API is enabled."
            ) from exc
        except socket.timeout as exc:
            raise HAError(f"Home Assistant timed out after {self.timeout_s:g}s at {url}.") from exc
        except json.JSONDecodeError as exc:
            raise HAError(f"Home Assistant returned non-JSON from {url}: {exc}") from exc

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """The ONE write path: POST /api/services/<domain>/<service>.

        Only virta/actions.py calls this, and only for its allowlisted actions
        (speak, the Virta lamp, confirmed turn-offs). Everything else stays read-only.
        """
        url = self.config.api_url + f"services/{domain}/{service}"
        request = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            raise HAError(f"Home Assistant refused {domain}.{service} (HTTP {exc.code}): {exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise HAError(f"Could not reach Home Assistant for {domain}.{service}: {exc.reason}") from exc

    # --- reads --------------------------------------------------------------
    def ping(self) -> str:
        """GET /api/ - confirms URL, token and that the API is up."""
        payload = self._get("")
        return str(payload.get("message", payload))

    def get_state(self, entity_id: str) -> SensorReading:
        payload = self._get(f"states/{entity_id}")
        attributes = payload.get("attributes") or {}
        state = str(payload.get("state", ""))
        return SensorReading(
            entity_id=entity_id,
            state=state,
            value=_parse_float(state),
            last_updated=_parse_time(payload.get("last_updated")),
            unit=str(attributes.get("unit_of_measurement", "")),
            attributes=attributes,
        )

    def get_states(self, entity_ids: list[str]) -> dict[str, SensorReading]:
        return {entity_id: self.get_state(entity_id) for entity_id in entity_ids}

    def power_now(self) -> dict[str, SensorReading]:
        """The four 3EM power sensors (spec 3.1)."""
        return self.get_states(self.config.power_entities)

    def is_dark(self) -> bool:
        """spec 3.3: sun.sun below_horizon is a free darkness flag."""
        return self.get_state(self.config.entity("SENSOR_SUN")).state == "below_horizon"

    def price_now(self) -> tuple[float | None, dict[str, Any]]:
        """Current Nordpool price (c/kWh) and the sensor's attributes (the curve)."""
        reading = self.get_state(self.config.entity("SENSOR_NORDPOOL"))
        return reading.value, (reading.attributes or {})


def parse_price_curve(attributes: dict[str, Any], key: str = "raw_today") -> list[dict[str, Any]]:
    """Parse `raw_today` / `raw_tomorrow` into slots with real datetimes (spec 3.2).

    Each entry is {start, end, value}: ISO timestamps WITH the local offset, value
    in c/kWh. `end` is used for slot duration - never inferred from the gap to the
    next slot. Local offset is kept; no UTC conversion, because the advice talks
    about clock times. Slot count is read from the array, never assumed to be 24.
    """
    slots: list[dict[str, Any]] = []
    for entry in attributes.get(key) or []:
        start = _parse_time(entry.get("start"))
        end = _parse_time(entry.get("end"))
        value = entry.get("value")
        if start is None or value is None:
            continue  # spec 3.5 discipline: skip unusable, do not guess
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        slots.append({"start": start, "end": end, "value": price})
    return slots
