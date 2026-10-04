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


def payback(savings: dict[date, float], total_cost: float, installed: date, today: date) -> dict:
    """Cumulative savings so far, and a projection to the day they equal the system's cost.

    `savings` is pounds saved on each day with readings. Days between the install date and
    the first reading are filled in at the average, and reported separately.
    """
    days = sorted(savings)
    if not days:
        return {"has_data": False}
    values = [savings[d] for d in days]
    average = sum(values) / len(values)

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
    pattern = values[-YEAR:] if not rough else [average]
    yearly = sum(pattern) if not rough else average * YEAR

    projection: list[tuple[date, float]] = []
    break_even = reached
    if reached is None and total_cost > 0 and yearly > 0:
        projection.append((days[-1], round(running, 2)))
        for ahead in range(MAX_PROJECTION_DAYS):
            running += pattern[ahead % len(pattern)]
            day = days[-1] + timedelta(days=ahead + 1)
            done = running >= total_cost
            if done or ahead % 30 == 29:
                projection.append((day, round(running, 2)))
            if done:
                break_even = day
                break

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
        "percent": round(saved / total_cost * 100, 1) if total_cost > 0 else None,
        "rough": rough,
        "already_reached": reached is not None,
        "break_even": break_even,
        "years_from_install": (
            round((break_even - installed).days / 365.25, 1) if break_even else None
        ),
        "series": series,
        "projection": projection,
        "today": today,
    }
