"""Gas bills typed in by hand, and extra income and gas under Costs by year."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, gas

client = TestClient(api.app)


def bill(first, last, cost, kwh=None, row_id=1):
    return {"id": row_id, "first_day": first, "last_day": last, "cost_gbp": cost, "kwh": kwh}


def test_a_bill_is_spread_evenly_over_its_days():
    daily = gas.by_day([bill("2026-03-17", "2026-04-15", 60.0, 300.0)])  # 30 days
    march = gas.between(daily, date(2026, 3, 1), date(2026, 3, 31))
    assert march == {"gbp": 30.0, "kwh": 150.0, "days": 15, "of_days": 31}
    summary = gas.summary([bill("2026-03-17", "2026-04-15", 60.0, 300.0)])
    assert summary["bills"][0]["per_day_gbp"] == 2.0 and summary["bills"][0]["p_per_kwh"] == 20.0
    assert summary["years"] == [
        {"year": 2026, "gbp": 60.0, "kwh": 300.0, "days": 30, "of_days": 365}
    ]
    # Without the kWh, use is left blank rather than shown as nothing.
    assert (
        gas.between(
            gas.by_day([bill("2026-03-01", "2026-03-10", 10.0)]),
            date(2026, 3, 1),
            date(2026, 3, 31),
        )["kwh"]
        is None
    )


def test_gas_bills_are_saved_edited_checked_for_overlaps_and_removed():
    body = {"first_day": "2026-01-01", "last_day": "2026-01-31", "cost_gbp": 93.0, "kwh": 900}
    data = client.post("/api/gas", json=body).json()
    assert data["enabled"] and data["bills"][0]["days"] == 31
    row_id = data["bills"][0]["id"]
    clash = client.post(
        "/api/gas", json={**body, "first_day": "2026-01-31", "last_day": "2026-02-27"}
    )
    assert clash.status_code == 422 and "overlap" in clash.json()["detail"]
    backwards = client.post(
        "/api/gas", json={**body, "first_day": "2026-02-10", "last_day": "2026-02-01"}
    )
    assert backwards.status_code == 422
    # Editing a bill may keep its own dates.
    edited = client.post(f"/api/gas?id={row_id}", json={**body, "cost_gbp": 95.0}).json()
    assert len(edited["bills"]) == 1 and edited["bills"][0]["cost_gbp"] == 95.0
    assert client.delete(f"/api/gas/{row_id}").json()["bills"] == []
    assert client.delete(f"/api/gas/{row_id}").status_code == 404


def readings_for_this_month():
    now = datetime.now(UTC).replace(microsecond=0)
    rows = []
    for minutes in range(0, 180, 5):
        when = now - timedelta(minutes=minutes)
        rows.append((when, "import_energy_total", 1000 - minutes / 60))
        rows.append((when, "export_energy_total", 500.0))
    api.database().insert_readings(rows)
    return now.astimezone(api.local_timezone()).date()


def test_costs_by_year_take_off_extra_income_and_add_gas():
    today = readings_for_this_month()
    plain = client.get("/api/years").json()
    assert not plain["has_income"] and not plain["has_gas"]
    year = plain["years"][-1]
    assert year["extra_income_gbp"] == 0 and year["gas_gbp"] is None
    assert year["total_gbp"] == year["cost"]["net_gbp"]

    client.post(
        "/api/income", json={"day": today.isoformat(), "description": "Axle", "amount_gbp": 10}
    )
    first = today.replace(day=1)
    client.post(
        "/api/gas",
        json={"first_day": first.isoformat(), "last_day": today.isoformat(), "cost_gbp": 31.0},
    )
    data = client.get("/api/years").json()
    assert data["has_income"] and data["has_gas"]
    year = data["years"][-1]
    month = next(m for m in year["months"] if m["key"] == f"{today:%Y-%m}")
    # Gas over the same days as the electricity: here only today, one of the bill's days.
    share = round(31.0 / ((today - first).days + 1), 2)
    assert year["extra_income_gbp"] == 10 and year["gas_gbp"] == pytest.approx(share)
    assert month["gas_days"] == 1 and month["days"] == 1
    assert year["total_gbp"] == pytest.approx(year["cost"]["net_gbp"] - 10 + share)
    assert month["total_gbp"] == pytest.approx(month["cost"]["net_gbp"] - 10 + share)

    # A home with no gas: left out of the totals, but the bills are kept.
    off = client.post("/api/gas/settings", json={"enabled": False}).json()
    assert not off["enabled"] and len(off["bills"]) == 1
    year = client.get("/api/years").json()["years"][-1]
    assert year["gas_gbp"] is None
    assert year["total_gbp"] == pytest.approx(year["cost"]["net_gbp"] - 10)
    client.post("/api/gas/settings", json={"enabled": True})


def test_gas_is_backed_up_and_an_older_backup_leaves_it_alone():
    client.post(
        "/api/gas", json={"first_day": "2026-01-01", "last_day": "2026-01-31", "cost_gbp": 90}
    )
    client.post("/api/gas/settings", json={"enabled": False})
    saved = client.get("/api/backup").json()
    assert len(saved["tables"]["gas_bills"]) == 1 and saved["settings"]["gas"] == "off"

    restored = client.post("/api/restore?dry_run=false", content=json.dumps(saved)).json()
    assert restored["found"]["gas_bills"] == 1
    assert len(client.get("/api/gas").json()["bills"]) == 1

    # A file from before gas existed: the gas bills here stay.
    older = {**saved, "tables": {k: v for k, v in saved["tables"].items() if k != "gas_bills"}}
    client.post("/api/restore?dry_run=false", content=json.dumps(older))
    assert len(client.get("/api/gas").json()["bills"]) == 1
