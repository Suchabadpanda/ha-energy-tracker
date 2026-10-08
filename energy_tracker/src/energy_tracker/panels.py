"""Would more panels, or a bigger inverter, pay?

The household is run again on the tariff actually in use, with the panels' output scaled up
for the extra capacity, half hour by half hour. The inverter can only pass so much solar
power, so anything above its limit is lost (clipped). The battery and the car charge as in
the tariff switch planner. The saving is what the bigger system would have cost less, over
the period, scaled to a year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .planner import Battery, simulate
from .tariff import Schedule
from .usage import Counter, half_hour_slots

EXTRA_KWP = (1.0, 2.0, 3.0, 4.0)
HALF_HOUR = timedelta(minutes=30)
YEAR = 365


@dataclass
class ScaledSolar:
    """Solar generation as it would have been with more panels, limited by the inverter."""

    measured: Counter
    scale: float
    limit_kw: float | None  # None: no limit

    def __bool__(self) -> bool:
        return bool(self.measured)

    def between(self, start: datetime, end: datetime) -> float:
        made = self.measured.between(start, end) * self.scale
        if self.limit_kw is None:
            return made
        return min(made, self.limit_kw * (end - start) / timedelta(hours=1))


def estimate_kwp(solar: Counter, start: datetime, end: datetime) -> float | None:
    """A rough size for the panels: the most they have produced in any half hour, as power.

    Panels seldom reach their full rating, so this tends to come out a little low; the
    user's own figure is better and is asked for.
    """
    best = 0.0
    for piece_start, piece_end in half_hour_slots(start, end):
        if piece_end - piece_start == HALF_HOUR:
            best = max(best, solar.between(piece_start, piece_end) * 2)
    return round(best / 0.85, 1) if best > 0 else None


def clipped(solar: Counter, start: datetime, end: datetime, scale: float, limit_kw: float) -> float:
    """kWh lost to the inverter limit with the panels scaled up."""
    lost = 0.0
    for piece_start, piece_end in half_hour_slots(start, end):
        made = solar.between(piece_start, piece_end) * scale
        lost += max(0.0, made - limit_kw * (piece_end - piece_start) / timedelta(hours=1))
    return lost


def options(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    battery: Battery,
    kwp: float,
    inverter_kw: float | None,
    new_inverter_kw: float | None = None,
) -> dict:
    """The yearly gain from each amount of extra panels, with today's inverter and, if given,
    a bigger one.

    `counters` holds "load", "solar" and optionally "ev", as for the planner.
    """
    solar = counters["solar"]
    days = max(1, (end - start).days)
    per_year = YEAR / days

    def cost(scale: float, limit: float | None) -> float:
        run = {**counters, "solar": ScaledSolar(solar, scale, limit)}
        return sum(
            row["gbp"] for row in simulate(run, start, end, timezone, schedule, battery).values()
        )

    # The baseline is the house as it is, modelled the same way, so like is compared with like.
    base = cost(1.0, None)
    generated = solar.between(start, end)
    rows = []
    for extra in EXTRA_KWP:
        scale = (kwp + extra) / kwp
        row: dict = {
            "extra_kwp": extra,
            "extra_kwh_year": round(generated * (scale - 1) * per_year),
            "saved_gbp_year": round((base - cost(scale, inverter_kw)) * per_year),
            "clipped_kwh_year": (
                round(clipped(solar, start, end, scale, inverter_kw) * per_year)
                if inverter_kw
                else None
            ),
        }
        if new_inverter_kw:
            row["saved_bigger_inverter_gbp_year"] = round(
                (base - cost(scale, new_inverter_kw)) * per_year
            )
        rows.append(row)
    return {
        "days": days,
        "generated_kwh_year": round(generated * per_year),
        "clipped_now_kwh_year": (
            round(clipped(solar, start, end, 1.0, inverter_kw) * per_year) if inverter_kw else None
        ),
        "options": rows,
        "rough": days < YEAR,
    }
