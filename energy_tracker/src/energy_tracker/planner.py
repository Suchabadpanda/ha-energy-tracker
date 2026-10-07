"""Planning a switch to another tariff: what a year would cost on it, month by month, and
how the battery and the car would need to charge to suit it.

Replaying usage as it happened is unfair to a tariff with different cheap times. Here the
household is run again from scratch on the tariff being tried: the house and the solar
panels behave exactly as recorded, but the battery and the car are charged from the grid
in that tariff's cheap half hours, and the battery then runs the house until it is empty.
That shows whether the battery would last from one cheap period to the next, and what it
would cost when it does not.

The same model is also run on the tariff actually in use. It never matches the measured
cost exactly (the real system makes cleverer choices), so the fair comparison is between
the two modelled figures, and the gap between model and measurement is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .tariff import Schedule, Tariff
from .usage import Counter, half_hour_slots

DEFAULT_EFFICIENCY = 0.9  # battery round trip
CHEAP_BAND = 0.25  # a half hour is cheap if within this share of the way from cheapest to dearest
MIN_CAPACITY_KWH = 1.0


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float  # usable
    charge_kw: float  # fastest it charges or discharges
    efficiency: float = DEFAULT_EFFICIENCY  # round trip


def estimate_battery(
    soc: list[tuple[datetime, float]], charge: Counter, discharge: Counter
) -> tuple[float | None, float | None]:
    """Usable capacity (kWh) and fastest charging (kW), worked out from readings.

    Capacity: over every stretch where the charge level rose and nothing was discharged,
    the energy put in divided by the share of the battery it filled. Returns None for
    either figure that cannot be worked out.
    """
    if not charge or len(soc) < 2:
        return None, None
    energy = filled = fastest = 0.0
    for (start, level), (end, later) in zip(soc, soc[1:], strict=False):
        hours = (end - start).total_seconds() / 3600
        if not 0 < hours <= 1:
            continue
        put_in = charge.between(start, end)
        fastest = max(fastest, put_in / hours)
        taken_out = discharge.between(start, end) if discharge else 0.0
        if later - level >= 1 and taken_out < 0.02:
            energy += put_in
            filled += (later - level) / 100
    capacity = None
    if filled >= 1:  # at least one full battery's worth of charging seen
        # What goes in is more than what is stored: allow for half the round-trip loss.
        capacity = round(energy * DEFAULT_EFFICIENCY**0.5 / filled, 1)
        if capacity < MIN_CAPACITY_KWH:
            capacity = None
    return capacity, round(fastest, 1) if fastest > 0 else None


def cheap_times(tariff: Tariff) -> list[dict]:
    """The cheap periods of a tariff with fixed time windows, as start, end and price."""
    prices = [b.p_per_kwh for b in tariff.import_bands]
    low, high = min(prices), max(prices)
    if high == low:
        return []
    limit = low + (high - low) * CHEAP_BAND
    bands = [b for b in tariff.import_bands if b.p_per_kwh <= limit]
    periods = [{"start": b.start, "end": b.end, "p_per_kwh": b.p_per_kwh} for b in bands]
    # A window running past midnight is stored as two bands: join them back up.
    if (
        len(periods) > 1
        and periods[0]["start"] == "00:00"
        and periods[-1]["end"] == "24:00"
        and periods[0]["p_per_kwh"] == periods[-1]["p_per_kwh"]
    ):
        periods = [
            *periods[1:-1],
            {**periods[-1], "end": periods[0]["end"]},
        ]
    return periods


def simulate(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    battery: Battery,
) -> dict[date, dict]:
    """Run the household on a tariff, charging the battery and the car in its cheap times.

    `counters` needs "load" and may have "solar" and "ev". Returns, for each local day:
    net cost in pounds (with the standing charge and export income), energy imported and
    exported, energy imported outside the cheap times, and how many half hours the battery
    was empty while the house needed it.
    """
    load, solar, ev = counters["load"], counters.get("solar"), counters.get("ev")
    one_way = battery.efficiency**0.5
    per_slot = battery.charge_kw / 2  # kWh the battery can take or give in half an hour

    # Gather each day's half hours first: the car's charging is planned a day at a time.
    days: dict[date, list[dict]] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        tariff = schedule.on(local.date())
        used = load.between(piece_start, piece_end)
        car = min(used, ev.between(piece_start, piece_end)) if ev else 0.0
        days.setdefault(local.date(), []).append(
            {
                "tariff": tariff,
                "price": tariff.import_price(local),
                "house": used - car,
                "car": car,
                "solar": solar.between(piece_start, piece_end) if solar else 0.0,
                "share": (piece_end - piece_start).total_seconds() / 1800,
            }
        )

    stored = battery.capacity_kwh / 2  # start half full
    results: dict[date, dict] = {}
    for day, slots in days.items():
        prices = [slot["price"] for slot in slots]
        low, high = min(prices), max(prices)
        limit = low + (high - low) * CHEAP_BAND
        flat = high - low < 1e-9
        # The car takes the day's energy in the cheapest half hours, at its usual speed.
        car_total = sum(slot["car"] for slot in slots)
        car_speed = max((slot["car"] for slot in slots), default=0.0)
        car_plan = [0.0] * len(slots)
        if flat:
            car_plan = [slot["car"] for slot in slots]
        else:
            left = car_total
            for index in sorted(range(len(slots)), key=lambda i: (slots[i]["price"], i)):
                if left <= 0:
                    break
                car_plan[index] = min(car_speed * slots[index]["share"], left)
                left -= car_plan[index]

        tariff = slots[0]["tariff"]
        row = {
            "pence": tariff.standing_charge_p_per_day * tariff.vat_multiplier,
            "import_kwh": 0.0,
            "export_kwh": 0.0,
            "dear_kwh": 0.0,
            "empty_slots": 0,
        }
        for slot, car in zip(slots, car_plan, strict=True):
            cheap = not flat and slot["price"] <= limit
            limit_kwh = per_slot * slot["share"]
            need = slot["house"] + car
            imported = exported = 0.0
            if cheap:
                # Buy what the house and car need, and fill the battery from the grid.
                room = (battery.capacity_kwh - stored) / one_way
                surplus = max(0.0, slot["solar"] - need)
                from_solar = min(surplus, room, limit_kwh)
                from_grid = min(room - from_solar, limit_kwh - from_solar)
                stored += (from_solar + from_grid) * one_way
                imported = max(0.0, need - slot["solar"]) + from_grid
                exported = surplus - from_solar
            else:
                gap = need - slot["solar"]
                if gap <= 0:  # spare solar: into the battery, then out to the grid
                    room = (battery.capacity_kwh - stored) / one_way
                    kept = min(-gap, room, limit_kwh)
                    stored += kept * one_way
                    exported = -gap - kept
                else:  # short: from the battery, then from the grid
                    given = min(gap, stored * one_way, limit_kwh)
                    stored -= given / one_way
                    imported = gap - given
                    if stored <= 1e-6 and imported > 0.01:
                        row["empty_slots"] += 1
                    row["dear_kwh"] += imported
            row["pence"] += imported * slot["price"] - exported * slot["tariff"].export_p_per_kwh
            row["import_kwh"] += imported
            row["export_kwh"] += exported
        results[day] = {
            "gbp": row["pence"] / 100,
            "import_kwh": row["import_kwh"],
            "export_kwh": row["export_kwh"],
            "dear_kwh": row["dear_kwh"],
            "empty_slots": row["empty_slots"],
        }
    return results


def shortfall(days: dict[date, dict]) -> dict:
    """How often the battery ran out before the next cheap period, overall and by month."""
    short = {day: row for day, row in days.items() if row["empty_slots"] > 0}
    by_month: dict[str, list[int]] = {}
    for day, row in days.items():
        counted = by_month.setdefault(f"{day:%Y-%m}", [0, 0])
        counted[0] += 1
        counted[1] += row["empty_slots"] > 0
    worst = max(by_month, key=lambda key: by_month[key][1] / by_month[key][0], default=None)
    return {
        "days": len(days),
        "short_days": len(short),
        "short_percent": round(len(short) / len(days) * 100) if days else 0,
        "dear_kwh": round(sum(row["dear_kwh"] for row in days.values()), 1),
        "hours_empty_on_short_days": (
            round(sum(row["empty_slots"] for row in short.values()) / 2 / len(short), 1)
            if short
            else 0.0
        ),
        "worst_month": worst if worst and by_month[worst][1] else None,
        "worst_month_short_days": by_month[worst][1] if worst else 0,
    }
