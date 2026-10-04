"""Today's energy by device, and the cost of a period (today, or the month so far)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .costs import cost_between
from .tariff import Schedule
from .usage import GAP, Counter, Sample, window_start

# Lifetime counters this module needs (names from config/metrics.toml).
LOAD = "load_energy_total"
CIRCUIT = "smart_load_energy_total"  # heat pump + EV charger circuit
EV = "ev_charger_energy_total"
IMPORT = "import_energy_total"
EXPORT = "export_energy_total"
DEVICE_COUNTERS = [LOAD, CIRCUIT, EV]
COST_COUNTERS = [IMPORT, EXPORT]


def local_midnight(now: datetime, timezone: ZoneInfo) -> datetime:
    return now.astimezone(timezone).replace(hour=0, minute=0, second=0, microsecond=0)


def energy_by_device(
    samples: dict[str, list[Sample]],
    now: datetime,
    timezone: ZoneInfo,
    inverter_used_today: float | None = None,
    ev_on_smart_load: bool = True,
) -> dict | None:
    """kWh used today by the smart load (e.g. a heat pump), the EV charger and the rest.

    The smart load and the EV charger are both optional: a device with no readings is left
    out of the result. `ev_on_smart_load` says whether the EV charger is wired through the
    smart load port (so its use is part of the smart load figure) or separately.

    `inverter_used_today` is the inverter's own "consumption today" figure. It is exact even
    across periods when nothing was collected, so it is preferred for the total when the
    whole day is being reported.
    """
    load, circuit, ev = (Counter(samples.get(name, [])) for name in DEVICE_COUNTERS)
    midnight = local_midnight(now, timezone)
    present = [c for c in (load, circuit, ev) if c]
    start = window_start(present, midnight) if load else None
    if start is None:
        return None
    full_day = start == midnight

    total = load.between(start, now)
    if full_day and inverter_used_today is not None:
        total = inverter_used_today
    circuit_kwh = circuit.between(start, now) if circuit else None
    ev_kwh = ev.between(start, now) if ev else None

    smart_load_kwh, measured = circuit_kwh, (circuit_kwh or 0.0)
    if circuit and ev and ev_on_smart_load:
        ev_kwh = min(ev_kwh, circuit_kwh)  # the charger cannot use more than its circuit
        smart_load_kwh = circuit_kwh - ev_kwh
    elif ev:
        measured += ev_kwh

    result = {
        "since": start,
        "full_period": full_day,
        "estimated_hours": round(
            max(
                (c.uncovered(start, now) for c in present[1:]), default=load.uncovered(start, now)
            ).total_seconds()
            / 3600,
            1,
        ),
        "total_kwh": round(total, 3),
        "rest_kwh": round(max(0.0, total - measured), 3),
    }
    if smart_load_kwh is not None:
        result["heat_pump_kwh"] = round(max(0.0, smart_load_kwh), 3)
    if ev_kwh is not None:
        result["ev_charger_kwh"] = round(ev_kwh, 3)
    return result


def cost_since(
    samples: dict[str, list[Sample]],
    period_start: datetime,
    now: datetime,
    timezone: ZoneInfo,
    schedule: Schedule,
    gap: timedelta = GAP,
) -> dict | None:
    """Cost from `period_start` to now, or from the first reading if that is later.

    `gap` is how far apart two samples can be before the time between them counts as
    "no readings"; it must be longer than the spacing of the samples passed in.
    """
    imports, exports = (Counter(samples.get(name, []), gap) for name in COST_COUNTERS)
    start = window_start([imports, exports], period_start)
    if start is None or start >= now:
        return None  # no readings at all, or none until after this period ended
    result = cost_between(imports, exports, start, now, timezone, schedule)
    result["full_period"] = start == period_start
    return result
