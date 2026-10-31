"""Extra income under Costs by year."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api

client = TestClient(api.app)


def test_costs_by_year_take_off_extra_income():
    now = datetime.now(UTC).replace(microsecond=0)
    rows = []
    for minutes in range(0, 180, 5):
        when = now - timedelta(minutes=minutes)
        rows.append((when, "import_energy_total", 1000 - minutes / 60))
        rows.append((when, "export_energy_total", 500.0))
    api.database().insert_readings(rows)
    today = now.astimezone(api.local_timezone()).date()

    plain = client.get("/api/years").json()
    assert not plain["has_income"]
    year = plain["years"][-1]
    assert year["extra_income_gbp"] == 0 and year["total_gbp"] == year["cost"]["net_gbp"]
    assert "gas_gbp" not in year and "has_gas" not in plain

    client.post(
        "/api/income", json={"day": today.isoformat(), "description": "Axle", "amount_gbp": 10}
    )
    # Income before the electricity was counted is left out, as those days are.
    client.post("/api/income", json={"day": "2000-01-01", "description": "Old", "amount_gbp": 99})
    data = client.get("/api/years").json()
    assert data["has_income"]
    year = data["years"][-1]
    month = next(m for m in year["months"] if m["key"] == f"{today:%Y-%m}")
    assert year["extra_income_gbp"] == 10
    assert year["total_gbp"] == pytest.approx(year["cost"]["net_gbp"] - 10)
    assert month["total_gbp"] == pytest.approx(month["cost"]["net_gbp"] - 10)


def test_gas_is_gone():
    assert client.get("/api/gas").status_code == 404
    with api.database()._connect() as conn:  # noqa: SLF001 - checking the table was dropped
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "gas_bills" not in tables


def test_today_and_month_show_extra_income():
    now = datetime.now(UTC).replace(microsecond=0)
    zone = api.local_timezone()
    midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0)
    # Readings from just after midnight, so today has a cost whatever the time.
    rows = []
    when = midnight + timedelta(minutes=1)
    while when <= now:
        rows.append((when, "import_energy_total", 1000 + (when - midnight).seconds / 3600))
        rows.append((when, "export_energy_total", 500.0))
        when += timedelta(minutes=5)
    rows.append((now, "import_energy_total", 1000 + (now - midnight).seconds / 3600 + 0.01))
    rows.append((now, "export_energy_total", 500.0))
    api.database().insert_readings(rows)
    today = midnight.date()

    assert client.get("/api/today").json()["income"] is None
    assert client.get("/api/month").json()["income"] is None

    client.post(
        "/api/income", json={"day": today.isoformat(), "description": "Axle", "amount_gbp": 4.5}
    )
    # A recorded event that has not been measured yet.
    start = now + timedelta(hours=1)
    api.database().add_event(start, start + timedelta(hours=1), today)
    # Income on another month is not this month's.
    client.post("/api/income", json={"day": "2000-01-01", "description": "Old", "amount_gbp": 99})

    for body in (client.get("/api/today").json(), client.get("/api/month").json()):
        income = body["income"]
        assert income["extra_income_gbp"] == 4.5
        assert income["entries"] == 2 and income["pending"] == 1
        assert income["net_after_income_gbp"] == pytest.approx(body["cost"]["net_gbp"] - 4.5)
