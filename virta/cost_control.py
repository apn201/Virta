"""Cloud-call safeguards: the gate between the free local loop and the paid one.

Spec section 8. The local loop reads HA every ~15s for free; this module decides
whether a given tick is allowed to spend money on Nemotron. Naive per-tick calling
would be ~5,760 calls/day of identical advice; the situation hash cuts that ~99%
and is better behaviour, because advice should change only when the situation does.

Layers, in the order the gate applies them:
  1. Kill switch        - env flag or a file on disk; stops everything, no exceptions.
  2. Hard caps          - per-hour / per-day call counts and an optional spend ceiling,
                          checked BEFORE every call, persisted across restarts.
  3. Trigger            - situation hash change, tomorrow_valid flip, or heartbeat.
  4. Debounce           - a settle window so one real event is one call.

Fails closed everywhere: any doubt means "do not call, serve the cached verdict".
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .config import CostConfig


class PriceTier(str, Enum):
    """Coarse price buckets (spec 8) - deliberately not the raw c/kWh.

    NEGATIVE is its own tier rather than part of CHEAP: spec 3.2 says a negative
    price means you are paid to consume, which is advice-changing on its own and
    must not be blurred into "cheap".
    """

    NEGATIVE = "negative"
    CHEAP = "cheap"
    NORMAL = "normal"
    EXPENSIVE = "expensive"
    PEAK = "peak"

    def __str__(self) -> str:
        return self.value


def price_tier(price_c_kwh: float, config: CostConfig) -> PriceTier:
    """Bucket a c/kWh price. Thresholds are config-driven (spec 8, 15)."""
    if price_c_kwh < 0:
        return PriceTier.NEGATIVE
    if price_c_kwh < config.price_cheap_max_c:
        return PriceTier.CHEAP
    if price_c_kwh < config.price_normal_max_c:
        return PriceTier.NORMAL
    if price_c_kwh < config.price_expensive_max_c:
        return PriceTier.EXPENSIVE
    return PriceTier.PEAK


@dataclass(frozen=True)
class Situation:
    """What the advice depends on (spec 8).

    The hash is (active appliances, price tier, is_dark). `tomorrow_valid` is
    carried but deliberately NOT hashed - it is a one-shot special trigger when
    it flips true (~13:00 CET, the horizon grew), not a state we compare on.
    """

    active_appliance_ids: frozenset[str] = frozenset()
    tier: PriceTier = PriceTier.NORMAL
    is_dark: bool = False
    tomorrow_valid: bool = False

    @classmethod
    def build(
        cls,
        active_appliance_ids: Iterable[str],
        price_c_kwh: float,
        is_dark: bool,
        config: CostConfig,
        *,
        tomorrow_valid: bool = False,
    ) -> "Situation":
        return cls(
            active_appliance_ids=frozenset(active_appliance_ids),
            tier=price_tier(price_c_kwh, config),
            is_dark=bool(is_dark),
            tomorrow_valid=bool(tomorrow_valid),
        )

    @property
    def key(self) -> tuple[frozenset[str], str, bool]:
        """The situation hash proper - what a change is measured against."""
        return (self.active_appliance_ids, self.tier.value, self.is_dark)

    def describe(self) -> str:
        loads = ", ".join(sorted(self.active_appliance_ids)) or "none"
        return f"[{loads}] tier={self.tier} dark={self.is_dark}"


@dataclass(frozen=True)
class Decision:
    """The gate's answer for one tick."""

    should_call: bool
    reason: str
    trigger: str = ""  # situation_change | tomorrow_valid | heartbeat | first_run
    blocked_by: str = ""  # kill_switch | hourly_cap | daily_cap | spend_ceiling | debounce | no_change

    def __str__(self) -> str:
        mark = "CALL" if self.should_call else "hold"
        return f"{mark}: {self.reason}"


@dataclass
class UsageRecord:
    """Persistent call/spend ledger (spec 8: caps survive a restart).

    A restart must not hand the budget a clean slate - otherwise a crash-loop
    becomes an unmetered spender.
    """

    calls: list[str] = field(default_factory=list)  # ISO timestamps, local tz
    spend_by_day: dict[str, float] = field(default_factory=dict)  # YYYY-MM-DD -> cost
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "spend_by_day": self.spend_by_day,
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "UsageRecord":
        return cls(
            calls=[str(t) for t in data.get("calls", [])],
            spend_by_day={str(k): float(v) for k, v in (data.get("spend_by_day") or {}).items()},
            total_calls=int(data.get("total_calls", 0)),
            total_input_tokens=int(data.get("total_input_tokens", 0)),
            total_output_tokens=int(data.get("total_output_tokens", 0)),
        )


