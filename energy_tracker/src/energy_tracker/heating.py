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


# --- Estimated SCOP, from the house's heat loss -----------------------------------------------
#
# Without a heat meter, the heat the house needed can be estimated from its heat loss survey:
# the heat loss at the design temperatures gives how many kW it loses for each degree colder
# outside, and each day's degree days say how cold the day was. Heat needed divided by the
# electricity the heat pump used for heating is its efficiency. Survey figures tend to be on
# the high side, so this is an estimate, best for comparing one period with another.

MIN_HEATING_KWH = 5.0  # below this a period's SCOP means little


def kw_per_degree(design: dict) -> float | None:
    """Heat lost for each degree between inside and outside, in kW."""
    spread = design.get("indoor_c", 21.0) - design.get("outdoor_c", -3.0)
    if not design.get("heat_loss_kw") or spread <= 0:
        return None
    return design["heat_loss_kw"] / spread


def scop(
    found: dict, design: dict, today: date, hot_water: dict[date, float] | None = None
) -> dict | None:
    """Estimated heating efficiency by month and winter, from `analyse`'s results.

    Each day's heating electricity is its use less its hot water: as measured from the
    cylinder's reheats (`hot_water`, kWh by day) where there is that, or else the usual
    warm-day use. Each
    day's heat is kW per degree × 24 hours × degree days below `base_c`; the base, lower than
    the indoor temperature, allows for free heat from people, appliances and the sun.
    """
    per_degree = kw_per_degree(design)
    if per_degree is None or not found.get("has_data"):
        return None
    base = design.get("base_c") or BASE_C
    usual = found.get("warm_day_kwh") or 0.0
    measured = hot_water or {}
    days = []
    for row in found["points"]:
        water = measured.get(row["day"], usual)
        heating = max(0.0, row["kwh"] - min(row["kwh"], water))
        heat = per_degree * 24 * max(0.0, base - row["temperature"])
        days.append((row["day"], heat, heating))

    def summary(name: str, members: list[tuple]) -> dict:
        heat = sum(h for _, h, _ in members)
        used = sum(e for _, _, e in members)
        degree_days = heat / (per_degree * 24)
        # Only with enough heating to go on: a few mild days say little.
        enough = degree_days >= MIN_DEGREE_DAYS and used >= MIN_HEATING_KWH
        return {
            "name": name,
            "days": len(members),
            "heat_kwh": round(heat),
            "heating_kwh": round(used, 1),
            "scop": round(heat / used, 2) if enough else None,
        }

    def grouped(key) -> list[dict]:
        found_groups: dict[str, list[tuple]] = {}
        for item in days:
            name = key(item[0])
            if name is not None:
                found_groups.setdefault(name, []).append(item)
        return [summary(name, members) for name, members in found_groups.items()]

    recent = [item for item in days if (today - item[0]).days <= 30]
    cold = [item for item in days if item[1] > 0]
    result = {
        "kw_per_degree": round(per_degree, 3),
        "base_c": base,
        "hot_water_kwh_a_day": usual or None,
        "measured_days": sum(1 for row in found["points"] if row["day"] in measured),
        "months": grouped(lambda d: f"{d:%Y-%m}"),
        "seasons": grouped(season_of),
        "last_30_days": summary("Last 30 days", recent) if recent else None,
        "overall": summary("All days needing heat", cold) if cold else None,
    }
    # What the survey expects over a typical year, where it gives the degree days.
    if design.get("degree_days"):
        heat = per_degree * 24 * design["degree_days"]
        result["expected_heat_kwh"] = round(heat)
        if design.get("design_scop"):
            result["expected_heating_kwh"] = round(heat / design["design_scop"])
    return result
