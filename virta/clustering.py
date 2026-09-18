"""Cluster recurring UNKNOWN loads from the event log (spec 5A, local half).

An UNKNOWN load that recurs with consistent size + phase + duration is almost
certainly one real, unlabelled appliance. Noticing that is plain statistics - no
LLM - and it runs on the compact EVENT LOG, never the raw power stream.

Stable identity matters more than clever clustering (spec 5A): a human answer
is attached to a cluster id, so tonight's `C-440W-36m` must be last night's
`C-440W-36m` even after re-clustering. Ids are therefore matched by SIGNATURE
(phase, size, duration) against the registry of known clusters, never by their
position in a file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, pstdev

# TUNABLE
MIN_OCCURRENCES = 3  # fewer than this is not "recurring" yet
SIZE_TOLERANCE = 0.15  # an occurrence joins a cluster within 15% of its size...
SIZE_TOLERANCE_MIN_W = 40.0  # ...or 40 W, whichever is larger
SHORT_S = 120.0  # below this, durations are "short" and compared loosely
DURATION_RATIO = 2.0  # otherwise durations must be within 2x of each other
SESSION_GAP = timedelta(hours=2)  # occurrences closer than this are one session
REGISTRY_SIZE_TOLERANCE = 0.20  # carrying an id forward is a little looser


@dataclass(frozen=True)
class Occurrence:
    """One UNKNOWN load, from its ON to its OFF."""

    start: datetime
    phases: str
    watts: float
    duration_s: float | None
    unreliable: bool


@dataclass
class Cluster:
    phases: str
    members: list[Occurrence] = field(default_factory=list)
    id: str = ""

    @property
    def watts(self) -> float:
        return median(o.watts for o in self.members)

    @property
    def duration_s(self) -> float | None:
        known = [o.duration_s for o in self.members if o.duration_s is not None]
        return median(known) if known else None

    @property
    def sessions(self) -> list[list[Occurrence]]:
        ordered = sorted(self.members, key=lambda o: o.start)
        groups: list[list[Occurrence]] = []
        for occ in ordered:
            if groups and occ.start - groups[-1][-1].start <= SESSION_GAP:
                groups[-1].append(occ)
            else:
                groups.append([occ])
        return groups

    def summary(self) -> dict:
        """The abstract description that goes into the worklist and the prompt.

        Deliberately compact: size, phase, duration, when, how often. This is
        the only form in which household power behaviour leaves the house.
        """
        members = sorted(self.members, key=lambda o: o.start)
        span_days = max(1.0, (members[-1].start - members[0].start).total_seconds() / 86400)
        buckets = {"night (00-06)": 0, "morning (06-10)": 0, "day (10-17)": 0, "evening (17-24)": 0}
        for occ in members:
            h = occ.start.hour
            key = "night (00-06)" if h < 6 else "morning (06-10)" if h < 10 else "day (10-17)" if h < 17 else "evening (17-24)"
            buckets[key] += 1
        sessions = self.sessions
        dur = self.duration_s
        return {
            "id": self.id,
            "phases": self.phases,
            "watts": round(self.watts),
            "watts_spread": round(pstdev([o.watts for o in members])) if len(members) > 1 else 0,
            "duration_min": None if dur is None else round(dur / 60, 1),
            "occurrences": len(members),
            "sessions": len(sessions),
            "pulsing": len(sessions) < len(members) / 2,  # many hits per session = cycling
            "per_session_max": max(len(s) for s in sessions),
            "time_of_day": {k: v for k, v in buckets.items() if v},
            "weekdays": sorted({o.start.strftime("%a") for o in members},
                               key=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index),
            "first_seen": members[0].start.strftime("%d.%m %H:%M"),
            "last_seen": members[-1].start.strftime("%d.%m %H:%M"),
            "span_days": round(span_days, 1),
            "during_ev": sum(1 for o in members if o.unreliable),
            "base_part_off": self.watts < 0,  # part of the always-on base load switched OFF
            "examples": [o.start.strftime("%a %d.%m %H:%M") for o in members[:6]],
        }


def occurrences_from_events(path: str | Path) -> list[Occurrence]:
    """Pair UNKNOWN ON events with their OFF by load_key."""
    ons: dict[int, dict] = {}
    offs: dict[int, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("state") != "UNKNOWN" or not event.get("load_key"):
            continue
        # BASE_OFF starts a NEGATIVE occurrence (a base part switched off) that
        # BASE_ON / BASE_SHIFT ends - clustered exactly like loads switching on.
        if event["kind"] in ("ON", "BASE_OFF"):
            ons[event["load_key"]] = event
        elif event["kind"] in ("OFF", "OFF_RECONCILED", "BASE_ON", "BASE_SHIFT"):
            offs[event["load_key"]] = event
    result = []
    for key, on in ons.items():
        off = offs.get(key)
        result.append(
            Occurrence(
                start=datetime.fromisoformat(on["when"]),
                phases=on["phases"],
                watts=sum(on["delta_w"].values()),
                duration_s=off.get("duration_s") if off else None,
                unreliable=bool(on.get("unreliable")),
            )
        )
    return sorted(result, key=lambda o: o.start)


def _similar_duration(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return True
    if a < SHORT_S and b < SHORT_S:
        return True
    lo, hi = sorted((max(a, 1.0), max(b, 1.0)))
    return hi / lo <= DURATION_RATIO


def cluster(occurrences: list[Occurrence]) -> list[Cluster]:
    """Greedy grouping by phase, size and duration. Returns recurring clusters only."""
    clusters: list[Cluster] = []
    for occ in sorted(occurrences, key=lambda o: (o.phases, o.watts)):
        home = None
        for c in clusters:
            if c.phases != occ.phases:
                continue
            if (occ.watts < 0) != (c.watts < 0):
                continue  # a part switching OFF is never the same cluster as a load switching ON
            tol = max(SIZE_TOLERANCE_MIN_W, SIZE_TOLERANCE * abs(c.watts))
            if abs(occ.watts - c.watts) <= tol and _similar_duration(occ.duration_s, c.duration_s):
                home = c
                break
        if home is None:
            home = Cluster(phases=occ.phases)
            clusters.append(home)
        home.members.append(occ)
    return [c for c in clusters if len(c.members) >= MIN_OCCURRENCES]


def signature_id(phases: str, watts: float, duration_s: float | None) -> str:
    if duration_s is None:
        dur = "open"
    elif duration_s < 90:
        dur = f"{round(duration_s)}s"
    else:
        dur = f"{round(duration_s / 60)}m"
    size = f"{round(abs(watts) / 10) * 10:.0f}W"
    return f"{phases}-{'OFF' if watts < 0 else ''}{size}-{dur}"


def assign_ids(clusters: list[Cluster], registry: dict[str, dict]) -> None:
    """Carry ids forward from the registry by signature; mint new ones otherwise."""
    taken: set[str] = set()
    for c in sorted(clusters, key=lambda c: -len(c.members)):
        best, best_err = None, float("inf")
        for cid, entry in registry.items():
            if cid in taken or entry.get("phases") != c.phases:
                continue
            ref_w = float(entry.get("watts", 0))
            if (ref_w < 0) != (c.watts < 0):
                continue
            tol = max(SIZE_TOLERANCE_MIN_W, REGISTRY_SIZE_TOLERANCE * abs(ref_w))
            err = abs(c.watts - ref_w)
            if err <= tol and _similar_duration(c.duration_s, entry.get("duration_s")) and err < best_err:
                best, best_err = cid, err
        if best is None:
            base = signature_id(c.phases, c.watts, c.duration_s)
            best, n = base, 2
            while best in registry or best in taken:
                best, n = f"{base}-{n}", n + 1
        c.id = best
        taken.add(best)
