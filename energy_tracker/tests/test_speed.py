"""The things that keep the app quick give the same answers as the slow way."""

import json
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, devices
from energy_tracker.db import HALF_HOUR
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

client = TestClient(api.app)
LONDON = ZoneInfo("Europe/London")


def raw_last_in_half_hours(metric, since, until):
    """What last_in_buckets gave before the summary: straight from the readings."""
    with api.database()._connect() as conn:  # noqa: SLF001 - comparing with the readings
        rows = conn.execute(
            "SELECT max(time), value FROM readings WHERE metric = ? AND time >= ? AND time <= ? "
            "GROUP BY time / ? ORDER BY 1",
            (metric, int(since.timestamp()), int(until.timestamp()), HALF_HOUR),
        ).fetchall()
    return [(datetime.fromtimestamp(t, UTC), v) for t, v in rows]


def test_the_half_hour_summary_matches_the_readings_and_follows_late_arrivals():
    db = api.database()
    now = datetime.now(UTC).replace(microsecond=0)
    random.seed(1)
    total, rows = 100.0, []
    for minutes in range(3 * 24 * 60, 0, -3):  # every 3 minutes for 3 days, up to now
        total += random.uniform(0, 0.2)
        rows.append((now - timedelta(minutes=minutes), "load_energy_total", round(total, 3)))
    db.insert_readings(rows)
    since, until = now - timedelta(days=2, minutes=7), now + timedelta(minutes=1)

    def check(a=since, b=until):
        found = db.last_in_buckets(["load_energy_total"], a, b, HALF_HOUR)["load_energy_total"]
        assert found == raw_last_in_half_hours("load_energy_total", a, b)

    check()  # fills the summary in
    check()  # and read back from it
    check(since, now - timedelta(hours=5, minutes=13))  # ending part-way through a half hour
    # History arriving for a time already summarised: that part is summarised again.
    db.insert_readings([(now - timedelta(days=1, minutes=1, seconds=30), "load_energy_total", 1.0)])
    check()
    # Readings removed (an import replacing them): summarised again from what is left.
    db.delete_before(["load_energy_total"], now - timedelta(days=1))
    check()


def test_day_by_day_kwh_matches_the_costed_figures():
    now = datetime(2026, 3, 4, 12, tzinfo=UTC)
    names = {
        devices.LOAD: 2.0,
        devices.IMPORT: 1.5,
        devices.CIRCUIT: 0.8,
        devices.EV: 0.3,
        devices.SOLAR: 0.4,
        devices.EXPORT: 0.1,
        devices.CHARGE: 0.2,
        devices.DISCHARGE: 0.18,
    }
    samples = {name: [] for name in names}
    for minutes in range(0, 5 * 24 * 60, 10):
        when = now - timedelta(days=5) + timedelta(minutes=minutes)
        for name, rate in names.items():
            samples[name].append((when, 1000 + rate * minutes / 60 * (1 + (minutes % 70) / 100)))
    counters = {name: Counter(series, timedelta(minutes=75)) for name, series in samples.items()}
    start, end = now - timedelta(days=4), now
    schedule = Schedule(
        [build_tariff("Flat", [{"start": "00:00", "end": "24:00", "p_per_kwh": 20}], 15, 50)]
    )
    costed = devices.daily(counters, start, end, LONDON, schedule)
    quick = devices.daily_kwh(counters, start, end, LONDON)
    assert costed.keys() == quick.keys()
    for day, row in quick.items():
        for key in ("heat_pump_kwh", "ev_charger_kwh", "rest_kwh", "total_kwh"):
            assert row[key] == pytest.approx(costed[day][key], abs=1e-6)
    some = counters[devices.LOAD]
    assert some.kwh(start.timestamp(), end.timestamp()) == pytest.approx(some.between(start, end))


def test_the_script_is_sent_compressed_once_and_plain_to_browsers_that_ask():
    packed = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/app.js", headers={"Accept-Encoding": "identity"})
    assert packed.headers["content-encoding"] == "gzip" and "content-encoding" not in plain.headers
    assert packed.content == plain.content  # the test client unpacks it
    assert int(packed.headers["content-length"]) < len(plain.content) / 2


