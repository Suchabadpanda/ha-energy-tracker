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

from .usage import Counter, half_hour_slots

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
    for piece_start, piece_end in half_hour_slots(start, end):
        day = days.setdefault(
            piece_start.astimezone(timezone).date(),
            {"hot_water_kwh": 0.0, "heating_kwh": 0.0, "covered_hours": 0.0, "hours": 0.0},
        )
        hours = (piece_end - piece_start).total_seconds() / 3600
        day["hours"] += hours
        used = max(0.0, heat_pump.between(piece_start, piece_end) or 0.0)
        if ev_on_circuit and ev:
            used = max(0.0, used - (ev.between(piece_start, piece_end) or 0.0))
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
) -> dict[date, float]:
    """kWh of hot water on each day the tank readings cover, from stored samples.

    The smart load circuit is preferred to the heat pump's own meter, as it is read every
    few seconds; a maker's meter may only report now and then, which would blur when the
    energy was used.
    """
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
