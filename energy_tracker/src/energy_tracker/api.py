"""The app: web dashboard and API, with the collector running in the background.

Run with:  python -m energy_tracker
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import collector, import_history, tariff_store
from .config import Metric, Settings, load_metrics, load_settings
from .costs import combine
from .db import Database
from .tariff import Schedule, Tariff, build_tariff
from .today import COST_COUNTERS, DEVICE_COUNTERS, cost_since, energy_by_device, local_midnight

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the collector alongside the web server, and stop it cleanly on shutdown."""
    stop = threading.Event()
    thread = None
    if settings().collecting:
        thread = threading.Thread(
            target=collector.run,
            args=(settings(), database(), list(metrics_by_name().values()), stop),
            name="collector",
            daemon=True,
        )
        thread.start()
    else:
        log.warning("No Home Assistant connection configured: showing stored readings only")
    yield
    stop.set()
    if thread:
        await asyncio.to_thread(thread.join, 10)


app = FastAPI(title="Energy Tracker", version="0.3.1", lifespan=lifespan)


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


_years_cache: dict = {"at": 0.0, "value": None}
YEARS_CACHE_SECONDS = 300


@app.get("/api/years")
def years() -> dict:
    """Costs for every month and year since readings began, for comparing years."""
    if (
        _years_cache["value"] is not None
        and time.monotonic() - _years_cache["at"] < YEARS_CACHE_SECONDS
    ):
        return _years_cache["value"]

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

    _years_cache.update(at=time.monotonic(), value=result)
    return result


# --- Tariff rates ----------------------------------------------------------------------------


class BandIn(BaseModel):
    start: str
    end: str
    p_per_kwh: float = Field(ge=0, le=500)


class TariffIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    effective_from: date
    export_p_per_kwh: float = Field(ge=0, le=500)
    standing_charge_p_per_day: float = Field(ge=0, le=1000)
    vat_percent: float = Field(default=0, ge=0, le=100)
    import_bands: list[BandIn] = Field(min_length=1, max_length=12)


def tariff_json(tariff: Tariff) -> dict:
    return {
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tariff_store.save_period(database(), tariff)
    _years_cache["value"] = None  # costs must be worked out again with the new rates
    return list_tariffs()


@app.delete("/api/tariffs/{effective_from}")
def delete_tariff(effective_from: date) -> dict:
    try:
        found = tariff_store.delete_period(database(), effective_from)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="No tariff period starts on that date")
    _years_cache["value"] = None
    return list_tariffs()


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
    _years_cache["value"] = None
    return {"report": report, "dry_run": dry_run}
