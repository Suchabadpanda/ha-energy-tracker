"""Room comfort and balance."""

import dataclasses
import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from energy_tracker import api, collector, comfort, income, weather
from energy_tracker.config import Metric, load_settings

client = TestClient(api.app)
LONDON = ZoneInfo("Europe/London")
TODAY = date(2026, 11, 20)  # GMT: local hours are UTC hours

KITCHEN = comfort.Room("sensor.kitchen_t", "Kitchen", "sensor.kitchen_t", "sensor.kitchen_h")
BEDROOM = comfort.Room("sensor.bedroom_t", "Rosie's bedroom", "sensor.bedroom_t")


def sensor(value, unit, device_class=None, name=None):
    attributes = {"unit_of_measurement": unit, "friendly_name": name or "x"}
    if device_class:
        attributes["device_class"] = device_class
    return {"state": value, "attributes": attributes}


def test_rooms_are_read_and_the_sensors_to_choose_from_noted(monkeypatch):
    monkeypatch.setattr(comfort, "rooms", [KITCHEN, BEDROOM])
    states = {
        "sensor.kitchen_t": sensor("21.46", "°C", "temperature", "Kitchen temperature"),
        "sensor.kitchen_h": sensor("55", "%", "humidity", "Kitchen humidity"),
        "sensor.bedroom_t": sensor("68", "°F", None, "Bedroom"),
        "sensor.power": sensor("2.1", "kW"),
        "weather.home": sensor("x", None, "temperature"),
    }
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    rows = comfort.rows(states, now)
    assert rows == [
        (now, "room_temperature:sensor.kitchen_t", 21.46),
        (now, "room_humidity:sensor.kitchen_h", 55.0),
        (now, "room_temperature:sensor.bedroom_t", 20.0),
    ]
    assert [(s["entity"], s["kind"]) for s in comfort.seen] == [
        ("sensor.kitchen_h", "humidity"),
        ("sensor.bedroom_t", "temperature"),
        ("sensor.kitchen_t", "temperature"),
    ]
    assert comfort.metrics() == [
        "room_temperature:sensor.kitchen_t",
        "room_humidity:sensor.kitchen_h",
        "room_temperature:sensor.bedroom_t",
    ]
    # An unusable reading is left out.
    states["sensor.kitchen_h"] = sensor("unavailable", "%", "humidity")
    assert len(comfort.rows(states, now)) == 2


def hourly(days, value_at):
    """Hourly readings for `days` days to the end of TODAY, from value_at(local hour)."""
    start = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=UTC) - timedelta(days=days)
    return [
        (start + timedelta(hours=n), value_at((start + timedelta(hours=n)).hour))
        for n in range((days + 1) * 24)
    ]


def night_setback(day_value):
    return lambda hour: day_value if 7 <= hour < 22 else 16.0


def test_comfort_balance_and_the_averages_by_period():
    temperatures = {
        KITCHEN.key: hourly(10, night_setback(22.0)),
        BEDROOM.key: hourly(10, night_setback(19.0)),
    }
    humidities = {KITCHEN.key: hourly(10, lambda hour: 70.0 if hour < 12 else 50.0)}
    outdoor = hourly(10, lambda hour: 5.0)
    args = (temperatures, humidities, outdoor, LONDON, 21.0, 1.0)
    found = comfort.analyse([KITCHEN, BEDROOM], *args, ("07:00", "22:00"), TODAY)
    assert found["has_data"] and found["last_day"] == TODAY - timedelta(days=1)  # not today
    day = found["days"][-1]
    assert day["rooms"][KITCHEN.key]["average"] == 22.0
    assert day["rooms"][KITCHEN.key]["in_band_percent"] == 100  # 22 is the top of the band
    assert day["rooms"][BEDROOM.key]["below_percent"] == 100
    assert day["house_average"] == 20.5 and day["spread"] == 3.0 and day["outdoor"] == 5.0
    assert day["in_band_percent"] == 50
    assert day["rooms"][KITCHEN.key]["humidity"] == 57  # 07:00 to 22:00: 5 hours at 70
    assert day["rooms"][KITCHEN.key]["humid_percent"] == 33
    assert day["rooms"][BEDROOM.key]["humidity"] is None
    summary = found["summary"]
    assert summary["days"] == 7 and summary["coldest"] == BEDROOM.key
    assert summary["rooms"][BEDROOM.key]["offset"] == -1.5
    # Weeks from Monday, months and years, averaged over every hour in them.
    assert [w["name"] for w in found["weeks"]] == ["2026-11-09", "2026-11-16"]
    assert found["weeks"][0]["days"] == 6 and found["weeks"][1]["days"] == 4
    assert found["months"] == [{**found["months"][0], "name": "2026-11", "days": 10}]
    assert found["years"][0]["rooms"][KITCHEN.key]["average"] == 22.0
    # All day, the night setback counts: lower averages and a wider low.
    allday = comfort.analyse([KITCHEN, BEDROOM], *args, None, TODAY)
    kitchen = allday["days"][-1]["rooms"][KITCHEN.key]
    assert kitchen["min"] == 16.0 and kitchen["average"] == round((15 * 22 + 9 * 16) / 24, 1)


