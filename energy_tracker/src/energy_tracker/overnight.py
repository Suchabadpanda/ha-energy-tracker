"""The overnight charge: was the battery charged about right in the cheap window?

For each day: how full the battery was when the cheap window ended, how low it got before
the next cheap window began, and what had to be bought at a dearer rate in between. A
battery that never came near its reserve was charged more than the day needed (the rest was
carried over, and with it went room for solar); one that hit its reserve while dear-rate
electricity was being bought was charged too little, or could not hold enough.

From that, the charge level that would have just lasted each day, and the level that
would have been enough on most days.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

from .planner import cheap_times
from .tariff import Schedule, _minutes
from .usage import Counter, half_hour_pieces

SHORT_KWH = 0.5  # dear-rate import above this once the battery reached its reserve: it ran short
NEAR_RESERVE = 3  # percentage points: this close to the reserve counts as reaching it
SPARE_SHARE = 0.1  # spare of at least this share of the battery (or 2 kWh) is worth noting
COVER = 0.8  # the suggested level is enough on this share of days
FIND_WITHIN = timedelta(minutes=20)  # a charge level read this close to a moment stands for it


class Levels:
    """Battery charge levels (%) by time, with lookups."""

    def __init__(self, samples: list[tuple[datetime, float]]) -> None:
        ordered = sorted(samples)
        self.times = [t for t, _ in ordered]
        self.values = [v for _, v in ordered]

    def at(self, when: datetime) -> float | None:
        """The reading nearest `when`, if one is within FIND_WITHIN."""
        index = bisect_left(self.times, when)
        near = [i for i in (index - 1, index) if 0 <= i < len(self.times)]
        if not near:
            return None
        best = min(near, key=lambda i: abs(self.times[i] - when))
        return self.values[best] if abs(self.times[best] - when) <= FIND_WITHIN else None

    def lowest(self, start: datetime, end: datetime) -> tuple[float, datetime] | None:
        """The lowest reading between two moments, and when it was."""
        a, b = bisect_left(self.times, start), bisect_right(self.times, end)
        if a >= b:
            return None
        index = min(range(a, b), key=lambda i: self.values[i])
        return self.values[index], self.times[index]

    def first_at_or_below(self, start: datetime, end: datetime, level: float) -> datetime | None:
        """When the charge first fell to `level` or below between two moments, if it did."""
        a, b = bisect_left(self.times, start), bisect_right(self.times, end)
        return next((self.times[i] for i in range(a, b) if self.values[i] <= level), None)


def estimate_reserve(levels: list[float]) -> float | None:
    """The level the battery is kept above: about the lowest it is seen at (ignoring the odd
    stray reading)."""
    if len(levels) < 100:
        return None
    ordered = sorted(levels)
    return float(math.floor(ordered[len(ordered) // 200]))  # the lowest half per cent


def _window(schedule: Schedule, day: date, timezone: ZoneInfo):
    """When the day's cheap window ends, when the next one begins, and its price with VAT.
    None for a tariff without fixed cheap times (or with none)."""
    tariff = schedule.on(day)
    if tariff.dynamic:
        return None
    periods = cheap_times(tariff)
    if not periods:
        return None

    def length(period: dict) -> int:
        start, end = _minutes(period["start"]), _minutes(period["end"])
        return (end - start) % (24 * 60) or 24 * 60

    main = max(periods, key=length)
    start, end = _minutes(main["start"]), _minutes(main["end"])

    def at(on: date, minutes: int) -> datetime:
        return datetime.combine(on, clock(0), timezone) + timedelta(minutes=minutes)

    ends = at(day, end)
    # The next window starts later the same day (one running past midnight), or tomorrow.
    begins = at(day, start) if start > end else at(day + timedelta(days=1), start)
    return ends, begins, main["p_per_kwh"] * tariff.vat_multiplier


def nights(
    levels: Levels,
    charge: Counter | None,
    discharge: Counter | None,
    imports: Counter,
    schedule: Schedule,
    timezone: ZoneInfo,
    first: date,
    last: date,
    capacity: float,
    reserve: float,
    now: datetime,
) -> list[dict]:
    """Each day from `first` to `last` whose span (cheap window's end to the next one's
    start) is over, with the charge left at each end and what was bought in between."""
    rows = []
    day = first
    while day <= last:
        found = _window(schedule, day, timezone)
        day += timedelta(days=1)
        if not found:
            continue
        ends, begins, cheap_p = found
        if begins > now:
            continue
        start_level, next_level = levels.at(ends), levels.at(begins)
        low = levels.lowest(ends, begins)
        if start_level is None or next_level is None or low is None:
            continue
        lowest, lowest_at = low
        # Only what was bought dear once the battery had reached its reserve was for want of
        # charge: before then, it was the house wanting more than the battery could give.
        emptied = levels.first_at_or_below(ends, begins, reserve + NEAR_RESERVE)
        dear_kwh = dear_pence = 0.0
        for piece_start, _, a, b in half_hour_pieces(emptied or begins, begins):
            local = piece_start.astimezone(timezone)
            price = schedule.on(local.date()).import_price(local)
            if price > cheap_p + 1e-9:
                kwh = imports.kwh(a, b)
                dear_kwh += kwh
                dear_pence += kwh * (price - cheap_p)
        short = dear_kwh >= SHORT_KWH
        spare_kwh = max(0.0, (lowest - reserve) / 100 * capacity)
        shortfall_kwh = dear_kwh if short else 0.0
        # The level that would have just lasted: what it started at, less what was never
        # used, plus what had to be bought dear for want of charge.
        needed = start_level - (lowest - reserve) + shortfall_kwh / capacity * 100
        rows.append(
            {
                "day": ends.date(),
                "charged_to": round(start_level, 1),
                "lowest": round(lowest, 1),
                "lowest_at": lowest_at,
                "left_at_next": round(next_level, 1),
                "discharged_kwh": round(discharge.between(ends, begins), 2) if discharge else None,
                "recharged_kwh": round(charge.between(ends, begins), 2) if charge else None,
                "emptied_at": emptied,
                "dear_kwh": round(dear_kwh, 2),
                "extra_cost_gbp": round(dear_pence / 100, 2),
                "spare_kwh": round(spare_kwh, 1),
                "needed_percent": round(max(reserve, needed), 1),
                "verdict": "short"
                if short
                else "spare"
                if spare_kwh >= max(2.0, capacity * SPARE_SHARE)
                else "right",
            }
        )
    return rows


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def summarise(rows: list[dict], capacity: float) -> dict | None:
    """Averages, counts and the level that would have been enough on most days."""
    if not rows:
        return None
    count = len(rows)
    needed = [row["needed_percent"] for row in rows]
    target = _percentile(needed, COVER)
    return {
        "days": count,
        "short_days": sum(row["verdict"] == "short" for row in rows),
        "spare_days": sum(row["verdict"] == "spare" for row in rows),
        "charged_to": round(sum(row["charged_to"] for row in rows) / count, 1),
        "lowest": round(sum(row["lowest"] for row in rows) / count, 1),
        "spare_kwh": round(sum(row["spare_kwh"] for row in rows) / count, 1),
        "shortfall_kwh": round(sum(row["dear_kwh"] for row in rows), 1),
        "extra_cost_gbp": round(sum(row["extra_cost_gbp"] for row in rows), 2),
        # Rounded up to the next 5%, as charge targets are usually set.
        "suggested_percent": min(100, 5 * math.ceil(target / 5)),
        "over_full": target > 100,
        "suggested_kwh": round(min(100.0, target) / 100 * capacity, 1),
        "covers_days": sum(value <= min(100, 5 * math.ceil(target / 5)) for value in needed),
    }


def by_month(rows: list[dict], capacity: float) -> list[dict]:
    months: dict[str, list[dict]] = {}
    for row in rows:
        months.setdefault(f"{row['day']:%Y-%m}", []).append(row)
    return [{"month": key, **summarise(group, capacity)} for key, group in sorted(months.items())]
