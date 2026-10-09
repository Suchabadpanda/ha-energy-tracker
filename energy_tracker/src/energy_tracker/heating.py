"""The heat pump against the weather.

A heat pump uses more electricity the colder it is outside. "Degree days" measure how cold
a day was: how far its average temperature fell below 15.5 °C, the usual UK base below
which a home needs heating. Electricity per degree day lets one winter be compared with
another fairly, whatever the weather, and a change in it points at a change in the heat
pump or its settings. Without a heat meter it is the nearest measure of efficiency there is.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BASE_C = 15.5
MIN_HOURS = 12  # hours of temperature readings needed to trust a day's average
MIN_DEGREE_DAYS = 50  # below this, hot water swamps heating and the ratio means little


def daily_means(hourly: list[tuple[datetime, float]], timezone: ZoneInfo) -> dict[date, float]:
    """Average temperature of each local day with enough hours of readings."""
    hours: dict[date, list[float]] = {}
    for when, value in hourly:
        hours.setdefault(when.astimezone(timezone).date(), []).append(value)
    return {day: sum(v) / len(v) for day, v in hours.items() if len(v) >= MIN_HOURS}


def season_of(day: date) -> str | None:
    """The heating season a day belongs to: October to April, as "2025/26". None in summer."""
    if 5 <= day.month <= 9:
        return None
    first = day.year if day.month >= 10 else day.year - 1
    return f"{first}/{str(first + 1)[2:]}"


def fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Straight line through (temperature, kWh) points: slope and intercept."""
    if len(points) < 10:
        return None
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if spread < 1:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return slope, mean_y - slope * mean_x


def analyse(used: dict[date, float], temperature: dict[date, float]) -> dict:
    """Heat pump use set against the weather: by day, by month and by heating season."""
    days = sorted(set(used) & set(temperature))
    if not days:
        return {"has_data": False, "days": 0}
    rows = [
        {
            "day": day,
            "temperature": round(temperature[day], 1),
            "kwh": round(used[day], 2),
            "degree_days": round(max(0.0, BASE_C - temperature[day]), 1),
        }
        for day in days
    ]

    def group(key) -> list[dict]:
        found: dict[str, list[dict]] = {}
        for row in rows:
            name = key(row["day"])
            if name is not None:
                found.setdefault(name, []).append(row)
        out = []
        for name, members in found.items():
            degree_days = sum(r["degree_days"] for r in members)
            kwh = sum(r["kwh"] for r in members)
            out.append(
                {
                    "name": name,
                    "days": len(members),
                    "mean_temperature": round(
                        sum(r["temperature"] for r in members) / len(members), 1
                    ),
                    "kwh": round(kwh, 1),
                    "degree_days": round(degree_days),
                    "kwh_per_degree_day": (
                        round(kwh / degree_days, 2) if degree_days >= MIN_DEGREE_DAYS else None
                    ),
                }
            )
        return out

    cold = [(r["temperature"], r["kwh"]) for r in rows if r["temperature"] < BASE_C]
    warm = [r["kwh"] for r in rows if r["temperature"] >= BASE_C]
    line = fit(cold)
    return {
        "has_data": True,
        "days": len(rows),
        "base_c": BASE_C,
        "points": rows,
        "months": group(lambda d: f"{d:%Y-%m}"),
        "seasons": [s for s in group(season_of) if s["degree_days"] >= MIN_DEGREE_DAYS],
        # kWh a day more for each degree colder, and the fitted line itself.
        "per_degree_colder_kwh": round(-line[0], 2) if line else None,
        "line": {"slope": round(line[0], 3), "intercept": round(line[1], 2)} if line else None,
        # Use on days warm enough to need no heating: mostly hot water.
        "warm_day_kwh": round(sum(warm) / len(warm), 2) if len(warm) >= 5 else None,
        "warm_days": len(warm),
    }
