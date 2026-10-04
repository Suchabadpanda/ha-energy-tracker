"""Payback: how much the solar and battery system has saved, and when it will have paid
for itself.

Saving on a day = what the house's consumption would have cost bought entirely from the
grid (no solar, no battery, no export income) minus what was actually paid.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots

YEAR = 365
MAX_PROJECTION_DAYS = 60 * YEAR  # stop looking for a break-even date after 60 years


def daily_costs(
    imports: Counter,
    exports: Counter | None,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
) -> dict[date, float]:
    """Net cost in pounds of each local day between `start` and `end`.

    Import at the rate for its time of day, less export income, plus the standing charge.
    Pass `exports=None` for a house that exports nothing.
    """
    pence: dict[date, float] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        day = local.date()
        tariff = schedule.on(day)
        if day not in pence:
            pence[day] = tariff.standing_charge_p_per_day * tariff.vat_multiplier
        imported = imports.between(piece_start, piece_end)
        pence[day] += imported * tariff.band_at(local).p_per_kwh * tariff.vat_multiplier
        if exports is not None:
            pence[day] -= exports.between(piece_start, piece_end) * tariff.export_p_per_kwh
    return {day: value / 100 for day, value in pence.items()}


def daily_solar_value(
    solar: Counter,
    load: Counter,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    import_prices: Schedule,
    export_prices: Schedule,
) -> dict[date, float]:
    """Pounds per day the panels alone would have saved, had there been no battery.

    In each half hour, generation up to the household's consumption is used directly and
    saves the import price for that time; the rest is exported. Whatever the system saved
    beyond this is put down to the battery.
    """
    pence: dict[date, float] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        day = local.date()
        generated = solar.between(piece_start, piece_end)
        used = min(generated, load.between(piece_start, piece_end))
        buying = import_prices.on(day)
        pence[day] = (
            pence.get(day, 0.0)
            + used * buying.band_at(local).p_per_kwh * buying.vat_multiplier
            + (generated - used) * export_prices.on(day).export_p_per_kwh
        )
    return {day: value / 100 for day, value in pence.items()}


def _project(
    running: float,
    last_day: date,
    solar_pattern: list[float],
    battery_pattern: list[float],
    total_cost: float,
    panel_ageing: float = 0.0,
    battery_ageing: float = 0.0,
    price_change: float = 0.0,
) -> tuple[date | None, list[tuple[date, float]]]:
    """Carry savings forward day by day until they reach the cost.

    The yearly rates are fractions (0.005 for 0.5%). Panel ageing shrinks the solar part,
    battery ageing the battery part, and a price change scales both.
    """
    points = [(last_day, round(running, 2))]
    for ahead in range(MAX_PROJECTION_DAYS):
        years = (ahead + 1) / YEAR
        prices = (1 + price_change) ** years
        running += prices * (
            solar_pattern[ahead % len(solar_pattern)] * (1 - panel_ageing) ** years
            + battery_pattern[ahead % len(battery_pattern)] * (1 - battery_ageing) ** years
        )
        day = last_day + timedelta(days=ahead + 1)
        done = running >= total_cost
        if done or ahead % 30 == 29:
            points.append((day, round(running, 2)))
        if done:
            return day, points
    return None, points  # not reached within the limit: no date, and no line to draw


def payback(
    savings: dict[date, float],
    total_cost: float,
    installed: date,
    today: date,
    solar: dict[date, float] | None = None,
    panel_ageing: float = 0.0,
    battery_ageing: float = 0.0,
    price_change: float = 0.0,
) -> dict:
    """Cumulative savings so far, and a projection to the day they equal the system's cost.

    `savings` is pounds saved on each day with readings. Days between the install date and
    the first reading are filled in at the average, and reported separately.

    `solar` is the part of each day's saving due to the panels alone; the rest is the
    battery's. With it, and any of the yearly rates set, a second "adjusted" projection is
    worked out alongside the plain one.
    """
    days = sorted(savings)
    if not days:
        return {"has_data": False}
    values = [savings[d] for d in days]
    average = sum(values) / len(values)
    # Without a solar figure nothing can be told apart, so all of it counts as "battery"
    # and panel ageing has nothing to act on.
    solar_values = [solar.get(d, 0.0) for d in days] if solar is not None else [0.0] * len(days)
    battery_values = [total - sun for total, sun in zip(values, solar_values, strict=True)]

    # Time the system was running before readings began.
    missing_days = max(0, (days[0] - installed).days)
    estimated_before = average * missing_days

    running = estimated_before
    reached: date | None = None
    series: list[tuple[date, float]] = [(days[0] - timedelta(days=1), round(running, 2))]
    for index, day in enumerate(days):
        running += savings[day]
        if reached is None and total_cost > 0 and running >= total_cost:
            reached = day
        if index % 7 == 6 or index == len(days) - 1:  # weekly points are plenty for a chart
            series.append((day, round(running, 2)))
    saved = running

    # With a full year of readings, repeat the last 365 days so the seasons are respected.
    # With less, all that can be done is to carry the average forward.
    rough = len(days) < YEAR
    if rough:
        solar_pattern = [sum(solar_values) / len(days)]
        battery_pattern = [sum(battery_values) / len(days)]
        yearly_solar, yearly_battery = solar_pattern[0] * YEAR, battery_pattern[0] * YEAR
    else:
        solar_pattern, battery_pattern = solar_values[-YEAR:], battery_values[-YEAR:]
        yearly_solar, yearly_battery = sum(solar_pattern), sum(battery_pattern)
    yearly = yearly_solar + yearly_battery

    def years_after_install(day: date | None) -> float | None:
        return round((day - installed).days / 365.25, 1) if day else None

    projection: list[tuple[date, float]] = []
    break_even = reached
    adjusted = None
    if reached is None and total_cost > 0:
        if yearly > 0:
            break_even, projection = _project(
                saved, days[-1], solar_pattern, battery_pattern, total_cost
            )
            if break_even is None:
                projection = []
        if panel_ageing or battery_ageing or price_change:
            adjusted_day, adjusted_points = _project(
                saved,
                days[-1],
                solar_pattern,
                battery_pattern,
                total_cost,
                panel_ageing,
                battery_ageing,
                price_change,
            )
            adjusted = {
                "break_even": adjusted_day,
                "years_from_install": years_after_install(adjusted_day),
                "projection": adjusted_points if adjusted_day else [],
            }

    return {
        "has_data": True,
        "first_day": days[0],
        "last_day": days[-1],
        "days": len(days),
        "saved_gbp": round(saved, 2),
        "estimated_before_gbp": round(estimated_before, 2),
        "estimated_before_days": missing_days,
        "average_per_day_gbp": round(average, 2),
        "yearly_gbp": round(yearly, 2),
        "yearly_solar_gbp": round(yearly_solar, 2) if solar is not None else None,
        "yearly_battery_gbp": round(yearly_battery, 2) if solar is not None else None,
        "percent": round(saved / total_cost * 100, 1) if total_cost > 0 else None,
        "rough": rough,
        "already_reached": reached is not None,
        "break_even": break_even,
        "years_from_install": years_after_install(break_even),
        "series": series,
        "projection": projection,
        "adjusted": adjusted,
        "today": today,
    }
