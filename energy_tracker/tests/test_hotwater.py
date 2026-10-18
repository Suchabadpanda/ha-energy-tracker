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
