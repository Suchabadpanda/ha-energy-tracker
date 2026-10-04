"""Work out what a period cost: import, export credit and standing charge."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots


def cost_between(
    imports: Counter,
    exports: Counter,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
) -> dict:
    """Price everything imported and exported between two moments.

    Each half-hour piece is priced at the rate for its time of day, using the tariff that
    applied on that date. The standing charge is added for every day the period touches.

    It also works out what the same import would have cost if every unit had been bought at
    the day rate (the dearest import price in force at the time). The difference is what
    was saved by importing at cheaper times.
    """
    bands: dict[tuple[str, float], float] = {}
    import_pence = export_pence = day_rate_pence = 0.0
    import_kwh = export_kwh = 0.0

    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        tariff = schedule.on(local.date())
        band = tariff.band_at(local)

        imported = imports.between(piece_start, piece_end)
        exported = exports.between(piece_start, piece_end)
        key = (band.label, band.p_per_kwh)
        bands[key] = bands.get(key, 0.0) + imported
        import_kwh += imported
        export_kwh += exported
        import_pence += imported * band.p_per_kwh * tariff.vat_multiplier
        day_rate_pence += (
            imported * max(b.p_per_kwh for b in tariff.import_bands) * tariff.vat_multiplier
        )
        export_pence += exported * tariff.export_p_per_kwh

    # A period that ends exactly at midnight does not include the day that starts then.
    first_day = start.astimezone(timezone).date()
    last_day = (end - timedelta(microseconds=1)).astimezone(timezone).date()
    days = (last_day - first_day).days + 1
    standing_pence = 0.0
    for n in range(days):
        tariff = schedule.on(first_day + timedelta(days=n))
        standing_pence += tariff.standing_charge_p_per_day * tariff.vat_multiplier

    return {
        "tariff": schedule.on(last_day).name,
        "vat_percent": schedule.on(last_day).vat_percent,
        "since": start,
        "estimated_hours": round(imports.uncovered(start, end).total_seconds() / 3600, 1),
        "import_kwh": round(import_kwh, 3),
        "import_gbp": round(import_pence / 100, 2),
        "all_day_rate_gbp": round(day_rate_pence / 100, 2),
        "saved_gbp": round((day_rate_pence - import_pence) / 100, 2),
        "export_kwh": round(export_kwh, 3),
        "export_credit_gbp": round(export_pence / 100, 2),
        "standing_charge_days": days,
        "standing_charge_gbp": round(standing_pence / 100, 2),
        "net_gbp": round((import_pence - export_pence + standing_pence) / 100, 2),
        "import_bands": [
            # The price is as entered (before VAT); the cost figures above include VAT.
            {"label": label, "p_per_kwh": rate, "kwh": round(kwh, 3)}
            for (label, rate), kwh in sorted(bands.items())
        ],
    }


SUMMED = [
    "estimated_hours",
    "import_kwh",
    "import_gbp",
    "all_day_rate_gbp",
    "saved_gbp",
    "export_kwh",
    "export_credit_gbp",
    "standing_charge_days",
    "standing_charge_gbp",
    "net_gbp",
]


def combine(costs: list[dict]) -> dict | None:
    """Add several periods together (for example, the months of a year)."""
    if not costs:
        return None
    total = {
        key: round(sum(c[key] for c in costs), 3 if key.endswith("kwh") else 2) for key in SUMMED
    }
    total["estimated_hours"] = round(total["estimated_hours"], 1)
    total["since"] = min(c["since"] for c in costs)
    total["full_period"] = all(c["full_period"] for c in costs)
    return total
