"""Averages per day by month and year."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, averages

client = TestClient(api.app)


def test_build_averages_months_years_and_the_last_30_days():
    today = date(2026, 10, 15)
    days = {}
    for n in range(1, 101):
        day = today - timedelta(days=n)
        days[day] = {"used": 20.0 if day.weekday() < 5 else 30.0, "imported": 5.0, "solar": 8.0}
    days[today - timedelta(days=3)]["used"] = 50.0
    found = averages.build(days, today)
    assert found["has_data"]
    september = next(m for m in found["months"] if m["name"] == "2026-09")
    assert september["days"] == 30 and september["complete"]
    assert found["months"][-1]["name"] == "2026-10" and not found["months"][-1]["complete"]
    assert found["weekday"]["used"] == pytest.approx(20.5, abs=0.1)  # one busy weekday
    assert found["weekend"]["used"] == 30.0
    assert found["busiest_day"]["used"] == 50.0
    assert found["last_30"]["days"] == 30 and found["previous_30"]["days"] == 30
    assert found["last_30"]["self_sufficiency_percent"] == pytest.approx(78, abs=2)
    assert found["year_ago_30"] is None
    assert averages.build({}, today) == {"has_data": False}


def test_averages_endpoint():
    assert client.get("/api/averages").json() == {"has_data": False}
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows, used, imported, solar = [], 100.0, 50.0, 10.0
    for hours in range(24 * 40, -1, -1):
        when = now - timedelta(hours=hours)
        rows += [
            (when, "load_energy_total", used),
            (when, "import_energy_total", imported),
            (when, "pv_energy_total", solar),
        ]
        used, imported, solar = used + 1.0, imported + 0.5, solar + 0.5
    api.database().insert_readings(rows)
    api.clear_caches()
    data = client.get("/api/averages").json()
    assert data["has_data"] and data["last_30"]["used"] == pytest.approx(24, abs=0.5)
    assert data["last_30"]["self_sufficiency_percent"] == pytest.approx(50, abs=2)
