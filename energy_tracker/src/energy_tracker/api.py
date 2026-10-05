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
    history,
    import_history,
    income,
    live,
    lookup,
    performance,
    prices,
    roi,
    shift,
    tariff_store,
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
    price_refresh.set()
    if thread:
        await asyncio.to_thread(thread.join, 10)


app = FastAPI(title="Energy Tracker", version="0.17.3", lifespan=lifespan)
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
    samples = counter_samples(DEVICE_COUNTERS + COST_COUNTERS, midnight - timedelta(days=1), 5)
    used = database().latest(["load_energy_today"], midnight).get("load_energy_today")

    return {
        "energy": energy_by_device(
            samples,
            now,
            local_timezone(),
            used[1] if used else None,
            settings().ev_on_smart_load,
        ),
        "cost": cost_since(samples, midnight, now, local_timezone(), schedule()),
    }


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
        COST_COUNTERS,
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
        "cost": cost_of_month(samples, start, now, schedule()),
    }


# --- Keeping worked-out figures ready -----------------------------------------------------------
#
# The figures that cover a long stretch (years, payback, comparisons, performance) take
# seconds to work out on a small machine and change slowly, most of them only at midnight.
# They are kept once worked out, and a background task works them out afresh every few
# minutes, so opening the page never has to wait for them.

CACHE_SECONDS = 20 * 60  # how long a kept figure may be served
KEEP_FRESH_SECONDS = 10 * 60  # how often the usual ones are worked out again
CACHE_SIZE = 200


class Kept:
    """Results by key, each with the moment it was worked out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple, tuple[float, object]] = {}
        self._working: dict[tuple, threading.Lock] = {}
        self._generation = 0

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._generation += 1  # anything being worked out now is out of date already

    def get(self, key: tuple, work, fresh: bool = False):
        """The kept result for `key`, working it out if there is none (or `fresh` is set)."""
        with self._lock:
            held = self._values.get(key)
            if held and not fresh and time.monotonic() - held[0] < CACHE_SECONDS:
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
            return value


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
    """Work out the figures the page opens with, now and every few minutes after."""
    stop.wait(5)  # let the collector make its first reading and fill any gap
    while not stop.is_set():
        for work in (
            years.fresh,
            device_costs.fresh,
            bills.fresh,
            payback_figures.fresh,
            lambda: monthly_summary.fresh(None),
            lambda: system_performance.fresh("12m"),
            lambda: compare.fresh("12m"),
        ):
            if stop.is_set():
                return
            try:
                work()
            except Exception:  # one failing must not stop the others
                log.exception("Could not prepare figures in the background")
            stop.wait(1)  # leave room for the page between the heavy jobs
        stop.wait(KEEP_FRESH_SECONDS)


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
    }


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


def month_figures(start: datetime, now: datetime) -> dict | None:
    """Everything the summary says about one calendar month, or None if it has no readings."""
    zone = local_timezone()
    end = min(following_month(start), local_midnight(now, zone))  # whole days only
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

    return {
        "from": begin.date(),
        "to": last_day,
        "days": len(days),
        "complete": begin == start and end == following_month(start),
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


class AxleRateIn(BaseModel):
    p_per_kwh: float = Field(ge=0, le=2000)


@app.get("/api/income")
def extra_income() -> dict:
    """Income on top of the tariff: entries typed in, and recorded Axle Energy events."""
    entries = database().income_rows()
    by_year: dict[str, float] = {}
    for entry in entries:
        if entry["amount_gbp"]:
            year = entry["day"][:4]
            by_year[year] = round(by_year.get(year, 0.0) + entry["amount_gbp"], 2)
    return {
        "entries": entries,
        "total_gbp": round(sum(by_year.values()), 2),
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
    database().save_income(body.day, body.description.strip(), body.amount_gbp, id)
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
    costs: list[CostIn] = Field(max_length=20)
    # Optional yearly assumptions for the adjusted estimate, in percent. 0 leaves one out.
    panel_ageing_percent: float = Field(default=0, ge=0, le=5)
    battery_ageing_percent: float = Field(default=0, ge=0, le=10)
    price_change_percent: float = Field(default=0, ge=-10, le=20)
    # Whether extra income (grid event payments) is assumed to carry on at the same rate.
    # Either way it counts towards what has been saved so far.
    project_extra_income: bool = True


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
    extra = 0.0
    extra_by_day: dict[date, float] = {}
    for entry in database().income_rows():
        day = date.fromisoformat(entry["day"])
        if entry["amount_gbp"] and day in savings:
            savings[day] += entry["amount_gbp"]
            extra_by_day[day] = extra_by_day.get(day, 0.0) + entry["amount_gbp"]
            extra += entry["amount_gbp"]
    result["extra_income_gbp"] = round(extra, 2)
    carry_on = settings_.get("project_extra_income", True)
    result["extra_income_projected"] = carry_on
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
    result["payback"] = roi.payback(
        savings,
        total_cost,
        installed,
        today_local,
        solar,
        settings_.get("panel_ageing_percent", 0) / 100,
        settings_.get("battery_ageing_percent", 0) / 100,
        settings_.get("price_change_percent", 0) / 100,
        None if carry_on else extra_by_day,
    )

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
