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


def test_an_unexpected_error_does_not_stop_collecting(monkeypatch, caplog):
    """A poll that fails in a way nobody foresaw is logged, and the next poll goes ahead:
    the collector must never stop for good while the dashboard carries on as if all is well."""
    import dataclasses
    import threading

    from energy_tracker import api, collector, income
    from energy_tracker.config import load_settings

    monkeypatch.setenv("HA_URL", "http://homeassistant.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "t")
    settings = dataclasses.replace(load_settings(), poll_seconds=0, backfill_days=0)
    monkeypatch.setattr(income, "backfill_events", lambda *args, **kwargs: 0)
    stop = threading.Event()
    calls = []

    def fetch_states(_client):
        calls.append(1)
        if len(calls) == 1:
            raise KeyError("attributes")  # not an HTTP or database error
        if len(calls) >= 3:
            stop.set()
        return {"sensor.pv": state("1.5", "kW")}

    monkeypatch.setattr(collector, "fetch_states", fetch_states)
    monkeypatch.setattr(collector, "last_poll_ok", None)
    metrics = [Metric("pv_power", "sensor.pv", "power", "Solar")]
    with caplog.at_level("ERROR"):
        worker = threading.Thread(
            target=collector.run, args=(settings, api.database(), metrics, stop), daemon=True
        )
        worker.start()
        worker.join(10)
    assert not worker.is_alive()
    assert len(calls) >= 3  # it carried on after the failure
    assert any("unexpectedly" in r.message for r in caplog.records)
    assert collector.last_poll_ok is not None
    assert api.database().latest(["pv_power"], datetime(2000, 1, 1, tzinfo=UTC))
