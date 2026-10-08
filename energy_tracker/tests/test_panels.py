"""Would more panels, or a bigger inverter, pay?"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, panels
from energy_tracker.planner import Battery
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)
GAP = timedelta(minutes=75)
DAY = datetime(2026, 6, 1, tzinfo=LONDON)
RATES = Schedule(
    [build_tariff("Flat", [{"start": "00:00", "end": "24:00", "p_per_kwh": 30.0}], 15.0, 0)]
)


def ramp(start, end, kw_at):
    out, t, total = [], start, 0.0
    while t <= end:
        out.append((t, total))
        total += kw_at(t.astimezone(LONDON)) * 0.5
        t += timedelta(minutes=30)
    return out


def midday_sun(peak):
    return lambda t: peak if 10 <= t.hour < 14 else 0.0


def test_scaled_solar_is_limited_by_the_inverter():
    solar = Counter(ramp(DAY, DAY + timedelta(days=1), midday_sun(4.0)), GAP)
    noon = DAY + timedelta(hours=12)
    assert panels.ScaledSolar(solar, 1.5, None).between(noon, noon + timedelta(minutes=30)) == 3.0
    assert panels.ScaledSolar(solar, 1.5, 5.0).between(noon, noon + timedelta(minutes=30)) == 2.5
    # Four hours at 6 kW against a 5 kW limit: 4 kWh lost.
    assert panels.clipped(solar, DAY, DAY + timedelta(days=1), 1.5, 5.0) == pytest.approx(4.0)
    assert panels.estimate_kwp(solar, DAY, DAY + timedelta(days=1)) == pytest.approx(4.7)


def test_more_panels_save_by_exporting_and_covering_the_house():
    end = DAY + timedelta(days=2)
    counters = {
        "load": Counter(ramp(DAY, end, lambda t: 1.0), GAP),
        "solar": Counter(ramp(DAY, end, midday_sun(4.0)), GAP),
        "ev": Counter([]),
    }
    found = panels.options(counters, DAY, end, LONDON, RATES, Battery(0.01, 0.01, 1), 4.0, 5.0, 8.0)
    first, last = found["options"][0], found["options"][-1]
    assert first["extra_kwp"] == 1 and first["extra_kwh_year"] == pytest.approx(4 * 365, rel=0.01)
    # 1 kWp more: 5 kW at midday, just inside the 5 kW inverter, all exported at 15p.
    assert first["clipped_kwh_year"] == 0
    assert first["saved_gbp_year"] == pytest.approx(4 * 0.15 * 365, abs=2)
    # 4 kWp more: 8 kW, of which 3 kW is lost to the 5 kW inverter; a bigger one keeps it.
    assert last["clipped_kwh_year"] == pytest.approx(12 * 365, rel=0.01)
    assert last["saved_bigger_inverter_gbp_year"] > last["saved_gbp_year"]
    assert found["clipped_now_kwh_year"] == 0 and found["rough"]


def test_panels_endpoint_needs_readings_then_uses_the_settings():
    assert client.get("/api/panels").json() == {"has_data": False, "settings": {}}
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows, used, made = [], 90000.0, 50000.0
    for hours in range(24 * 30, -1, -1):
        when = now - timedelta(hours=hours)
        rows += [(when, "load_energy_total", used), (when, "pv_energy_total", made)]
        used += 1.0
        made += 3.0 if 10 <= when.astimezone(LONDON).hour < 14 else 0.0
    api.database().insert_readings(rows)
    api.clear_caches()  # the empty answer above was kept
    data = client.get("/api/panels").json()
    assert data["has_data"] and data["used_kwp"] == data["estimated_kwp"]
    settings = {"kwp": 4.0, "inverter_kw": 5.0, "cost_per_kwp": 1000}
    saved = client.post("/api/panels/settings", json=settings).json()
    assert saved["used_kwp"] == 4.0 and saved["options"][1]["cost_gbp"] == 2000
    assert saved["options"][1]["payback_years"] > 0
    assert client.post("/api/panels/settings", json={"kwp": -1}).status_code == 422
