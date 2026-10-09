"""The heat pump against the weather."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, heating, weather

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)


def test_temperatures_are_read_in_celsius_from_weather_entities_and_sensors():
    assert weather.celsius("50", "°F") == 10.0
    assert weather.celsius("unavailable", "°C") is None
    assert weather.celsius(12, "%") is None and weather.celsius(99, "°C") is None
    forecast = {
        "entity_id": "weather.home",
        "state": "rainy",
        "attributes": {"temperature": 7.5, "temperature_unit": "°C"},
    }
    sensor = {
        "entity_id": "sensor.garden",
        "state": "41",
        "attributes": {"unit_of_measurement": "°F"},
    }
    assert weather.reading(forecast) == 7.5
    assert weather.reading(sensor) == 5.0
    states = {"weather.home": forecast, "sensor.garden": sensor, "weather.alt": forecast}
    assert weather.pick_entity(states, "") == "weather.alt"  # the first, in name order
    assert weather.pick_entity(states, "sensor.garden") == "sensor.garden"
    assert weather.pick_entity(states, "sensor.missing") is None
    now = datetime(2026, 1, 5, 12, tzinfo=UTC)
    assert weather.row(states, "sensor.garden", now) == (now, weather.METRIC, 5.0)
    assert weather.entity_in_use == "sensor.garden"


def test_a_weather_entity_is_backfilled_once_from_recent_history():
    asked = []

    def reply(request):
        asked.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                [
                    {
                        "state": "cloudy",
                        "last_updated": "2026-01-04T10:00:00+00:00",
                        "attributes": {"temperature": 4.0, "temperature_unit": "°C"},
                    },
                    {
                        "state": "cloudy",
                        "last_updated": "2026-01-04T11:00:00+00:00",
                        "attributes": {"temperature": 41, "temperature_unit": "°F"},
                    },
                ]
            ],
        )

    ha = httpx.Client(base_url="http://ha", transport=httpx.MockTransport(reply))
    now = datetime(2026, 1, 5, tzinfo=UTC)
    db = api.database()
    assert weather.backfill(db, ha, "ws://unused", "t", "weather.home", now, 10) == 2
    assert weather.backfill(db, ha, "ws://unused", "t", "weather.home", now, 10) == 0
    assert len(asked) == 1
    assert [
        v for _, v in db.series([weather.METRIC], now - timedelta(days=2), 3600)[weather.METRIC]
    ] == [4.0, 5.0]


def test_daily_means_need_half_a_day_of_readings():
    start = datetime(2026, 1, 1, tzinfo=LONDON)
    hourly = [(start + timedelta(hours=h), 4.0 if h < 24 else 9.0) for h in range(24 + 6)]
    assert heating.daily_means(hourly, LONDON) == {date(2026, 1, 1): 4.0}
    assert heating.season_of(date(2026, 1, 1)) == "2025/26"
    assert heating.season_of(date(2026, 11, 1)) == "2026/27"
    assert heating.season_of(date(2026, 7, 1)) is None


def test_use_is_set_against_degree_days():
    used, temperature = {}, {}
    day = date(2025, 11, 1)
    for n in range(120):
        t = 12.0 - (n % 14)  # 12 °C down to -1 °C, over and over
        temperature[day + timedelta(days=n)] = t
        used[day + timedelta(days=n)] = 3.0 + 1.5 * (15.5 - t)  # 1.5 kWh more each °C colder
    for n in range(10):  # warm days: hot water only
        temperature[date(2025, 8, 1) + timedelta(days=n)] = 19.0
        used[date(2025, 8, 1) + timedelta(days=n)] = 2.0
    found = heating.analyse(used, temperature)
    assert found["has_data"] and found["days"] == 130
    assert found["per_degree_colder_kwh"] == pytest.approx(1.5)
    assert found["warm_day_kwh"] == 2.0
    assert [s["name"] for s in found["seasons"]] == ["2025/26"]  # August is too warm to count
    november = next(m for m in found["months"] if m["name"] == "2025-11")
    assert november["kwh_per_degree_day"] > 1.5  # hot water is in there too
    assert heating.analyse({}, temperature) == {"has_data": False, "days": 0}


def test_heating_endpoint_reports_what_is_missing_then_the_figures():
    data = client.get("/api/heating").json()
    assert not data["has_data"] and not data["has_heat_pump"] and not data["has_temperature"]
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows, used, heat, imported = [], 1000.0, 500.0, 800.0
    for hours in range(24 * 40, -1, -1):
        when = now - timedelta(hours=hours)
        day_number = (when.astimezone(LONDON).date() - date(2026, 1, 1)).days
        outside = 2.0 + (day_number % 10)  # 2 °C to 11 °C
        rows += [
            (when, "load_energy_total", used),
            (when, "smart_load_energy_total", heat),
            (when, "import_energy_total", imported),
            (when, weather.METRIC, outside),
        ]
        hp = (1.0 + 0.5 * (15.5 - outside)) / 24
        heat += hp
        used += hp + 0.3
        imported += hp + 0.3
    api.database().insert_readings(rows)
    api.clear_caches()  # the empty answer above was kept
    data = client.get("/api/heating").json()
    assert data["has_data"] and data["days"] >= 35
    assert data["per_degree_colder_kwh"] == pytest.approx(0.5, abs=0.05)
    assert data["points"][0]["degree_days"] > 0 and data["months"]
