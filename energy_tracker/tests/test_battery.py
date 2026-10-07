"""Battery health by month."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, battery
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)


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
