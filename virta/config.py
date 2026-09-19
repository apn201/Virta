"""Configuration for Virta, read from the environment only (spec sections 8, 11).

Nothing is hardcoded except documented spec defaults. The same code runs on the
dev PC and on the Pi3 by changing env vars only, so every deployment-specific
value lands here and nowhere else.

Three config blocks:
  NebiusConfig - the cloud reasoning endpoint (spec 2, 6, 11).
  HAConfig     - the local Home Assistant REST API + entity ids (spec 11).
  CostConfig   - cloud call safeguards: tiers, debounce, caps (spec 8).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- spec defaults ----------------------------------------------------------
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_TIMEOUT_S = 60.0
# Right-sized models per reasoning tier (the track text: "Reach for Nemotron 3 Ultra
# when you need serious reasoning, and let Nano or Super handle the fast, everyday
# calls"). NEBIUS_MODEL_ID stays the fallback for anything unset.
DEFAULT_MODEL_LIVE = "nvidia/nemotron-3-super-120b-a12b"  # Tier 1: the butler; Nano was flatter (see FEEDBACK.md)
DEFAULT_MODEL_LABELS = "nvidia/nemotron-3-super-120b-a12b"  # discovery guesses
DEFAULT_MODEL_NIGHTLY = "nvidia/Nemotron-3-Ultra-550b-a55b"  # Tier 2: once a night, deepest
DEFAULT_MAX_TOKENS = 2048  # reasoning tokens come out of this budget too

DEFAULT_HA_BASE_URL = "http://192.168.86.33:8123"  # spec 11, confirmed LAN address
DEFAULT_POLL_INTERVAL_S = 15.0  # spec 11: matches the 3EM's ~10-15s update rate

# spec 8: coarse price tiers so small price wiggles do not trigger cloud calls.
# Builder-supplied boundaries in c/kWh; HA itself uses a moving average-based
# threshold, so treat these as a fixed-mode starting point, not a final answer.
DEFAULT_PRICE_CHEAP_MAX_C = 5.0
DEFAULT_PRICE_NORMAL_MAX_C = 8.0
DEFAULT_PRICE_EXPENSIVE_MAX_C = 12.0

DEFAULT_DEBOUNCE_S = 8.0  # spec 8: TUNABLE ~5-10s settle window
# Eased 2026-09-18 (builder): reasoning is the product, not a cost to minimise -
# but the daily cap stays so credits survive until the demo.
DEFAULT_MAX_CALLS_PER_HOUR = 30
DEFAULT_MAX_CALLS_PER_DAY = 250
DEFAULT_HEARTBEAT_MINUTES = 15  # the butler speaks up every quarter hour even when nothing changes
# --demo: reasoning is the show. More often, same daily ceiling.
DEMO_MAX_CALLS_PER_HOUR = 60
DEMO_HEARTBEAT_MINUTES = 5
DEMO_DEBOUNCE_S = 5.0
DEFAULT_USAGE_STATE_PATH = "var/cloud_usage.json"
DEFAULT_KILL_SWITCH_PATH = "var/CLOUD_OFF"

# spec 11 config block - entity ids, overridable per deployment.
DEFAULT_ENTITIES = {
    "SENSOR_PHASE_A": "sensor.3em_channel_a_power",
    "SENSOR_PHASE_B": "sensor.3em_channel_b_power",
    "SENSOR_PHASE_C": "sensor.3em_channel_c_power",
    "SENSOR_TOTAL": "sensor.3em_total_power",
    "SENSOR_NORDPOOL": "sensor.nordpool_kwh_fi_eur_3_10_0255",
    "SENSOR_TEMP_OUT": "sensor.current_temperature",  # outdoor, despite the name
    "SENSOR_TEMP_IN": "sensor.temperature_average",
    "SENSOR_SUN": "sun.sun",
}


class ConfigError(RuntimeError):
    """The environment is missing something we cannot run without."""


# --- env plumbing -----------------------------------------------------------
def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Dependency-free and non-overriding: real env vars always win, so the Pi can
    be configured with systemd/profile env and ignore any stray .env.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use 1/0, true/false, on/off)")


def _redact(secret: str, label: str) -> str:
    if not secret:
        return "<unset>"
    if len(secret) <= 8:
        return f"{label} set (short)"
    return f"{label} set ({secret[:4]}...{secret[-4:]}, {len(secret)} chars)"


# --- Nebius -----------------------------------------------------------------
@dataclass(frozen=True)
class NebiusConfig:
    """Everything needed to reach Nebius Token Factory (spec 2, 6)."""

    api_key: str
    base_url: str
    model_id: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_live: str = DEFAULT_MODEL_LIVE
    model_labels: str = DEFAULT_MODEL_LABELS
    model_nightly: str = DEFAULT_MODEL_NIGHTLY

    def __repr__(self) -> str:  # never let the key reach a log or traceback
        return (
            f"NebiusConfig(base_url={self.base_url!r}, model_id={self.model_id!r}, "
            f"timeout_s={self.timeout_s!r}, max_tokens={self.max_tokens!r}, "
            f"api_key={self.redacted_key!r})"
        )

    @property
    def redacted_key(self) -> str:
        return _redact(self.api_key, "key")

    def describe(self) -> str:
        return (
            "NEBIUS (cloud reasoning)\n"
            f"  base_url : {self.base_url}\n"
            f"  model_id : {self.model_id}  (fallback)\n"
            f"  tiers    : live {self.model_live}\n"
            f"             labels {self.model_labels}\n"
            f"             nightly {self.model_nightly}\n"
            f"  api_key  : {self.redacted_key}\n"
            f"  timeout  : {self.timeout_s:g}s   max_tokens: {self.max_tokens}"
        )


def load_nebius_config(*, dotenv_path: str | Path = ".env") -> NebiusConfig:
    """Build NebiusConfig from env, with actionable errors for the usual snags."""
    load_dotenv(dotenv_path)

    api_key = _env_str("NEBIUS_API_KEY")
    if not api_key:
        raise ConfigError(
            "NEBIUS_API_KEY is not set.\n"
            "  Get a key from the Nebius Token Factory console, then either set it\n"
            "  in the shell or add NEBIUS_API_KEY=<your key> to .env next to this\n"
            "  project (copy .env.example to .env to start)."
        )

    model_id = _env_str("NEBIUS_MODEL_ID")
    if not model_id:
        raise ConfigError(
            "NEBIUS_MODEL_ID is not set.\n"
            "  Virta does not guess a model id - take the exact string from the\n"
            "  Token Factory catalog/Playground (an nvidia/nemotron-* id) and set\n"
            "  NEBIUS_MODEL_ID in the shell or in .env."
        )

    base_url = _env_str("NEBIUS_BASE_URL", DEFAULT_NEBIUS_BASE_URL)
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"NEBIUS_BASE_URL={base_url!r} does not look like a URL "
            f"(expected something like {DEFAULT_NEBIUS_BASE_URL})."
        )

    return NebiusConfig(
        api_key=api_key,
        base_url=base_url,
        model_id=model_id,
        timeout_s=_env_float("NEBIUS_TIMEOUT_S", DEFAULT_TIMEOUT_S),
        max_tokens=_env_int("NEBIUS_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        model_live=_env_str("NEBIUS_MODEL_LIVE", DEFAULT_MODEL_LIVE),
        model_labels=_env_str("NEBIUS_MODEL_LABELS", DEFAULT_MODEL_LABELS),
        model_nightly=_env_str("NEBIUS_MODEL_NIGHTLY", DEFAULT_MODEL_NIGHTLY),
    )


# --- Home Assistant ---------------------------------------------------------
@dataclass(frozen=True)
class HAConfig:
    """Local Home Assistant REST access + the entity ids we read (spec 11)."""

    base_url: str
    token: str
    entities: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENTITIES))
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S

    def __repr__(self) -> str:
        return (
            f"HAConfig(base_url={self.base_url!r}, token={self.redacted_token!r}, "
            f"poll_interval_s={self.poll_interval_s!r})"
        )

    @property
    def redacted_token(self) -> str:
        return _redact(self.token, "token")

    @property
    def api_url(self) -> str:
        """Base URL with the /api/ suffix, however the env var was written."""
        root = self.base_url.rstrip("/")
        if root.endswith("/api"):
            return root + "/"
        return root + "/api/"

    def entity(self, key: str) -> str:
        try:
            return self.entities[key]
        except KeyError as exc:
            raise ConfigError(f"No entity configured for {key!r}") from exc

    @property
    def power_entities(self) -> list[str]:
        keys = ("SENSOR_PHASE_A", "SENSOR_PHASE_B", "SENSOR_PHASE_C", "SENSOR_TOTAL")
        return [self.entity(k) for k in keys]

    def describe(self) -> str:
        lines = [
            "HOME ASSISTANT (local, read-only over LAN)",
            f"  api_url  : {self.api_url}",
            f"  token    : {self.redacted_token}",
            f"  poll     : every {self.poll_interval_s:g}s",
            "  entities :",
        ]
        for key, default in DEFAULT_ENTITIES.items():
            value = self.entities.get(key, "<unset>")
            marker = " " if value == default else "*"
            lines.append(f"    {marker}{key:16s} {value}")
        return "\n".join(lines)


def load_ha_config(*, dotenv_path: str | Path = ".env", require_token: bool = True) -> HAConfig:
    """Build HAConfig from env. Entity ids default to the spec 11 config block."""
    load_dotenv(dotenv_path)

    base_url = _env_str("HA_BASE_URL") or _env_str("HA_URL") or DEFAULT_HA_BASE_URL
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"HA_BASE_URL={base_url!r} does not look like a URL "
            f"(expected something like {DEFAULT_HA_BASE_URL})."
        )

    token = _env_str("HA_TOKEN")
    if require_token and not token:
        raise ConfigError(
            "HA_TOKEN is not set.\n"
            "  Create one in Home Assistant: Profile -> Security -> Long-Lived\n"
            "  Access Tokens, then add HA_TOKEN=<token> to .env (git-ignored).\n"
            "  Note: a token exported in one shell is NOT visible to other\n"
            "  processes - .env is the reliable way to share it."
        )

    entities = {key: _env_str(key, default) for key, default in DEFAULT_ENTITIES.items()}

    return HAConfig(
        base_url=base_url,
        token=token,
        entities=entities,
        poll_interval_s=_env_float("HA_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S),
    )


# --- cloud cost control -----------------------------------------------------
@dataclass(frozen=True)
class CostConfig:
    """Guardrails for the paid cloud loop (spec 8).

    Every value is config-driven and echoed on startup, because the failure mode
    this prevents - a flapping sensor draining the credit budget overnight - is
    silent until the money is gone.
    """

    cloud_enabled: bool = True
    price_cheap_max_c: float = DEFAULT_PRICE_CHEAP_MAX_C
    price_normal_max_c: float = DEFAULT_PRICE_NORMAL_MAX_C
    price_expensive_max_c: float = DEFAULT_PRICE_EXPENSIVE_MAX_C
    debounce_s: float = DEFAULT_DEBOUNCE_S
    max_calls_per_hour: int = DEFAULT_MAX_CALLS_PER_HOUR
    max_calls_per_day: int = DEFAULT_MAX_CALLS_PER_DAY
    heartbeat_minutes: int = DEFAULT_HEARTBEAT_MINUTES
    daily_spend_ceiling: float = 0.0  # 0 = no ceiling
    cost_per_1m_input_tokens: float = 0.0  # 0 = pricing unknown, spend not tracked
    cost_per_1m_output_tokens: float = 0.0
    usage_state_path: str = DEFAULT_USAGE_STATE_PATH
    kill_switch_path: str = DEFAULT_KILL_SWITCH_PATH

    @property
    def spend_tracking_enabled(self) -> bool:
        return self.cost_per_1m_input_tokens > 0 or self.cost_per_1m_output_tokens > 0

    def describe(self) -> str:
        heartbeat = f"every {self.heartbeat_minutes} min" if self.heartbeat_minutes else "off"
        ceiling = f"{self.daily_spend_ceiling:g}/day" if self.daily_spend_ceiling else "none"
        spend = (
            f"in {self.cost_per_1m_input_tokens:g} / out "
            f"{self.cost_per_1m_output_tokens:g} per 1M tokens"
            if self.spend_tracking_enabled
            else "not tracked (token prices unset)"
        )
        return (
            "COST CONTROL (cloud calls)\n"
            f"  cloud calls  : {'ENABLED' if self.cloud_enabled else 'DISABLED (kill switch)'}\n"
            f"  price tiers  : cheap <{self.price_cheap_max_c:g} | "
            f"normal <{self.price_normal_max_c:g} | expensive <{self.price_expensive_max_c:g} | "
            f"peak >={self.price_expensive_max_c:g} c/kWh\n"
            f"  debounce     : {self.debounce_s:g}s settle window\n"
            f"  hard caps    : {self.max_calls_per_hour}/hour, {self.max_calls_per_day}/day\n"
            f"  heartbeat    : {heartbeat}\n"
            f"  spend        : {spend}   ceiling: {ceiling}\n"
            f"  usage state  : {self.usage_state_path}\n"
            f"  kill switch  : {self.kill_switch_path} (create the file to stop calling)"
        )


def load_cost_config(*, dotenv_path: str | Path = ".env") -> CostConfig:
    load_dotenv(dotenv_path)

    cheap = _env_float("PRICE_CHEAP_MAX_C", DEFAULT_PRICE_CHEAP_MAX_C)
    normal = _env_float("PRICE_NORMAL_MAX_C", DEFAULT_PRICE_NORMAL_MAX_C)
    expensive = _env_float("PRICE_EXPENSIVE_MAX_C", DEFAULT_PRICE_EXPENSIVE_MAX_C)
    if not cheap < normal < expensive:
        raise ConfigError(
            "Price tier thresholds must increase: "
            f"PRICE_CHEAP_MAX_C ({cheap:g}) < PRICE_NORMAL_MAX_C ({normal:g}) "
            f"< PRICE_EXPENSIVE_MAX_C ({expensive:g})."
        )

    config = CostConfig(
        cloud_enabled=_env_bool("VIRTA_CLOUD_ENABLED", True),
        price_cheap_max_c=cheap,
        price_normal_max_c=normal,
        price_expensive_max_c=expensive,
        debounce_s=_env_float("CLOUD_DEBOUNCE_S", DEFAULT_DEBOUNCE_S),
        max_calls_per_hour=_env_int("MAX_CLOUD_CALLS_PER_HOUR", DEFAULT_MAX_CALLS_PER_HOUR),
        max_calls_per_day=_env_int("MAX_CLOUD_CALLS_PER_DAY", DEFAULT_MAX_CALLS_PER_DAY),
        heartbeat_minutes=_env_int("CLOUD_HEARTBEAT_MINUTES", DEFAULT_HEARTBEAT_MINUTES),
        daily_spend_ceiling=_env_float("DAILY_SPEND_CEILING", 0.0),
        cost_per_1m_input_tokens=_env_float("COST_PER_1M_INPUT_TOKENS", 0.0),
        cost_per_1m_output_tokens=_env_float("COST_PER_1M_OUTPUT_TOKENS", 0.0),
        usage_state_path=_env_str("CLOUD_USAGE_STATE_PATH", DEFAULT_USAGE_STATE_PATH),
        kill_switch_path=_env_str("CLOUD_KILL_SWITCH_PATH", DEFAULT_KILL_SWITCH_PATH),
    )

    if config.max_calls_per_hour < 1 or config.max_calls_per_day < 1:
        raise ConfigError(
            "MAX_CLOUD_CALLS_PER_HOUR and MAX_CLOUD_CALLS_PER_DAY must be >= 1 "
            "(set VIRTA_CLOUD_ENABLED=0 to disable cloud calls instead)."
        )
    return config


# --- aggregate --------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    nebius: NebiusConfig
    ha: HAConfig
    cost: CostConfig

    def describe(self) -> str:
        return "\n\n".join([self.nebius.describe(), self.ha.describe(), self.cost.describe()])


def load_app_config(
    *, dotenv_path: str | Path = ".env", require_ha_token: bool = True
) -> AppConfig:
    """Load every config block. Startup echo: config.describe()."""
    return AppConfig(
        nebius=load_nebius_config(dotenv_path=dotenv_path),
        ha=load_ha_config(dotenv_path=dotenv_path, require_token=require_ha_token),
        cost=load_cost_config(dotenv_path=dotenv_path),
    )
