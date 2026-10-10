"""What each device cost to run: the heat pump (or other smart load), the EV charger and
the rest of the house.

The energy is followed to where it came from. In each half hour the house is supplied by
solar, the grid and the battery, and each device pays for its share of that half hour's
supply: grid power at that half hour's price, and battery power at what the energy in the
battery cost to put there. A car charged overnight at the cheap rate therefore costs the
cheap rate, and the house run from the battery during the day carries the cost of charging
it. Solar costs nothing to use, but using it gives up the export payment; the "true cost"
counts it at the export rate, for solar used straight away and for solar stored in the
battery alike.

Without battery readings, each day's import cost is shared out by how much of that day's
electricity each device used instead.

The standing charge and export income belong to the house as a whole and are left out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
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
CHARGE = "battery_charge_energy_total"
DISCHARGE = "battery_discharge_energy_total"
BATTERY_COUNTERS = [CHARGE, DISCHARGE]
# Everything the costing can use: ask for these.
ALL_COUNTERS = [*COUNTERS, *SOLAR_COUNTERS, *BATTERY_COUNTERS]
# The energy in the battery at the start is priced from this far back, so follow it from
# then. Callers fetch readings from a day before the period anyway.
WARM_UP = timedelta(days=1)
PARTS = ("heat_pump", "ev_charger", "rest")


def daily(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    ev_on_smart_load: bool = True,
) -> dict[date, dict]:
    """Energy and cost for each device, for each local day.

    Each row has `{part}_kwh` and `{part}_gbp` (what it cost in imported electricity, VAT
    included), and with solar recorded `{part}_solar_gbp` (the export payment given up on
    the solar it used). `import_gbp` is what the day's import actually cost; it differs from
    the parts' total by energy carried in or out in the battery. `followed` says which way
    the costs were worked out.
    """
    if all(counters.get(name) for name in (*BATTERY_COUNTERS, *SOLAR_COUNTERS)):
        return _followed(counters, start, end, timezone, schedule, ev_on_smart_load)
    return _shared_by_day(counters, start, end, timezone, schedule, ev_on_smart_load)


def _split(load: float, circuit: float, ev: float, has: tuple, ev_on_smart_load: bool):
    """kWh used by the heat pump, the EV charger and the rest of the house."""
    has_circuit, has_ev = has
    ev_kwh, smart_kwh = ev, circuit
    if has_circuit and has_ev and ev_on_smart_load:
        ev_kwh = min(ev_kwh, smart_kwh)  # the charger cannot use more than its circuit
        smart_kwh -= ev_kwh
    total = max(load, smart_kwh + ev_kwh)
    return total, {
        name: kwh
        for name, kwh, present in (
            ("heat_pump", smart_kwh, has_circuit),
            ("ev_charger", ev_kwh, has_ev),
            ("rest", total - smart_kwh - ev_kwh, True),
        )
        if present
    }


def _kwh(counter: Counter, start: datetime, end: datetime) -> float:
    return max(0.0, counter.between(start, end) or 0.0)


class _Battery:
    """What is in the battery, in kWh it can give back, and what putting it there cost: at
    import prices, and with solar counted at the export rate ("true")."""

    def __init__(self, efficiency: float) -> None:
        self.efficiency = efficiency  # what it gives back of what it takes in
        self.stored = self.paid = self.paid_true = 0.0

    def put(self, kwh: float, price: float, price_true: float) -> None:
        self.stored += kwh * self.efficiency
        self.paid += kwh * price
        self.paid_true += kwh * price_true

    def take(self, kwh: float, fallback: float) -> tuple[float, float]:
        """Cost of `kwh` taken out, at what it cost to put in. If the battery holds less than
        is taken (readings began part-full), the rest is priced at `fallback`."""
        if kwh <= 0:
            return 0.0, 0.0
        if self.stored <= 1e-9:
            return kwh * fallback, kwh * fallback
        rate, rate_true = self.paid / self.stored, self.paid_true / self.stored
        held = min(kwh, self.stored)
        cost = held * rate + (kwh - held) * fallback
        cost_true = held * rate_true + (kwh - held) * fallback
        remaining = 1 - held / self.stored
        self.stored -= held
        self.paid *= remaining
        self.paid_true *= remaining
        return cost, cost_true


def _followed(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    ev_on_smart_load: bool,
) -> dict[date, dict]:
    """Costs following the energy through the battery, half hour by half hour."""
    load, circuit, ev, imports = (counters.get(name) for name in COUNTERS)
    solar, exports = (counters[name] for name in SOLAR_COUNTERS)
    charge, discharge = (counters[name] for name in BATTERY_COUNTERS)
    has = (bool(circuit), bool(ev))
    first = start - WARM_UP

    # Round-trip losses: the battery gives back less than it takes in.
    charged = charge.between(first, end) or 0.0
    returned = discharge.between(first, end) or 0.0
    efficiency = min(1.0, max(0.7, returned / charged)) if charged > 1 else 0.9

    battery = _Battery(efficiency)
    days: dict[date, dict] = {}
    for piece_start, piece_end in half_hour_slots(first, end):
        local = piece_start.astimezone(timezone)
        tariff = schedule.on(local.date())
        price = tariff.import_price(local) / 100
        export_price = tariff.export_p_per_kwh / 100
        sun, bought, sold, into, out_of = (
            _kwh(c, piece_start, piece_end) for c in (solar, imports, exports, charge, discharge)
        )

        # Where each source went: export from solar first, the battery from solar before
        # the grid, and whatever is left supplies the house.
        sun_sold = min(sun, sold)
        battery_sold = min(out_of, sold - sun_sold)
        sun_left = sun - sun_sold
        sun_stored = min(sun_left, into)
        grid_stored = min(bought, into - sun_stored)
        sun_used = sun_left - sun_stored
        grid_used = bought - grid_stored
        battery_used = out_of - battery_sold

        cheap = tariff.cheap_rate(local) / 100
        battery_cost, battery_cost_true = battery.take(battery_used, cheap)
        battery.take(battery_sold, cheap)
        battery.put(grid_stored, price, price)
        battery.put(sun_stored, 0.0, export_price)

        if piece_start < start:
            continue  # only following the battery so far
        house_cost = grid_used * price + battery_cost
        house_true = grid_used * price + sun_used * export_price + battery_cost_true

        total, parts = _split(
            _kwh(load, piece_start, piece_end),
            _kwh(circuit, piece_start, piece_end) if circuit else 0.0,
            _kwh(ev, piece_start, piece_end) if ev else 0.0,
            has,
            ev_on_smart_load,
        )
        row = days.setdefault(local.date(), {"total_kwh": 0.0, "import_gbp": 0.0, "followed": True})
        row["total_kwh"] += total
        row["import_gbp"] += bought * price
        # Share the half hour's supply cost by use. Readings rarely balance exactly, so
        # this goes by shares of the house's use rather than kWh for kWh.
        for name, kwh in parts.items():
            share = kwh / total if total > 0 else 0.0
            row[f"{name}_kwh"] = row.get(f"{name}_kwh", 0.0) + kwh
            row[f"{name}_gbp"] = row.get(f"{name}_gbp", 0.0) + house_cost * share
            row[f"{name}_solar_gbp"] = (
                row.get(f"{name}_solar_gbp", 0.0) + (house_true - house_cost) * share
            )
    return days


def _shared_by_day(
    counters: dict[str, Counter],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    ev_on_smart_load: bool,
) -> dict[date, dict]:
    """Without battery readings: each day's import cost shared by each device's use."""
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
