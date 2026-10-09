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
        pence[day] += imported * tariff.import_price(local)
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
            + used * buying.import_price(local)
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
    horizon: date | None = None,
    reached: date | None = None,
    income_pattern: list[float] | None = None,
) -> tuple[date | None, list[tuple[date, float]], float | None]:
    """Carry savings forward day by day: to the day they reach the cost, and on to `horizon`.

    The yearly rates are fractions (0.005 for 0.5%). Panel ageing shrinks the solar part,
    battery ageing the battery part, and a price change scales both. `reached` is the
    break-even day if it has already passed. `income_pattern` is regular income (Axle),
    carried on as it is: it does not age with the panels or battery, or follow energy prices.

    Returns the break-even day (None if not reached within the limit), points for a chart,
    and the total saved by the horizon (None if the horizon is not after `last_day`). The
    points stop at the horizon, or at break-even if that comes later.
    """
    horizon = horizon or last_day
    points = [(last_day, round(running, 2))]
    break_even = reached
    at_horizon = None
    for ahead in range(MAX_PROJECTION_DAYS):
        day = last_day + timedelta(days=ahead + 1)
        if break_even is not None and day > horizon:
            break
        years = (ahead + 1) / YEAR
        prices = (1 + price_change) ** years
        running += prices * (
            solar_pattern[ahead % len(solar_pattern)] * (1 - panel_ageing) ** years
            + battery_pattern[ahead % len(battery_pattern)] * (1 - battery_ageing) ** years
        )
        if income_pattern:
            running += income_pattern[ahead % len(income_pattern)]
        just_reached = break_even is None and running >= total_cost
        if just_reached:
            break_even = day
        if day == horizon:
            at_horizon = running
        if day <= horizon or just_reached:
            if just_reached or day == horizon or ahead % 30 == 29:
                points.append((day, round(running, 2)))
    return break_even, points, at_horizon


def payback(
    savings: dict[date, float],
    total_cost: float,
    installed: date,
    today: date,
    solar: dict[date, float] | None = None,
    panel_ageing: float = 0.0,
    battery_ageing: float = 0.0,
    price_change: float = 0.0,
    one_off: dict[date, float] | None = None,
    horizon_years: int = 20,
    regular: dict[date, float] | None = None,
    regular_minimum_per_year: float = 0.0,
) -> dict:
    """Cumulative savings so far, and a projection to the day they equal the system's cost.

    `one_off` is the part of each day's saving that should not be assumed to carry on
    (extra income the user has chosen to leave out of the projection). It counts in full
    towards what has been saved, but not towards the rate savings are projected at.

    `regular` is the part that is income which can be expected to carry on, such as Axle's
    monthly payments. It is projected on its own, at no less than `regular_minimum_per_year`
    (Axle's guaranteed minimum, say), and does not age with the equipment.

    The projection runs on past break-even to `horizon_years` after the install date, to
    show what the system could be worth beyond its cost.

    `savings` is pounds saved on each day with readings. Days between the install date and
    the first reading are filled in at the average, and reported separately.

    `solar` is the part of each day's saving due to the panels alone; the rest is the
    battery's. With it, and any of the yearly rates set, a second "adjusted" projection is
    worked out alongside the plain one.
    """
    days = sorted(savings)
    if not days:
        return {"has_data": False}
    # What can be expected to carry on: each day's saving less anything one-off.
    income = [(regular or {}).get(d, 0.0) for d in days]
    values = [savings[d] - (one_off or {}).get(d, 0.0) for d in days]
    average = sum(values) / len(values)
    # Energy savings alone, to be aged; regular income is carried on separately.
    values = [value - paid for value, paid in zip(values, income, strict=True)]
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
    income_pattern = (
        ([sum(income) / len(days)] if rough else income[-YEAR:]) if regular is not None else [0.0]
    )
    yearly_income = sum(income_pattern) * (YEAR if rough else 1)
    if regular is not None and yearly_income < regular_minimum_per_year:
        # At least the guaranteed minimum: the shortfall spread evenly over the year.
        top_up = (regular_minimum_per_year - yearly_income) / YEAR
        income_pattern = [value + top_up for value in income_pattern]
        yearly_income = regular_minimum_per_year
    yearly = yearly_solar + yearly_battery + yearly_income

    def years_after_install(day: date | None) -> float | None:
        return round((day - installed).days / 365.25, 1) if day else None

    horizon = installed + timedelta(days=round(horizon_years * 365.25))
    projection: list[tuple[date, float]] = []
    break_even = reached
    profit = None
    adjusted = None
    if total_cost > 0:
        if yearly > 0:
            break_even, projection, at_horizon = _project(
                saved,
                days[-1],
                solar_pattern,
                battery_pattern,
                total_cost,
                0,
                0,
                0,
                horizon,
                reached,
                income_pattern,
            )
            profit = at_horizon - total_cost if at_horizon is not None else None
        if panel_ageing or battery_ageing or price_change:
            adjusted_day, adjusted_points, at_horizon = _project(
                saved,
                days[-1],
                solar_pattern,
                battery_pattern,
                total_cost,
                panel_ageing,
                battery_ageing,
                price_change,
                horizon,
                reached,
                income_pattern,
            )
            adjusted = {
                "break_even": adjusted_day,
                "years_from_install": years_after_install(adjusted_day),
                "projection": adjusted_points,
                "profit_at_horizon_gbp": (
                    round(at_horizon - total_cost, 2) if at_horizon is not None else None
                ),
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
        "yearly_regular_income_gbp": round(yearly_income, 2) if regular is not None else None,
        "percent": round(saved / total_cost * 100, 1) if total_cost > 0 else None,
        # What is still to be recovered; below zero once the system has paid for itself.
        "remaining_gbp": round(total_cost - saved, 2),
        "horizon": horizon,
        "horizon_years": horizon_years,
        "profit_at_horizon_gbp": round(profit, 2) if profit is not None else None,
        "rough": rough,
        "already_reached": reached is not None,
        "break_even": break_even,
        "years_from_install": years_after_install(break_even),
        "series": series,
        "projection": projection,
        "adjusted": adjusted,
        "today": today,
    }


