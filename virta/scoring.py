"""Context scoring - the edge's deterministic first pass (spec 7).

The detector's confidence says how well the SIGNATURE fits (phase + size). This
module adjusts it by CONTEXT, using the plain-language factors in each profile:

  time_of_day      in a typical part of the day -> small boost;
                   outside it -> demotion, and a strong factor flags "unusual"
  correlates_with  a correlated device is already on -> boost, doubled when the
                   profile says `stronger_when: dark` and it is dark
  EV charging      every other load stays flagged unreliable (builder rule)

Factors are statements, not fitted coefficients (spec 7: no Bayesian engine).
`strength` maps to a coarse weight. Context can DEMOTE a good power match - a
sauna-shaped step at 03:00 is surfaced as unusual, not reported as a sauna.
The fuzzier multi-factor reasoning is Nemotron's job (slice 7); this pass only
has to be cheap, explainable and right about the obvious cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .detector import ActiveLoad, DetectionState, LoadState

STRENGTH = {"weak": 0.5, "medium": 1.0, "strong": 2.0}
TYPICAL_BOOST = 0.03
ATYPICAL_PENALTY = 0.10
CORRELATION_BOOST = 0.04
UNUSUAL_BELOW = 0.5  # a context-demoted match under this is flagged unusual
CONF_FLOOR, CONF_CEILING = 0.25, 0.98


def part_of_day(when: datetime) -> str:
    h = when.hour
    return "night" if h < 6 else "morning" if h < 10 else "day" if h < 17 else "evening"


@dataclass(frozen=True)
class Context:
    when: datetime
    is_dark: bool | None = None
    outdoor_c: float | None = None
    indoor_c: float | None = None

    def describe(self) -> str:
        bits = [f"{self.when:%a %H:%M}", part_of_day(self.when)]
        if self.is_dark is not None:
            bits.append("dark" if self.is_dark else "light")
        if self.outdoor_c is not None:
            bits.append(f"out {self.outdoor_c:.1f} C")
        if self.indoor_c is not None:
            bits.append(f"in {self.indoor_c:.1f} C")
        return ", ".join(bits)


@dataclass(frozen=True)
class ScoredLoad:
    load: ActiveLoad
    confidence: float
    reason: str
    unusual: bool = False

    @property
    def unreliable(self) -> bool:
        return self.load.unreliable

    def label(self) -> str:
        if self.load.state is LoadState.UNKNOWN or self.load.base_part_off:
            return self.load.label()
        mark = "?" if self.unusual or self.confidence < UNUSUAL_BELOW else ""
        return f"{self.load.display_name} {self.confidence:.0%}{mark}"


def score_load(load: ActiveLoad, context: Context, active_ids: set[str],
               linked: dict[str, str] | None = None) -> ScoredLoad:
    if load.state is LoadState.UNKNOWN or load.profile is None:
        return ScoredLoad(load, 0.0, load.reason)

    # A second, independent source (spec 13A): if HA controls this device, HA's
    # own on/off state confirms or contradicts what the meter inferred.
    entity = load.profile.raw.get("ha_entity")
    ha_state = (linked or {}).get(entity) if entity else None
    if ha_state == "on":
        return ScoredLoad(load, CONF_CEILING, f"{load.reason}; HA confirms {entity} is on")

    conf = load.confidence
    reasons = [load.reason]
    unusual = False
    now_part = part_of_day(context.when)

    for factor in load.profile.raw.get("context_factors", []) or []:
        weight = STRENGTH.get(str(factor.get("strength", "medium")), 1.0)
        kind = factor.get("factor")
        if kind == "time_of_day":
            typical = {str(t).split(" ")[0] for t in factor.get("typical", [])}  # "morning (06-10)" -> "morning"
            if not typical:
                continue
            if now_part in typical:
                conf += TYPICAL_BOOST * weight
                reasons.append(f"usual {now_part} use")
            else:
                conf -= ATYPICAL_PENALTY * weight
                reasons.append(f"{now_part} is outside its usual {'/'.join(sorted(typical))}")
                if weight >= STRENGTH["strong"]:
                    unusual = True
        elif kind == "correlates_with":
            partners = sorted(set(factor.get("devices", [])) & (active_ids - {load.load_id}))
            if partners:
                boost = CORRELATION_BOOST * weight
                dark_note = ""
                if factor.get("stronger_when") == "dark" and context.is_dark:
                    boost *= 2
                    dark_note = ", stronger in the dark"
                conf += boost
                reasons.append(f"{factor.get('note') or 'correlated'}: {', '.join(partners)} also on{dark_note}")

    if ha_state == "off":  # the meter saw its size, but HA says it is off: something else
        conf *= 0.5
        reasons.append(f"but HA says {entity} is off - probably a different load of the same size")
    conf = max(CONF_FLOOR, min(CONF_CEILING, conf))
    if conf < UNUSUAL_BELOW and load.confidence >= UNUSUAL_BELOW:
        unusual = True  # a good power match that context pulled down: say so
    if unusual:
        reasons.append("flagged unusual - suggestion only, never an action")
    return ScoredLoad(load, conf, "; ".join(reasons), unusual)


def score_state(state: DetectionState, context: Context, linked: dict[str, str] | None = None) -> list[ScoredLoad]:
    """`linked`: HA states of devices profiles are linked to (profile "ha_entity")."""
    active_ids = {l.load_id for l in state.loads if l.state is LoadState.MATCHED}
    return [score_load(l, context, active_ids, linked) for l in state.loads]
