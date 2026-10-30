"""Before and after an upgrade: how the system did in the weeks either side of a change
(more panels, a new inverter), allowing for the season.

Solar depends on the season far more than on anything changed: a month after an upgrade in
November has much less daylight than the month before it. So each day is set against how
much sun a cloudless sky would give on that date at the home's latitude, and the "before"
figures are scaled to the "after" period's daylight before the two are compared. Cloud is
not allowed for (there is no sunshine sensor): over a few weeks it largely evens out, and
the clear days (the best fifth, against what a cloudless sky could give) are compared on
their own as a steadier check.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from functools import lru_cache

from .performance import CHARGE, DISCHARGE, EXPORT, IMPORT, LOAD, SOLAR

DEFAULT_LATITUDE = 51.0  # southern England, if Home Assistant's home zone is not known
MIN_AFTER_DAYS = 14  # fewer days after than this: the comparison is shown as early
CLEAR_SHARE = 0.2  # the best fifth of days count as the clear ones
SOLAR_CONSTANT_KW = 1.353  # sunlight above the atmosphere, kW per m²


@lru_cache(maxsize=4096)
def clear_sky_kwh(day: date, latitude: float) -> float:
    """Sunshine a cloudless sky gives a flat square metre on `day`, in kWh.

    The sun's height through the day from its declination, and how much of its light gets
    through the air from how much air it passes through (Meinel's clear-sky model, with
    Kasten and Young's air mass). Five-minute steps; longitude does not change a day's total.
    """
    declination = math.radians(23.44) * math.sin(
        2 * math.pi * (284 + day.timetuple().tm_yday) / 365
    )
    lat = math.radians(latitude)
    total = 0.0
    step_hours = 5 / 60
    for step in range(288):
        hour_angle = math.radians((step + 0.5) * 5 / 4 - 180)  # 0 at solar noon
        height = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
            declination
        ) * math.cos(hour_angle)
        if height <= 0:
            continue
        degrees = math.degrees(math.asin(height))
        air_mass = 1 / (height + 0.50572 * (degrees + 6.07995) ** -1.6364)
        total += SOLAR_CONSTANT_KW * 0.7 ** (air_mass**0.678) * height * step_hours
    return total


def _group(days: dict[date, dict], latitude: float) -> dict | None:
    """Averages per day over a group of days, with the season's sunshine."""
    if not days:
        return None
    rows = list(days.values())
    count = len(rows)

    def total(key: str) -> float | None:
        values = [row[key] for row in rows if row.get(key) is not None]
        return sum(values) if values else None

    def average(key: str) -> float | None:
        values = [row[key] for row in rows if row.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else None

    solar, used = total(SOLAR), total(LOAD)
    imported, exported = total(IMPORT), total(EXPORT)
    sky = {day: clear_sky_kwh(day, latitude) for day in days}
    sky_total = sum(sky.values())
    # Each day's solar against what a cloudless sky could have given: the clear days have
    # the highest. Their average is little affected by how cloudy the period was.
    ratios = sorted(
        (row[SOLAR] / sky[day] for day, row in days.items() if row.get(SOLAR) is not None),
        reverse=True,
    )
    best = ratios[: max(1, round(len(ratios) * CLEAR_SHARE))] if ratios else []
    return {
        "first_day": min(days),
        "last_day": max(days),
        "days": count,
        "solar_kwh": round(solar / count, 2) if solar is not None else None,
        "used_kwh": round(used / count, 2) if used is not None else None,
        "imported_kwh": round(imported / count, 2) if imported is not None else None,
        "exported_kwh": round(exported / count, 2) if exported is not None else None,
        "charged_kwh": average(CHARGE),
        "discharged_kwh": average(DISCHARGE),
        "dear_kwh": average("dear_kwh"),
        "net_gbp": average("net_gbp"),
        "temperature": average("temperature"),
        "self_sufficiency_percent": round(max(0.0, min(1.0, 1 - imported / used)) * 100, 1)
        if used and imported is not None
        else None,
        "solar_used_percent": round(max(0.0, min(1.0, 1 - exported / solar)) * 100, 1)
        if solar and exported is not None
        else None,
        "clear_sky_kwh": round(sky_total / count, 2),
        # kWh of solar for each kWh per m² a cloudless sky would have given.
        "solar_per_sky": round(solar / sky_total, 3) if solar is not None and sky_total else None,
        "clear_day_per_sky": round(sum(best) / len(best), 3) if best else None,
        # Unrounded, for the comparison (taken out before it is returned).
        "_solar": solar / count if solar is not None else None,
        "_sky": sky_total / count,
        "_clear": sum(best) / len(best) if best else None,
    }


def _change(after: float | None, before: float | None) -> float | None:
    if after is None or not before:
        return None
    return round((after / before - 1) * 100, 1)


def compare(
    days: dict[date, dict],
    upgrade_day: date,
    today: date,
    length: int,
    latitude: float,
    kwp_before: float | None = None,
    kwp_after: float | None = None,
) -> dict:
    """The `length` days before `upgrade_day` against up to `length` days after it.

    `days` holds each day's figures (as performance.daily gives, with "net_gbp" and
    "temperature" where known). The upgrade day itself is left out: the work was probably
    done during it. Only days before `today` count.
    """
    before = {
        d: row for d, row in days.items() if upgrade_day - timedelta(days=length) <= d < upgrade_day
    }
    after = {
        d: row
        for d, row in days.items()
        if upgrade_day < d <= upgrade_day + timedelta(days=length) and d < today
    }
    result: dict = {
        "upgrade_day": upgrade_day,
        "length": length,
        "latitude": round(latitude, 2),
        "started": upgrade_day < today - timedelta(days=1),
        "before": _group(before, latitude),
        "after": _group(after, latitude),
        "early": len(after) < MIN_AFTER_DAYS,
        "expected_solar_change_percent": _change(kwp_after, kwp_before)
        if kwp_before and kwp_after
        else None,
    }
    b, a = result["before"], result["after"]
    raw = {
        name: {key: group.pop(key) for key in ("_solar", "_sky", "_clear")}
        for name, group in (("b", b), ("a", a))
        if group
    }
    if not (a and b):
        return result
    # The before period's solar as it would have been with the after period's daylight.
    adjusted = (
        raw["b"]["_solar"] * raw["a"]["_sky"] / raw["b"]["_sky"]
        if raw["b"]["_solar"] is not None and raw["b"]["_sky"]
        else None
    )
    result["changes"] = {
        "solar_before_adjusted_kwh": round(adjusted, 2) if adjusted is not None else None,
        "solar_percent": _change(raw["a"]["_solar"], adjusted),
        "clear_day_percent": _change(raw["a"]["_clear"], raw["b"]["_clear"]),
        "solar_kwh": _change(a["solar_kwh"], b["solar_kwh"]),
        "used_percent": _change(a["used_kwh"], b["used_kwh"]),
        "imported_percent": _change(a["imported_kwh"], b["imported_kwh"]),
        "exported_percent": _change(a["exported_kwh"], b["exported_kwh"]),
        "net_gbp_per_day": round(a["net_gbp"] - b["net_gbp"], 2)
        if a["net_gbp"] is not None and b["net_gbp"] is not None
        else None,
        "self_sufficiency_points": round(
            a["self_sufficiency_percent"] - b["self_sufficiency_percent"], 1
        )
        if a["self_sufficiency_percent"] is not None and b["self_sufficiency_percent"] is not None
        else None,
    }
    return result