def _now() -> datetime:
    """Local, timezone-aware. Prices are local time (spec 3.2); so are our days."""
    return datetime.now().astimezone()


class CloudCallGate:
    """Decides whether a cloud call may happen, and records it when it does.

    Usage in the live loop:

        decision = gate.evaluate(situation)
        if decision.should_call:
            result = chat(...)
            gate.record_call(result.usage)
            gate.set_verdict(verdict)
        console.show(gate.cached_verdict)
    """

    def __init__(self, config: CostConfig, *, clock=_now) -> None:
        self.config = config
        self._clock = clock
        self._usage_path = Path(config.usage_state_path)
        self._kill_path = Path(config.kill_switch_path)
        # The live loop, the advice worker and the nightly thread share one gate.
        self._lock = threading.RLock()
        self.usage = self._load_usage()

        self._last_situation: Situation | None = None
        self._pending_since: datetime | None = None
        self._pending_situation: Situation | None = None
        self._last_call_at: datetime | None = self._parse_last_call()
        self._last_tomorrow_valid: bool | None = None
        self.cached_verdict: str = ""
        self.cached_verdict_at: datetime | None = None

    # --- persistence --------------------------------------------------------
    def _load_usage(self) -> UsageRecord:
        try:
            raw = json.loads(self._usage_path.read_text(encoding="utf-8"))
            return UsageRecord.from_json(raw)
        except FileNotFoundError:
            return UsageRecord()
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            # A corrupt ledger must not hand out a fresh budget. Keep the file
            # for inspection and start from an empty record only in memory.
            return UsageRecord()

    def _merge_disk(self) -> None:
        """Fold in calls another writer recorded (the nightly thread, a CLI run).

        The caps count call TIMESTAMPS, so the union of both lists is the truth;
        token/spend totals take the larger value (informational, never lower).
        """
        try:
            disk = UsageRecord.from_json(json.loads(self._usage_path.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, OSError):
            return
        merged = sorted(set(self.usage.calls) | set(disk.calls), key=lambda s: _safe_parse(s) or datetime.min.astimezone())
        self.usage.calls = merged
        self.usage.total_calls = max(self.usage.total_calls, disk.total_calls, len(merged))
        self.usage.total_input_tokens = max(self.usage.total_input_tokens, disk.total_input_tokens)
        self.usage.total_output_tokens = max(self.usage.total_output_tokens, disk.total_output_tokens)
        for day, spent in disk.spend_by_day.items():
            self.usage.spend_by_day[day] = max(self.usage.spend_by_day.get(day, 0.0), spent)

    def _save_usage(self) -> None:
        """Atomic write - a half-written ledger on a Pi power cut is worse than none.

        Merges with the file first so a concurrent writer's calls survive.
        Raises if the ledger truly cannot be written: silently losing the record
        of a call would let the caps under-count (fail closed).
        """
        from .fsutil import atomic_write_text

        with self._lock:
            self._merge_disk()
            # trim AFTER the merge, or the merge re-adds what was trimmed and the
            # ledger grows forever; two days of timestamps is all the caps need
            stamps = [(_safe_parse(s), s) for s in self.usage.calls]
            newest = max((t for t, _ in stamps if t), default=None)
            if newest:
                cutoff = newest - timedelta(days=2)
                self.usage.calls = [s for t, s in stamps if t is None or t >= cutoff]
            atomic_write_text(self._usage_path, json.dumps(self.usage.to_json(), indent=2))

    def _parse_last_call(self) -> datetime | None:
        for stamp in reversed(self.usage.calls):
            try:
                return datetime.fromisoformat(stamp)
            except ValueError:
                continue
        return None

    # --- accounting ---------------------------------------------------------
    def _recent_calls(self, since: datetime) -> int:
        count = 0
        for stamp in self.usage.calls:
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when >= since:
                count += 1
        return count

    def calls_last_hour(self, now: datetime | None = None) -> int:
        now = now or self._clock()
        return self._recent_calls(now - timedelta(hours=1))

    def calls_today(self, now: datetime | None = None) -> int:
        now = now or self._clock()
        today = now.date().isoformat()
        count = 0
        for stamp in self.usage.calls:
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.date().isoformat() == today:
                count += 1
        return count

    def spend_today(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        return self.usage.spend_by_day.get(now.date().isoformat(), 0.0)

    def cost_of(self, usage: dict[str, Any]) -> float:
        """Cost of one call from its token usage. 0 when token prices are unset."""
        if not self.config.spend_tracking_enabled:
            return 0.0
        prompt = float(usage.get("prompt_tokens", 0) or 0)
        completion = float(usage.get("completion_tokens", 0) or 0)
        return (
            prompt / 1_000_000 * self.config.cost_per_1m_input_tokens
            + completion / 1_000_000 * self.config.cost_per_1m_output_tokens
        )

    def kill_switch_engaged(self) -> tuple[bool, str]:
        if not self.config.cloud_enabled:
            return True, "VIRTA_CLOUD_ENABLED=0"
        if self._kill_path.exists():
            return True, f"{self._kill_path} exists"
        return False, ""

    # --- the gate -----------------------------------------------------------
    def evaluate(self, situation: Situation, *, now: datetime | None = None) -> Decision:
        """Should we call the cloud for this situation, right now?

        Order matters: the hard stops are checked before the triggers, so a
        capped-out or killed system does no work and spends nothing even while
        the situation churns.
        """
        with self._lock:
            self._merge_disk()  # calls another writer made count too
        now = now or self._clock()

        killed, why = self.kill_switch_engaged()
        if killed:
            self._last_situation = situation
            self._last_tomorrow_valid = situation.tomorrow_valid
            return Decision(False, f"cloud calls disabled ({why})", blocked_by="kill_switch")

        hourly = self.calls_last_hour(now)
        if hourly >= self.config.max_calls_per_hour:
            return Decision(
                False,
                f"hourly cap reached ({hourly}/{self.config.max_calls_per_hour}) - serving cached verdict",
                blocked_by="hourly_cap",
            )

        daily = self.calls_today(now)
        if daily >= self.config.max_calls_per_day:
            return Decision(
                False,
                f"daily cap reached ({daily}/{self.config.max_calls_per_day}) - serving cached verdict",
                blocked_by="daily_cap",
            )

        if self.config.daily_spend_ceiling:
            spent = self.spend_today(now)
            if spent >= self.config.daily_spend_ceiling:
                return Decision(
                    False,
                    f"daily spend ceiling reached ({spent:.4f}/{self.config.daily_spend_ceiling:g})"
                    " - serving cached verdict",
                    blocked_by="spend_ceiling",
                )

        trigger = self._trigger_for(situation, now)
        if not trigger:
            self._pending_since = None
            self._pending_situation = None
            return Decision(False, "situation unchanged - cached verdict holds", blocked_by="no_change")

        # Heartbeat and the tomorrow_valid flip are not switch-on flurries, so
        # they skip the settle window; a situation change gets debounced.
        if trigger == "situation_change":
            if self._pending_situation is None or self._pending_situation.key != situation.key:
                self._pending_situation = situation
                self._pending_since = now
            waited = (now - self._pending_since).total_seconds()
            if waited < self.config.debounce_s:
                remaining = self.config.debounce_s - waited
                return Decision(
                    False,
                    f"situation changed, settling ({remaining:.1f}s left of "
                    f"{self.config.debounce_s:g}s debounce)",
                    trigger=trigger,
                    blocked_by="debounce",
                )

        return Decision(True, self._reason_for(trigger, situation), trigger=trigger)

    def allow_housekeeping(self, purpose: str, *, now: datetime | None = None) -> Decision:
        """Gate for occasional non-advice calls (spec 5A labelling pass).

        No situation hash - these are not triggered by the live situation - but
        the kill switch and every hard cap still apply, and the call still counts
        against them once recorded. One budget, however the money is spent.
        """
        with self._lock:
            self._merge_disk()  # calls another writer made count too
        now = now or self._clock()
        killed, why = self.kill_switch_engaged()
        if killed:
            return Decision(False, f"cloud calls disabled ({why})", blocked_by="kill_switch")
        hourly = self.calls_last_hour(now)
        if hourly >= self.config.max_calls_per_hour:
            return Decision(False, f"hourly cap reached ({hourly}/{self.config.max_calls_per_hour})",
                            blocked_by="hourly_cap")
        daily = self.calls_today(now)
        if daily >= self.config.max_calls_per_day:
            return Decision(False, f"daily cap reached ({daily}/{self.config.max_calls_per_day})",
                            blocked_by="daily_cap")
        if self.config.daily_spend_ceiling and self.spend_today(now) >= self.config.daily_spend_ceiling:
            return Decision(False, "daily spend ceiling reached", blocked_by="spend_ceiling")
        return Decision(True, f"housekeeping: {purpose}", trigger="housekeeping")

    def _trigger_for(self, situation: Situation, now: datetime) -> str:
        if self._last_situation is None:
            return "first_run"
        if situation.key != self._last_situation.key:
            return "situation_change"
        if situation.tomorrow_valid and not self._last_tomorrow_valid:
            return "tomorrow_valid"
        if self.config.heartbeat_minutes:
            last = self._last_call_at
            if last is None:
                return "heartbeat"
            if (now - last) >= timedelta(minutes=self.config.heartbeat_minutes):
                return "heartbeat"
        return ""

    def _reason_for(self, trigger: str, situation: Situation) -> str:
        if trigger == "first_run":
            return f"first evaluation - {situation.describe()}"
        if trigger == "situation_change":
            before = self._last_situation.describe() if self._last_situation else "unknown"
            return f"situation changed: {before} -> {situation.describe()}"
        if trigger == "tomorrow_valid":
            return "tomorrow's prices published - horizon grew"
        return f"heartbeat ({self.config.heartbeat_minutes} min since last call)"

    # --- bookkeeping --------------------------------------------------------
    def _record_call_unlocked(
        self,
        usage: dict[str, Any] | None = None,
        *,
        situation: Situation | None = None,
        now: datetime | None = None,
    ) -> float:
        """Record that a call happened. Call this even if the call FAILED.

        A failed call still cost latency and may have cost tokens; more
        importantly, counting only successes lets an erroring endpoint be
        retried without limit.
        """
        now = now or self._clock()
        usage = usage or {}

        self.usage.calls.append(now.isoformat())
        self.usage.total_calls += 1
        self.usage.total_input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.usage.total_output_tokens += int(usage.get("completion_tokens", 0) or 0)

        cost = self.cost_of(usage)
        if cost:
            day = now.date().isoformat()
            self.usage.spend_by_day[day] = self.usage.spend_by_day.get(day, 0.0) + cost

        # Keep the ledger bounded - two days of timestamps is all the caps need.
        cutoff = now - timedelta(days=2)
        self.usage.calls = [
            stamp
            for stamp in self.usage.calls
            if _safe_parse(stamp) is None or _safe_parse(stamp) >= cutoff
        ]

        self._last_call_at = now
        if situation is not None:
            self._last_situation = situation
            self._last_tomorrow_valid = situation.tomorrow_valid
        self._pending_since = None
        self._pending_situation = None
        self._save_usage()
        return cost

    def _add_usage_unlocked(self, usage: dict[str, Any] | None, *, now: datetime | None = None) -> float:
        """Attach token usage to a call already counted by record_call().

        The live loop records the call when it is SENT (fail closed: a crash mid-
        call still counts), and the tokens when the answer arrives.
        """
        usage = usage or {}
        now = now or self._clock()
        self.usage.total_input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.usage.total_output_tokens += int(usage.get("completion_tokens", 0) or 0)
        cost = self.cost_of(usage)
        if cost:
            day = now.date().isoformat()
            self.usage.spend_by_day[day] = self.usage.spend_by_day.get(day, 0.0) + cost
        self._save_usage()
        return cost

    def mark_would_call(self, situation: Situation, *, now: datetime | None = None) -> None:
        """Dry run: behave as if a call happened, without the ledger or any spend.

        Used before the advice prompt exists (slice 6) so the trigger cadence can
        be watched live. Nothing is recorded - caps only count real calls.
        """
        self._last_call_at = now or self._clock()
        self._last_situation = situation
        self._last_tomorrow_valid = situation.tomorrow_valid
        self._pending_since = None
        self._pending_situation = None

    def commit(self, situation: Situation) -> None:
        """Accept a situation as the new baseline without recording a call.

        Used when the gate said no but we still want the observed state to
        become the comparison point (e.g. after a kill-switch hold).
        """
        self._last_situation = situation
        self._last_tomorrow_valid = situation.tomorrow_valid

    def set_verdict(self, verdict: str, *, now: datetime | None = None) -> None:
        self.cached_verdict = verdict
        self.cached_verdict_at = now or self._clock()

    def status(self, now: datetime | None = None) -> str:
        now = now or self._clock()
        killed, why = self.kill_switch_engaged()
        bits = [
            f"calls: {self.calls_last_hour(now)}/{self.config.max_calls_per_hour} this hour",
            f"{self.calls_today(now)}/{self.config.max_calls_per_day} today",
            f"{self.usage.total_calls} lifetime",
        ]
        if self.config.spend_tracking_enabled:
            bits.append(f"spend today: {self.spend_today(now):.4f}")
        if killed:
            bits.append(f"KILLED ({why})")
        return " | ".join(bits)


def _safe_parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _locked(method_name: str):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return getattr(self, method_name)(*args, **kwargs)
    wrapper.__doc__ = getattr(CloudCallGate, method_name).__doc__
    return wrapper


# Mutations of the shared ledger hold the gate's lock end to end.
CloudCallGate.record_call = _locked("_record_call_unlocked")
CloudCallGate.add_usage = _locked("_add_usage_unlocked")
