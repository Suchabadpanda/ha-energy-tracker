"""Supply headroom: how close the house comes to what its supply can carry, and what was
running when it came closest.

Worked out from the power readings kept in full detail (the last `detail_days`), as an
average over each minute: a fuse is not troubled by a spike lasting a second or two, but
the sensors are only read every half minute anyway. The export side is set against the
export limit agreed with the network operator, which the inverter holds exports to.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

GRID, LOAD, SMART, EV, BATTERY, SOLAR = (
    "grid_power",
    "load_power",
    "smart_load_power",
    "ev_charger_power",
    "battery_power",
    "pv_power",
)
POWERS = [GRID, LOAD, SMART, EV, BATTERY, SOLAR]
VOLTS = 230  # UK nominal
PEAKS = 8  # the highest moments listed
PEAK_GAP = timedelta(minutes=30)  # moments closer than this count as the same peak
LEVELS = (50, 75, 90)  # shares of the supply counted towards
AT_LIMIT = 0.97  # exporting at this share of the limit or more counts as held at the limit


def supply_kw(fuse_amps: float | None) -> float | None:
    """What a single-phase supply with this main fuse can carry, in kW."""
    return round(fuse_amps * VOLTS / 1000, 1) if fuse_amps else None


def moments(series: dict[str, list[tuple[datetime, float]]]) -> list[dict]:
    """The readings joined by time: one row per minute that has a grid reading."""
    by_time: dict[datetime, dict] = {when: {GRID: value} for when, value in series.get(GRID, [])}
    for name, samples in series.items():
        if name == GRID:
            continue
        for when, value in samples:
            if when in by_time:
                by_time[when][name] = value
    return [{"time": when, **values} for when, values in sorted(by_time.items())]


def parts(row: dict, ev_on_smart_load: bool) -> dict:
    """What was drawing power at a moment: the heat pump (or other smart load), the car,
    the battery charging and the rest of the house, in kW."""
    smart, ev = row.get(SMART), row.get(EV)
    heat_pump = None
    if smart is not None:
        heat_pump = max(0.0, smart - ((ev or 0.0) if ev_on_smart_load else 0.0))
    measured = (heat_pump or 0.0) + (ev or 0.0)
    load = row.get(LOAD)
    battery = row.get(BATTERY)
    return {
        "heat_pump_kw": round(heat_pump, 2) if heat_pump is not None else None,
        "ev_kw": round(max(0.0, ev), 2) if ev is not None else None,
        "battery_charging_kw": round(max(0.0, battery), 2) if battery is not None else None,
        "house_kw": round(max(0.0, load - measured), 2) if load is not None else None,
        "solar_kw": round(max(0.0, row[SOLAR]), 2) if row.get(SOLAR) is not None else None,
    }


def _top(rows: list[dict], key: str, count: int) -> list[dict]:
    """The highest rows by `key`, no two within PEAK_GAP of each other."""
    chosen: list[dict] = []
    for row in sorted(rows, key=lambda r: r[key], reverse=True):
        if row[key] <= 0 or len(chosen) >= count:
            break
        if all(abs(row["time"] - other["time"]) >= PEAK_GAP for other in chosen):
            chosen.append(row)
    return chosen


def analyse(
    series: dict[str, list[tuple[datetime, float]]],
    timezone: ZoneInfo,
    supply: float | None,
    export_limit: float | None,
    ev_on_smart_load: bool,
    minutes_each: float = 1.0,
) -> dict:
    """Peaks, the pattern through the day and time spent near the limits.

    `series` holds each power's average for every minute (`minutes_each` minutes a row).
    `supply` is what the supply can carry and `export_limit` the agreed export limit, in kW.
    """
    rows = moments(series)
    if not rows:
        return {"has_data": False}
    for row in rows:
        row["import_kw"] = max(0.0, row[GRID])
        row["export_kw"] = max(0.0, -row[GRID])
        local = row["time"].astimezone(timezone)
        row["day"], row["hour"] = local.date(), local.hour

    peaks = [
        {
            "time": row["time"],
            "import_kw": round(row["import_kw"], 2),
            **parts(row, ev_on_smart_load),
        }
        for row in _top(rows, "import_kw", PEAKS)
    ]
    # Through the day: the highest import in each hour, and the average of each day's
    # highest in that hour (what an ordinary day comes to, rather than the worst).
    highest: dict[int, float] = {}
    daily: dict[tuple[date, int], float] = {}
    for row in rows:
        hour = row["hour"]
        highest[hour] = max(highest.get(hour, 0.0), row["import_kw"])
        key = (row["day"], hour)
        daily[key] = max(daily.get(key, 0.0), row["import_kw"])
    typical: dict[int, list[float]] = {}
    for (_, hour), value in daily.items():
        typical.setdefault(hour, []).append(value)
    hours = [
        {
            "hour": hour,
            "highest_kw": round(highest[hour], 2),
            "typical_kw": round(sum(typical[hour]) / len(typical[hour]), 2),
        }
        for hour in sorted(highest)
    ]
    by_day: dict[date, float] = {}
    for row in rows:
        by_day[row["day"]] = max(by_day.get(row["day"], 0.0), row["import_kw"])

    result: dict = {
        "has_data": True,
        "from": rows[0]["time"],
        "to": rows[-1]["time"],
        "days": len(by_day),
        "supply_kw": supply,
        "export_limit_kw": export_limit,
        "highest_import_kw": peaks[0]["import_kw"] if peaks else 0.0,
        "peaks": peaks,
        "hours": hours,
        "daily_peaks": [{"day": d, "import_kw": round(v, 2)} for d, v in sorted(by_day.items())],
        "above": None,
        "export": None,
    }
    if supply:
        result["highest_percent"] = round(result["highest_import_kw"] / supply * 100)
        result["above"] = [
            {
                "percent": level,
                "kw": round(supply * level / 100, 1),
                "minutes": round(
                    sum(minutes_each for row in rows if row["import_kw"] >= supply * level / 100)
                ),
            }
            for level in LEVELS
        ]
    highest_export = max(row["export_kw"] for row in rows)
    export: dict = {"highest_kw": round(highest_export, 2)}
    if export_limit:
        held = [row for row in rows if row["export_kw"] >= export_limit * AT_LIMIT]
        export.update(
            limit_kw=export_limit,
            minutes_at_limit=round(len(held) * minutes_each),
            days_at_limit=len({row["day"] for row in held}),
            # Solar beyond what the house, the battery and the limit could take is held back
            # by the inverter: how much, can't be measured, but these are the times it was.
            sunniest=[
                {
                    "time": row["time"],
                    "export_kw": round(row["export_kw"], 2),
                    "solar_kw": round(row.get(SOLAR) or 0.0, 2),
                }
                for row in _top(held, "export_kw", 3)
            ],
        )
    result["export"] = export
    return result


def busiest_half_hour(
    samples: list[tuple[datetime, float]],
) -> dict | None:
    """The half hour with the most imported, as an average in kW, from an import counter's
    half-hourly readings (kept for every day stored, not only the detailed ones)."""
    best: tuple[float, datetime] | None = None
    for (start, before), (end, after) in zip(samples, samples[1:], strict=False):
        if timedelta(minutes=25) <= end - start <= timedelta(minutes=35) and after >= before:
            kw = (after - before) / ((end - start).total_seconds() / 3600)
            if best is None or kw > best[0]:
                best = (kw, start)
    return {"import_kw": round(best[0], 2), "start": best[1]} if best else None