def billed_line(
    otherwise: dict[date, float],
    paid: dict[date, float],
    paid_before_export: dict[date, float],
    bills: list[dict],
    extra: dict[date, float],
    estimated_before: float,
) -> dict | None:
    """Savings so far worked out from the bills instead of the tracker's own costing.

    On each day a bill covers, what was paid is the bill's charge spread evenly over its
    days, less the export payment from an export bill covering that day (or, without one,
    the export credit the tracker measured). Days no bill covers use the tracker's figure,
    so the line can be read against the tracker's own. It stops at the last billed day.

    Returns None if no bill with a charge covers a day with readings.
    """
    charged: dict[date, float] = {}
    credited: dict[date, float] = {}
    for bill in sorted(bills, key=lambda b: b["first_day"]):
        first, last = date.fromisoformat(bill["first_day"]), date.fromisoformat(bill["last_day"])
        length = (last - first).days + 1
        for n in range(length):
            day = first + timedelta(days=n)
            if bill.get("charge_gbp") is not None:
                charged[day] = bill["charge_gbp"] / length
            if bill.get("export_gbp") is not None:
                credited[day] = bill["export_gbp"] / length
    days = sorted(paid)
    billed = [day for day in days if day in charged]
    if not billed:
        return None
    last_billed = billed[-1]
    running = tracker = estimated_before
    series = [(days[0] - timedelta(days=1), round(running, 2))]
    covered = [day for day in days if day <= last_billed]
    by_day: dict[date, float] = {}
    for index, day in enumerate(covered):
        if day in charged:
            credit = credited.get(day, paid_before_export[day] - paid[day])
            cost = charged[day] - credit
        else:
            cost = paid[day]
        by_day[day] = otherwise[day] - cost + extra.get(day, 0.0)
        running += by_day[day]
        tracker += otherwise[day] - paid[day] + extra.get(day, 0.0)
        if index % 7 == 6 or index == len(covered) - 1:
            series.append((day, round(running, 2)))
    return {
        "savings": by_day,  # each day's saving, for projecting forward
        "series": series,
        "saved_gbp": round(running, 2),
        "tracker_saved_gbp": round(tracker, 2),
        "first_billed": billed[0],
        "last_billed": last_billed,
        "billed_days": len(billed),
    }
