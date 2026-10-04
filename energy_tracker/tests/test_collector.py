from datetime import UTC, datetime

from energy_tracker.collector import build_rows
from energy_tracker.config import Metric

NOW = datetime(2026, 10, 3, 23, 0, tzinfo=UTC)

METRICS = [
    Metric("pv_power", "sensor.pv", "power", "Solar"),
    Metric("heat_pump_power", "sensor.hp", "power", "Heat pump"),
    Metric("import_total", "sensor.imp", "energy", "Import"),
    Metric("missing", "sensor.gone", "power", "Missing"),
]


def state(value, unit):
    return {"state": value, "attributes": {"unit_of_measurement": unit}}


def test_build_rows_converts_and_skips_unusable_readings():
    states = {
        "sensor.pv": state("1.5", "kW"),
        "sensor.hp": state("unavailable", "W"),
        "sensor.imp": state("9.90468", "MWh"),
    }

    rows = build_rows(states, METRICS, NOW)

    assert [(name, round(value, 2)) for _, name, value in rows] == [
        ("pv_power", 1.5),
        ("import_total", 9904.68),
    ]
    assert all(time == NOW for time, _, _ in rows)


def test_missing_sensors_are_remembered_and_reported_once(caplog):
    from energy_tracker import collector

    collector.missing.clear()
    metrics = [
        Metric("pv_power", "sensor.pv", "power", "Solar"),
        Metric("ev_charger_power", "sensor.ev", "power", "EV charger"),
    ]
    states = {"sensor.pv": {"state": "1.5", "attributes": {"unit_of_measurement": "kW"}}}
    now = datetime(2026, 10, 4, tzinfo=UTC)
    with caplog.at_level("WARNING"):
        collector.build_rows(states, metrics, now)
        collector.build_rows(states, metrics, now)
    assert collector.missing == {"ev_charger_power"}
    assert sum("sensor.ev" in r.message for r in caplog.records) == 1

    states["sensor.ev"] = {"state": "7", "attributes": {"unit_of_measurement": "kW"}}
    collector.build_rows(states, metrics, now)
    assert collector.missing == set()
