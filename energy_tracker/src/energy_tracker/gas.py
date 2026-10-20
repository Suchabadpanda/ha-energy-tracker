"""Gas costs, from bills typed in by hand (there are no gas sensors to read).

Each bill's cost is spread evenly over the days it covers, so a bill running from mid-March
to mid-April puts about half its cost in each month. Optional: many homes with a heat pump
have no gas at all.
"""

from __future__ import annotations

from datetime import date, timedelta


def days_of(bill: dict) -> int:
    first, last = date.fromisoformat(bill["first_day"]), date.fromisoformat(bill["last_day"])
    return (last - first).days + 1


def overlapping(bills: list[dict], first: date, last: date, skip: int | None = None) -> dict | None:
    """A bill sharing any day with first to last (other than the one being changed)."""
    for bill in bills:
        if bill["id"] == skip:
            continue
        if date.fromisoformat(bill["first_day"]) <= last and first <= date.fromisoformat(
            bill["last_day"]
        ):
            return bill
    return None


def by_day(bills: list[dict]) -> dict[date, tuple[float, float | None]]:
    """Each day's share of the bills: (cost, kWh or None)."""
    found: dict[date, tuple[float, float | None]] = {}
    for bill in bills:
        days = days_of(bill)
        if days <= 0:
            continue
        cost = bill["cost_gbp"] / days
        kwh = bill["kwh"] / days if bill["kwh"] is not None else None
        day = date.fromisoformat(bill["first_day"])
        for _ in range(days):
            found[day] = (cost, kwh)
            day += timedelta(days=1)
    return found


def between(daily: dict[date, tuple[float, float | None]], first: date, last: date) -> dict:
    """Gas cost and use from first to last (inclusive), and how many of those days the bills
    cover."""
    cost = kwh = 0.0
    covered = 0
    known_kwh = True
    day = first
    while day <= last:
        if day in daily:
            covered += 1
            cost += daily[day][0]
            if daily[day][1] is None:
                known_kwh = False
            else:
                kwh += daily[day][1]
        day += timedelta(days=1)
    return {
        "gbp": round(cost, 2),
        "kwh": round(kwh, 1) if covered and known_kwh else None,
        "days": covered,
        "of_days": (last - first).days + 1,
    }


def summary(bills: list[dict]) -> dict:
    """The bills with a few worked-out figures, and totals by year."""
    rows = []
    for bill in sorted(bills, key=lambda b: (b["first_day"], b["id"])):
        days = days_of(bill)
        rows.append(
            {
                **bill,
                "days": days,
                "per_day_gbp": round(bill["cost_gbp"] / days, 2),
                # The all-in price, standing charge included.
                "p_per_kwh": (
                    round(bill["cost_gbp"] * 100 / bill["kwh"], 2) if bill["kwh"] else None
                ),
            }
        )
    daily = by_day(bills)
    years = []
    if daily:
        for year in range(min(daily).year, max(daily).year + 1):
            figures = between(daily, date(year, 1, 1), date(year, 12, 31))
            if figures["days"]:
                years.append({"year": year, **figures})
    return {"bills": rows[::-1], "years": years[::-1]}
