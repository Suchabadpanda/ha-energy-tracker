"""The app: web dashboard and API, with the collector running in the background.

Run with:  python -m energy_tracker
"""

from __future__ import annotations

import asyncio
import json
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

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import (
    billreader,
    collector,
    history,
    import_history,
    income,
    lookup,
    performance,
    roi,
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


app = FastAPI(title="Energy Tracker", version="0.13.1", lifespan=lifespan)


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
_roi_cache: dict = {"at": 0.0, "value": None}
_performance_cache: dict = {}


def clear_caches() -> None:
    """Forget worked-out costs, after anything they depend on has changed."""
    _years_cache["value"] = None
    _roi_cache["value"] = None
    _performance_cache.clear()


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
    import_bands: list[BandIn] = Field(min_length=1, max_length=48)


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
    clear_caches()  # costs must be worked out again with the new rates
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


class ComparisonIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    export_p_per_kwh: float = Field(ge=0, le=500)
    standing_charge_p_per_day: float = Field(ge=0, le=1000)
    vat_percent: float = Field(default=0, ge=0, le=100)
    import_bands: list[BandIn] = Field(min_length=1, max_length=48)


def comparison_tariff(row: dict) -> Tariff:
    return build_tariff(
        name=row["name"],
        import_bands=row["import_bands"],
        export_p_per_kwh=row["export_p_per_kwh"],
        standing_charge_p_per_day=row["standing_charge_p_per_day"],
        vat_percent=row["vat_percent"],
    )


@app.get("/api/compare")
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
    samples = counter_samples(COST_COUNTERS, start - timedelta(days=1), HALF_HOURLY, now)

    def cost_on(rates: Schedule) -> dict | None:
        return cost_since(samples, start, now, local_timezone(), rates, gap=HALF_HOURLY_GAP)

    actual = cost_on(schedule())
    result["actual"] = actual
    result["from"] = actual["since"] if actual else start
    result["days"] = actual["standing_charge_days"] if actual else 0
    for row in rows:
        cost = cost_on(Schedule([comparison_tariff(row)]))
        difference = round(cost["net_gbp"] - actual["net_gbp"], 2) if cost and actual else None
        result["candidates"].append({**row, "cost": cost, "difference_gbp": difference})
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
    _roi_cache["value"] = None  # it may be the tariff payback is measured against
    return {"id": saved}


@app.delete("/api/compare/tariffs/{row_id}")
def delete_comparison(row_id: int) -> dict:
    if not database().delete_comparison_row(row_id):
        raise HTTPException(status_code=404, detail="That tariff is not in the list")
    _roi_cache["value"] = None
    return {"removed": row_id}


# --- Performance and battery sizing --------------------------------------------------------------


@app.get("/api/performance")
def system_performance(
    period: Annotated[str, Query(pattern="^(30d|90d|12m|all)$")] = "12m",
) -> dict:
    """Self-sufficiency, battery efficiency and what a bigger battery would have saved."""
    cached = _performance_cache.get(period)
    if cached and time.monotonic() - cached[0] < ROI_CACHE_SECONDS:
        return cached[1]

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
    _performance_cache[period] = (time.monotonic(), result)
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
        return await asyncio.to_thread(billreader.read_pdf, body)
    except billreader.BillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/bills/{row_id}")
def delete_bill(row_id: int) -> dict:
    if not database().delete_bill(row_id):
        raise HTTPException(status_code=404, detail="That bill is not in the list")
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
    _roi_cache["value"] = None
    return extra_income()


@app.delete("/api/income/{row_id}")
def remove_income(row_id: int) -> dict:
    if not database().remove_income(row_id):
        raise HTTPException(status_code=404, detail="That entry is not in the list")
    _roi_cache["value"] = None
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
ROI_CACHE_SECONDS = 1800


class CostIn(BaseModel):
    date: date
    description: str = Field(default="", max_length=80)
    amount: float = Field(gt=0, le=10_000_000)


class PaybackIn(BaseModel):
    install_date: date
    # "own": the tariff you were on each day. Otherwise the id of a tariff saved for comparison.
    baseline: str = Field(default="own", pattern=r"^(own|\d{1,9})$")
    costs: list[CostIn] = Field(max_length=20)
    # Optional yearly assumptions for the adjusted estimate, in percent. 0 leaves one out.
    panel_ageing_percent: float = Field(default=0, ge=0, le=5)
    battery_ageing_percent: float = Field(default=0, ge=0, le=10)
    price_change_percent: float = Field(default=0, ge=-10, le=20)


def payback_settings() -> dict | None:
    stored = database().get_setting("payback")
    return json.loads(stored) if stored else None


@app.post("/api/roi/settings")
def save_payback_settings(body: PaybackIn) -> dict:
    database().set_setting("payback", body.model_dump_json())
    _roi_cache["value"] = None
    return payback_figures()


@app.get("/api/roi")
def payback_figures() -> dict:
    """Savings from the system so far and the estimated date it will have paid for itself."""
    if _roi_cache["value"] is not None and time.monotonic() - _roi_cache["at"] < ROI_CACHE_SECONDS:
        return _roi_cache["value"]

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
    for entry in database().income_rows():
        day = date.fromisoformat(entry["day"])
        if entry["amount_gbp"] and day in savings:
            savings[day] += entry["amount_gbp"]
            extra += entry["amount_gbp"]
    result["extra_income_gbp"] = round(extra, 2)
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
    )

    _roi_cache.update(at=time.monotonic(), value=result)
    return result


# --- Looking up published prices ---------------------------------------------------------------


def lookup_client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": "ha-energy-tracker"})


@app.get("/api/lookup/octopus")
def octopus_products() -> dict:
    """Octopus Energy import tariffs on sale now, and the regions prices are published for."""
    try:
        with lookup_client() as client:
            products = lookup.list_products(client)
    except lookup.PriceLookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "regions": [{"code": code, "name": name} for code, name in lookup.REGIONS.items()],
        "products": products,
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
