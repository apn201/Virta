"""Device profiles (spec 7): plain JSON files, read by the detector and the prompt.

Profiles are human-editable records, not trained weights. The detector uses only
the hard signature (phase + magnitude + tolerance); context factors are for the
later scoring pass and for Nemotron to read as sentences.

Profiles with `"signature": null` (e.g. status "not_observed") are loaded so the
reasoning layer knows they exist, but never matched against events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PHASES = ("A", "B", "C")


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str
    tier: int
    kind: str  # "step" | "balanced_3phase" | "" when unmatched
    phase: str  # "A" / "B" / "C" / "ABC"
    step_w: float
    tolerance_w: float
    per_phase_step_w: float = 0.0
    per_phase_tolerance_w: float = 0.0
    others_unreliable_while_active: bool = False
    status: str = "active"
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def matchable(self) -> bool:
        return self.kind in ("step", "balanced_3phase") and self.status == "active"


def _from_json(data: dict[str, Any]) -> Profile:
    signature = data.get("signature") or {}
    load_control = data.get("load_control") or {}
    return Profile(
        id=str(data["id"]),
        display_name=str(data.get("display_name", data["id"])),
        tier=int(data.get("tier", 1)),
        kind=str(signature.get("type", "")),
        phase=str(signature.get("phase", "")).upper(),
        step_w=float(signature.get("step_w", 0) or 0),
        tolerance_w=float(signature.get("step_tolerance_w", 0) or 0),
        per_phase_step_w=float(signature.get("per_phase_step_w", 0) or 0),
        per_phase_tolerance_w=float(signature.get("per_phase_tolerance_w", 0) or 0),
        others_unreliable_while_active=bool(load_control.get("others_unreliable_while_active", False)),
        status=str(data.get("status", "active")),
        raw=data,
    )


def load_profiles(directory: str | Path = "profiles") -> list[Profile]:
    folder = Path(directory)
    if not folder.is_dir():
        return []
    profiles = []
    for path in sorted(folder.glob("*.json")):
        profiles.append(_from_json(json.loads(path.read_text(encoding="utf-8"))))
    return profiles
