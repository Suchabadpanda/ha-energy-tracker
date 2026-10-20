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
