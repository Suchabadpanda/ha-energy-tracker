"""Energy and cost for any past period, split into hours, days or months.

Everything comes from the lifetime energy counters, so it works for the whole of the
stored history, including readings imported from before the app was installed.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots

# Column name -> the counter it is worked out from.
ENERGY_COLUMNS = {
    "solar_kwh": "pv_energy_total",
    "consumption_kwh": "load_energy_total",
    "import_kwh": "import_energy_total",
    "export_kwh": "export_energy_total",
    "battery_charge_kwh": "battery_charge_energy_total",
    "battery_discharge_kwh": "battery_discharge_energy_total",
    "smart_load_kwh": "smart_load_energy_total",
    "ev_charger_kwh": "ev_charger_energy_total",
}
COUNTERS = list(ENERGY_COLUMNS.values())

# What each view is divided into, and what the CSV export may be divided into.
VIEW_INTERVAL = {"day": "hour", "week": "day", "month": "day", "year": "month"}
INTERVALS = ("halfhour", "hour", "day", "month")


def period_bounds(period: str, day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """Start and end of the day, week (Monday start), month or year containing `day`."""
    if period == "week":
        day -= timedelta(days=day.weekday())
        last = day + timedelta(days=7)
    elif period == "month":
        day = day.replace(day=1)
        last = (day + timedelta(days=32)).replace(day=1)
    elif period == "year":
        day = day.replace(month=1, day=1)
        last = day.replace(year=day.year + 1)
    else:
        last = day + timedelta(days=1)
    midnight = datetime.min.time()
    return datetime.combine(day, midnight, timezone), datetime.combine(last, midnight, timezone)


def step_starts(
    start: datetime, end: datetime, interval: str, timezone: ZoneInfo
) -> list[datetime]:
    """The moment each interval begins, from `start` up to (not including) `end`."""
    starts: list[datetime] = []
    if interval in ("halfhour", "hour"):
        # Counted in real elapsed time, so a day the clocks change has 23 or 25 hours.
        step = timedelta(minutes=30 if interval == "halfhour" else 60)
        cursor = start.astimezone(UTC)
        while cursor < end:
            starts.append(cursor.astimezone(timezone))
            cursor += step
        return starts
    day = start.astimezone(timezone).date()
    while True:
        moment = datetime.combine(day, datetime.min.time(), timezone)
        if moment >= end:
            return starts
        starts.append(moment)
        day = (
            day + timedelta(days=1)
            if interval == "day"
            else (day + timedelta(days=32)).replace(day=1)
        )


def rows(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    interval: str,
    timezone: ZoneInfo,
    schedule: Schedule,
    now: datetime,
) -> list[dict]:
    """One row per interval: kWh for every counter that has readings, and what it cost.

    Intervals with no readings yet (the future, or before the first reading) have empty
    values, not zeros. The interval containing `now` is counted up to `now`.
    """
    starts = step_starts(start, end, interval, timezone)
    imports, exports = counters.get("import_energy_total"), counters.get("export_energy_total")
    result = []
    for index, begin in enumerate(starts):
        finish = starts[index + 1] if index + 1 < len(starts) else end
        row: dict = {"start": begin, "end": finish}
        upto = min(finish, now)
        for column, name in ENERGY_COLUMNS.items():
            counter = counters.get(name)
            covered = (
                counter and counter.first_time < upto and begin < counter.times[-1] + counter.gap
            )
            if not counter:
                continue  # this installation does not collect it: leave the column out
            row[column] = (
                round(counter.between(begin, upto), 3) if covered and begin < now else None
            )

        # Cost needs half-hour pricing whatever the interval. The standing charge is left
        # out here: it is a daily amount and belongs to the period's total.
        priced = imports and exports and row.get("import_kwh") is not None
        if priced:
            import_pence = export_pence = 0.0
            for piece_start, piece_end in half_hour_slots(begin, upto):
                local = piece_start.astimezone(timezone)
                tariff = schedule.on(local.date())
                rate = tariff.band_at(local).p_per_kwh * tariff.vat_multiplier
                import_pence += imports.between(piece_start, piece_end) * rate
                export_pence += exports.between(piece_start, piece_end) * tariff.export_p_per_kwh
            row["import_cost_gbp"] = round(import_pence / 100, 4)
            row["export_credit_gbp"] = round(export_pence / 100, 4)
        elif imports and exports:
            row["import_cost_gbp"] = row["export_credit_gbp"] = None
        result.append(row)
    return result


def totals(data: list[dict]) -> dict:
    """Sum of every numeric column over the rows that have a value."""
    summed: dict = {}
    for row in data:
        for key, value in row.items():
            if isinstance(value, int | float):
                summed[key] = summed.get(key, 0.0) + value
    return {key: round(value, 3) for key, value in summed.items()}


def to_csv(data: list[dict], timezone: ZoneInfo) -> str:
    """The rows as CSV text, with times in local time. Empty cells mean no reading."""
    columns = [
        c
        for c in (*ENERGY_COLUMNS, "import_cost_gbp", "export_credit_gbp")
        if any(c in r for r in data)
    ]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    # Money columns are in your own currency, so the headings do not name one.
    writer.writerow(["start", "end", *[c.removesuffix("_gbp") for c in columns]])
    for row in data:
        writer.writerow(
            [
                row["start"].astimezone(timezone).isoformat(timespec="minutes"),
                row["end"].astimezone(timezone).isoformat(timespec="minutes"),
                *["" if row.get(c) is None else row[c] for c in columns],
            ]
        )
    return out.getvalue()
