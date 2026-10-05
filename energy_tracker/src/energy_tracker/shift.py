"""What a tariff would cost if flexible charging moved to its cheapest times.

Replaying usage exactly as it happened is unfair to a tariff with a different cheap window:
on it, the battery and the car would simply be charged at other times. This estimate moves
the import that went into the battery or the car (which can be done at any time of day) to
the cheapest half hours of the same day on the tariff being tried, no faster than the
fastest charging actually seen. Everything else stays where it was.

It is a best case: it does not check that the battery would still last until its next
charge, so a tariff whose cheap hours fall late in the day may do a little worse.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots


def shifted_import(
    imports: Counter,
    flexible: list[Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
) -> dict | None:
    """Import cost in pounds with flexible charging moved, or None if nothing can be moved.

    `flexible` holds the counters of what can be charged at any time: energy into the
    battery, and energy into the car.
    """
    flexible = [counter for counter in flexible if counter]
    if not flexible:
        return None
    days: dict[date, list[tuple[float, float, float]]] = {}  # price, fixed kWh, movable kWh
    fastest = 0.0
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        imported = imports.between(piece_start, piece_end)
        charging = sum(counter.between(piece_start, piece_end) for counter in flexible)
        movable = min(imported, charging)
        fastest = max(fastest, movable)
        price = schedule.on(local.date()).import_price(local)
        days.setdefault(local.date(), []).append((price, imported - movable, movable))
    if fastest <= 0:
        return None

    pence = moved = 0.0
    for slots in days.values():
        pence += sum(price * fixed for price, fixed, _ in slots)
        left = sum(movable for _, _, movable in slots)
        moved += left
        for price, _, _ in sorted(slots):  # cheapest half hours first
            if left <= 0:
                break
            placed = min(fastest, left)
            pence += placed * price
            left -= placed
        if left > 0:  # more than the day can hold at that speed: cannot happen, but be safe
            pence += left * max(price for price, _, _ in slots)
    return {
        "import_gbp": round(pence / 100, 2),
        "moved_kwh": round(moved, 1),
        "fastest_kw": round(fastest * 2, 1),
    }
