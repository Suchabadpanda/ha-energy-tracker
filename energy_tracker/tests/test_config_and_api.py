from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api
from energy_tracker.config import load_metrics, load_settings

client = TestClient(api.app)


def test_shipped_metrics_file_is_valid():
    metrics = load_metrics(Path("config/metrics.toml"))
    names = {m.name for m in metrics}
    assert {"pv_power", "load_power", "grid_power", "battery_power", "battery_soc"} <= names
    assert all(m.entity.startswith("sensor.") for m in metrics)


def test_duplicate_metric_names_are_rejected(tmp_path):
    bad = tmp_path / "metrics.toml"
    block = '[[metric]]\nname="a"\nentity="sensor.a"\nkind="power"\nlabel="A"\n'
    bad.write_text(block * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        load_metrics(bad)


def test_settings_as_a_home_assistant_addon(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abc")
    settings = load_settings()
    assert settings.ha_url == "http://supervisor/core"
    assert settings.ha_websocket_url == "ws://supervisor/core/websocket"
    assert settings.database_path == Path("/data/energy.db") or settings.database_path.name
    assert settings.allowed_client == "172.30.32.2"
    assert settings.port == 8099
    assert settings.collecting


def test_settings_standalone(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://192.168.1.50:8123/")
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setenv("POLL_SECONDS", "1")
    settings = load_settings()
    assert settings.ha_websocket_url == "ws://192.168.1.50:8123/api/websocket"
    assert settings.poll_seconds == 10  # never faster than every ten seconds
    assert settings.allowed_client is None


def test_health_and_dashboard_are_served():
    assert client.get("/healthz").json() == {"status": "ok", "collecting": False}
    page = client.get("/")
    assert page.status_code == 200
    assert "Energy Tracker" in page.text


def test_dashboard_uses_relative_addresses_so_it_works_inside_home_assistant():
    page = client.get("/").text
    assert '"/api/' not in page and "'/api/" not in page


def test_metrics_endpoint_reports_units():
    by_name = {m["name"]: m for m in client.get("/api/metrics").json()}
    assert by_name["pv_power"]["unit"] == "kW"
    assert by_name["battery_soc"]["unit"] == "%"


def test_series_rejects_unknown_metric():
    response = client.get("/api/series", params={"metric": "not_a_metric"})
    assert response.status_code == 400


def test_readings_flow_through_to_latest_series_and_costs():
    now = datetime.now(UTC).replace(microsecond=0)
    rows = []
    for minutes in range(0, 180, 5):
        when = now - timedelta(minutes=minutes)
        rows.append((when, "pv_power", 2.0))
        rows.append((when, "import_energy_total", 1000 - minutes / 60))  # 1 kW of import
        rows.append((when, "export_energy_total", 500.0))
    api.database().insert_readings(rows)

    assert client.get("/api/latest").json()["pv_power"]["value"] == 2.0
    points = client.get("/api/series", params={"metric": "pv_power", "hours": 6}).json()
    assert points["pv_power"] and all(value == 2.0 for _, value in points["pv_power"])

    month = client.get("/api/month").json()
    assert month["is_current"] and month["cost"]["import_kwh"] > 0
    assert client.get("/api/years").json()["years"][-1]["year"] == now.year


def test_tariff_periods_can_be_added_and_removed():
    assert len(client.get("/api/tariffs").json()["periods"]) == 1
    body = {
        "name": "Winter",
        "effective_from": "2026-11-01",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 60,
        "vat_percent": 5,
        "import_bands": [
            {"start": "00:00", "end": "06:00", "p_per_kwh": 8},
            {"start": "06:00", "end": "24:00", "p_per_kwh": 27},
        ],
    }
    assert len(client.post("/api/tariffs", json=body).json()["periods"]) == 2
    assert len(client.delete("/api/tariffs/2026-11-01").json()["periods"]) == 1
    assert client.delete("/api/tariffs/2026-11-01").status_code == 409  # last one stays


def test_import_needs_a_home_assistant_connection():
    assert client.post("/api/import-history").status_code == 409


def test_addon_only_accepts_home_assistant(monkeypatch):
    locked = api.settings().__class__(
        **{**api.settings().__dict__, "allowed_client": "172.30.32.2"}
    )
    monkeypatch.setattr(api, "settings", lambda: locked)
    assert client.get("/healthz").status_code == 403


def test_setup_reports_which_equipment_was_found(monkeypatch):
    from energy_tracker import collector

    monkeypatch.setattr(collector, "polled", True)
    monkeypatch.setattr(collector, "missing", {"ev_charger_power", "smart_load_power"})
    found = client.get("/api/setup").json()
    assert found["sigenergy_found"] is True
    assert found["ev_charger"] is False and found["smart_load"] is False
    assert found["smart_load_label"] == "Heat pump"

    monkeypatch.setattr(collector, "missing", set(api.metrics_by_name()))
    assert client.get("/api/setup").json()["sigenergy_found"] is False


def house_with_payback_set_up():
    """Twenty days of a house using 2 kW and importing 0.5 kW, with payback configured."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for hours in range(24 * 20):
        when = now - timedelta(hours=hours)
        rows.append((when, "load_energy_total", 90000 - hours * 2.0))
        rows.append((when, "import_energy_total", 70000 - hours * 0.5))
        rows.append((when, "export_energy_total", 60000.0))
    api.database().insert_readings(rows)
    body = {
        "install_date": (now - timedelta(days=10)).date().isoformat(),
        "baseline": "own",
        "costs": [{"date": "2026-01-01", "description": "System", "amount": 9000}],
    }
    return body, client.post("/api/roi/settings", json=body).json()


def test_payback_needs_setting_up_then_reports_savings():
    assert client.get("/api/roi").json()["configured"] is False
    body, data = house_with_payback_set_up()
    figures = data["payback"]
    assert data["configured"] and data["total_cost_gbp"] == 9000
    assert figures["has_data"] and figures["rough"] and figures["days"] >= 9
    assert figures["saved_gbp"] > 0 and figures["break_even"] is not None
    assert client.get("/api/roi").json()["payback"]["saved_gbp"] == figures["saved_gbp"]
    assert (
        client.post(
            "/api/roi/settings", json={**body, "costs": [{"date": "x", "amount": 1}]}
        ).status_code
        == 422
    )


def test_extra_income_is_listed_totalled_and_counts_towards_payback():
    house_with_payback_set_up()
    yesterday = (datetime.now(api.local_timezone()) - timedelta(days=1)).date().isoformat()
    before = client.get("/api/roi").json()["payback"]["saved_gbp"]
    body = {"day": yesterday, "description": "Axle export event", "amount_gbp": 12.5}
    data = client.post("/api/income", json=body).json()
    assert data["total_gbp"] == 12.5 and data["by_year"] == {yesterday[:4]: 12.5}
    assert data["axle"] == {"entity": "sensor.axle_event", "status": "missing", "rate_p": 100.0}
    roi_now = client.get("/api/roi").json()
    assert roi_now["payback"]["saved_gbp"] == pytest.approx(before + 12.5)
    assert roi_now["extra_income_gbp"] == 12.5

    entry = data["entries"][0]["id"]
    assert (
        client.post(f"/api/income?id={entry}", json={**body, "amount_gbp": 10}).json()["total_gbp"]
        == 10
    )
    assert (
        client.post("/api/income/axle-rate", json={"p_per_kwh": 80}).json()["axle"]["rate_p"] == 80
    )
    assert client.delete(f"/api/income/{entry}").json()["entries"] == []
    assert client.delete(f"/api/income/{entry}").status_code == 404
    assert client.post("/api/income", json={**body, "amount_gbp": -1}).status_code == 422


def test_currency_comes_from_settings(monkeypatch):
    assert client.get("/api/setup").json()["currency"] == {"symbol": "£", "minor": "p"}
    monkeypatch.setenv("CURRENCY_SYMBOL", "€")
    monkeypatch.setenv("CURRENCY_MINOR", "c")
    settings = load_settings()
    assert (settings.currency_symbol, settings.currency_minor) == ("€", "c")


def test_bill_is_set_beside_the_trackers_own_figures():
    house_with_payback_set_up()  # 0.5 kW imported round the clock, nothing exported
    today = datetime.now(api.local_timezone()).date()
    first, last = today - timedelta(days=8), today - timedelta(days=2)  # seven whole days
    body = {
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "import_kwh": 90,
        "charge_gbp": 20,
    }
    data = client.post("/api/bills", json=body).json()["bills"][0]

    assert data["tracker"]["import_kwh"] == pytest.approx(84.0, abs=0.6)  # 7 days x 12 kWh
    assert data["tracker"]["covers_whole_bill"] is True
    assert data["differences"]["import_kwh"]["amount"] == pytest.approx(6.0, abs=0.6)
    assert data["differences"]["import_kwh"]["percent"] == pytest.approx(7.1, abs=0.8)
    assert data["differences"]["charge_gbp"]["amount"] == pytest.approx(
        20 - data["tracker"]["charge_gbp"]
    )
    assert data["differences"]["export_kwh"] is None  # not entered, so not compared

    edited = client.post(f"/api/bills?id={data['id']}", json={**body, "import_kwh": 84}).json()[
        "bills"
    ]
    assert len(edited) == 1 and abs(edited[0]["differences"]["import_kwh"]["amount"]) < 0.6
    assert client.post("/api/bills", json={**body, "last_day": "2020-01-01"}).status_code == 422
    assert (
        client.post(
            "/api/bills", json={"first_day": body["first_day"], "last_day": body["last_day"]}
        ).status_code
        == 422
    )
    assert client.delete(f"/api/bills/{data['id']}").json() == {"bills": []}
    assert client.delete(f"/api/bills/{data['id']}").status_code == 404


def test_monthly_summary_compares_with_other_months():
    assert client.get("/api/summary").json()["this"] is None
    zone = api.local_timezone()
    now = datetime.now(zone).replace(minute=0, second=0, microsecond=0)
    rows = []
    for hours in range(24 * 100):
        when = now - timedelta(hours=hours)
        rows += [
            (when, "load_energy_total", 900000 - hours * 2.0),
            (when, "import_energy_total", 700000 - hours * 0.5),
            (when, "export_energy_total", 60000 - hours * 0.25),
            (when, "pv_energy_total", 500000 - hours * 1.0),
        ]
    api.database().insert_readings(rows)
    api.clear_caches()

    data = client.get("/api/summary").json()
    this, before = data["this"], data["month_before"]
    assert this["complete"] and before and data["year_before"] is None
    days = this["days"]
    assert 28 <= days <= 31 and this["cost"]["import_kwh"] == pytest.approx(days * 12, abs=1)
    assert this["solar_kwh"] == pytest.approx(days * 24, abs=1)
    assert this["self_sufficiency_percent"] == 75.0
    assert this["dearest_day"]["value"] >= this["cheapest_day"]["value"]
    assert this["busiest_day"]["value"] == pytest.approx(48.0, abs=2.1)  # a 25-hour day uses more
    assert data["next"] is not None and data["previous"] is not None

    current = client.get("/api/summary", params={"month": data["next"]}).json()
    assert current["next"] is None
    assert current["this"] is None or current["this"]["complete"] is False
    assert client.get("/api/summary", params={"month": "2099-01"}).status_code == 404
