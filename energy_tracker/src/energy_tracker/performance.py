"""How well the system is doing: self-sufficiency, battery efficiency, and whether a
bigger battery would pay.

Everything is worked out from the lifetime energy counters, a day at a time.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .tariff import Schedule
from .usage import Counter, half_hour_slots

SOLAR, LOAD = "pv_energy_total", "load_energy_total"
IMPORT, EXPORT = "import_energy_total", "export_energy_total"
CHARGE, DISCHARGE = "battery_charge_energy_total", "battery_discharge_energy_total"
COUNTERS = [SOLAR, LOAD, IMPORT, EXPORT, CHARGE, DISCHARGE]

SHORT_DAY_KWH = 0.5  # dear-rate import above this means the battery did not last the day
DEFAULT_EFFICIENCY = 0.9  # battery round trip, used until enough has been measured
EXTRA_SIZES_KWH = (2.5, 5.0, 8.0, 10.0)
YEAR = 365


def daily(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
) -> dict[date, dict]:
    """Energy for each local day, plus how much was imported outside the cheapest rate.

    A counter with no readings is left out of the day (None), not counted as zero.
    """
    days: dict[date, dict] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        local = piece_start.astimezone(timezone)
        tariff = schedule.on(local.date())
        day = days.setdefault(
            local.date(),
            {
                **{name: (0.0 if counters.get(name) else None) for name in COUNTERS},
                "dear_kwh": 0.0,
                "dear_pence": 0.0,
                # The cheapest import price that day, with VAT: what extra charging would cost.
                "cheap_p": min(b.p_per_kwh for b in tariff.import_bands) * tariff.vat_multiplier,
                "flat": len({b.p_per_kwh for b in tariff.import_bands}) == 1,
            },
        )
        for name in COUNTERS:
            if counters.get(name):
                day[name] += counters[name].between(piece_start, piece_end)
        band = tariff.band_at(local)
        if counters.get(IMPORT) and band.p_per_kwh * tariff.vat_multiplier > day["cheap_p"]:
            imported = counters[IMPORT].between(piece_start, piece_end)
            day["dear_kwh"] += imported
            day["dear_pence"] += imported * band.p_per_kwh * tariff.vat_multiplier
    return days


def _share(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole or whole <= 0:
        return None
    return round(min(100.0, max(0.0, part / whole * 100)), 1)


def _sum(days: list[dict], key: str) -> float | None:
    values = [d[key] for d in days if d[key] is not None]
    return sum(values) if values else None


def figures(days: list[dict]) -> dict:
    """Headline measures for a set of days."""
    solar, load = _sum(days, SOLAR), _sum(days, LOAD)
    imported, exported = _sum(days, IMPORT), _sum(days, EXPORT)
    charged, discharged = _sum(days, CHARGE), _sum(days, DISCHARGE)
    dear = sum(d["dear_kwh"] for d in days)
    used_at_home = solar - exported if solar is not None and exported is not None else None
    met_at_home = load - imported if load is not None and imported is not None else None
    return {
        "solar_kwh": round(solar, 1) if solar is not None else None,
        "consumption_kwh": round(load, 1) if load is not None else None,
        "import_kwh": round(imported, 1) if imported is not None else None,
        "export_kwh": round(exported, 1) if exported is not None else None,
        # Share of consumption that did not come from the grid.
        "self_sufficiency_percent": _share(met_at_home, load),
        # Share of generation that was not exported.
        "solar_used_percent": _share(used_at_home, solar),
        # Out per kWh in. Only meaningful over weeks: over a day or two the battery may
        # simply be fuller or emptier than it started.
        "battery_efficiency_percent": (
            _share(discharged, charged) if charged and charged >= 50 and len(days) >= 14 else None
        ),
        "battery_charged_kwh": round(charged, 1) if charged is not None else None,
        "battery_discharged_kwh": round(discharged, 1) if discharged is not None else None,
        "dear_import_kwh": round(dear, 1),
        "dear_import_percent": _share(dear, imported),
        "dear_import_gbp": round(sum(d["dear_pence"] for d in days) / 100, 2),
    }


def monthly(days: dict[date, dict]) -> list[dict]:
    """The same measures for each calendar month, oldest first."""
    months: dict[str, list[dict]] = {}
    for day, values in sorted(days.items()):
        months.setdefault(f"{day:%Y-%m}", []).append(values)
    return [{"month": key, "days": len(rows), **figures(rows)} for key, rows in months.items()]


def sizing(days: dict[date, dict], efficiency: float | None = None) -> dict | None:
    """What more battery capacity would have saved, replaying the days recorded.

    On each day, energy imported at a dear rate is what the battery failed to cover. With
    more capacity, charged at the cheap rate, some of it would have been avoided: at most
    the extra capacity, once a day. The saving is the dear price avoided less the cheap
    price of charging, allowing for what is lost in the battery.

    Returns None on a flat tariff, where there is no cheap rate to charge at.
    """
    rows = [d for d in days.values() if not d["flat"]]
    if not rows:
        return None
    efficiency = efficiency or DEFAULT_EFFICIENCY
    short = [d for d in rows if d["dear_kwh"] > SHORT_DAY_KWH]
    scale = YEAR / len(rows)
    options = []
    for extra in EXTRA_SIZES_KWH:
        avoided_kwh = saved_pence = 0.0
        for day in short:
            avoided = min(day["dear_kwh"], extra)
            paid = day["dear_pence"] * avoided / day["dear_kwh"]
            saved_pence += paid - avoided / efficiency * day["cheap_p"]
            avoided_kwh += avoided
        options.append(
            {
                "extra_kwh": extra,
                "avoided_kwh_year": round(avoided_kwh * scale, 0),
                "saved_gbp_year": round(saved_pence * scale / 100, 0),
            }
        )
    return {
        "days": len(rows),
        "short_days": len(short),
        "short_percent": round(len(short) / len(rows) * 100, 0),
        "dear_kwh_year": round(sum(d["dear_kwh"] for d in rows) * scale, 0),
        "dear_gbp_year": round(sum(d["dear_pence"] for d in rows) * scale / 100, 0),
        "worst_day_kwh": round(max((d["dear_kwh"] for d in rows), default=0.0), 1),
        "efficiency_percent": round(efficiency * 100, 0),
        "rough": len(rows) < YEAR,
        "options": options,
    }
