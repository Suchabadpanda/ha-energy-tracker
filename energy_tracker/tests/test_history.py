from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, history
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
SCHEDULE = Schedule([build_tariff("Test", BANDS, 15.0, 50.0)])
GAP = timedelta(minutes=75)


def steady(start, end, kw):
    out, t = [], start
    while t <= end:
        out.append((t, kw * (t - start).total_seconds() / 3600))
        t += timedelta(minutes=30)
    return out


def test_period_bounds():
    day = date(2026, 10, 7)  # a Wednesday
    assert history.period_bounds("day", day, LONDON)[0].date() == day
    week = history.period_bounds("week", day, LONDON)
    assert (week[0].date(), week[1].date()) == (date(2026, 10, 5), date(2026, 10, 12))
    month = history.period_bounds("month", date(2026, 12, 15), LONDON)
    assert (month[0].date(), month[1].date()) == (date(2026, 12, 1), date(2027, 1, 1))
    year = history.period_bounds("year", day, LONDON)
    assert (year[0].date(), year[1].date()) == (date(2026, 1, 1), date(2027, 1, 1))


def test_a_day_the_clocks_go_back_has_25_hours():
    start, end = history.period_bounds("day", date(2026, 10, 25), LONDON)
    assert len(history.step_starts(start, end, "hour", LONDON)) == 25
    assert len(history.step_starts(start, end, "halfhour", LONDON)) == 50
    start, end = history.period_bounds("month", date(2026, 10, 1), LONDON)
    assert len(history.step_starts(start, end, "day", LONDON)) == 31
    start, end = history.period_bounds("year", date(2026, 10, 1), LONDON)
    assert len(history.step_starts(start, end, "month", LONDON)) == 12


def test_rows_give_energy_and_cost_and_leave_gaps_empty():
    start, end = history.period_bounds("day", date(2026, 6, 2), LONDON)
    readings_from = start + timedelta(hours=3)
    now = start + timedelta(hours=10)
    counters = {
        "import_energy_total": Counter(steady(readings_from, now, 2.0), GAP),
        "export_energy_total": Counter(steady(readings_from, now, 1.0), GAP),
        "pv_energy_total": Counter([], GAP),  # not collected at all
    }
    data = history.rows(counters, start, end, "hour", LONDON, SCHEDULE, now)

    assert len(data) == 24 and "solar_kwh" not in data[0]
    assert data[1]["import_kwh"] is None  # before the first reading
    assert data[4]["import_kwh"] == 2.0 and data[4]["import_cost_gbp"] == pytest.approx(0.20)
    assert data[7]["import_cost_gbp"] == pytest.approx(0.60)  # the day rate from 06:00
    assert data[7]["export_credit_gbp"] == pytest.approx(0.15)
    assert data[12]["import_kwh"] is None  # the future
    assert history.totals(data)["import_kwh"] == pytest.approx(14.0)

    lines = history.to_csv(data, LONDON).split("\r\n")
    assert lines[0] == "start,end,import_kwh,export_kwh,import_cost,export_credit"
    assert lines[2] == "2026-06-02T01:00+01:00,2026-06-02T02:00+01:00,,,,"
    assert lines[5].startswith("2026-06-02T04:00+01:00,2026-06-02T05:00+01:00,2.0,1.0,0.2,")


def test_history_and_export_endpoints():
    client = TestClient(api.app)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for half_hours in range(48 * 5):
        when = now - timedelta(minutes=30 * half_hours)
        rows.append((when, "pv_energy_total", 300000 - half_hours * 0.25))
    api.database().insert_readings(rows)

    for period, interval in history.VIEW_INTERVAL.items():
        data = client.get("/api/history", params={"period": period}).json()
        assert data["interval"] == interval and data["rows"] and data["next"] is None
        # Just after midnight (or on a Monday, or the 1st) the newest period can be empty.
        assert data["totals"].get("solar_kwh", 0) >= 0
    assert client.get("/api/history", params={"period": "year"}).json()["totals"][
        "solar_kwh"
    ] > 0 or (datetime.now(LONDON).timetuple().tm_yday == 1)

    yesterday = (datetime.now(LONDON) - timedelta(days=1)).date().isoformat()
    past = client.get("/api/history", params={"period": "day", "day": yesterday}).json()
    assert past["next"] is not None
    assert past["totals"]["solar_kwh"] == pytest.approx(12.0, abs=0.6)

    response = client.get(
        "/api/export.csv", params={"period": "day", "day": yesterday, "interval": "halfhour"}
    )
    assert response.headers["content-type"].startswith("text/csv")
    assert f"energy-day-{yesterday}-halfhour.csv" in response.headers["content-disposition"]
    assert len(response.text.strip().split("\r\n")) in (
        47,
        49,
        51,
    )  # header + 46, 48 or 50 half hours
    assert client.get("/api/export.csv", params={"interval": "minute"}).status_code == 422
