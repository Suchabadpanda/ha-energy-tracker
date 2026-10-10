"""Device costs that follow the energy through the battery."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_tracker import devices
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
DAY = datetime(2026, 1, 12, tzinfo=LONDON)
RATES = Schedule(
    [
        build_tariff(
            "Night saver",
            [
                {"start": "00:00", "end": "06:00", "p_per_kwh": 7.0},
                {"start": "06:00", "end": "24:00", "p_per_kwh": 26.0},
            ],
            15.0,
            0,
        )
    ]
)


def counters_for(flows) -> dict[str, Counter]:
    """Lifetime counters from a function giving each half hour's kWh by name."""
    names = ["load", "circuit", "ev", "import", "export", "solar", "charge", "discharge"]
    totals = dict.fromkeys(names, 0.0)
    series = {name: [] for name in names}
    t = DAY - timedelta(days=1)
    while t <= DAY + timedelta(days=1):
        for name in names:
            series[name].append((t, totals[name]))
        local = t.astimezone(LONDON)
        for name, kwh in flows(local.hour + local.minute / 60).items():
            totals[name] += kwh
        t += timedelta(minutes=30)
    keys = {
        "load": devices.LOAD,
        "circuit": devices.CIRCUIT,
        "ev": devices.EV,
        "import": devices.IMPORT,
        "export": devices.EXPORT,
        "solar": devices.SOLAR,
        "charge": devices.CHARGE,
        "discharge": devices.DISCHARGE,
    }
    return {keys[name]: Counter(points, timedelta(minutes=75)) for name, points in series.items()}


def night_charging(hour):
    """The car and the battery charge overnight; the battery runs the house by day."""
    if hour < 6:  # 12 half hours: car 15 kWh and battery 15 kWh from the grid
        return {"import": 2.5, "ev": 1.25, "circuit": 1.25, "load": 1.25, "charge": 1.25}
    if hour < 21:  # 30 half hours: house 0.5 kWh each, from the battery
        return {"load": 0.5, "discharge": 0.5}
    return {"load": 0.5, "import": 0.5}  # 6 half hours from the grid at the day rate


def test_a_car_charged_overnight_costs_the_night_rate():
    day = devices.daily(counters_for(night_charging), DAY, DAY + timedelta(days=1), LONDON, RATES)[
        date(2026, 1, 12)
    ]
    assert day["followed"]
    assert day["ev_charger_kwh"] == pytest.approx(15)
    assert day["ev_charger_gbp"] == pytest.approx(15 * 0.07)  # 7p a unit, not the day average
    # The house: 15 kWh from the battery charged at 7p, and 3 kWh bought at 26p.
    assert day["rest_gbp"] == pytest.approx(15 * 0.07 + 3 * 0.26)
    assert day["import_gbp"] == pytest.approx(30 * 0.07 + 3 * 0.26)


def test_solar_is_free_to_use_but_its_true_cost_is_the_export_given_up():
    def sunny(hour):
        if 10 <= hour < 14:  # 8 half hours: 2 kWh of sun, 1 to the house, 1 to the battery
            return {"solar": 2.0, "load": 1.0, "circuit": 1.0, "charge": 1.0}
        if 18 <= hour < 22:  # the stored 8 kWh run the heat pump in the evening
            return {"load": 1.0, "circuit": 1.0, "discharge": 1.0}
        return {}

    day = devices.daily(counters_for(sunny), DAY, DAY + timedelta(days=1), LONDON, RATES)[
        date(2026, 1, 12)
    ]
    assert day["heat_pump_kwh"] == pytest.approx(16)
    assert day["heat_pump_gbp"] == pytest.approx(0)
    assert day["heat_pump_solar_gbp"] == pytest.approx(16 * 0.15)  # all of it at 15p export


def test_without_battery_readings_each_day_is_shared_by_use():
    found = counters_for(night_charging)
    for name in devices.BATTERY_COUNTERS:
        found[name] = Counter([])
    day = devices.daily(found, DAY, DAY + timedelta(days=1), LONDON, RATES)[date(2026, 1, 12)]
    assert not day.get("followed")
    assert day["ev_charger_gbp"] + day["rest_gbp"] == pytest.approx(day["import_gbp"])


def test_the_heat_pump_meter_is_used_in_place_of_the_circuit():
    def with_meter(hour):
        # The circuit carries the car overnight; the heat pump's own meter reads 0.5 a slot.
        flows = {"load": 1.0, "import": 1.0}
        if hour < 6:
            flows.update(ev=2.0, circuit=2.5, load=3.0, **{"import": 3.0})
        return flows

    found = counters_for(with_meter)
    # counters_for knows nothing of the meter: build it the same way.
    series, total, t = [], 0.0, DAY - timedelta(days=1)
    while t <= DAY + timedelta(days=1):
        series.append((t, total))
        total += 0.5
        t += timedelta(minutes=30)
    found[devices.HEAT_PUMP] = Counter(series, timedelta(minutes=75))
    day = devices.daily(found, DAY, DAY + timedelta(days=1), LONDON, RATES)[date(2026, 1, 12)]
    assert day["heat_pump_kwh"] == pytest.approx(24)  # 48 half hours at 0.5
    assert day["ev_charger_kwh"] == pytest.approx(24)  # 12 half hours at 2
    # 72 kWh in all (12 half hours at 3, 36 at 1), less the heat pump and the car.
    assert day["rest_kwh"] == pytest.approx(72 - 24 - 24)


def test_the_heat_pump_meter_option_adds_its_sensor():
    from energy_tracker.config import load_metrics

    plain = {m.name for m in load_metrics()}
    assert devices.HEAT_PUMP not in plain
    added = {m.name: m for m in load_metrics(heat_pump_entity="sensor.heat_pump_energy")}
    assert added[devices.HEAT_PUMP].entity == "sensor.heat_pump_energy"
    assert added[devices.HEAT_PUMP].kind == "energy"


def test_battery_energy_sold_back_to_the_grid_is_costed_apart():
    def export_event(hour):
        if hour < 6:  # 12 half hours: the battery is filled from the grid, 1 kWh each
            return {"import": 1.0, "charge": 1.0}
        if 17 <= hour < 18:  # 2 half hours: an export event empties it to the grid
            return {"discharge": 6.0, "export": 6.0}
        return {}

    day = devices.daily(counters_for(export_event), DAY, DAY + timedelta(days=1), LONDON, RATES)[
        date(2026, 1, 12)
    ]
    assert day["battery_export_kwh"] == pytest.approx(12)
    assert day["battery_export_gbp"] == pytest.approx(12 * 0.07)  # what storing it cost
    assert day["import_gbp"] == pytest.approx(12 * 0.07)
