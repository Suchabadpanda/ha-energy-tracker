"""The app: web dashboard and API, with the collector running in the background.

Run with:  python -m energy_tracker
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from functools import lru_cache, wraps
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import (
    billcheck,
    billreader,
    collector,
    devices,
    heating,
    history,
    import_history,
    income,
    live,
    lookup,
    panels,
    performance,
    planner,
    prices,
    roi,
    shift,
    tariff_store,
    weather,
)
from .config import Metric, Settings, load_metrics, load_settings
from .costs import combine
from .db import Database
from .tariff import Schedule, Tariff, build_tariff
from .today import COST_COUNTERS, DEVICE_COUNTERS, cost_since, energy_by_device, local_midnight
from .usage import Counter

log = logging.getLogger("app")

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@lru_cache
def settings() -> Settings:
    return load_settings()


@lru_cache
def database() -> Database:
    return Database(settings().database_path)


@lru_cache
def metrics_by_name() -> dict[str, Metric]:
    return {m.name: m for m in load_metrics()}


def local_timezone():
    return settings().timezone


# Set to have published half-hourly prices fetched straight away (a tariff was just saved).
price_refresh = threading.Event()


def prices_wanted_from() -> datetime:
    """How far back published prices are worth having: to when readings began."""
    first = database().first_time(COST_COUNTERS[0])
    return first or datetime.now(UTC) - timedelta(days=30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the collector alongside the web server, and stop it cleanly on shutdown."""
    stop = threading.Event()
    thread = None
    if settings().collecting:
        metrics = list(metrics_by_name().values())
        thread = threading.Thread(
            target=collector.run,
            args=(settings(), database(), metrics, stop),
            name="collector",
            daemon=True,
        )
        thread.start()
        threading.Thread(
            target=live.run, args=(settings(), metrics, stop), name="live", daemon=True
        ).start()
    else:
        log.warning("No Home Assistant connection configured: showing stored readings only")
    threading.Thread(target=keep_figures_ready, args=(stop,), name="figures", daemon=True).start()
    threading.Thread(
        target=prices.run,
        args=(database(), stop, price_refresh, prices_wanted_from, clear_caches),
        name="prices",
        daemon=True,
    ).start()
    yield
    stop.set()
    _kept.wake.set()  # let the background worker see it is time to stop
    price_refresh.set()
    if thread:
        await asyncio.to_thread(thread.join, 10)


app = FastAPI(title="Energy Tracker", version="0.30.0", lifespan=lifespan)
# The page and its chart data are mostly text: sent compressed, they are a quarter the size.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def only_from_home_assistant(request: Request, call_next):
    """As an add-on, accept connections only from Home Assistant, which handles the login."""
    allowed = settings().allowed_client
    if allowed and (request.client is None or request.client.host != allowed):
        return JSONResponse({"detail": "Open this from the Home Assistant sidebar"}, 403)
    return await call_next(request)


@app.exception_handler(sqlite3.Error)
async def database_error(_: Request, exc: sqlite3.Error) -> JSONResponse:
    log.error("Database error: %s", exc)
    return JSONResponse({"detail": "Database unavailable"}, 503)


def schedule() -> Schedule:
    return tariff_store.load_schedule(database())


def counter_samples(
    names: list[str], since: datetime, bucket_minutes: int, until: datetime | None = None
) -> dict[str, list]:
    """The last reading of each counter in every time bucket from `since` (to `until`)."""
    return database().last_in_buckets(
        names, since, until or datetime.now(UTC) + timedelta(days=1), bucket_minutes * 60
    )


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    # Always fetched fresh, so an update shows up without clearing the browser's cache.
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "collecting": settings().collecting}


@app.get("/api/metrics")
def list_metrics() -> list[dict]:
    """The metrics being collected, with their labels and units."""
    units = {"power": "kW", "energy": "kWh", "percent": "%"}
    return [
        {"name": m.name, "label": m.label, "kind": m.kind, "unit": units[m.kind]}
        for m in metrics_by_name().values()
    ]


# If none of these are in Home Assistant, this is probably not a Sigenergy system.
CORE_METRICS = ("pv_power", "load_power", "grid_power", "battery_power")


@app.get("/api/setup")
def setup() -> dict:
    """Which optional equipment was found, so the dashboard can leave out what is not there."""
    known = metrics_by_name()

    def found(name: str) -> bool:
        return name in known and name not in collector.missing

    return {
        "collecting": settings().collecting,
        "currency": {
            "symbol": settings().currency_symbol,
            "minor": settings().currency_minor,
        },
        "database_mb": round(database().size_bytes() / 1e6, 1),
        "keep_years": settings().keep_years,
        # Until the first poll there is nothing to judge by, so assume the best.
        "sigenergy_found": not collector.polled or any(found(name) for name in CORE_METRICS),
        "smart_load": found("smart_load_power"),
        "smart_load_label": settings().smart_load_label,
        "ev_charger": found("ev_charger_power"),
        "ev_on_smart_load": settings().ev_on_smart_load,
    }


@app.get("/api/latest")
def latest() -> dict[str, dict]:
    """Most recent value of every metric (ignores anything older than a day)."""
    since = datetime.now(UTC) - timedelta(days=1)
    found = database().latest(list(metrics_by_name()), since)
    return {name: {"time": when, "value": value} for name, (when, value) in found.items()}


# What the "right now" tiles show.
LIVE_AFTER = timedelta(minutes=5)


@app.get("/api/live")
def live_readings() -> dict:
    """The newest value of each power and battery-level sensor, straight from Home Assistant.

    These follow the sensors as they change, between the stored readings. A value that has
    not been heard for a few minutes is left out, so the page falls back to what is stored.
    """
    cutoff = datetime.now(UTC) - LIVE_AFTER
    return {
        "live": live.connected,
        "values": {
            name: {"time": when, "value": value}
            for name, (when, value) in live.snapshot().items()
            if when >= cutoff and name in metrics_by_name()
        },
    }


