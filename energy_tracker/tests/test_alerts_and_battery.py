"""Alerts sent through Home Assistant, and battery health by month."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from energy_tracker import alerts, api, battery
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
RATES = Schedule([build_tariff("Night", BANDS, 15.0, 50.0)])
DAY = datetime(2026, 6, 1, tzinfo=LONDON)
ON = {**alerts.DEFAULTS, "enabled": True}


class HomeAssistant:
    """Stands in for Home Assistant: lists its notify services and records what is sent."""

    def __init__(
        self, services=("mobile_app_phone", "mobile_app_tablet", "persistent_notification")
    ):
        self.services = {name: {} for name in services}
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/services":
            return httpx.Response(200, json=[{"domain": "notify", "services": self.services}])
        name = request.url.path.removeprefix("/api/services/")
        if name in self.fail:
            return httpx.Response(500)
        self.calls.append((name, json.loads(request.content)))
        return httpx.Response(200, json=[])

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://ha", transport=httpx.MockTransport(self))


def store(rows):
    api.database().insert_readings(rows)


def steady(name, start, end, per_hour, begin=1000.0, step=timedelta(minutes=5)):
    rows, when, value = [], start, begin
    while when <= end:
        rows.append((when, name, value))
        value += per_hour * step.total_seconds() / 3600
        when += step
    return rows


def failing(now, **options):
    return alerts.evaluate(api.database(), RATES, now, LONDON, {**ON, **options})


def test_nothing_is_wrong_on_an_ordinary_day():
    now = DAY + timedelta(hours=13)
    store(steady("pv_power", DAY, now, 0, 2.0))
    store(steady("import_energy_total", DAY, DAY + timedelta(hours=6), 3.0))  # cheap hours only
    assert failing(now) == {}


def test_silence_from_the_sensors_is_noticed_after_an_hour():
    store(steady("pv_power", DAY, DAY + timedelta(hours=8), 0, 2.0))
    assert "no_readings" not in failing(DAY + timedelta(hours=8, minutes=50))
    found = failing(DAY + timedelta(hours=9, minutes=30))
    assert found["no_readings"][0] == "on" and "08:00 on 01 Jun" in found["no_readings"][1]
    assert "no_readings" not in failing(DAY + timedelta(hours=9, minutes=30), no_readings=False)


def test_a_battery_left_empty_after_the_cheap_period_is_reported_once_the_period_ends():
    end = DAY + timedelta(hours=6)
    store(steady("battery_charge_energy_total", DAY - timedelta(hours=1), end, 0.0))
    store(steady("battery_soc", DAY - timedelta(hours=1), end, 0, 12.0))
    assert "battery_not_charged" not in failing(DAY + timedelta(hours=5))  # still in the period
    found = failing(DAY + timedelta(hours=6, minutes=20))["battery_not_charged"]
    assert found[0] == "2026-06-01 06:00" and "0.0 kWh" in found[1] and "12%" in found[1]
    assert "battery_not_charged" not in failing(DAY + timedelta(hours=10))  # too late to matter
    # A battery already well charged needed nothing.
    relaxed = failing(DAY + timedelta(hours=6, minutes=20), battery_expected_percent=10)
    assert "battery_not_charged" not in relaxed


def test_a_battery_that_charged_raises_nothing():
    end = DAY + timedelta(hours=6)
    store(steady("battery_charge_energy_total", DAY - timedelta(hours=1), end, 2.0))
    store(steady("battery_soc", DAY - timedelta(hours=1), end, 0, 20.0))
    assert "battery_not_charged" not in failing(DAY + timedelta(hours=6, minutes=20))


def test_import_outside_the_cheap_rate_is_reported_past_the_limit():
    now = DAY + timedelta(hours=10)
    store(steady("import_energy_total", DAY, now, 2.0))  # 12 kWh cheap, then 8 kWh dear
    found = failing(now)["dear_import"]
    assert found[0] == "2026-06-01" and found[1].startswith("8.0 kWh")
    assert "dear_import" not in failing(now, dear_import_kwh=9)
    flat = Schedule([build_tariff("Flat", [{**BANDS[0], "end": "24:00"}], 0, 0)])
    assert alerts.evaluate(api.database(), flat, now, LONDON, ON) == {}  # no cheap rate to miss


def test_an_alert_goes_to_every_phone_and_into_home_assistant_once():
    db, ha = api.database(), HomeAssistant()
    db.set_setting("alerts", json.dumps({"enabled": True}))
    now = DAY + timedelta(hours=10)
    store(steady("import_energy_total", DAY, now, 2.0))
    with ha.client() as http:
        assert alerts.run(db, http, RATES, now, LONDON) == ["dear_import"]
        assert [name for name, _ in ha.calls] == [
            "notify/mobile_app_phone",
            "notify/mobile_app_tablet",
            "persistent_notification/create",
        ]
        assert ha.calls[0][1]["title"] == "Buying a lot at the dearer rate today"
        assert ha.calls[2][1]["notification_id"] == "energy_tracker_dear_import"
        # Still failing five minutes later: nothing more is sent.
        assert alerts.run(db, http, RATES, now + timedelta(minutes=5), LONDON) == []
        assert len(ha.calls) == 3
    logged = alerts.history(db)
    assert len(logged) == 1 and logged[0]["check"] == "dear_import" and logged[0]["problems"] == []


def test_only_the_chosen_services_are_used_and_failures_are_recorded():
    db, ha = api.database(), HomeAssistant()
    db.set_setting("alerts", json.dumps({"enabled": True, "services": ["mobile_app_tablet"]}))
    ha.fail.add("notify/mobile_app_tablet")
    now = DAY + timedelta(hours=10)
    store(steady("import_energy_total", DAY, now, 2.0))
    with ha.client() as http:
        alerts.run(db, http, RATES, now, LONDON)
    assert [name for name, _ in ha.calls] == ["persistent_notification/create"]
    assert "mobile_app_tablet" in alerts.history(db)[0]["problems"][0]


def test_nothing_is_sent_while_alerts_are_switched_off():
    ha = HomeAssistant()
    now = DAY + timedelta(hours=10)
    store(steady("import_energy_total", DAY, now, 2.0))
    with ha.client() as http:
        assert alerts.run(api.database(), http, RATES, now, LONDON) == []
    assert ha.calls == []


def test_silence_is_announced_again_after_readings_come_back():
    db, ha = api.database(), HomeAssistant()
    db.set_setting("alerts", json.dumps({"enabled": True}))
    store(steady("pv_power", DAY, DAY + timedelta(hours=8), 0, 2.0))
    with ha.client() as http:
        assert alerts.run(db, http, RATES, DAY + timedelta(hours=10), LONDON) == ["no_readings"]
        store(steady("pv_power", DAY + timedelta(hours=11), DAY + timedelta(hours=12), 0, 2.0))
        assert alerts.run(db, http, RATES, DAY + timedelta(hours=12), LONDON) == []
        assert alerts.run(db, http, RATES, DAY + timedelta(hours=14), LONDON) == ["no_readings"]


def test_alert_settings_can_be_saved_and_are_checked():
    data = client.get("/api/alerts").json()
    assert data["settings"]["enabled"] is False and data["services"] == []
    assert "No Home Assistant connection" in data["problem"]
    body = {"enabled": True, "services": ["mobile_app_phone"], "dear_import_kwh": 8}
    saved = client.post("/api/alerts/settings", json=body).json()["settings"]
    assert saved["enabled"] and saved["services"] == ["mobile_app_phone"]
    assert saved["dear_import_kwh"] == 8 and saved["no_readings"] is True
    assert "no_solar" not in saved and "battery_fading" not in saved  # removed in 0.20.1
    bad = {"enabled": True, "services": ["notify/../../x"]}
    assert client.post("/api/alerts/settings", json=bad).status_code == 422
    assert client.post("/api/alerts/test").status_code == 409  # nothing to send through here


# --- Battery health --------------------------------------------------------------------------


def months_of_charging(capacities):
    """A battery filled from 10% to 100% every night, its capacity changing month by month."""
    soc, charged, given = [], [], []
    put_in = taken = 0.0
    when = datetime(2026, 1, 1, tzinfo=UTC)
    for capacity in capacities:
        month = when.month
        while when.month == month:
            hour = when.hour + when.minute / 60
            filling, emptying = hour < 4, 8 <= hour < 20
            level = 10 + 90 * min(hour, 4) / 4 if hour < 8 else 100 - 90 * min(hour - 8, 12) / 12
            soc.append((when, level))
            charged.append((when, put_in))
            given.append((when, taken))
            if filling:
                put_in += capacity * 0.9 / 8 / 0.9**0.5
            if emptying:
                taken += capacity * 0.9 / 24 * 0.9**0.5
            when += timedelta(minutes=30)
    gap = timedelta(minutes=75)
    return soc, Counter(charged, gap), Counter(given, gap)


def test_capacity_and_efficiency_are_worked_out_for_each_month():
    months = battery.by_month(*months_of_charging([10.0, 10.0]), LONDON)
    assert [m["month"] for m in months] == ["2026-01", "2026-02"]
    assert months[0]["capacity_kwh"] == pytest.approx(10.0, abs=0.2)
    assert months[0]["efficiency_percent"] == pytest.approx(90.0, abs=1.5)
    assert months[0]["charged_kwh"] > 250


def test_the_trend_compares_the_latest_months_with_the_first():
    fading = [10.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 8.8]
    trend = battery.summary(battery.by_month(*months_of_charging(fading), LONDON))
    assert trend["first_kwh"] == pytest.approx(10.0, abs=0.2)
    assert trend["latest_kwh"] == pytest.approx(9.0, abs=0.2)
    assert trend["change_percent"] == pytest.approx(-10, abs=1.5)
    assert trend["cycles"] > 200 and trend["measured_months"] == 9

    young = battery.summary(battery.by_month(*months_of_charging([10.0, 10.0]), LONDON))
    assert young["has_data"] and young["change_percent"] is None  # too soon to call a trend
    assert battery.summary([]) == {"has_data": False, "months": []}


def test_battery_endpoint():
    assert client.get("/api/battery").json() == {"has_data": False, "months": []}
