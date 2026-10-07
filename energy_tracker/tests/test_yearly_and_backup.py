"""The yearly report, and backing up and restoring what the user has entered."""

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from energy_tracker import api

client = TestClient(api.app)
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
RATES = {
    "name": "Night",
    "effective_from": "2026-01-01",
    "export_p_per_kwh": 15,
    "standing_charge_p_per_day": 50,
    "vat_percent": 5,
    "import_bands": BANDS,
    "fixed_until": "2027-05-29",
}


def a_house_with_readings(days=40):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for hours in range(24 * days):
        when = now - timedelta(hours=hours)
        rows.append((when, "load_energy_total", 90000 - hours * 2.0))
        rows.append((when, "import_energy_total", 70000 - hours * 0.5))
        rows.append((when, "export_energy_total", 60000 - hours * 0.1))
        rows.append((when, "pv_energy_total", 50000 - hours * 1.0))
        rows.append((when, "smart_load_energy_total", 30000 - hours * 0.5))
    api.database().insert_readings(rows)
    return now


def test_yearly_report_gives_the_year_its_months_and_device_costs():
    now = a_house_with_readings()
    year = now.astimezone(api.local_timezone()).year
    data = client.get("/api/yearly").json()
    assert data["year"] == year and data["is_current"] and data["next"] is None
    this = data["this"]
    assert this["cost"]["net_gbp"] > 0 and this["solar_kwh"] > 0 and not this["complete"]
    assert sum(m["days"] for m in this["months"]) == this["days"]
    assert round(sum(m["net_gbp"] for m in this["months"]), 1) == round(this["cost"]["net_gbp"], 1)
    assert data["devices"]["heat_pump_kwh"] > 0 and data["smart_load_label"] == "Heat pump"
    assert client.get(f"/api/yearly?year={year + 1}").status_code == 404
    assert client.get("/api/yearly?year=1990").status_code == 422


def test_yearly_report_without_readings_is_empty_not_an_error():
    data = client.get("/api/yearly").json()
    assert data["this"] is None and data["previous"] is None and data["devices"] is None


def everything_entered():
    client.post("/api/tariffs", json=RATES)
    client.post("/api/compare/tariffs", json={**RATES, "name": "Other"})
    client.post(
        "/api/bills", json={"first_day": "2026-03-01", "last_day": "2026-03-31", "import_kwh": 300}
    )
    client.post(
        "/api/income", json={"day": "2026-04-02", "description": "Grid event", "amount_gbp": 12.5}
    )
    roi = {"install_date": "2025-10-29", "costs": [{"date": "2025-10-29", "amount": 12000}]}
    client.post("/api/roi/settings", json=roi)
    client.post("/api/layout", json={"folded": ["bills-heading"], "bill_years": [2025]})
    client.post("/api/alerts/settings", json={"enabled": True, "dear_import_kwh": 8})


def test_a_backup_holds_what_was_entered_and_restores_onto_a_changed_install():
    everything_entered()
    saved = client.get("/api/backup")
    assert "attachment" in saved.headers["content-disposition"]
    content = saved.json()
    assert content["app"] == "energy-tracker" and content["version"] == api.app.version
    assert len(content["tables"]["bills"]) == 1 and len(content["tables"]["extra_income"]) == 1
    assert set(content["settings"]) == {"payback", "layout", "alerts"}
    assert "readings" not in content["tables"]

    # Change everything, then put the backup back.
    client.post("/api/tariffs", json={**RATES, "name": "Changed", "effective_from": "2026-06-01"})
    client.post(
        "/api/bills", json={"first_day": "2026-05-01", "last_day": "2026-05-31", "import_kwh": 1}
    )
    client.post("/api/planner/settings", json={"battery_kwh": 30})
    client.delete("/api/income/" + str(client.get("/api/income").json()["entries"][0]["id"]))

    checked = client.post("/api/restore", content=saved.content).json()
    assert checked["restored"] is False and checked["found"]["bills"] == 1
    assert len(client.get("/api/bills").json()["bills"]) == 2  # only checked: nothing changed

    done = client.post("/api/restore?dry_run=false", content=saved.content).json()
    assert done["restored"] and done["found"]["tariff_periods"] == len(
        content["tables"]["tariff_periods"]
    )
    names = [p["name"] for p in client.get("/api/tariffs").json()["periods"]]
    assert "Changed" not in names and "Night" in names
    kept = next(p for p in client.get("/api/tariffs").json()["periods"] if p["name"] == "Night")
    assert kept["fixed_until"] == "2027-05-29" and kept["import_bands"] == BANDS
    assert [b["import_kwh"] for b in client.get("/api/bills").json()["bills"]] == [300]
    assert client.get("/api/income").json()["total_gbp"] == 12.5
    assert client.get("/api/roi").json()["settings"]["install_date"] == "2025-10-29"
    assert client.get("/api/layout").json()["folded"] == ["bills-heading"]
    assert client.get("/api/alerts").json()["settings"]["dear_import_kwh"] == 8
    # A setting the backup did not have goes back to unset.
    assert client.get("/api/planner").json()["settings"] == {}


def test_a_file_that_is_not_a_backup_changes_nothing():
    everything_entered()
    good = client.get("/api/backup").json()
    for body in (
        b"not json at all",
        json.dumps({"app": "something-else"}).encode(),
        json.dumps({**good, "format": 99}).encode(),
        json.dumps({**good, "tables": {**good["tables"], "tariff_periods": []}}).encode(),
        json.dumps(
            {**good, "tables": {**good["tables"], "bills": [{"first_day": "nonsense"}]}}
        ).encode(),
        json.dumps(
            {
                **good,
                "tables": {
                    **good["tables"],
                    "tariff_periods": [
                        {**good["tables"]["tariff_periods"][0], "import_bands": "[]"}
                    ],
                },
            }
        ).encode(),
    ):
        refused = client.post("/api/restore?dry_run=false", content=body)
        assert refused.status_code == 422, body[:60]
    assert len(client.get("/api/bills").json()["bills"]) == 1
    assert client.get("/api/income").json()["total_gbp"] == 12.5
