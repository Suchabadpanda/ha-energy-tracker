"""Averages per day: by month and by year, and how the last 30 days compare.

Every figure is an average over whole days, so months and years of different lengths, or
only partly recorded, can be set side by side.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .usage import Counter

LOAD = "load_energy_total"
SOLAR = "pv_energy_total"
IMPORT = "import_energy_total"
EXPORT = "export_energy_total"
COUNTERS = [LOAD, SOLAR, IMPORT, EXPORT]
ENERGY = {"used": LOAD, "solar": SOLAR, "imported": IMPORT, "exported": EXPORT}


def daily_energy(
    counters: dict[str, Counter], first: date, last: date, timezone: ZoneInfo
) -> dict[date, dict]:
    """kWh used, generated, imported and exported on each local day from `first` to `last`."""
    days: dict[date, dict] = {}
    day = first
    while day <= last:
        start = datetime.combine(day, time(0), timezone)
        end = datetime.combine(day + timedelta(days=1), time(0), timezone)
        row = {}
        for key, name in ENERGY.items():
            counter = counters.get(name)
            if counter:
                row[key] = counter.between(start, end) or 0.0
        days[day] = row
        day += timedelta(days=1)
    return days


def _average(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 2) if values else None


def summarise(name: str, rows: list[dict], full_days: int) -> dict:
    """Averages per day for a group of days."""
    out = {"name": name, "days": len(rows), "complete": len(rows) >= full_days}
    for key in (
        "used",
        "solar",
        "imported",
        "exported",
        "net_gbp",
        "house_kwh",
        "ev_kwh",
        "heat_pump_kwh",
        "temperature",
    ):
        out[key] = _average(rows, key)
    used = sum(row.get("used", 0.0) for row in rows)
    imported = sum(row.get("imported", 0.0) for row in rows)
    # Share of the electricity used that did not have to be bought.
    out["self_sufficiency_percent"] = (
        round(max(0.0, min(1.0, 1 - imported / used)) * 100) if used > 0 else None
    )
    return out


def days_in_month(key: str) -> int:
    year, month = map(int, key.split("-"))
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - date(year, month, 1)).days


def build(days: dict[date, dict], today: date) -> dict:
    """Month and year averages, and the last 30 days against the 30 before."""
    if not days:
        return {"has_data": False}
    months: dict[str, list[dict]] = {}
    years: dict[str, list[dict]] = {}
    for day, row in sorted(days.items()):
        months.setdefault(f"{day:%Y-%m}", []).append(row)
        years.setdefault(f"{day:%Y}", []).append(row)

    def recent(first_back: int, last_back: int) -> list[dict]:
        return [
            row
            for day, row in days.items()
            if today - timedelta(days=first_back) <= day < today - timedelta(days=last_back)
        ]

    last30, before = recent(30, 0), recent(60, 30)
    year_ago = [
        row
        for day, row in days.items()
        if today - timedelta(days=365 + 30) <= day < today - timedelta(days=365)
    ]
    weekdays = [
        row for day, row in days.items() if day >= today - timedelta(days=90) and day.weekday() < 5
    ]
    weekends = [
        row for day, row in days.items() if day >= today - timedelta(days=90) and day.weekday() >= 5
    ]
    year_days = {day: row for day, row in days.items() if day >= today - timedelta(days=365)}

    def extreme(highest: bool) -> dict | None:
        used = {day: row["used"] for day, row in year_days.items() if row.get("used") is not None}
        if not used:
            return None
        day = (max if highest else min)(used, key=used.get)
        return {"day": day, "used": round(used[day], 1)}

    return {
        "has_data": True,
        "from": min(days),
        "to": max(days),
        "months": [summarise(k, v, days_in_month(k)) for k, v in months.items()],
        "years": [summarise(k, v, 366 if int(k) % 4 == 0 else 365) for k, v in years.items()],
        "last_30": summarise("Last 30 days", last30, 30) if last30 else None,
        "previous_30": summarise("The 30 days before", before, 30) if len(before) >= 20 else None,
        "year_ago_30": summarise("The same 30 days last year", year_ago, 30)
        if len(year_ago) >= 20
        else None,
        "weekday": summarise("Weekdays", weekdays, 1) if weekdays else None,
        "weekend": summarise("Weekends", weekends, 1) if weekends else None,
        "busiest_day": extreme(True),
        "quietest_day": extreme(False),
    }