def test_pages_in_use_are_remembered_across_a_restart():
    api.page_opened.clear()
    client.get("/devices")
    saved = json.loads(api.database().get_setting("pages_opened"))
    assert "devices" in saved
    api.page_opened.clear()
    api.load_pages_opened()
    assert api.page_opened["devices"] == pytest.approx(saved["devices"])


def five_days_of_readings(now):
    names = {
        devices.LOAD: 2.0,
        devices.IMPORT: 1.5,
        devices.CIRCUIT: 0.8,
        devices.EV: 0.3,
        devices.SOLAR: 0.4,
        devices.EXPORT: 0.1,
        devices.CHARGE: 0.2,
        devices.DISCHARGE: 0.18,
    }
    rows = []
    for minutes in range(5 * 24 * 60, 0, -10):
        when = now - timedelta(minutes=minutes)
        for name, rate in names.items():
            rows.append((when, name, 1000 + rate * (5 * 24 * 60 - minutes) / 60))
    api.database().insert_readings(rows)


def test_finished_days_are_kept_and_give_the_same_averages():
    now = datetime.now(UTC).replace(microsecond=0)
    five_days_of_readings(now)
    first = client.get("/api/averages").json()
    kept = api.database().day_figures("averages", api.figures_version())
    assert kept and max(kept) < now.astimezone(api.local_timezone()).date()  # never today
    assert api.database().day_figures("devices", api.figures_version())
    # Worked out again from the kept days: the same answer.
    api.clear_caches()
    assert client.get("/api/averages").json() == first
    # And the same as working every day out afresh.
    api.database().forget_days()
    api.clear_caches()
    assert client.get("/api/averages").json() == first


def test_kept_days_are_dropped_when_late_readings_or_new_rates_arrive():
    now = datetime.now(UTC).replace(microsecond=0)
    five_days_of_readings(now)
    client.get("/api/averages")
    version = api.figures_version()
    kept = api.database().day_figures("averages", version)
    assert len(kept) >= 2
    # A reading for yesterday, arriving now: from the day before it on is worked out again;
    # earlier days stay. (Yesterday, not earlier: the first whole day kept depends on the
    # time of day and the clocks, and at least one must be left before the cut.)
    late = now - timedelta(days=1)
    cut = min(late.date(), late.astimezone(api.local_timezone()).date()) - timedelta(days=1)
    api.database().insert_readings([(late + timedelta(seconds=7), devices.LOAD, 1000.5)])
    left = api.database().day_figures("averages", version)
    assert left and all(day < cut for day in left)
    assert all(day in left for day in kept if day < cut)
    # A live reading, a moment old, leaves them be.
    api.clear_caches()
    client.get("/api/averages")
    before = api.database().day_figures("averages", version)
    api.database().insert_readings([(datetime.now(UTC), devices.LOAD, 2000.0)])
    assert api.database().day_figures("averages", version) == before
    # New rates: the kept days no longer match, so they are worked out again.
    body = {
        "name": "Dearer",
        "effective_from": "2020-01-01",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 60,
        "vat_percent": 5,
        "import_bands": [{"start": "00:00", "end": "24:00", "p_per_kwh": 30}],
    }
    assert client.post("/api/tariffs", json=body).status_code == 200
    assert api.figures_version() != version


def test_finished_months_are_kept_and_give_the_same_costs_by_year():
    now = datetime.now(UTC).replace(microsecond=0)
    rows = []
    for hours in range(45 * 24, 0, -1):
        when = now - timedelta(hours=hours)
        rows += [
            (when, "import_energy_total", 5000 - hours * 0.6),
            (when, "export_energy_total", 900 - hours * 0.1),
        ]
    api.database().insert_readings(rows)
    first = client.get("/api/years").json()
    assert api.database().day_figures("month_cost", api.figures_version())
    api.clear_caches()
    assert client.get("/api/years").json() == first
    api.database().forget_days()
    api.clear_caches()
    assert client.get("/api/years").json() == first
