"""What each device cost to run: the heat pump (or other smart load), the EV charger and
the rest of the house.

Each day's import cost is shared out by how much of the day's electricity each device used.
A device that used 40% of the day's consumption carries 40% of what was paid for import
that day. This counts cheap overnight charging of the battery towards whatever it later
powered, without having to follow every unit through the battery. The standing charge and
export income belong to the house as a whole and are left out.

Where solar generation and export are recorded, each device's share of the solar the house
used itself is also given a value: what that solar would have earned if it had been
exported. Import cost plus that is a device's "true cost": solar is free to use, but using
it gives up the export payment.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots

LOAD = "load_energy_total"
CIRCUIT = "smart_load_energy_total"
EV = "ev_charger_energy_total"
IMPORT = "import_energy_total"
SOLAR = "pv_energy_total"
EXPORT = "export_energy_total"
COUNTERS = [LOAD, CIRCUIT, EV, IMPORT]
# Optional: with these, the solar each device used is valued at the export rate.
SOLAR_COUNTERS = [SOLAR, EXPORT]
PARTS = ("heat_pump", "ev_charger", "rest")


def daily(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    ev_on_smart_load: bool = True,
) -> dict[date, dict]:
    """Energy and share of the import cost for each device, for each local day."""
    load, circuit, ev, imports = (counters.get(name) for name in COUNTERS)
    solar, exports = (counters.get(name) for name in SOLAR_COUNTERS)
    valued = bool(solar) and bool(exports)
    raw: dict[date, dict] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        day = raw.setdefault(
            local.date(),
            {"load": 0.0, "circuit": 0.0, "ev": 0.0, "pence": 0.0, "solar": 0.0, "export": 0.0},
        )
        if valued:
            day["solar"] += solar.between(piece_start, piece_end)
            day["export"] += exports.between(piece_start, piece_end)
        day["load"] += load.between(piece_start, piece_end)
        if circuit:
            day["circuit"] += circuit.between(piece_start, piece_end)
        if ev:
            day["ev"] += ev.between(piece_start, piece_end)
        imported = imports.between(piece_start, piece_end)
        day["pence"] += imported * schedule.on(local.date()).import_price(local)

    days: dict[date, dict] = {}
    for when, day in raw.items():
        ev_kwh = day["ev"]
        smart_kwh = day["circuit"]
        if circuit and ev and ev_on_smart_load:
            ev_kwh = min(ev_kwh, smart_kwh)  # the charger cannot use more than its circuit
            smart_kwh -= ev_kwh
        total = max(day["load"], smart_kwh + ev_kwh)
        rest_kwh = total - smart_kwh - ev_kwh
        row: dict = {"total_kwh": total, "import_gbp": day["pence"] / 100}
        # Solar the house kept (used straight away or stored), at what exporting it would
        # have paid. A battery sending grid power back out can make export exceed solar.
        solar_value = 0.0
        if valued:
            kept = max(0.0, day["solar"] - day["export"])
            solar_value = kept * schedule.on(when).export_p_per_kwh / 100
            row.update(solar_used_kwh=kept, solar_value_gbp=solar_value)
        for name, kwh, present in (
            ("heat_pump", smart_kwh, bool(circuit)),
            ("ev_charger", ev_kwh, bool(ev)),
            ("rest", rest_kwh, True),
        ):
            if present:
                share = kwh / total if total > 0 else 0.0
                row[f"{name}_kwh"] = kwh
                row[f"{name}_gbp"] = day["pence"] / 100 * share
                if valued:
                    row[f"{name}_solar_gbp"] = solar_value * share
        days[when] = row
    return days


def total(rows: list[dict]) -> dict:
    """Add days together."""
    keys = sorted({key for row in rows for key in row})
    return {
        key: round(sum(row.get(key, 0.0) for row in rows), 2 if key.endswith("gbp") else 1)
        for key in keys
    }


def by_month_and_year(days: dict[date, dict]) -> dict:
    months: dict[str, list[dict]] = {}
    years: dict[str, list[dict]] = {}
    for day, row in sorted(days.items()):
        months.setdefault(f"{day:%Y-%m}", []).append(row)
        years.setdefault(f"{day:%Y}", []).append(row)
    return {
        "months": [{"month": k, "days": len(v), **total(v)} for k, v in months.items()],
        "years": [{"year": k, "days": len(v), **total(v)} for k, v in years.items()],
    }