@app.get("/api/series")
def series(
    metric: Annotated[list[str], Query(description="One or more metric names")],
    hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
) -> dict[str, list]:
    """Averaged history for the requested metrics over the last N hours."""
    unknown = sorted(set(metric) - metrics_by_name().keys())
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown metric(s): {', '.join(unknown)}")

    # Aim for roughly 300 points per line whatever the time range.
    bucket_seconds = max(1, hours * 60 // 300) * 60
    since = datetime.now(UTC) - timedelta(hours=hours)
    found = database().series(metric, since, bucket_seconds)
    return {name: [[when, round(value, 4)] for when, value in found[name]] for name in metric}


@app.get("/api/today")
def today() -> dict:
    """Energy used today by device, and what today has cost so far."""
    now = datetime.now(UTC)
    midnight = local_midnight(now, local_timezone())

    # Reaching back a day before midnight lets the midnight value be worked out even if
    # nothing was being collected at midnight itself.
    samples = counter_samples(
        [*DEVICE_COUNTERS, *COST_COUNTERS, devices.SOLAR], midnight - timedelta(days=1), 5
    )
    used = database().latest(["load_energy_today"], midnight).get("load_energy_today")
    cost = cost_since(samples, midnight, now, local_timezone(), schedule())

    return {
        "energy": energy_by_device(
            samples,
            now,
            local_timezone(),
            used[1] if used else None,
            settings().ev_on_smart_load,
        ),
        "cost": cost,
        "by_device": cost_by_device(samples, midnight, now, cost),
    }


def cost_by_device(
    samples: dict[str, list],
    start: datetime,
    end: datetime,
    cost: dict | None,
    gap: timedelta | None = None,
) -> dict | None:
    """The period's import cost shared between the heat pump, the EV charger and the rest
    of the house, by how much of each day's electricity each used.

    Only worth showing where a smart load or an EV charger is fitted. The shares are scaled
    to the import cost shown beside them, so they add up to it exactly.

    Where solar and export are recorded, each part also has a "true cost": its import cost
    plus the export payment given up by using solar. The heat pump's is split into heating
    and hot water where the weather comparison has worked out its hot-water use.
    """
    if not cost or cost.get("import_gbp") is None:
        return None
    counters = {
        name: Counter(samples.get(name, []), *([gap] if gap else []))
        for name in [*devices.COUNTERS, *devices.SOLAR_COUNTERS]
    }
    if not counters[devices.LOAD] or not counters[devices.IMPORT]:
        return None
    if not counters[devices.CIRCUIT] and not counters[devices.EV]:
        return None
    days = devices.daily(
        counters, start, end, local_timezone(), schedule(), settings().ev_on_smart_load
    )
    if not days:
        return None
    worked_out = sum(row.get(f"{part}_gbp", 0.0) for row in days.values() for part in devices.PARTS)
    scale = cost["import_gbp"] / worked_out if worked_out > 0 else 0.0
    valued = any("solar_value_gbp" in row for row in days.values())

    parts = {}
    for part in devices.PARTS:
        if not any(f"{part}_kwh" in row for row in days.values()):
            continue
        kwh = sum(row.get(f"{part}_kwh", 0.0) for row in days.values())
        gbp = sum(row.get(f"{part}_gbp", 0.0) for row in days.values()) * scale
        solar = sum(row.get(f"{part}_solar_gbp", 0.0) for row in days.values())
        parts[part] = {
            "kwh": round(kwh, 1),
            "gbp": round(gbp, 2),
            "share_percent": round(gbp / cost["import_gbp"] * 100) if cost["import_gbp"] else 0,
            # Import cost plus the export payment given up on the solar it used.
            "true_gbp": round(gbp + solar, 2) if valued else None,
            "solar_gbp": round(solar, 2) if valued else None,
        }

    heat_pump = parts.get("heat_pump")
    hot_water_per_day = None
    if heat_pump:
        try:
            hot_water_per_day = heat_pump_and_weather().get("warm_day_kwh")
        except Exception:  # noqa: BLE001 - the split is a nice extra; the costs stand without it
            log.exception("Could not work out hot-water use")
    if heat_pump and hot_water_per_day:
        # Each day, up to the usual warm-day use is hot water; anything above it is heating.
        hot = {"kwh": 0.0, "gbp": 0.0, "true_gbp": 0.0}
        for row in days.values():
            used = row.get("heat_pump_kwh", 0.0)
            if used <= 0:
                continue
            fraction = min(used, hot_water_per_day) / used
            hot["kwh"] += used * fraction
            hot["gbp"] += row.get("heat_pump_gbp", 0.0) * scale * fraction
            hot["true_gbp"] += (
                row.get("heat_pump_gbp", 0.0) * scale + row.get("heat_pump_solar_gbp", 0.0)
            ) * fraction

        def rounded(values: dict, true: bool) -> dict:
            return {
                "kwh": round(values["kwh"], 1),
                "gbp": round(values["gbp"], 2),
                "true_gbp": round(values["true_gbp"], 2) if true else None,
            }

        heating = {
            "kwh": heat_pump["kwh"] - hot["kwh"],
            "gbp": heat_pump["gbp"] - hot["gbp"],
            "true_gbp": (heat_pump["true_gbp"] or 0.0) - hot["true_gbp"],
        }
        heat_pump["hot_water"] = rounded(hot, valued)
        heat_pump["heating"] = rounded(heating, valued)
        heat_pump["hot_water_per_day_kwh"] = hot_water_per_day
    return {"parts": parts, "smart_load_label": settings().smart_load_label}


# --- Costs by month and by year ---------------------------------------------------------------

# Half-hourly samples are plenty for month and year totals; allow for that spacing when
# deciding whether a stretch has no readings.
HALF_HOURLY = 30
HALF_HOURLY_GAP = timedelta(minutes=75)


def month_start(year: int, month_number: int) -> datetime:
    return datetime(year, month_number, 1, tzinfo=local_timezone())


def following_month(start: datetime) -> datetime:
    return month_start(start.year + start.month // 12, start.month % 12 + 1)


def previous_month(start: datetime) -> datetime:
    return month_start(start.year - (start.month == 1), (start.month - 2) % 12 + 1)


def month_key(start: datetime) -> str:
    return f"{start:%Y-%m}"


def first_cost_reading() -> datetime | None:
    """When cost data starts: the earliest reading of the import counter."""
    return database().first_time(COST_COUNTERS[0])


def cost_of_month(samples: dict[str, list], start: datetime, now: datetime, rates: Schedule):
    end = min(now, following_month(start))
    return cost_since(samples, start, end, local_timezone(), rates, gap=HALF_HOURLY_GAP)


@app.get("/api/month")
def month(
    month: Annotated[
        str | None,
        Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM; defaults to this month"),
    ] = None,
) -> dict:
    """What a calendar month cost (so far, for the current month), with the standing charge."""
    now = datetime.now(UTC)
    current = local_midnight(now, local_timezone()).replace(day=1)
    start = current if month is None else month_start(*map(int, month.split("-")))
    if start > current:
        raise HTTPException(status_code=404, detail="That month has not started yet")

    samples = counter_samples(
        [*COST_COUNTERS, *DEVICE_COUNTERS, devices.SOLAR],
        start - timedelta(days=1),
        HALF_HOURLY,
        following_month(start) + timedelta(days=1),
    )
    first = first_cost_reading()
    earliest = (
        first.astimezone(local_timezone()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if first
        else current
    )
    return {
        "key": month_key(start),
        "month": f"{start:%B %Y}",
        "is_current": start == current,
        "previous": month_key(previous_month(start)) if start > earliest else None,
        "next": month_key(following_month(start)) if start < current else None,
        "cost": (cost := cost_of_month(samples, start, now, schedule())),
        # Shared out once every few minutes: it walks the whole month, and this is asked
        # for every few seconds.
        "by_device": _kept.get(
            ("month_by_device", month_key(start), int(time.time() // 300)),
            lambda: cost_by_device(
                samples, start, min(now, following_month(start)), cost, HALF_HOURLY_GAP
            ),
        ),
    }


# --- Keeping worked-out figures ready -----------------------------------------------------------
#
# The figures that cover a long stretch (years, payback, comparisons, performance) take
# seconds to work out on a small machine and change slowly, most of them only at midnight.
# They are kept once worked out, and a background task works them out afresh every few
# minutes, so opening the page never has to wait for them.

CACHE_SECONDS = 10 * 60  # a kept figure older than this is worked out again
# Until then, a figure up to this old is still served at once, and brought up to date in the
# background: opening the page never waits for one that is merely a little old.
STALE_SECONDS = 12 * 3600
KEEP_FRESH_SECONDS = 10 * 60  # how often the page's opening figures are worked out again
CACHE_SIZE = 200


class Kept:
    """Results by key, each with the moment it was worked out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple, tuple[float, object]] = {}
        self._working: dict[tuple, threading.Lock] = {}
        self._generation = 0
        self._due: dict[tuple, object] = {}  # served while old: to work out again soon
        self.cleared = False  # everything was forgotten: the opening figures are needed again
        self.wake = threading.Event()  # tells the background worker there is work

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._due.clear()
            self._generation += 1  # anything being worked out now is out of date already
            self.cleared = True
        self.wake.set()

    def get(self, key: tuple, work, fresh: bool = False):
        """The kept result for `key`, working it out if there is none (or `fresh` is set)."""
        with self._lock:
            held = self._values.get(key)
            if held and not fresh:
                age = time.monotonic() - held[0]
                if age < CACHE_SECONDS:
                    return held[1]
                if age < STALE_SECONDS:
                    self._due[key] = work
                    self.wake.set()
                    return held[1]
            working = self._working.setdefault(key, threading.Lock())
        with working:  # one caller works it out; any others wait and share the result
            with self._lock:
                held = self._values.get(key)
                generation = self._generation
                if held and not fresh and time.monotonic() - held[0] < CACHE_SECONDS:
                    return held[1]
            value = work()
            with self._lock:
                if generation == self._generation:
                    if len(self._values) >= CACHE_SIZE:
                        del self._values[min(self._values, key=lambda k: self._values[k][0])]
                    self._values[key] = (time.monotonic(), value)
                    self._due.pop(key, None)
            return value

    def next_due(self) -> tuple[tuple, object] | None:
        """A figure that was served while old, to work out again."""
        with self._lock:
            return self._due.popitem() if self._due else None


_kept = Kept()


def kept(func):
    """Keep an endpoint's result for each set of arguments. `func.fresh(...)` works it out
    again regardless."""
    signature = inspect.signature(func)

    def key(args: tuple, kwargs: dict) -> tuple:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return (func.__name__, *bound.arguments.items())

    @wraps(func)
    def wrapper(*args, **kwargs):
        return _kept.get(key(args, kwargs), lambda: func(*args, **kwargs))

    wrapper.fresh = lambda *args, **kwargs: _kept.get(
        key(args, kwargs), lambda: func(*args, **kwargs), fresh=True
    )
    return wrapper


def clear_caches() -> None:
    """Forget worked-out costs, after anything they depend on has changed."""
    _kept.clear()


def keep_figures_ready(stop: threading.Event) -> None:
    """Work out the figures the page opens with, now and every few minutes after; at once
    after anything they depend on has changed; and any served while old."""
    # In the order they appear on the page, so the top of it is ready first.
    opening = (
        (years, ()),
        (monthly_summary, (None,)),
        (yearly_report, (None,)),
        (device_costs, ()),
        (heat_pump_and_weather, ()),
        (system_performance, ("12m",)),
        (more_panels, ()),
        (payback_figures, ()),
        (bills, ()),
        (compare, ("12m",)),
        (switch_planner, (None,)),
    )

    def attempt(work) -> None:
        try:
            work()
        except Exception:  # one failing must not stop the others
            log.exception("Could not prepare figures in the background")
        stop.wait(0.5)  # leave room for the page between the heavy jobs

    stop.wait(5)  # let the collector make its first reading and fill any gap
    next_round = 0.0
    while not stop.is_set():
        _kept.wake.clear()
        everything = time.monotonic() >= next_round
        if everything or _kept.cleared:
            _kept.cleared = False
            for func, args in opening:
                if stop.is_set():
                    return
                # After a change, anything the page has already asked for again is kept.
                attempt(lambda f=func, a=args, e=everything: f.fresh(*a) if e else f(*a))
            if everything:
                next_round = time.monotonic() + KEEP_FRESH_SECONDS
        while (due := _kept.next_due()) and not stop.is_set():
            key, work = due
            attempt(lambda k=key, w=work: _kept.get(k, w, fresh=True))
        _kept.wake.wait(max(1.0, next_round - time.monotonic()))


@app.get("/api/years")
@kept
def years() -> dict:
    """Costs for every month and year since readings began, for comparing years."""
    now = datetime.now(UTC)
    first = first_cost_reading()
    result: dict = {"years": []}
    if first is not None:
        rates = schedule()
        local_first = first.astimezone(local_timezone())
        samples = counter_samples(COST_COUNTERS, first - timedelta(days=1), HALF_HOURLY)
        current = local_midnight(now, local_timezone()).replace(day=1)

        for year in range(local_first.year, current.year + 1):
            months = []
            for number in range(1, 13):
                start = month_start(year, number)
                cost = cost_of_month(samples, start, now, rates) if start <= current else None
                months.append({"key": month_key(start), "label": f"{start:%b}", "cost": cost})
            counted = [m["cost"] for m in months if m["cost"]]
            total = combine(counted)
            if total:
                # A whole year needs all twelve months, each counted in full.
                total["full_period"] = (
                    total["full_period"] and len(counted) == 12 and year < current.year
                )
            result["years"].append({"year": year, "cost": total, "months": months})

    return result


# --- Tariff rates ----------------------------------------------------------------------------


class BandIn(BaseModel):
    start: str
    end: str
    p_per_kwh: float = Field(ge=0, le=500)


# A tariff that follows published half-hourly prices names them as "PRODUCT/REGION".
DYNAMIC = Field(default="", pattern=r"^([A-Z0-9-]{3,60}/[A-P])?$")


class TariffIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    effective_from: date
    export_p_per_kwh: float = Field(ge=0, le=500)
    standing_charge_p_per_day: float = Field(ge=0, le=1000)
    vat_percent: float = Field(default=0, ge=0, le=100)
    import_bands: list[BandIn] = Field(min_length=1, max_length=48)
    dynamic: str = DYNAMIC
    fixed_until: date | None = None


def published_prices(source: str) -> dict | None:
    """For a tariff that follows published prices: how far they reach, and any problem."""
    if not source:
        return None
    state = prices.status.get(source, {})
    since, until = prices.held(database(), source)
    return {"source": source, "from": since, "until": until, "error": state.get("error")}


def tariff_json(tariff: Tariff) -> dict:
    return {
        "dynamic": tariff.dynamic,
        "published": published_prices(tariff.dynamic),
        "fixed_until": tariff.fixed_until,
        "effective_from": tariff.effective_from,
        "name": tariff.name,
        "export_p_per_kwh": tariff.export_p_per_kwh,
        "standing_charge_p_per_day": tariff.standing_charge_p_per_day,
        "vat_percent": tariff.vat_percent,
        "import_bands": [
            {"start": b.start, "end": b.end, "p_per_kwh": b.p_per_kwh} for b in tariff.import_bands
        ],
    }


@app.get("/api/tariffs")
def list_tariffs() -> dict:
    """Every tariff period, oldest first, and which one applies today."""
    current = schedule()
    today_local = datetime.now(local_timezone()).date()
    return {
        "current_from": current.on(today_local).effective_from,
        "periods": [tariff_json(p) for p in current.periods],
        "reminders": reminders(current, today_local),
    }


REMIND_DAYS = 30


def reminders(current: Schedule, today_local: date) -> list[dict]:
    """Things about the rates that need attention soon."""
    found: list[dict] = []
    in_use = current.on(today_local)
    later = [p for p in current.periods if p.effective_from > today_local]
    ends = in_use.fixed_until
    # Nothing to say once the rates that follow have been entered.
    if ends and (ends - today_local).days <= REMIND_DAYS:
        follow_on = [p for p in later if p.effective_from <= ends + timedelta(days=1)]
        if not follow_on and not (later and ends < today_local):
            days = (ends - today_local).days
            found.append(
                {
                    "kind": "fixed_ended" if days < 0 else "fixed_ending",
                    "date": ends,
                    "days": days,
                    "tariff": in_use.name,
                }
            )
    for period in current.periods:
        state = prices.status.get(period.dynamic, {}) if period.dynamic else {}
        if state.get("error"):
            found.append({"kind": "prices_failed", "tariff": period.name, "error": state["error"]})
    return found


@app.post("/api/tariffs")
def save_tariff(body: TariffIn) -> dict:
    """Add a tariff period, or replace the one that starts on the same date."""
    try:
        tariff = build_tariff(
            name=body.name.strip(),
            import_bands=[band.model_dump() for band in body.import_bands],
            export_p_per_kwh=body.export_p_per_kwh,
            standing_charge_p_per_day=body.standing_charge_p_per_day,
            effective_from=body.effective_from,
            vat_percent=body.vat_percent,
            dynamic=body.dynamic,
            fixed_until=body.fixed_until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tariff_store.save_period(database(), tariff)
    clear_caches()  # costs must be worked out again with the new rates
    if body.dynamic:
        price_refresh.set()
    return list_tariffs()


@app.delete("/api/tariffs/{effective_from}")
def delete_tariff(effective_from: date) -> dict:
    try:
        found = tariff_store.delete_period(database(), effective_from)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="No tariff period starts on that date")
    clear_caches()
    return list_tariffs()


# --- Comparing tariffs -------------------------------------------------------------------------

# How far back each choice of period reaches. None means everything stored.
COMPARE_PERIODS = {"30d": 30, "90d": 90, "12m": 365, "all": None}
# Charging that could be done at any time of day: into the battery, and into the car.
FLEXIBLE_COUNTERS = ["battery_charge_energy_total", "ev_charger_energy_total"]


class ComparisonIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    export_p_per_kwh: float = Field(ge=0, le=500)
    standing_charge_p_per_day: float = Field(ge=0, le=1000)
    vat_percent: float = Field(default=0, ge=0, le=100)
    import_bands: list[BandIn] = Field(min_length=1, max_length=48)
    dynamic: str = DYNAMIC


def comparison_tariff(row: dict) -> Tariff:
    return build_tariff(
        name=row["name"],
        import_bands=row["import_bands"],
        export_p_per_kwh=row["export_p_per_kwh"],
        standing_charge_p_per_day=row["standing_charge_p_per_day"],
        vat_percent=row["vat_percent"],
        dynamic=row.get("dynamic") or "",
        slot_prices=prices.for_row(database(), row),
    )


@app.get("/api/compare")
@kept
def compare(period: Annotated[str, Query(pattern="^(30d|90d|12m|all)$")] = "12m") -> dict:
    """What the same imports and exports would have cost on each saved tariff.

    The usage is replayed exactly as it happened, half hour by half hour. It does not allow
    for habits changing to suit a different tariff (such as charging the battery at other
    times), so a tariff with a different cheap window may do better in practice.
    """
    now = datetime.now(UTC)
    first = first_cost_reading()
    rows = database().comparison_rows()
    result: dict = {"period": period, "actual": None, "candidates": []}
    if first is None:
        result["candidates"] = [{**row, "cost": None, "difference_gbp": None} for row in rows]
        return result

    days = COMPARE_PERIODS[period]
    # Whole local days, ending now, so the standing charge is counted fairly.
    wanted = local_midnight(now, local_timezone()) - timedelta(days=days) if days else first
    start = max(wanted, first)
    samples = counter_samples(
        [*COST_COUNTERS, *FLEXIBLE_COUNTERS], start - timedelta(days=1), HALF_HOURLY, now
    )
    counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}

    def cost_on(rates: Schedule) -> dict | None:
        return cost_since(samples, start, now, local_timezone(), rates, gap=HALF_HOURLY_GAP)

    def with_charging_moved(rates: Schedule, cost: dict | None) -> dict | None:
        """The same cost with battery and car charging moved to the tariff's cheapest times."""
        if not cost or not actual:
            return None
        moved = shift.shifted_import(
            counters[COST_COUNTERS[0]],
            [counters[name] for name in FLEXIBLE_COUNTERS],
            cost["since"],
            now,
            local_timezone(),
            rates,
        )
        if moved is None:
            return None
        net = round(cost["net_gbp"] - cost["import_gbp"] + moved["import_gbp"], 2)
        return {**moved, "net_gbp": net, "difference_gbp": round(net - actual["net_gbp"], 2)}

    actual = cost_on(schedule())
    result["actual"] = actual
    result["from"] = actual["since"] if actual else start
    result["days"] = actual["standing_charge_days"] if actual else 0
    for row in rows:
        rates = Schedule([comparison_tariff(row)])
        cost = cost_on(rates)
        difference = round(cost["net_gbp"] - actual["net_gbp"], 2) if cost and actual else None
        result["candidates"].append(
            {
                **row,
                "published": published_prices(row.get("dynamic") or ""),
                "cost": cost,
                "difference_gbp": difference,
                "shifted": with_charging_moved(rates, cost),
            }
        )
    return result


@app.post("/api/compare/tariffs")
def save_comparison(body: ComparisonIn, id: int | None = None) -> dict:  # noqa: A002
    """Add a tariff to compare against, or replace the one with the given id."""
    row = body.model_dump()
    row["name"] = row["name"].strip()
    try:
        comparison_tariff(row)  # checks the bands cover the whole day
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved = database().save_comparison_row(row, id)
    clear_caches()  # the comparison, and payback if it is measured against this tariff
    if body.dynamic:
        price_refresh.set()
    return {"id": saved}


@app.delete("/api/compare/tariffs/{row_id}")
def delete_comparison(row_id: int) -> dict:
    if not database().delete_comparison_row(row_id):
        raise HTTPException(status_code=404, detail="That tariff is not in the list")
    clear_caches()
    return {"removed": row_id}


# --- Planning a switch of tariff -----------------------------------------------------------------

PLAN_COUNTERS = {
    "load": "load_energy_total",
    "solar": "pv_energy_total",
    "ev": "ev_charger_energy_total",
    "charge": "battery_charge_energy_total",
    "discharge": "battery_discharge_energy_total",
}


class PlannerIn(BaseModel):
    """The battery the plan is worked out for. Empty means: work it out from the readings."""

    battery_kwh: float | None = Field(default=None, ge=1, le=500)
    charge_kw: float | None = Field(default=None, ge=0.5, le=100)


@app.post("/api/planner/settings")
def save_planner_settings(body: PlannerIn) -> dict:
    database().set_setting("planner", body.model_dump_json())
    clear_caches()
    return {"saved": True}


@app.get("/api/planner")
@kept
def switch_planner(tariff: Annotated[int | None, Query(ge=1)] = None) -> dict:
    """The last twelve months on another tariff, month by month, with a charging plan.

    `tariff` is the id of a tariff saved under Compare tariffs (the first one if left out).
    """
    zone = local_timezone()
    now = datetime.now(UTC)
    rows = database().comparison_rows()
    stored = json.loads(database().get_setting("planner") or "{}")
    result: dict = {
        "has_data": False,
        "tariffs": [{"id": row["id"], "name": row["name"]} for row in rows],
        "settings": stored,
    }
    chosen = next((row for row in rows if row["id"] == tariff), rows[0] if rows else None)
    first = first_cost_reading()
    load_first = database().first_time(PLAN_COUNTERS["load"])
    end = local_midnight(now, zone)
    if chosen is None or first is None or load_first is None:
        return result
    start = max(end - timedelta(days=365), first, load_first).astimezone(zone)
    if start.time() != clock(0):  # begin on the first whole day
        start = datetime.combine(start.date() + timedelta(days=1), clock(0), zone)
    if start >= end:
        return result

    names = [*COST_COUNTERS, *PLAN_COUNTERS.values()]
    samples = counter_samples(names, start - timedelta(days=1), HALF_HOURLY, end)
    counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}
    plan = {key: counters[name] for key, name in PLAN_COUNTERS.items()}
    own, other = schedule(), Schedule([comparison_tariff(chosen)])

    # The battery: as entered, or else as worked out from how it has been charging.
    levels = database().last_in_buckets(["battery_soc"], end - timedelta(days=90), end, 1800)
    capacity, speed = planner.estimate_battery(
        levels["battery_soc"], plan["charge"], plan["discharge"]
    )
    charged = plan["charge"].between(start, end) if plan["charge"] else 0.0
    given = plan["discharge"].between(start, end) if plan["discharge"] else 0.0
    measured = given / charged if charged >= 50 else None
    efficiency = measured if measured and 0.6 <= measured <= 1 else planner.DEFAULT_EFFICIENCY
    battery = None
    if (stored.get("battery_kwh") or capacity) and (stored.get("charge_kw") or speed):
        battery = planner.Battery(
            stored.get("battery_kwh") or capacity, stored.get("charge_kw") or speed, efficiency
        )
    result["battery"] = {
        "estimated_kwh": capacity,
        "estimated_kw": speed,
        "used_kwh": battery.capacity_kwh if battery else None,
        "used_kw": battery.charge_kw if battery else None,
        "efficiency_percent": round(efficiency * 100),
    }

    modelled = modelled_own = None
    if battery:
        modelled = planner.simulate(plan, start, end, zone, other, battery)
        modelled_own = planner.simulate(plan, start, end, zone, own, battery)

    def in_month(days: dict | None, key: str) -> float | None:
        if days is None:
            return None
        return round(sum(row["gbp"] for day, row in days.items() if f"{day:%Y-%m}" == key), 2)

    months = []
    cursor = start
    while cursor < end:
        month_end = min(following_month(cursor.replace(day=1)), end)
        paid = cost_since(samples, cursor, month_end, zone, own, gap=HALF_HOURLY_GAP)
        replayed = cost_since(samples, cursor, month_end, zone, other, gap=HALF_HOURLY_GAP)
        moved = None
        if replayed:
            shifted = shift.shifted_import(
                counters[COST_COUNTERS[0]],
                [plan["charge"], plan["ev"]],
                replayed["since"],
                month_end,
                zone,
                other,
            )
            if shifted:
                moved = round(
                    replayed["net_gbp"] - replayed["import_gbp"] + shifted["import_gbp"], 2
                )
        key = month_key(cursor)
        months.append(
            {
                "month": key,
                "days": (month_end - cursor).days,
                "paid_gbp": paid["net_gbp"] if paid else None,
                "replayed_gbp": replayed["net_gbp"] if replayed else None,
                "moved_gbp": moved,
                "planned_gbp": in_month(modelled, key),
                "planned_own_gbp": in_month(modelled_own, key),
            }
        )
        cursor = month_end

    def total(key: str) -> float | None:
        values = [month[key] for month in months]
        return None if any(v is None for v in values) else round(sum(values), 2)

    result.update(
        has_data=True,
        tariff={"id": chosen["id"], "name": chosen["name"], "dynamic": bool(chosen.get("dynamic"))},
        own_name=own.on(end.date() - timedelta(days=1)).name,
        first_day=start.date(),
        last_day=(end - timedelta(days=1)).date(),
        days=(end - start).days,
        months=months,
        totals={key: total(key) for key in months[0] if key.endswith("_gbp")},
        cheap_times=planner.cheap_times(other.periods[0]),
        own_cheap_times=planner.cheap_times(own.on(end.date() - timedelta(days=1))),
        shortfall=planner.shortfall(modelled) if modelled else None,
        own_shortfall=planner.shortfall(modelled_own) if modelled_own else None,
    )
    return result


# --- More panels or a bigger inverter -----------------------------------------------------------


class PanelsIn(BaseModel):
    """The solar system as it is, for working out what more of it would add."""

    kwp: float | None = Field(default=None, gt=0, le=100)
    inverter_kw: float | None = Field(default=None, gt=0, le=100)
    new_inverter_kw: float | None = Field(default=None, gt=0, le=100)
    cost_per_kwp: float | None = Field(default=None, ge=0, le=10_000)


@app.post("/api/panels/settings")
def save_panel_settings(body: PanelsIn) -> dict:
    database().set_setting("panels", body.model_dump_json())
    clear_caches()
    return more_panels()


@app.get("/api/panels")
@kept
def more_panels() -> dict:
    """What extra panels, with today's inverter or a bigger one, would have saved in a year."""
    zone = local_timezone()
    now = datetime.now(UTC)
    stored = json.loads(database().get_setting("panels") or "{}")
    result: dict = {"has_data": False, "settings": stored}
    end = local_midnight(now, zone)
    firsts = [database().first_time(PLAN_COUNTERS[key]) for key in ("load", "solar")]
    if not all(firsts):
        return result
    start = max(end - timedelta(days=365), *firsts).astimezone(zone)
    if start.time() != clock(0):
        start = datetime.combine(start.date() + timedelta(days=1), clock(0), zone)
    if end - start < timedelta(days=14):
        return result
    names = list(PLAN_COUNTERS.values())
    samples = counter_samples(names, start - timedelta(days=1), HALF_HOURLY, end)
    counters = {key: Counter(samples[name], HALF_HOURLY_GAP) for key, name in PLAN_COUNTERS.items()}
    estimate = panels.estimate_kwp(counters["solar"], start, end)
    kwp = stored.get("kwp") or estimate
    result.update(estimated_kwp=estimate, used_kwp=kwp)
    if not kwp:
        return result

    # The same battery the planner uses, so the two agree.
    levels = database().last_in_buckets(["battery_soc"], end - timedelta(days=90), end, 1800)
    capacity, speed = planner.estimate_battery(
        levels["battery_soc"], counters["charge"], counters["discharge"]
    )
    plan_settings = json.loads(database().get_setting("planner") or "{}")
    battery = planner.Battery(
        plan_settings.get("battery_kwh") or capacity or 0.01,
        plan_settings.get("charge_kw") or speed or 0.01,
    )
    found = panels.options(
        {key: counters[key] for key in ("load", "solar", "ev")},
        start,
        end,
        zone,
        schedule(),
        battery,
        kwp,
        stored.get("inverter_kw"),
        stored.get("new_inverter_kw"),
    )
    cost = stored.get("cost_per_kwp")
    for row in found["options"]:
        row["cost_gbp"] = round(cost * row["extra_kwp"]) if cost else None
        row["payback_years"] = (
            round(row["cost_gbp"] / row["saved_gbp_year"], 1)
            if cost and row["saved_gbp_year"] > 0
            else None
        )
    result.update(has_data=True, first_day=start.date(), battery_kwh=battery.capacity_kwh, **found)
    return result


# --- The heat pump against the weather ------------------------------------------------------------


@app.get("/api/heating")
@kept
def heat_pump_and_weather() -> dict:
    """The heat pump's daily use set against the outdoor temperature."""
    zone = local_timezone()
    now = datetime.now(UTC)
    result: dict = {
        "has_data": False,
        "entity": weather.entity_in_use or settings().outdoor_temperature_entity or None,
        "smart_load_label": settings().smart_load_label,
    }
    first_heat = database().first_time(devices.CIRCUIT)
    first_temperature = database().first_time(weather.METRIC)
    result.update(has_heat_pump=bool(first_heat), has_temperature=bool(first_temperature))
    if not first_heat or not first_temperature:
        return result
    end = local_midnight(now, zone)  # whole days
    start = max(end - timedelta(days=3 * 365), first_heat, first_temperature).astimezone(zone)
    if start >= end:
        return result
    samples = counter_samples(devices.COUNTERS, start - timedelta(days=1), HALF_HOURLY, end)
    counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}
    if not counters[devices.LOAD] or not counters[devices.IMPORT]:
        return result
    days = devices.daily(counters, start, end, zone, schedule(), settings().ev_on_smart_load)
    used = {day: row["heat_pump_kwh"] for day, row in days.items() if "heat_pump_kwh" in row}
    hourly = database().series([weather.METRIC], start, 3600)[weather.METRIC]
    result.update(heating.analyse(used, heating.daily_means(hourly, zone)))
    return result


# --- Running cost by device ----------------------------------------------------------------------


@app.get("/api/devices")
@kept
def device_costs() -> dict:
    """What the heat pump (or other smart load), the EV charger and the rest of the house
    cost to run: today, and for every month and year with readings."""
    zone = local_timezone()
    now = datetime.now(UTC)
    result: dict = {"has_data": False, "smart_load_label": settings().smart_load_label}
    firsts = [database().first_time(name) for name in (devices.LOAD, devices.IMPORT)]
    if all(firsts):
        start = max(firsts).astimezone(zone)
        samples = counter_samples(devices.COUNTERS, start - timedelta(days=1), HALF_HOURLY, now)
        counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}
        days = devices.daily(counters, start, now, zone, schedule(), settings().ev_on_smart_load)
        today_local = now.astimezone(zone).date()
        if days:
            result = {
                **result,
                "has_data": True,
                "from": min(days),
                "heat_pump": bool(counters[devices.CIRCUIT]),
                "ev_charger": bool(counters[devices.EV]),
                "today": devices.total([days[today_local]]) if today_local in days else None,
                **devices.by_month_and_year(days),
            }
    return result


# --- Performance and battery sizing --------------------------------------------------------------


@app.get("/api/performance")
@kept
def system_performance(
    period: Annotated[str, Query(pattern="^(30d|90d|12m|all)$")] = "12m",
) -> dict:
    """Self-sufficiency, battery efficiency and what a bigger battery would have saved."""
    zone = local_timezone()
    now = datetime.now(UTC)
    first = first_cost_reading()
    result: dict = {"period": period, "has_data": False}
    end = local_midnight(now, zone)  # whole days only, up to the end of yesterday
    if first is not None:
        span = COMPARE_PERIODS[period]
        start = max(end - timedelta(days=span) if span else first, first).astimezone(zone)
        if start.time() != clock(0):  # begin on the first whole day
            start = datetime.combine(start.date() + timedelta(days=1), clock(0), zone)
        if start < end:
            samples = counter_samples(
                performance.COUNTERS, start - timedelta(days=1), HALF_HOURLY, end
            )
            counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}
            days = performance.daily(counters, start, end, zone, schedule())
            overall = performance.figures(list(days.values()))
            measured = overall["battery_efficiency_percent"]
            result = {
                "period": period,
                "has_data": True,
                "from": start.date(),
                "days": len(days),
                "overall": overall,
                "monthly": performance.monthly(days),
                # Use the battery's own measured efficiency when it is believable.
                "sizing": performance.sizing(
                    days, measured / 100 if measured and 60 <= measured <= 100 else None
                ),
            }
    return result


# --- Monthly summary ---------------------------------------------------------------------------


def month_figures(
    start: datetime, now: datetime, finish: datetime | None = None, by_month: bool = False
) -> dict | None:
    """Everything a summary says about one calendar month (or any period from `start` to
    `finish`), or None if it has no readings. `by_month` adds a line for each month in it."""
    zone = local_timezone()
    finish = finish or following_month(start)
    end = min(finish, local_midnight(now, zone))  # whole days only
    first = first_cost_reading()
    if first is None or start >= end or first >= end:
        return None
    begin = max(start, first.astimezone(zone))
    if begin.time() != clock(0):  # readings began partway through a day: start on the next
        begin = datetime.combine(begin.date() + timedelta(days=1), clock(0), zone)
    if begin >= end:
        return None
    rates = schedule()
    names = list(dict.fromkeys([*performance.COUNTERS, *COST_COUNTERS]))
    samples = counter_samples(names, begin - timedelta(days=1), HALF_HOURLY, end)
    counters = {name: Counter(series, HALF_HOURLY_GAP) for name, series in samples.items()}
    days = performance.daily(counters, begin, end, zone, rates)
    cost = cost_since(samples, begin, end, zone, rates, gap=HALF_HOURLY_GAP)
    if cost is None:
        return None
    net_by_day = roi.daily_costs(
        counters[COST_COUNTERS[0]], counters[COST_COUNTERS[1]], begin, end, zone, rates
    )
    last_day = (end - timedelta(days=1)).date()
    extra = sum(
        entry["amount_gbp"] or 0.0
        for entry in database().income_rows()
        if begin.date().isoformat() <= entry["day"] <= last_day.isoformat()
    )

    def standout(values: dict, highest: bool) -> dict | None:
        usable = {day: value for day, value in values.items() if value is not None}
        if not usable:
            return None
        day = (max if highest else min)(usable, key=usable.get)
        return {"day": day, "value": round(usable[day], 2)}

    months = None
    if by_month:
        net: dict[str, float] = {}
        for day, value in net_by_day.items():
            net[f"{day:%Y-%m}"] = net.get(f"{day:%Y-%m}", 0.0) + value
        months = [
            {**row, "net_gbp": round(net.get(row["month"], 0.0), 2)}
            for row in performance.monthly(days)
        ]

    return {
        "months": months,
        "from": begin.date(),
        "to": last_day,
        "days": len(days),
        "complete": begin == start and end == finish,
        "cost": cost,
        "extra_income_gbp": round(extra, 2),
        **performance.figures(list(days.values())),
        "dearest_day": standout(net_by_day, True),
        "cheapest_day": standout(net_by_day, False),
        "sunniest_day": standout({d: v[performance.SOLAR] for d, v in days.items()}, True),
        "busiest_day": standout({d: v[performance.LOAD] for d, v in days.items()}, True),
    }


@app.get("/api/summary")
@kept
def monthly_summary(
    month: Annotated[
        str | None,
        Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM; defaults to last month"),
    ] = None,
) -> dict:
    """A calendar month in one page, set beside the month before and the same month last year."""
    now = datetime.now(UTC)
    zone = local_timezone()
    current = local_midnight(now, zone).replace(day=1)
    start = previous_month(current) if month is None else month_start(*map(int, month.split("-")))
    if start > current:
        raise HTTPException(status_code=404, detail="That month has not started yet")
    first = first_cost_reading()
    earliest = (
        first.astimezone(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if first
        else current
    )
    # With no earlier month to show, fall back to the current one.
    if month is None and start < earliest:
        start = current
    return {
        "key": month_key(start),
        "month": f"{start:%B %Y}",
        "previous": month_key(previous_month(start)) if start > earliest else None,
        "next": month_key(following_month(start)) if start < current else None,
        "this": month_figures(start, now),
        "month_before": month_figures(previous_month(start), now),
        "year_before": month_figures(month_start(start.year - 1, start.month), now),
    }


# --- Notes on the timeline ----------------------------------------------------------------------


class NoteIn(BaseModel):
    day: date
    text: str = Field(min_length=1, max_length=200)


@app.get("/api/notes")
def notes() -> dict:
    """Notes pinned to days, newest first."""
    return {"notes": database().note_rows()}


@app.post("/api/notes")
def save_note(body: NoteIn, id: int | None = None) -> dict:  # noqa: A002
    """Add a note, or change the one with the given id."""
    database().save_note(body.day, body.text.strip(), id)
    return notes()


@app.delete("/api/notes/{row_id}")
def delete_note(row_id: int) -> dict:
    if not database().delete_note(row_id):
        raise HTTPException(status_code=404, detail="That note is not in the list")
    return notes()


# --- Yearly report -------------------------------------------------------------------------------


@app.get("/api/yearly")
@kept
def yearly_report(year: Annotated[int | None, Query(ge=2000, le=2200)] = None) -> dict:
    """A calendar year on one page, beside the year before."""
    zone = local_timezone()
    now = datetime.now(UTC)
    this_year = now.astimezone(zone).year
    first = first_cost_reading()
    first_year = first.astimezone(zone).year if first else this_year
    chosen = year or this_year
    if not first_year <= chosen <= this_year:
        raise HTTPException(status_code=404, detail="There are no readings for that year")

    def figures(which: int, by_month: bool = False) -> dict | None:
        return month_figures(month_start(which, 1), now, month_start(which + 1, 1), by_month)

    device_years = {row["year"]: row for row in device_costs().get("years", [])}
    payback = payback_figures().get("payback") or {}
    saved = None
    if payback.get("has_data"):
        # Savings to date as they stood at the end of the year (the chart's weekly points).
        upto = [value for day, value in payback["series"] if str(day) <= f"{chosen}-12-31"]
        saved = upto[-1] if upto else None
    cost = payback_figures().get("total_cost_gbp")
    return {
        "year": chosen,
        "previous": chosen - 1 if chosen > first_year else None,
        "next": chosen + 1 if chosen < this_year else None,
        "is_current": chosen == this_year,
        "this": figures(chosen, by_month=True),
        "year_before": figures(chosen - 1) if chosen > first_year else None,
        "devices": device_years.get(str(chosen)),
        "smart_load_label": settings().smart_load_label,
        "saved_by_year_end_gbp": saved,
        "system_cost_gbp": cost,
    }


# --- Backing up and restoring settings -----------------------------------------------------------

# Settings worth carrying to another install. Left out: how far old readings have been
# thinned, which belongs to this install's readings.
BACKUP_SETTINGS = ("payback", "planner", "layout", "axle_rate_p")
BACKUP_FORMAT = 1


@app.get("/api/backup")
def download_backup() -> Response:
    """Everything the user has entered, as one file. Readings are not included: they are
    covered by Home Assistant's own backups."""
    tariff_store.load_periods(database())  # so the starting rates are in the file too
    content = {
        "app": "energy-tracker",
        "format": BACKUP_FORMAT,
        "version": app.version,
        "saved": datetime.now(UTC).isoformat(timespec="seconds"),
        "tables": database().dump(),
        "settings": {
            key: value
            for key in BACKUP_SETTINGS
            if (value := database().get_setting(key)) is not None
        },
    }
    name = f"energy-tracker-settings-{datetime.now(local_timezone()):%Y-%m-%d}.json"
    return Response(
        json.dumps(content, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def checked_backup(body: bytes) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Read a backup file and make sure every part of it can be used, before anything is
    replaced. Raises ValueError with a message fit to show."""
    try:
        content = json.loads(body)
    except ValueError:
        raise ValueError("That is not a settings file saved by Energy Tracker") from None
    if not isinstance(content, dict) or content.get("app") != "energy-tracker":
        raise ValueError("That is not a settings file saved by Energy Tracker")
    if content.get("format") != BACKUP_FORMAT:
        raise ValueError("That file was saved by a newer version: update Energy Tracker first")
    tables, stored = content.get("tables"), content.get("settings", {})
    if not isinstance(tables, dict) or not isinstance(stored, dict):
        raise ValueError("That settings file is damaged")
    for table in Database.BACKED_UP:
        rows = tables.get(table, [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("That settings file is damaged")
    if not tables.get("tariff_periods"):
        raise ValueError("That file has no tariff rates in it")
    try:
        for row in [*tables["tariff_periods"], *tables.get("comparison_tariffs", [])]:
            bands = json.loads(row["import_bands"])
            build_tariff(**{**row, "import_bands": bands})
            if "effective_from" in row:
                date.fromisoformat(row["effective_from"])
        for row in tables.get("bills", []):
            date.fromisoformat(row["first_day"]), date.fromisoformat(row["last_day"])
        for row in tables.get("extra_income", []):
            date.fromisoformat(row["day"])
            str(row["description"])
        for row in tables.get("notes", []):
            date.fromisoformat(row["day"])
            if not isinstance(row["text"], str):
                raise ValueError("a note has no text")
        settings_ = {}
        for key in BACKUP_SETTINGS:
            if key in stored:
                if key != "axle_rate_p":
                    json.loads(stored[key])
                settings_[key] = str(stored[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"That settings file is damaged ({exc})") from None
    return {table: tables.get(table, []) for table in Database.BACKED_UP}, settings_


@app.post("/api/restore")
async def restore_backup(request: Request, dry_run: bool = True) -> dict:
    """Replace everything the user has entered with a settings file's contents.

    With `dry_run` (the default) the file is only checked, and what it holds is reported.
    """
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large (20 MB at most)")
    try:
        tables, stored = checked_backup(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    found = {
        "tariff_periods": len(tables["tariff_periods"]),
        "comparison_tariffs": len(tables["comparison_tariffs"]),
        "bills": len(tables["bills"]),
        "extra_income": len(tables["extra_income"]),
        "notes": len(tables["notes"]),
        "settings": sorted(stored),
        "saved": json.loads(body).get("saved"),
        "version": json.loads(body).get("version"),
    }
    if not dry_run:
        try:
            database().restore(
                tables, stored, [key for key in BACKUP_SETTINGS if key not in stored]
            )
        except sqlite3.Error as exc:
            raise HTTPException(
                status_code=422, detail=f"That settings file could not be stored ({exc})"
            ) from exc
        prices.forget()
        clear_caches()
        price_refresh.set()
    return {"restored": not dry_run, "found": found}


# --- Checking a bill ---------------------------------------------------------------------------


class BillIn(BaseModel):
    first_day: date
    last_day: date
    import_kwh: float | None = Field(default=None, ge=0, le=1_000_000)
    charge_gbp: float | None = Field(default=None, ge=0, le=1_000_000)
    export_kwh: float | None = Field(default=None, ge=0, le=1_000_000)
    export_gbp: float | None = Field(default=None, ge=0, le=1_000_000)


def difference(billed: float | None, measured: float | None) -> dict | None:
    """How far the bill is from the tracker's figure: positive means the bill is higher."""
    if billed is None or measured is None:
        return None
    return {
        "amount": round(billed - measured, 2),
        "percent": round((billed - measured) / measured * 100, 1) if measured else None,
    }


@app.get("/api/bills")
@kept
def bills() -> dict:
    """Bills entered by hand, each beside the tracker's own figures for the same dates."""
    zone = local_timezone()
    now = datetime.now(UTC)
    rates = schedule()
    checked = []
    for row in database().bill_rows():
        start = datetime.combine(date.fromisoformat(row["first_day"]), clock(0), zone)
        end = datetime.combine(
            date.fromisoformat(row["last_day"]) + timedelta(days=1), clock(0), zone
        )
        samples = counter_samples(COST_COUNTERS, start - timedelta(days=1), HALF_HOURLY, end)
        cost = None
        if start < now:
            cost = cost_since(samples, start, min(end, now), zone, rates, gap=HALF_HOURLY_GAP)
        measured = None
        if cost:
            measured = {
                "import_kwh": cost["import_kwh"],
                # What a bill charges: energy and standing charge together, VAT included.
                "charge_gbp": round(cost["import_gbp"] + cost["standing_charge_gbp"], 2),
                "export_kwh": cost["export_kwh"],
                "export_gbp": cost["export_credit_gbp"],
                "covers_whole_bill": cost["full_period"] and end <= now,
                "estimated_hours": cost["estimated_hours"],
            }
        checked.append(
            {
                **row,
                "tracker": measured,
                "differences": {
                    key: difference(row[key], measured[key] if measured else None)
                    for key in ("import_kwh", "charge_gbp", "export_kwh", "export_gbp")
                },
            }
        )
    return {"bills": checked}


@app.post("/api/bills")
def save_bill(body: BillIn, id: int | None = None) -> dict:  # noqa: A002
    if body.last_day < body.first_day:
        raise HTTPException(
            status_code=422, detail="The bill must end on or after the day it starts"
        )
    if (body.last_day - body.first_day).days > 400:
        raise HTTPException(status_code=422, detail="A bill can cover at most about a year")
    if all(v is None for v in (body.import_kwh, body.charge_gbp, body.export_kwh, body.export_gbp)):
        raise HTTPException(status_code=422, detail="Enter at least one figure from the bill")
    database().save_bill(body.model_dump(), id)
    clear_caches()
    return bills()


@app.post("/api/bills/read")
async def read_bill(request: Request) -> dict:
    """Pick the figures out of a bill PDF sent as the request body.

    The file is read in memory and not kept. Nothing is saved: the figures go back to the
    page for the person to check and save themselves.
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="Choose a PDF file first")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large (20 MB at most)")
    try:
        result = await asyncio.to_thread(billreader.read_pdf, body)
    except billreader.BillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["tariff_check"] = billcheck.check(
        result["kind"],
        result["found"]["first_day"],
        [rate["p_per_kwh"] for rate in result["rates"]],
        result["standing_charge_p_per_day"],
        result["vat_percent"],
        schedule(),
    )
    return result


class RatesIn(BaseModel):
    """The rates read off a bill, sent back to be compared with the tracker's again."""

    kind: str = Field(pattern="^(import|export)$")
    first_day: date
    rates: list[Annotated[float, Field(ge=0, le=500)]] = Field(default=[], max_length=20)
    standing_charge_p_per_day: float | None = Field(default=None, ge=0, le=1000)
    vat_percent: float | None = Field(default=None, ge=0, le=100)


@app.post("/api/bills/tariff-check")
def check_bill_rates(body: RatesIn) -> dict:
    """Compare a bill's rates with the tracker's for the same dates (after a correction, say)."""
    return billcheck.check(
        body.kind,
        body.first_day,
        body.rates,
        body.standing_charge_p_per_day,
        body.vat_percent,
        schedule(),
    )


@app.delete("/api/bills/{row_id}")
def delete_bill(row_id: int) -> dict:
    if not database().delete_bill(row_id):
        raise HTTPException(status_code=404, detail="That bill is not in the list")
    clear_caches()
    return bills()


# --- Extra income ----------------------------------------------------------------------------


class IncomeIn(BaseModel):
    day: date
    description: str = Field(min_length=1, max_length=80)
    amount_gbp: float = Field(ge=0, le=100_000)
    # Whether it can be expected to carry on (Axle's monthly payments), or is a one-off (a
    # referral bonus). Left out: told from the description ("Axle" is regular).
    regular: bool | None = None


class AxleRateIn(BaseModel):
    p_per_kwh: float = Field(ge=0, le=2000)


@app.get("/api/income")
def extra_income() -> dict:
    """Income on top of the tariff: entries typed in, and recorded Axle Energy events."""
    entries = database().income_rows()
    by_year: dict[str, float] = {}
    regular = one_off = 0.0
    for entry in entries:
        if entry["amount_gbp"]:
            year = entry["day"][:4]
            by_year[year] = round(by_year.get(year, 0.0) + entry["amount_gbp"], 2)
            if entry["regular"]:
                regular += entry["amount_gbp"]
            else:
                one_off += entry["amount_gbp"]
    return {
        "entries": entries,
        "total_gbp": round(sum(by_year.values()), 2),
        "regular_gbp": round(regular, 2),
        "one_off_gbp": round(one_off, 2),
        "by_year": dict(sorted(by_year.items())),
        "axle": {
            "entity": settings().axle_event_entity,
            "status": collector.axle_status,
            "rate_p": income.rate_p(database()),
        },
    }


@app.post("/api/income")
def save_income(body: IncomeIn, id: int | None = None) -> dict:  # noqa: A002
    """Add an entry, or correct the one with the given id."""
    database().save_income(body.day, body.description.strip(), body.amount_gbp, id, body.regular)
    clear_caches()
    return extra_income()


@app.delete("/api/income/{row_id}")
def remove_income(row_id: int) -> dict:
    if not database().remove_income(row_id):
        raise HTTPException(status_code=404, detail="That entry is not in the list")
    clear_caches()
    return extra_income()


@app.post("/api/income/axle-rate")
def set_axle_rate(body: AxleRateIn) -> dict:
    """What Axle pays per kWh exported during an event. Used for events measured from now on."""
    database().set_setting("axle_rate_p", str(body.p_per_kwh))
    return extra_income()


# --- History for any period, and exporting it ----------------------------------------------------

PERIOD = Annotated[str, Query(pattern="^(day|week|month|year)$")]


def history_rows(period: str, day: date | None, interval: str) -> tuple[datetime, datetime, list]:
    zone = local_timezone()
    now = datetime.now(UTC)
    start, end = history.period_bounds(period, day or now.astimezone(zone).date(), zone)
    # Half-hourly export needs every reading; coarser views are fine with one per half hour.
    bucket = 5 if interval == "halfhour" else HALF_HOURLY
    samples = counter_samples(
        history.COUNTERS, start - timedelta(days=1), bucket, end + timedelta(hours=1)
    )
    gap = timedelta(minutes=15) if interval == "halfhour" else HALF_HOURLY_GAP
    counters = {name: Counter(series, gap) for name, series in samples.items()}
    return start, end, history.rows(counters, start, end, interval, zone, schedule(), now)


@app.get("/api/history")
def past_period(period: PERIOD = "day", day: date | None = None) -> dict:
    """Energy and cost for the day, week, month or year containing `day` (default today)."""
    zone = local_timezone()
    now = datetime.now(UTC)
    start, end, data = history_rows(period, day, history.VIEW_INTERVAL[period])
    first = first_cost_reading()
    earliest = first.astimezone(zone) if first else now.astimezone(zone)
    cost = None
    if start < now and (first is None or first < end):
        samples = counter_samples(COST_COUNTERS, start - timedelta(days=1), HALF_HOURLY, end)
        cost = cost_since(samples, start, min(end, now), zone, schedule(), gap=HALF_HOURLY_GAP)
    previous_day = (start - timedelta(days=1)).date()
    return {
        "period": period,
        "interval": history.VIEW_INTERVAL[period],
        "start": start.date(),
        "end": (end - timedelta(days=1)).date(),
        "previous": previous_day if start > earliest else None,
        "next": end.date() if end <= now.astimezone(zone) else None,
        "rows": data,
        "totals": history.totals(data),
        "cost": cost,
    }


@app.get("/api/export.csv")
def export_csv(
    period: PERIOD = "day",
    day: date | None = None,
    interval: Annotated[str, Query(pattern="^(halfhour|hour|day|month)$")] = "hour",
) -> Response:
    """The same figures as a spreadsheet file, at the level of detail asked for."""
    start, end, data = history_rows(period, day, interval)
    name = f"energy-{period}-{start:%Y-%m-%d}-{interval}.csv"
    return Response(
        history.to_csv(data, local_timezone()),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- Payback -----------------------------------------------------------------------------------

LOAD_COUNTER = "load_energy_total"
SOLAR_COUNTER = "pv_energy_total"


class CostIn(BaseModel):
    date: date
    description: str = Field(default="", max_length=80)
    amount: float = Field(gt=0, le=10_000_000)


class LayoutIn(BaseModel):
    """Which parts of the page are folded away."""

    folded: list[Annotated[str, Field(max_length=80)]] = Field(default=[], max_length=100)
    bill_years: list[Annotated[int, Field(ge=1990, le=2200)]] = Field(default=[], max_length=100)


@app.get("/api/layout")
def layout() -> dict:
    """The folded sections, kept here so every device and browser shows the same."""
    saved = database().get_setting("layout")
    if saved is None:
        return {"saved": False, "folded": [], "bill_years": []}
    return {"saved": True, **LayoutIn.model_validate_json(saved).model_dump()}


@app.post("/api/layout")
def save_layout(body: LayoutIn) -> dict:
    database().set_setting("layout", body.model_dump_json())
    return layout()


class PaybackIn(BaseModel):
    install_date: date
    # "own": the tariff you were on each day. Otherwise the id of a tariff saved for comparison.
    baseline: str = Field(default="own", pattern=r"^(own|\d{1,9})$")
    # A second tariff to measure against, drawn as its own line ("" for none): the id of a
    # tariff saved for comparison, such as a standard variable rate.
    second_baseline: str = Field(default="", pattern=r"^(\d{1,9})?$")
    costs: list[CostIn] = Field(max_length=20)
    # Optional yearly assumptions for the adjusted estimate, in percent. 0 leaves one out.
    panel_ageing_percent: float = Field(default=0, ge=0, le=5)
    battery_ageing_percent: float = Field(default=0, ge=0, le=10)
    price_change_percent: float = Field(default=0, ge=-10, le=20)
    # Whether regular extra income (Axle's payments) is assumed to carry on at the same rate.
    # Either way it counts towards what has been saved so far. One-off income (a referral
    # bonus, say) is never assumed to carry on.
    project_extra_income: bool = True
    # The least regular income is projected at, a month: Axle's guaranteed minimum, say.
    regular_income_minimum_gbp: float = Field(default=0, ge=0, le=1000)
    # How long after the install date the chart and the profit estimate run.
    horizon_years: int = Field(default=20, ge=5, le=40)


def payback_settings() -> dict | None:
    stored = database().get_setting("payback")
    return json.loads(stored) if stored else None


@app.post("/api/roi/settings")
def save_payback_settings(body: PaybackIn) -> dict:
    database().set_setting("payback", body.model_dump_json())
    clear_caches()
    return payback_figures()


@app.get("/api/roi")
@kept
def payback_figures() -> dict:
    """Savings from the system so far and the estimated date it will have paid for itself."""
    settings_ = payback_settings()
    baselines = [{"id": "own", "name": "My own tariff, without solar or battery"}] + [
        {"id": str(row["id"]), "name": row["name"]} for row in database().comparison_rows()
    ]
    result: dict = {
        "configured": settings_ is not None,
        "settings": settings_,
        "baselines": baselines,
    }
    if settings_ is None:
        return result

    zone = local_timezone()
    now = datetime.now(UTC)
    today_local = now.astimezone(zone).date()
    installed = date.fromisoformat(settings_["install_date"])
    total_cost = sum(item["amount"] for item in settings_["costs"])

    rows = {str(row["id"]): row for row in database().comparison_rows()}
    chosen = rows.get(settings_["baseline"])
    without_system = Schedule([comparison_tariff(chosen)]) if chosen else schedule()
    result["baseline_name"] = chosen["name"] if chosen else "your own tariff"
    result["total_cost_gbp"] = round(total_cost, 2)

    counters = {}
    firsts = []
    for name in (*COST_COUNTERS, LOAD_COUNTER):
        first = database().first_time(name)
        if first is None:
            result["payback"] = {"has_data": False}
            return result
        firsts.append(first)
    # Whole local days only: from the later of the install date and the first reading, up to
    # the end of yesterday.
    start = max(datetime.combine(installed, clock(0), zone), max(firsts).astimezone(zone))
    end = local_midnight(now, zone)
    if start >= end:
        result["payback"] = {"has_data": False}
        return result
    samples = counter_samples(
        [*COST_COUNTERS, LOAD_COUNTER, SOLAR_COUNTER], start - timedelta(days=1), HALF_HOURLY, end
    )
    for name, series in samples.items():
        counters[name] = Counter(series, HALF_HOURLY_GAP)

    paid = roi.daily_costs(
        counters[COST_COUNTERS[0]], counters[COST_COUNTERS[1]], start, end, zone, schedule()
    )
    otherwise = roi.daily_costs(counters[LOAD_COUNTER], None, start, end, zone, without_system)
    savings = {day: otherwise[day] - paid[day] for day in paid}
    # Extra income (grid event payments and the like) counts towards paying the system off.
    # Regular income (Axle) is projected forward if chosen; one-offs never are.
    extra = 0.0
    extra_by_day: dict[date, float] = {}
    regular_by_day: dict[date, float] = {}
    one_off_by_day: dict[date, float] = {}
    for entry in database().income_rows():
        day = date.fromisoformat(entry["day"])
        if entry["amount_gbp"] and day in savings:
            savings[day] += entry["amount_gbp"]
            extra_by_day[day] = extra_by_day.get(day, 0.0) + entry["amount_gbp"]
            kind = regular_by_day if entry["regular"] else one_off_by_day
            kind[day] = kind.get(day, 0.0) + entry["amount_gbp"]
            extra += entry["amount_gbp"]
    result["extra_income_gbp"] = round(extra, 2)
    result["regular_income_gbp"] = round(sum(regular_by_day.values()), 2)
    result["one_off_income_gbp"] = round(sum(one_off_by_day.values()), 2)
    carry_on = settings_.get("project_extra_income", True)
    result["extra_income_projected"] = carry_on
    # How each line treats income in its projection.
    if carry_on:
        not_projected = one_off_by_day
        projected = {
            "regular": regular_by_day,
            "regular_minimum_per_year": settings_.get("regular_income_minimum_gbp", 0) * 12,
        }
    else:
        not_projected = extra_by_day
        projected = {}
    # The panels' share of the saving, where solar generation has been recorded.
    solar = None
    if counters[SOLAR_COUNTER]:
        solar = roi.daily_solar_value(
            counters[SOLAR_COUNTER],
            counters[LOAD_COUNTER],
            start,
            end,
            zone,
            without_system,
            schedule(),
        )
    # The same savings, worked out from the bills wherever there are bills.
    paid_before_export = roi.daily_costs(
        counters[COST_COUNTERS[0]], None, start, end, zone, schedule()
    )
    payback = result["payback"] = roi.payback(
        savings,
        total_cost,
        installed,
        today_local,
        solar,
        settings_.get("panel_ageing_percent", 0) / 100,
        settings_.get("battery_ageing_percent", 0) / 100,
        settings_.get("price_change_percent", 0) / 100,
        not_projected,
        settings_.get("horizon_years", 20),
        **projected,
    )
    if payback.get("has_data"):
        billed = roi.billed_line(
            otherwise,
            paid,
            paid_before_export,
            database().bill_rows(),
            extra_by_day,
            payback["estimated_before_gbp"],
        )
        if billed:
            # Carried forward the same way as the tracker's line, from the last billed day,
            # at the rate the bills show.
            ahead = roi.payback(
                billed.pop("savings"),
                total_cost,
                installed,
                today_local,
                one_off=not_projected,
                horizon_years=settings_.get("horizon_years", 20),
                **projected,
            )
            billed.update(
                projection=ahead["projection"],
                break_even=ahead["break_even"],
                already_reached=ahead["already_reached"],
                years_from_install=ahead["years_from_install"],
                yearly_gbp=ahead["yearly_gbp"],
                profit_at_horizon_gbp=ahead["profit_at_horizon_gbp"],
                rough=ahead["rough"],
            )
        result["bills_line"] = billed

        # Savings against a second tariff, such as the standard variable rate.
        second = rows.get(settings_.get("second_baseline") or "")
        result["second_line"] = None
        if second:
            alternative = Schedule([comparison_tariff(second)])
            instead = roi.daily_costs(counters[LOAD_COUNTER], None, start, end, zone, alternative)
            against = {day: instead[day] - paid[day] + extra_by_day.get(day, 0.0) for day in paid}
            other = roi.payback(
                against,
                total_cost,
                installed,
                today_local,
                one_off=not_projected,
                horizon_years=settings_.get("horizon_years", 20),
                **projected,
            )
            result["second_line"] = {
                "name": second["name"],
                "follows_published": bool(second.get("dynamic")),
                **{
                    key: other[key]
                    for key in (
                        "series",
                        "projection",
                        "saved_gbp",
                        "break_even",
                        "already_reached",
                        "years_from_install",
                        "yearly_gbp",
                        "profit_at_horizon_gbp",
                        "rough",
                    )
                },
            }

    return result


# --- Looking up published prices ---------------------------------------------------------------


def lookup_client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": "ha-energy-tracker"})


@app.get("/api/lookup/octopus")
def octopus_products() -> dict:
    """Octopus Energy import tariffs on sale now, and the regions prices are published for."""
    try:
        with lookup_client() as client:
            products, half_hourly = lookup.product_lists(client)
    except lookup.PriceLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "regions": [{"code": code, "name": name} for code, name in lookup.REGIONS.items()],
        "products": products,
        # Tariffs whose price changes every half hour: followed day by day, not typed in.
        "half_hourly": half_hourly,
    }


@app.get("/api/lookup/octopus/tariff")
def octopus_tariff(
    product: Annotated[str, Query(pattern=r"^[A-Z0-9-]{3,60}$")],
    region: Annotated[str, Query(pattern=r"^[A-P]$")],
) -> dict:
    """Today's prices for one Octopus Energy tariff, ready to drop into the comparison form."""
    today_local = datetime.now(local_timezone()).date()
    try:
        with lookup_client() as client:
            return lookup.fetch_tariff(client, product, region, local_timezone(), today_local)
    except lookup.PriceLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# --- Importing older history -----------------------------------------------------------------


@app.post("/api/import-history")
async def import_older_history(
    request: Request,
    offpeak_share: Annotated[float, Query(ge=0, le=1)] = 0.99,
    cheap_until: Annotated[str, Query(pattern=r"^([01]\d|2[0-3]):[03]0$")] = "06:00",
    dry_run: bool = False,
) -> dict:
    """Import hourly history from Home Assistant, and optionally a Sigenergy export.

    The request body is the Sigenergy .xlsx file itself (or empty to use Home Assistant only).
    """
    if not settings().collecting:
        raise HTTPException(status_code=409, detail="No Home Assistant connection is configured")
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large (20 MB at most)")

    def work() -> list[str]:
        with tempfile.TemporaryDirectory() as folder:
            path = None
            if body:
                path = Path(folder) / "export.xlsx"
                path.write_bytes(body)
            return import_history.run(
                settings(),
                database(),
                path,
                offpeak_share,
                clock.fromisoformat(cheap_until),
                dry_run,
            )

    try:
        report = await asyncio.to_thread(work)
    except sqlite3.Error:
        raise
    except Exception as exc:  # a bad file, or Home Assistant not answering: tell the user why
        log.warning("History import failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"Import failed: {exc}") from exc
    clear_caches()
    return {"report": report, "dry_run": dry_run}