def test_comfort_hours_can_run_past_midnight():
    assert comfort._in_hours(datetime(2026, 11, 20, 23, tzinfo=UTC), ("22:00", "06:00"))  # noqa: SLF001
    assert not comfort._in_hours(datetime(2026, 11, 20, 12, tzinfo=UTC), ("22:00", "06:00"))  # noqa: SLF001
    assert comfort.analyse([KITCHEN], {}, {}, [], LONDON, 21, 1, None, TODAY) == {"has_data": False}


def test_choosing_rooms_and_reading_their_figures():
    body = {
        "rooms": [
            {"name": "Kitchen", "temperature": "sensor.kitchen_t", "humidity": "sensor.kitchen_h"},
            {"name": "Office", "temperature": "sensor.office_t"},
        ],
        "target_c": 21.5,
        "band_c": 1,
        "hours_from": "07:00",
        "hours_to": "22:00",
    }
    empty = client.post("/api/comfort/settings", json=body).json()
    assert [room["name"] for room in empty["rooms"]] == ["Kitchen", "Office"]
    assert not empty["has_data"]
    assert [room.key for room in comfort.rooms] == ["sensor.kitchen_t", "sensor.office_t"]
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    rows = []
    for n in range(4 * 24 * 12, -1, -1):  # every 5 minutes for four days
        when = now - timedelta(minutes=5 * n)
        rows += [
            (when, "room_temperature:sensor.kitchen_t", 21.5),
            (when, "room_humidity:sensor.kitchen_h", 48.0),
            (when, "room_temperature:sensor.office_t", 20.0),
        ]
    api.database().insert_readings(rows)
    api.clear_caches()
    found = client.get("/api/comfort?hours=all").json()
    assert found["has_data"] and found["hours"] == "all"
    assert 3 <= len(found["days"]) <= 4
    assert found["days"][-1]["rooms"]["sensor.kitchen_t"]["average"] == 21.5
    assert found["days"][-1]["spread"] == 1.5
    assert found["now"]["sensor.kitchen_t"] == {"temperature": 21.5, "humidity": 48.0}
    assert found["now"]["sensor.office_t"]["humidity"] is None
    assert found["summary"]["coldest"] == "sensor.office_t"
    assert client.get("/api/comfort").json()["settings"]["target_c"] == 21.5
    # The same sensor for two rooms, or something that is not an entity, is refused.
    twice = {**body, "rooms": [body["rooms"][1], body["rooms"][1]]}
    assert client.post("/api/comfort/settings", json=twice).status_code == 422
    wrong = {**body, "rooms": [{"name": "X", "temperature": "not an entity"}]}
    assert client.post("/api/comfort/settings", json=wrong).status_code == 422
    assert {"comfort"} <= set(client.get("/api/backup").json()["settings"])


def test_the_sensors_to_choose_from(monkeypatch):
    monkeypatch.setattr(comfort, "seen", [{"entity": "sensor.a", "name": "A", "kind": "humidity"}])
    assert client.get("/api/comfort/sensors").json()["sensors"][0]["entity"] == "sensor.a"


def test_the_collector_reads_the_rooms_and_brings_in_their_history(monkeypatch):
    api.database().set_setting(
        "comfort",
        '{"rooms": [{"name": "Kitchen", "temperature": "sensor.kitchen_t", '
        '"humidity": "sensor.kitchen_h"}]}',
    )
    monkeypatch.setenv("HA_URL", "http://homeassistant.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "t")
    settings = dataclasses.replace(load_settings(), poll_seconds=0, backfill_days=0)
    monkeypatch.setattr(income, "backfill_events", lambda *args, **kwargs: 0)
    asked = []
    monkeypatch.setattr(
        weather, "backfill", lambda *args, **kwargs: asked.append((args[4], kwargs)) or 0
    )
    stop = threading.Event()
    polls = []

    def fetch_states(_client):
        polls.append(1)
        if len(polls) >= 2:
            stop.set()
        return {
            "sensor.kitchen_t": sensor("20.5", "°C", "temperature"),
            "sensor.kitchen_h": sensor("51", "%", "humidity"),
        }

    monkeypatch.setattr(collector, "fetch_states", fetch_states)
    metrics = [Metric("pv_power", "sensor.pv", "power", "Solar")]
    cleared = []
    worker = threading.Thread(
        target=collector.run,
        args=(settings, api.database(), metrics, stop, lambda: cleared.append(1)),
        daemon=True,
    )
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    # Figures worked out before the room's readings arrived are worked out again, once.
    assert cleared == [1]
    # History brought in once for each sensor, the humidity as a humidity.
    assert [(entity, kw["metric"], kw["humidity"]) for entity, kw in asked] == [
        ("sensor.kitchen_t", "room_temperature:sensor.kitchen_t", False),
        ("sensor.kitchen_h", "room_humidity:sensor.kitchen_h", True),
    ]
    since = datetime(2000, 1, 1, tzinfo=UTC)
    stored = api.database().latest(comfort.metrics(), since)
    assert stored["room_temperature:sensor.kitchen_t"][1] == 20.5
    assert stored["room_humidity:sensor.kitchen_h"][1] == 51.0


def test_humidity_history_is_read_as_a_percentage():
    assert weather.percent("55.4") == 55.4
    assert weather.percent("120") is None and weather.percent(None) is None
