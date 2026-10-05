from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, performance
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
SCHEDULE = Schedule([build_tariff("Test", BANDS, 15.0, 50.0)])
FLAT = Schedule(
    [build_tariff("Flat", [{"start": "00:00", "end": "24:00", "p_per_kwh": 25.0}], 15.0, 50.0)]
)
GAP = timedelta(minutes=75)
START, END = datetime(2026, 6, 1, tzinfo=LONDON), datetime(2026, 6, 3, tzinfo=LONDON)


def steady(kw, start=START, end=END):
    out, t = [], start
    while t <= end:
        out.append((t, kw * (t - start).total_seconds() / 3600))
        t += timedelta(minutes=30)
    return out


def counters(**kw):
    names = {
        "solar": performance.SOLAR,
        "load": performance.LOAD,
        "imp": performance.IMPORT,
        "exp": performance.EXPORT,
        "charge": performance.CHARGE,
        "discharge": performance.DISCHARGE,
    }
    return {names[key]: Counter(steady(value), GAP) for key, value in kw.items()}


def test_daily_energy_and_dear_rate_import():
    days = performance.daily(
        counters(solar=1.0, load=2.0, imp=1.5, exp=0.5), START, END, LONDON, SCHEDULE
    )
    day = days[date(2026, 6, 1)]
    assert len(days) == 2 and day[performance.LOAD] == pytest.approx(48.0)
    assert day[performance.CHARGE] is None  # not collected: left out, not zero
    assert day["dear_kwh"] == pytest.approx(27.0)  # 18 hours at 1.5 kW outside the cheap rate
    assert day["dear_pence"] == pytest.approx(27.0 * 30) and day["cheap_p"] == 10.0


def test_headline_figures():
    days = performance.daily(
        counters(solar=1.0, load=2.0, imp=1.5, exp=0.5, charge=1.0, discharge=0.9),
        START,
        END,
        LONDON,
        SCHEDULE,
    )
    figures = performance.figures(list(days.values()))
    assert figures["self_sufficiency_percent"] == 25.0  # 2 used, 1.5 of it imported
    assert figures["solar_used_percent"] == 50.0  # 1 generated, 0.5 exported
    assert figures["dear_import_percent"] == 75.0
    assert figures["battery_efficiency_percent"] is None  # two days is too short to judge
    assert performance.monthly(days)[0]["month"] == "2026-06"

    long_days = [dict(d) for d in days.values()] * 10
    assert performance.figures(long_days)["battery_efficiency_percent"] == 90.0
    empty = performance.figures([])
    assert empty["self_sufficiency_percent"] is None and empty["dear_import_kwh"] == 0


def test_bigger_battery_saves_the_price_gap_less_losses():
    day = {"dear_kwh": 6.0, "dear_pence": 180.0, "cheap_p": 10.0, "flat": False}
    easy = {"dear_kwh": 0.2, "dear_pence": 6.0, "cheap_p": 10.0, "flat": False}
    result = performance.sizing(
        {date(2026, 1, 1) + timedelta(days=n): (day if n < 73 else easy) for n in range(365)}, 0.8
    )

    assert result["short_days"] == 73 and result["short_percent"] == 20 and not result["rough"]
    by_size = {o["extra_kwh"]: o for o in result["options"]}
    # +5 kWh avoids 5 of the 6 on each short day: 5 x 30p saved, 5 / 0.8 x 10p spent charging.
    assert by_size[5.0]["avoided_kwh_year"] == 365
    assert by_size[5.0]["saved_gbp_year"] == round(73 * (150 - 62.5) / 100)
    # +10 kWh can only avoid the 6 that was needed.
    assert by_size[10.0]["avoided_kwh_year"] == 438
    assert result["dear_kwh_year"] == round(73 * 6 + 292 * 0.2) and result["worst_day_kwh"] == 6.0


def test_sizing_is_rough_under_a_year_and_absent_on_a_flat_tariff():
    days = performance.daily(counters(load=2.0, imp=1.5, exp=0.0), START, END, LONDON, SCHEDULE)
    assert performance.sizing(days)["rough"] is True
    assert performance.sizing(days)["efficiency_percent"] == 90
    flat = performance.daily(counters(load=2.0, imp=1.5, exp=0.0), START, END, LONDON, FLAT)
    assert performance.sizing(flat) is None and flat[date(2026, 6, 1)]["dear_kwh"] == 0


def test_performance_endpoint():
    client = TestClient(api.app)
    assert client.get("/api/performance").json()["has_data"] is False
    api.clear_caches()
    now = datetime.now(LONDON).replace(minute=0, second=0, microsecond=0)
    rows = []
    for hours in range(24 * 6):
        when = now - timedelta(hours=hours)
        rows += [
            (when, "load_energy_total", 9000 - hours * 2.0),
            (when, "import_energy_total", 7000 - hours * 0.5),
            (when, "export_energy_total", 600.0),
            (when, "pv_energy_total", 5000 - hours * 1.0),
        ]
    api.database().insert_readings(rows)
    data = client.get("/api/performance", params={"period": "30d"}).json()
    assert data["has_data"] and 4 <= data["days"] <= 5
    assert data["overall"]["self_sufficiency_percent"] == 75.0
    assert data["sizing"]["short_days"] == data["days"] and len(data["sizing"]["options"]) == 4
    assert data["monthly"] and client.get("/api/performance?period=week").status_code == 422
