"""Settings and the metric list.

The app runs in two places, and works out which from its environment:

- As a Home Assistant add-on. Home Assistant supplies an access token and the add-on's
  options, and the database lives in the add-on's /data folder.
- On its own (for development), configured by environment variables / a .env file.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

VALID_KINDS = {"power", "energy", "percent"}

ADDON_OPTIONS_FILE = Path("/data/options.json")
ADDON_METRICS_OVERRIDE = Path("/config/metrics.toml")  # the add-on's own config folder
BUNDLED_METRICS_FILE = Path(__file__).resolve().parents[2] / "config" / "metrics.toml"
BUNDLED_TARIFF_FILE = Path(__file__).resolve().parents[2] / "config" / "tariff.toml"


@dataclass(frozen=True)
class Metric:
    name: str
    entity: str
    kind: str
    label: str
    scale: float = 1.0


def metrics_file() -> Path:
    """Which metric list to use: an explicit one, the add-on's override, or the built-in one."""
    if os.environ.get("METRICS_FILE"):
        return Path(os.environ["METRICS_FILE"])
    if ADDON_METRICS_OVERRIDE.exists():
        return ADDON_METRICS_OVERRIDE
    return BUNDLED_METRICS_FILE


def load_metrics(path: Path | None = None) -> list[Metric]:
    """Read and validate the metric definitions."""
    path = path or metrics_file()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    metrics = [Metric(**item) for item in raw.get("metric", [])]
    if not metrics:
        raise ValueError(f"No [[metric]] entries found in {path}")

    names = [m.name for m in metrics]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"Duplicate metric names in {path}: {sorted(duplicates)}")

    for m in metrics:
        if m.kind not in VALID_KINDS:
            raise ValueError(f"Metric '{m.name}' has unknown kind '{m.kind}'")
    return metrics


@dataclass(frozen=True)
class Settings:
    database_path: Path
    timezone: ZoneInfo
    # Home Assistant connection. Empty url means "don't collect" (dashboard only).
    ha_url: str
    ha_token: str
    ha_websocket_url: str
    poll_seconds: int
    backfill_days: int
    # Readings older than this are thinned to one every five minutes to save space.
    detail_days: int
    # Readings older than this many years are deleted, so the database stops growing.
    keep_years: int
    # If set, only this address may connect (Home Assistant's ingress proxy).
    allowed_client: str | None
    port: int
    # How money is written on the dashboard: the main symbol, and the small unit prices
    # are entered in (pence, cents).
    currency_symbol: str = "£"
    currency_minor: str = "p"
    # Home Assistant sensor showing Axle Energy grid events, if there is one.
    axle_event_entity: str = "sensor.axle_event"
    # What the Sigenergy "smart load 1" port feeds, as shown on the dashboard.
    smart_load_label: str = "Heat pump"
    # True if the EV charger is wired through the smart load port, so its use is taken off
    # the smart load figure. False if the EV charger is on its own circuit.
    ev_on_smart_load: bool = True

    @property
    def collecting(self) -> bool:
        return bool(self.ha_url and self.ha_token)


def _int(value: object, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _text(value: object, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text[:40] or default


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    env = os.environ
    timezone = ZoneInfo(env.get("TZ_NAME", "Europe/London"))
    supervisor_token = env.get("SUPERVISOR_TOKEN", "").strip()

    if supervisor_token:
        # Running as a Home Assistant add-on.
        options: dict = {}
        if ADDON_OPTIONS_FILE.exists():
            options = json.loads(ADDON_OPTIONS_FILE.read_text())
        return Settings(
            database_path=Path(env.get("DATABASE_PATH", "/data/energy.db")),
            timezone=timezone,
            ha_url="http://supervisor/core",
            ha_token=supervisor_token,
            ha_websocket_url="ws://supervisor/core/websocket",
            poll_seconds=_int(options.get("poll_seconds"), 60, 10, 3600),
            backfill_days=_int(options.get("backfill_days"), 10, 0, 30),
            detail_days=_int(options.get("detail_days"), 30, 7, 3650),
            keep_years=_int(options.get("keep_years"), 10, 1, 50),
            allowed_client="172.30.32.2",
            port=8099,
            smart_load_label=_text(options.get("smart_load_label"), "Heat pump"),
            currency_symbol=_text(options.get("currency_symbol"), "£")[:4],
            currency_minor=_text(options.get("currency_minor"), "p")[:4],
            axle_event_entity=str(options.get("axle_event_entity") or "sensor.axle_event").strip(),
            ev_on_smart_load=_flag(options.get("ev_on_smart_load"), True),
        )

    ha_url = env.get("HA_URL", "").strip().rstrip("/")
    return Settings(
        database_path=Path(env.get("DATABASE_PATH", "data/energy.db")),
        timezone=timezone,
        ha_url=ha_url,
        ha_token=env.get("HA_TOKEN", "").strip(),
        ha_websocket_url=env.get("HA_WEBSOCKET_URL", "").strip()
        or (ha_url.replace("http", "ws", 1) + "/api/websocket" if ha_url else ""),
        poll_seconds=_int(env.get("POLL_SECONDS"), 60, 10, 3600),
        backfill_days=_int(env.get("BACKFILL_DAYS"), 10, 0, 30),
        detail_days=_int(env.get("DETAIL_DAYS"), 30, 7, 3650),
        keep_years=_int(env.get("KEEP_YEARS"), 10, 1, 50),
        allowed_client=env.get("ALLOWED_CLIENT") or None,
        port=_int(env.get("PORT"), 8000, 1, 65535),
        smart_load_label=_text(env.get("SMART_LOAD_LABEL"), "Heat pump"),
        currency_symbol=_text(env.get("CURRENCY_SYMBOL"), "£")[:4],
        currency_minor=_text(env.get("CURRENCY_MINOR"), "p")[:4],
        axle_event_entity=(env.get("AXLE_EVENT_ENTITY") or "sensor.axle_event").strip(),
        ev_on_smart_load=_flag(env.get("EV_ON_SMART_LOAD"), True),
    )
