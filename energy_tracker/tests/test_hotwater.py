"""Telling hot water from heating by the cylinder's reheats."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_tracker import hotwater
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 12, tzinfo=LONDON)


def heat_pump_and_tank():
    """1 kWh every half hour all day; the tank is reheated 02:00–03:00, else cooling."""
    used, tank, total, temp = [], [], 0.0, 45.0
    t = DAY - timedelta(hours=1)
    while t <= DAY + timedelta(days=1, hours=1):
        used.append((t, total))
        tank.append((t, temp))
        total += 0.5  # 1 kWh a half hour
        local = t.astimezone(LONDON)
        reheating = local.date() == DAY.date() and 2 <= local.hour < 3
        temp += 1.5 if reheating else -0.05  # every 15 minutes
        t += timedelta(minutes=15)
    return Counter(used, timedelta(minutes=75)), hotwater.Tank(tank)


def test_reheats_are_hot_water_and_the_rest_heating():
    heat_pump, tank = heat_pump_and_tank()
    found = hotwater.split(tank, heat_pump, None, DAY, DAY + timedelta(days=1), LONDON)
    day = found[date(2026, 10, 12)]
    assert day["hot_water_kwh"] == pytest.approx(2.0)  # two half hours of reheating
    assert day["heating_kwh"] == pytest.approx(46.0)


def test_days_without_tank_readings_are_left_to_estimate():
    heat_pump, _ = heat_pump_and_tank()
    sparse = hotwater.Tank([(DAY, 45.0), (DAY + timedelta(hours=6), 44.0)])
    assert hotwater.split(sparse, heat_pump, None, DAY, DAY + timedelta(days=1), LONDON) == {}


def test_measured_hot_water_takes_the_car_off_the_circuit():
    heat_pump, tank = heat_pump_and_tank()
    samples = {
        hotwater.TANK: list(
            zip([datetime.fromtimestamp(s, LONDON) for s in tank.stamps], tank.values, strict=True)
        ),
        "circuit": list(zip(heat_pump.times, heat_pump.values, strict=True)),
        # The car charges 1 kWh a half hour during the reheat too.
        "ev": [(DAY + timedelta(hours=2), 0.0), (DAY + timedelta(hours=3), 2.0)],
    }
    found = hotwater.measured_hot_water(
        samples, DAY, DAY + timedelta(days=1), LONDON, "circuit", "ev", "meter", True
    )
    assert found[date(2026, 10, 12)] == pytest.approx(0.0)  # the car used it all


# The night of 22–23 Oct 2026: SmartThings froze from 23:43 to 03:00. Home Assistant kept
# showing the tank at 16 °C and the water heater off, then the tank jumped to 56 °C. The heat
# pump had reheated the cylinder 01:00–02:45, drawing these kW each quarter hour.
NIGHT = datetime(2026, 10, 23, tzinfo=LONDON)
REHEAT_KW = {
    (1, 0): 0.32,
    (1, 15): 0.68,
    (1, 30): 0.68,
    (1, 45): 2.68,
    (2, 0): 4.68,
    (2, 15): 3.93,
    (2, 30): 0.82,
}


def frozen_night():
    """Minute readings 22:00–05:00: heat pump kWh counter, tank (frozen, then the jump)."""
    used, tank, total = [], [], 0.0
    t = NIGHT - timedelta(hours=2)
    while t <= NIGHT + timedelta(hours=5):
        local = t.astimezone(LONDON)
        kw = (
            REHEAT_KW.get((local.hour, local.minute // 15 * 15), 0.02)
            if local.hour < 3 and local.date() == NIGHT.date()
            else 0.02
        )
        used.append((t, total))
        total += kw / 60
        if t < NIGHT - timedelta(minutes=17):
            temp = 17.0 if t < NIGHT - timedelta(minutes=30) else 16.5
        elif t < NIGHT + timedelta(hours=3):
            temp = 16.0  # frozen: SmartThings stopped sending
        else:
            temp = 56.0
        tank.append((t, temp))
        t += timedelta(minutes=1)
    return used, tank


def test_a_jump_after_frozen_readings_is_a_catch_up():
    _, tank = frozen_night()
    spells = hotwater.catch_ups(tank)
    assert len(spells) == 1
    start, end, rise = spells[0]
    assert start == NIGHT - timedelta(minutes=17)  # when it last really changed
    assert end == NIGHT + timedelta(hours=3)
    assert rise == pytest.approx(40.0)


def test_an_ordinary_reheat_is_not_a_catch_up():
    # 0.5 °C a minute at most, even read half-hourly, and readings moving beforehand
    tank = [(DAY + timedelta(minutes=m), 20.0 + max(0, m - 60) * 0.3) for m in range(0, 240, 5)]
    assert hotwater.catch_ups(tank) == []
    half_hourly = [
        (DAY + timedelta(minutes=30 * i), v) for i, v in enumerate([20, 20, 20, 29, 38, 47])
    ]
    assert hotwater.catch_ups(half_hourly) == []


def test_the_frozen_reheat_counts_as_hot_water():
    used, tank = frozen_night()
    found = hotwater.split(
        hotwater.Tank(tank),
        Counter(used, timedelta(minutes=5)),
        None,
        NIGHT,
        NIGHT + timedelta(hours=5),
        LONDON,
        spells=hotwater.catch_ups(tank),
    )
    day = found[NIGHT.date()]
    reheat = sum(REHEAT_KW.values()) / 4
    assert day["hot_water_kwh"] == pytest.approx(
        reheat + 0.02 * 1.25, abs=0.005
    )  # plus standby 00:00–03:00 outside the reheat
    assert day["heating_kwh"] == pytest.approx(0.02 * 2, abs=0.005)  # 03:00–05:00
