"""Telling the heat pump's hot water from its heating, by watching the cylinder.

When the heat pump reheats the hot water cylinder, the tank temperature climbs quickly:
several degrees in half an hour. Water being used makes it fall, and standing losses make
it drift down slowly, so a rise means the cylinder is being heated. The heat pump's
electricity in half hours where the tank rose is counted as hot water, the rest as heating.

An immersion heater (a legionella cycle, say) also makes the tank rise; if it is not on the
heat pump's meter it adds no heat pump electricity, so it does no harm here.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .usage import Counter, half_hour_pieces

TANK = "hot_water_tank_temperature"
RISE_C = 1.0  # a half hour in which the tank rose this much is a reheat
MAX_GAP = timedelta(hours=2)  # readings further apart than this tell nothing
MIN_COVER = 0.8  # share of a day the tank readings must cover for it to count as measured


class Tank:
    """The tank temperature over time, interpolated between readings."""

    def __init__(self, samples: list[tuple[datetime, float]]) -> None:
        points = sorted((t.astimezone(UTC).timestamp(), v) for t, v in samples)
        self.stamps = [t for t, _ in points]
        self.values = [v for _, v in points]

    def __bool__(self) -> bool:
        return len(self.stamps) >= 2

    def at(self, when: datetime) -> float | None:
        """Temperature at a moment, or None outside the readings or across a long gap."""
        stamp = when.timestamp()
        index = bisect_right(self.stamps, stamp)
        if index == 0 or index == len(self.stamps):
            return self.values[index - 1] if index and stamp == self.stamps[-1] else None
        s0, s1 = self.stamps[index - 1], self.stamps[index]
        if s1 - s0 > MAX_GAP.total_seconds():
            return None
        v0, v1 = self.values[index - 1], self.values[index]
        return v0 + (v1 - v0) * (stamp - s0) / (s1 - s0)


def split(
    tank: Tank,
    heat_pump: Counter,
    ev: Counter | None,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    ev_on_circuit: bool = False,
) -> dict[date, dict]:
    """Hot water and heating kWh for each local day the tank readings cover.

    `heat_pump` is the heat pump's electricity: its own meter, or the smart load circuit (in
    which case `ev_on_circuit` takes the EV charger's use off it, half hour by half hour).
    Days with too few tank readings are left out, to be estimated instead.
    """
    days: dict[date, dict] = {}
    for piece_start, piece_end, a, b in half_hour_pieces(start, end):
        day = days.setdefault(
            piece_start.astimezone(timezone).date(),
            {"hot_water_kwh": 0.0, "heating_kwh": 0.0, "covered_hours": 0.0, "hours": 0.0},
        )
        hours = (piece_end - piece_start).total_seconds() / 3600
        day["hours"] += hours
        used = max(0.0, heat_pump.kwh(a, b) or 0.0)
        if ev_on_circuit and ev:
            used = max(0.0, used - (ev.kwh(a, b) or 0.0))
        before, after = tank.at(piece_start), tank.at(piece_end)
        if before is None or after is None:
            day["heating_kwh"] += used  # unknown: kept out of the measured share below
            continue
        day["covered_hours"] += hours
        # Scale the threshold to the piece's length (the first and last may be short).
        if after - before >= RISE_C * hours / 0.5:
            day["hot_water_kwh"] += used
        else:
            day["heating_kwh"] += used
    return {
        when: {"hot_water_kwh": row["hot_water_kwh"], "heating_kwh": row["heating_kwh"]}
        for when, row in days.items()
        if row["hours"] and row["covered_hours"] / row["hours"] >= MIN_COVER
    }


def electricity(
    samples: dict[str, list],
    circuit_name: str,
    ev_name: str,
    meter_name: str,
    ev_on_smart_load: bool,
    gap: timedelta | None = None,
):
    """The heat pump's kWh between two moments: from the smart load circuit (less the EV
    charger, if it shares the circuit) where that was read, else the heat pump's own meter.
    None if there is neither."""

    def counter(name: str) -> Counter:
        return Counter(samples.get(name, []), *([gap] if gap else []))

    circuit, ev, meter = counter(circuit_name), counter(ev_name), counter(meter_name)
    if not circuit and not meter:
        return None
    first = circuit.first_time if circuit else None

    def used(start: datetime, end: datetime) -> float:
        if circuit and (not meter or (first and start >= first)):
            kwh = circuit.between(start, end) or 0.0
            if ev_on_smart_load and ev:
                kwh -= ev.between(start, end) or 0.0
            return max(0.0, kwh)
        return max(0.0, meter.between(start, end) or 0.0)

    return used


def measured_hot_water(
    samples: dict[str, list],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    circuit_name: str,
    ev_name: str,
    meter_name: str,
    ev_on_smart_load: bool,
    gap: timedelta | None = None,
    modes: dict[str, list] | None = None,
    outdoor: list | None = None,
) -> dict[date, float]:
    """kWh of hot water on each day it can be measured, from stored samples.

    Best is the heat pump's own hot water mode (`modes`), which says exactly when it was
    heating the cylinder; otherwise the cylinder's temperature rises. The smart load circuit
    is preferred to the heat pump's own meter, as it is read every few seconds; a maker's
    meter may only report now and then, which would blur when the energy was used.
    """
    found = by_tank(
        samples, start, end, timezone, circuit_name, ev_name, meter_name, ev_on_smart_load, gap
    )
    split_by_mode = by_mode(
        samples,
        start,
        end,
        timezone,
        circuit_name,
        ev_name,
        meter_name,
        ev_on_smart_load,
        gap,
        modes,
        outdoor,
    )
    found.update({day: row["hot_water_kwh"] for day, row in split_by_mode.items()})
    return found


def by_mode(
    samples: dict[str, list],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    circuit_name: str,
    ev_name: str,
    meter_name: str,
    ev_on_smart_load: bool,
    gap: timedelta | None = None,
    modes: dict[str, list] | None = None,
    outdoor: list | None = None,
) -> dict[date, dict]:
    """The split by the heat pump's own modes, day by day (see heatpump.split)."""
    from . import heatpump  # it builds on this module

    if not modes or not modes.get(heatpump.HOT_WATER_MODE):
        return {}
    used = electricity(samples, circuit_name, ev_name, meter_name, ev_on_smart_load, gap)
    if used is None:
        return {}
    return heatpump.split(
        heatpump.Steps(modes[heatpump.HOT_WATER_MODE]),
        heatpump.Steps(modes.get(heatpump.HEATING_MODE, [])),
        heatpump.Steps(modes.get(heatpump.FLOW, [])),
        Tank(outdoor or []),
        used,
        start,
        end,
        timezone,
    )


def by_tank(
    samples: dict[str, list],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    circuit_name: str,
    ev_name: str,
    meter_name: str,
    ev_on_smart_load: bool,
    gap: timedelta | None = None,
) -> dict[date, float]:
    """kWh of hot water on each day the tank readings cover, from its temperature rises."""
    tank = Tank(samples.get(TANK, []))
    if not tank:
        return {}

    def counter(name: str) -> Counter:
        return Counter(samples.get(name, []), *([gap] if gap else []))

    circuit, ev, meter = counter(circuit_name), counter(ev_name), counter(meter_name)
    if circuit:
        found = split(tank, circuit, ev, start, end, timezone, ev_on_circuit=ev_on_smart_load)
        # The circuit only carries the heat pump from when it was read; before, the meter.
        first = circuit.first_time
        if meter and first and first > start:
            found = {**split(tank, meter, None, start, first, timezone), **found}
    elif meter:
        found = split(tank, meter, None, start, end, timezone)
    else:
        return {}
    return {day: row["hot_water_kwh"] for day, row in found.items()}
