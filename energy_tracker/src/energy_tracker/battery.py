"""Battery health: usable capacity and efficiency month by month, to see ageing as it
happens.

Capacity is worked out from how the battery charges: over every stretch where the charge
level rose and nothing was taken out, the energy put in divided by the share of the
battery it filled. A single month's figure wobbles by a few percent, so it is the trend
over many months that matters.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .planner import estimate_battery
from .usage import Counter

MIN_CHARGED_KWH = 50  # too little through the battery in a month to judge it by
MIN_SPAN = timedelta(days=20)


def by_month(
    soc: list[tuple[datetime, float]],
    charge: Counter,
    discharge: Counter,
    timezone: ZoneInfo,
) -> list[dict]:
    """Capacity, efficiency and throughput for each calendar month with enough readings."""
    months: dict[str, list[tuple[datetime, float]]] = {}
    for when, level in soc:
        months.setdefault(f"{when.astimezone(timezone):%Y-%m}", []).append((when, level))
    rows = []
    for key, levels in sorted(months.items()):
        start, end = levels[0][0], levels[-1][0]
        charged = charge.between(start, end) if charge else 0.0
        given = discharge.between(start, end) if discharge else 0.0
        capacity = estimate_battery(levels, charge, discharge)[0] if charged else None
        enough = charged >= MIN_CHARGED_KWH
        rows.append(
            {
                "month": key,
                "capacity_kwh": capacity if enough else None,
                # Over a short stretch the battery may simply end fuller or emptier than it
                # began, which looks like gained or lost energy.
                "efficiency_percent": (
                    round(min(100.0, given / charged * 100), 1)
                    if enough and given and end - start >= MIN_SPAN
                    else None
                ),
                "charged_kwh": round(charged, 1),
                "discharged_kwh": round(given, 1),
            }
        )
    return rows


def summary(months: list[dict]) -> dict:
    """The trend: first and latest capacity, the change, and full cycles so far."""
    measured = [m for m in months if m["capacity_kwh"]]
    result: dict = {"has_data": bool(measured), "months": months}
    if not measured:
        return result
    first, latest = measured[0], measured[-1]
    # Judge the start by the best of the first three months, the present by the average of
    # the last three: one odd month should not look like ageing.
    start_kwh = max(m["capacity_kwh"] for m in measured[:3])
    recent = [m["capacity_kwh"] for m in measured[-3:]]
    now_kwh = round(sum(recent) / len(recent), 1)
    given = sum(m["discharged_kwh"] for m in months)
    rated = [m["efficiency_percent"] for m in months if m["efficiency_percent"]]
    result.update(
        first_month=first["month"],
        latest_month=latest["month"],
        first_kwh=start_kwh,
        latest_kwh=now_kwh,
        # Needs months on both sides to mean anything.
        change_percent=(
            round((now_kwh - start_kwh) / start_kwh * 100, 1) if len(measured) >= 6 else None
        ),
        cycles=round(given / start_kwh),
        efficiency_percent=round(sum(rated) / len(rated), 1) if rated else None,
        measured_months=len(measured),
    )
    return result
