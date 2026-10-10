"""Half-hourly prices, moved charging, device costs, bill rate checks, live readings and
leaving extra income out of the payback projection."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, billcheck, devices, live, lookup, prices, shift
from energy_tracker.config import Metric
from energy_tracker.costs import cost_between
from energy_tracker.roi import payback
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)
GAP = timedelta(minutes=75)
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
DAY = datetime(2026, 6, 1, tzinfo=LONDON)


def steady(start, end, kw):
    out, t = [], start
    while t <= end:
        out.append((t, kw * (t - start).total_seconds() / 3600))
        t += timedelta(minutes=30)
    return out


def ramp(start, end, kw_at):
    """A counter that rises at `kw_at(local time)` kW."""
    out, t, total = [], start, 0.0
    while t <= end:
        out.append((t, total))
        total += kw_at(t.astimezone(LONDON)) * 0.5
        t += timedelta(minutes=30)
    return out


# --- Half-hourly prices ----------------------------------------------------------------------


def half_hourly_tariff(price_at):
    start = int(DAY.timestamp())
    published = {start + n * 1800: price_at(n) for n in range(48)}
    return build_tariff(
        "Agile", BANDS, 15.0, 50.0, vat_percent=5, dynamic="AGILE-24-10-01/H", slot_prices=published
    )


def test_published_prices_are_used_as_they_are_and_bands_fill_the_gaps():
    tariff = half_hourly_tariff(lambda n: 5.0 if n < 12 else 20.0)
    assert tariff.import_price(DAY + timedelta(hours=1, minutes=10)) == 5.0  # no VAT added
    assert tariff.import_price(DAY + timedelta(hours=12)) == 20.0
    # The next day has no published prices: the typed-in bands apply, with VAT.
    assert tariff.import_price(DAY + timedelta(days=1, hours=1)) == pytest.approx(10.5)
    assert tariff.day_rate(DAY) == pytest.approx((12 * 5 + 36 * 20) / 48)
    assert tariff.cheap_rate(DAY) == 5.0
    assert tariff.cheap_rate(DAY + timedelta(days=1)) == pytest.approx(10.5)


def test_a_day_is_costed_at_its_published_prices():
    tariff = half_hourly_tariff(lambda n: 5.0 if n < 12 else 20.0)
    end = DAY + timedelta(days=1)
    imports = Counter(steady(DAY, end, 1.0), GAP)
    exports = Counter(steady(DAY, end, 0.0), GAP)
    cost = cost_between(imports, exports, DAY, end, LONDON, Schedule([tariff]))
    assert cost["import_gbp"] == pytest.approx((6 * 5 + 18 * 20) / 100)
    assert cost["import_bands"] == [
        {"label": "Half-hourly prices", "p_per_kwh": 16.25, "kwh": 24.0, "average": True}
    ]
    # Against the day's average price, buying evenly saves nothing.
    assert cost["saved_gbp"] == 0


def test_fetching_prices_fills_every_half_hour_and_follows_pages():
    calls = []

    def octopus(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "next": None,
                    "results": [
                        {
                            "value_inc_vat": 12.0,
                            "valid_from": "2026-06-01T00:00:00Z",
                            "valid_to": "2026-06-01T00:30:00Z",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "next": str(request.url.copy_with(query=b"page=2")),
                "results": [
                    # A price that lasts two hours covers four half hours.
                    {
                        "value_inc_vat": 30.0,
                        "valid_from": "2026-06-01T00:30:00Z",
                        "valid_to": "2026-06-01T02:30:00Z",
                    }
                ],
            },
        )

    start = datetime(2026, 6, 1, tzinfo=UTC)
    with httpx.Client(transport=httpx.MockTransport(octopus)) as http:
        found = lookup.fetch_slot_prices(
            http, "AGILE-24-10-01", "H", start, start + timedelta(hours=2)
        )
    assert "E-1R-AGILE-24-10-01-H/standard-unit-rates" in calls[0]
    assert "period_from=2026-06-01T00%3A00Z" in calls[0]
    first = int(start.timestamp())
    assert found == [
        (first, 12.0),
        (first + 1800, 30.0),
        (first + 3600, 30.0),
        (first + 5400, 30.0),
    ]


def test_refresh_stores_prices_for_tariffs_that_follow_them_and_costs_use_them():
    now = datetime(2026, 6, 2, 12, tzinfo=UTC)

    def octopus(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    {
                        "value_inc_vat": 9.0,
                        "valid_from": "2026-06-01T00:00:00Z",
                        "valid_to": "2026-06-05T00:00:00Z",
                    }
                ],
            },
        )

    body = {
        "name": "Agile",
        "effective_from": "2026-01-01",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 50,
        "vat_percent": 0,
        "import_bands": BANDS,
        "dynamic": "AGILE-24-10-01/H",
    }
    saved = client.post("/api/tariffs", json=body).json()
    period = next(p for p in saved["periods"] if p["dynamic"])
    assert period["published"] == {
        "source": "AGILE-24-10-01/H",
        "from": None,
        "until": None,
        "error": None,
    }

    with httpx.Client(transport=httpx.MockTransport(octopus)) as http:
        added = prices.refresh(api.database(), http, now, now - timedelta(days=1))
        assert added == 3 * 48  # from a day back to two days ahead
        # A second run only looks for anything newer.
        assert prices.refresh(api.database(), http, now, now - timedelta(days=1)) <= 1
    assert api.schedule().on(date(2026, 6, 2)).import_price(now) == 9.0
    listed = client.get("/api/tariffs").json()["periods"]
    assert next(p for p in listed if p["dynamic"])["published"]["until"] is not None

    # Once no tariff follows them, the prices are dropped.
    client.post("/api/tariffs", json={**body, "dynamic": ""})
    with httpx.Client(transport=httpx.MockTransport(octopus)) as http:
        prices.refresh(api.database(), http, now, now - timedelta(days=1))
    assert api.database().slot_prices("AGILE-24-10-01/H") == {}
    assert client.post("/api/tariffs", json={**body, "dynamic": "nonsense"}).status_code == 422


def test_products_are_split_into_fixed_and_half_hourly():
    listing = {
        "next": None,
        "results": [
            {"code": "GO-VAR-22-10-14", "full_name": "Octopus Go", "direction": "IMPORT"},
            {"code": "AGILE-24-10-01", "full_name": "Agile Octopus", "direction": "IMPORT"},
            {"code": "AGILE-OUTGOING-19-05-13", "full_name": "Agile Out", "direction": "EXPORT"},
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=listing))
    with httpx.Client(transport=transport) as http:
        fixed, half_hourly = lookup.product_lists(http)
    assert [p["code"] for p in fixed] == ["GO-VAR-22-10-14"]
    assert [p["code"] for p in half_hourly] == ["AGILE-24-10-01"]


# --- Moving flexible charging ----------------------------------------------------------------


def test_charging_is_moved_to_the_cheapest_half_hours_and_the_rest_stays_put():
    end = DAY + timedelta(days=1)
    # The battery charges at 4 kW from 00:00 to 02:00; the house draws 1 kW all day.
    charging = lambda t: 4.0 if t.hour < 2 else 0.0  # noqa: E731
    imports = Counter(ramp(DAY, end, lambda t: 1.0 + charging(t)), GAP)
    battery = Counter(ramp(DAY, end, charging), GAP)
    cheap_afternoon = Schedule(
        [
            build_tariff(
                "Afternoon",
                [
                    {"start": "00:00", "end": "13:00", "p_per_kwh": 30.0},
                    {"start": "13:00", "end": "16:00", "p_per_kwh": 5.0},
                    {"start": "16:00", "end": "24:00", "p_per_kwh": 30.0},
                ],
                0,
                0,
            )
        ]
    )
    moved = shift.shifted_import(imports, [battery, Counter([])], DAY, end, LONDON, cheap_afternoon)
    # House: 21 kWh at 30p and 3 kWh at 5p. Battery: 8 kWh moved to 5p.
    assert moved == {
        "import_gbp": pytest.approx(6.30 + 0.15 + 0.40),
        "moved_kwh": 8.0,
        "fastest_kw": 4.0,
    }
    # With nothing flexible there is nothing to move.
    assert shift.shifted_import(imports, [Counter([])], DAY, end, LONDON, cheap_afternoon) is None


def test_moved_charging_overflows_into_the_next_cheapest_time():
    end = DAY + timedelta(days=1)
    charging = lambda t: 2.0 if t.hour < 4 else 0.0  # noqa: E731  8 kWh at 2 kW
    imports = Counter(ramp(DAY, end, charging), GAP)
    battery = Counter(ramp(DAY, end, charging), GAP)
    one_cheap_hour = Schedule(
        [
            build_tariff(
                "One hour",
                [
                    {"start": "00:00", "end": "12:00", "p_per_kwh": 20.0},
                    {"start": "12:00", "end": "13:00", "p_per_kwh": 5.0},
                    {"start": "13:00", "end": "24:00", "p_per_kwh": 40.0},
                ],
                0,
                0,
            )
        ]
    )
    moved = shift.shifted_import(imports, [battery], DAY, end, LONDON, one_cheap_hour)
    # Only 2 kWh fit in the cheap hour at 2 kW; the other 6 go at the next cheapest price.
    assert moved["import_gbp"] == pytest.approx((2 * 5 + 6 * 20) / 100)


def test_comparison_reports_the_cost_with_charging_moved():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    imported, charged = 70000.0, 50000.0
    for hours in range(24 * 6, -1, -1):
        when = now - timedelta(hours=hours)
        rows.append((when, "import_energy_total", imported))
        rows.append((when, "export_energy_total", 60000.0))
        rows.append((when, "battery_charge_energy_total", charged))
        charging = 3.0 if when.hour < 2 else 0.0  # the battery fills in the small hours
        imported += 0.5 + charging
        charged += charging
    api.database().insert_readings(rows)
    body = {
        "name": "Cheap afternoons",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 50,
        "vat_percent": 0,
        "import_bands": [
            {"start": "00:00", "end": "13:00", "p_per_kwh": 30.0},
            {"start": "13:00", "end": "16:00", "p_per_kwh": 5.0},
            {"start": "16:00", "end": "24:00", "p_per_kwh": 30.0},
        ],
    }
    assert client.post("/api/compare/tariffs", json=body).status_code == 200
    candidate = client.get("/api/compare?period=30d").json()["candidates"][0]
    shifted = candidate["shifted"]
    assert shifted["moved_kwh"] > 0 and shifted["net_gbp"] < candidate["cost"]["net_gbp"]
    assert shifted["difference_gbp"] < candidate["difference_gbp"]


# --- Running cost by device ------------------------------------------------------------------


def test_each_device_carries_its_share_of_the_days_import_cost():
    end = DAY + timedelta(days=1)
    counters = {
        devices.LOAD: Counter(steady(DAY, end, 2.0), GAP),  # 48 kWh used
        devices.CIRCUIT: Counter(steady(DAY, end, 1.0), GAP),  # 24 kWh on the smart load
        devices.EV: Counter(steady(DAY, end, 0.25), GAP),  # 6 kWh of that is the car
        devices.IMPORT: Counter(steady(DAY, end, 1.0), GAP),  # 24 kWh bought
    }
    rates = Schedule([build_tariff("Test", BANDS, 15.0, 50.0)])
    day = devices.daily(counters, DAY, end, LONDON, rates)[date(2026, 6, 1)]
    assert day["import_gbp"] == pytest.approx(6.0)  # 6 kWh at 10p + 18 kWh at 30p
    assert day["heat_pump_kwh"] == pytest.approx(18.0) and day["ev_charger_kwh"] == pytest.approx(
        6.0
    )
    assert day["rest_kwh"] == pytest.approx(24.0)
    assert day["heat_pump_gbp"] == pytest.approx(6.0 * 18 / 48)
    assert day["ev_charger_gbp"] + day["heat_pump_gbp"] + day["rest_gbp"] == pytest.approx(6.0)

    # With the car on its own circuit, nothing is taken off the smart load.
    apart = devices.daily(counters, DAY, end, LONDON, rates, ev_on_smart_load=False)
    assert apart[date(2026, 6, 1)]["heat_pump_kwh"] == pytest.approx(24.0)

    # A house without either device has only "rest".
    plain = {**counters, devices.CIRCUIT: Counter([]), devices.EV: Counter([])}
    only = devices.daily(plain, DAY, end, LONDON, rates)[date(2026, 6, 1)]
    assert "heat_pump_kwh" not in only and only["rest_gbp"] == pytest.approx(6.0)

    summary = devices.by_month_and_year({date(2026, 6, 1): day, date(2026, 6, 2): day})
    assert summary["months"][0]["month"] == "2026-06" and summary["months"][0]["days"] == 2
    assert summary["years"][0]["heat_pump_gbp"] == pytest.approx(4.5)


def test_device_costs_endpoint():
    assert client.get("/api/devices").json()["has_data"] is False
    api.clear_caches()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = []
    for hours in range(24 * 3):
        when = now - timedelta(hours=hours)
        rows.append((when, "load_energy_total", 90000 - hours * 2.0))
        rows.append((when, "import_energy_total", 70000 - hours * 1.0))
        rows.append((when, "smart_load_energy_total", 30000 - hours * 0.5))
    api.database().insert_readings(rows)
    data = client.get("/api/devices").json()
    assert data["has_data"] and data["heat_pump"] and not data["ev_charger"]
    year = data["years"][-1]
    assert year["heat_pump_gbp"] == pytest.approx(year["import_gbp"] / 4, abs=0.02)
    assert "ev_charger_gbp" not in year


# --- Checking a bill's rates -----------------------------------------------------------------


def two_rate_schedule():
    first = build_tariff("Drive", BANDS, 16.5, 42.978, vat_percent=5)
    later = build_tariff("Drive", BANDS, 16.5, 45.0, date(2026, 4, 1), vat_percent=5)
    return Schedule([first, later])


def test_a_bill_that_agrees_with_the_tracker_needs_no_correction():
    found = billcheck.check(
        "import", date(2026, 1, 6), [10.0, 30.0], 42.978, 5.0, two_rate_schedule()
    )
    assert found["differences"] == [] and found["corrected"] is None
    assert found["period_from"] is None and found["next_change"] == date(2026, 4, 1)


def test_a_wrong_day_rate_is_corrected_in_every_band_that_used_it():
    found = billcheck.check(
        "import", date(2026, 1, 6), [27.092, 10.0], 42.978, 5.0, two_rate_schedule()
    )
    assert found["differences"] == [
        {"what": "Day rate", "tracker": 30.0, "bill": 27.092, "unit": "per kWh"}
    ]
    assert [b["p_per_kwh"] for b in found["corrected"]["import_bands"]] == [10.0, 27.092]
    assert found["corrected"]["standing_charge_p_per_day"] == 42.978


def test_standing_charge_vat_and_export_rate_are_checked():
    rates = two_rate_schedule()
    found = billcheck.check("import", date(2026, 5, 1), [10.0, 30.0], 42.978, 0.0, rates)
    assert [d["what"] for d in found["differences"]] == ["Standing charge", "VAT"]
    assert found["period_from"] == date(2026, 4, 1) and found["corrected"]["vat_percent"] == 0.0
    export = billcheck.check("export", date(2026, 1, 6), [15.0], None, None, rates)
    assert export["differences"][0]["what"] == "Export rate"
    assert export["corrected"]["export_p_per_kwh"] == 15.0
    # An export bill says nothing about import rates.
    assert [b["p_per_kwh"] for b in export["corrected"]["import_bands"]] == [10.0, 30.0]


def test_rates_that_cannot_be_matched_up_are_left_for_the_user():
    found = billcheck.check(
        "import", date(2026, 1, 6), [10.0, 20.0, 30.0], None, None, two_rate_schedule()
    )
    assert found["corrected"] is None and "cannot be matched" in found["notes"][0]
    assert billcheck.check("import", None, [10.0], None, None, two_rate_schedule()) is None


def test_bill_rates_can_be_checked_again_through_the_api():
    client.get("/api/tariffs")  # the starting rates
    current = client.get("/api/tariffs").json()["periods"][0]
    billed = sorted({b["p_per_kwh"] for b in current["import_bands"]})
    billed[-1] += 1.5
    body = {"kind": "import", "first_day": "2026-01-06", "rates": billed}
    found = client.post("/api/bills/tariff-check", json=body).json()
    assert len(found["differences"]) == 1
    fixed = {**found["corrected"], "effective_from": found["period_key"]}
    assert client.post("/api/tariffs", json=fixed).status_code == 200
    assert client.post("/api/bills/tariff-check", json=body).json()["differences"] == []


# --- Live readings ---------------------------------------------------------------------------

METRICS = [
    Metric("pv_power", "sensor.pv", "power", "Solar"),
    Metric("battery_soc", "sensor.soc", "percent", "Battery"),
    Metric("import_energy_total", "sensor.import", "energy", "Import"),
]


def test_live_values_come_from_state_changes_and_polls():
    by_entity = {m.entity: m for m in METRICS if m.kind in live.LIVE_KINDS}
    assert set(by_entity) == {"sensor.pv", "sensor.soc"}  # counters are not followed live
    state = {
        "entity_id": "sensor.pv",
        "state": "2500",
        "attributes": {"unit_of_measurement": "W"},
        "last_updated": "2026-06-01T12:00:05+00:00",
    }
    assert live.take_state(state, by_entity) is True
    assert live.snapshot()["pv_power"] == (datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC), 2.5)
    assert live.take_state({**state, "state": "unavailable"}, by_entity) is False
    assert live.take_state({**state, "entity_id": "sensor.other"}, by_entity) is False
    assert live.take_state(None, by_entity) is False

    # An older reading does not replace a newer one; a poll's newer one does.
    earlier = datetime(2026, 6, 1, 11, tzinfo=UTC)
    later = datetime(2026, 6, 1, 13, tzinfo=UTC)
    live.note_rows([(earlier, "pv_power", 9.0), (later, "battery_soc", 55.0)], METRICS)
    live.note_rows([(later, "import_energy_total", 1.0)], METRICS)
    held = live.snapshot()
    assert held["pv_power"][1] == 2.5 and held["battery_soc"] == (later, 55.0)
    assert "import_energy_total" not in held


def test_live_endpoint_leaves_out_values_not_heard_recently():
    now = datetime.now(UTC)
    live.note("pv_power", now, 1.25)
    live.note("load_power", now - timedelta(minutes=30), 0.4)
    data = client.get("/api/live").json()
    assert data["live"] is False and list(data["values"]) == ["pv_power"]
    assert data["values"]["pv_power"]["value"] == 1.25


# --- Payback: leaving extra income out of the projection ---------------------------------------


def test_one_off_income_counts_as_saved_but_is_not_projected():
    first = date(2026, 1, 1)
    savings = {first + timedelta(days=n): 2.0 for n in range(100)}
    savings[first] += 100.0  # a one-off payment on the first day
    plain = payback(savings, 5000, first, first + timedelta(days=100))
    apart = payback(savings, 5000, first, first + timedelta(days=100), one_off={first: 100.0})
    assert plain["saved_gbp"] == apart["saved_gbp"] == 300.0
    assert plain["yearly_gbp"] == pytest.approx(3.0 * 365)
    assert apart["yearly_gbp"] == pytest.approx(2.0 * 365)
    assert apart["break_even"] > plain["break_even"]


def test_regular_income_is_carried_on_unaged_and_at_least_the_minimum():
    first = date(2026, 1, 1)
    savings = {first + timedelta(days=n): 2.0 for n in range(100)}
    for n in (0, 30, 60, 90):  # Axle pays 5 a month, below its 10 minimum
        savings[first + timedelta(days=n)] += 5.0
    regular = {first + timedelta(days=n): 5.0 for n in (0, 30, 60, 90)}
    at_rate = payback(savings, 5000, first, first + timedelta(days=100), regular=regular)
    assert at_rate["saved_gbp"] == 220.0
    assert at_rate["yearly_regular_income_gbp"] == pytest.approx(20 / 100 * 365)
    assert at_rate["yearly_gbp"] == pytest.approx(2.2 * 365)
    floor = payback(
        savings,
        5000,
        first,
        first + timedelta(days=100),
        regular=regular,
        regular_minimum_per_year=120,
    )
    assert floor["yearly_regular_income_gbp"] == 120
    assert floor["yearly_gbp"] == pytest.approx(2.0 * 365 + 120)
    assert floor["break_even"] < at_rate["break_even"]
    # Battery ageing wears down the energy saving, not Axle's payments.
    aged = payback(
        savings,
        5000,
        first,
        first + timedelta(days=100),
        battery_ageing=0.5,
        regular=regular,
        regular_minimum_per_year=120,
    )
    year_on = [v for d, v in aged["adjusted"]["projection"] if d >= first + timedelta(days=3650)]
    assert year_on  # by year ten the energy saving is nearly gone; the income still adds up
    tenth, eleventh = (
        next(v for d, v in aged["adjusted"]["projection"] if d >= first + timedelta(days=365 * y))
        for y in (10, 11)
    )
    assert eleventh - tenth > 115


def test_income_is_sorted_into_regular_and_one_off(tmp_path):
    import sqlite3
    from contextlib import closing

    from energy_tracker.db import Database

    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as conn, conn:  # an income table from before 0.28
        conn.execute(
            "CREATE TABLE extra_income (id INTEGER PRIMARY KEY, day TEXT NOT NULL, "
            "description TEXT NOT NULL, amount_gbp REAL, source TEXT NOT NULL DEFAULT 'manual', "
            "event_start INTEGER UNIQUE, event_end INTEGER, kwh REAL, "
            "estimated INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO extra_income (day, description, amount_gbp, source) VALUES (?, ?, ?, ?)",
            [
                ("2026-01-05", "Axle monthly payment", 10, "manual"),
                ("2026-01-06", "Referral bonus", 50, "manual"),
                ("2026-01-07", "Axle export event", 2, "axle"),
            ],
        )
    db = Database(path)
    kinds = {row["description"]: row["regular"] for row in db.income_rows()}
    assert kinds == {
        "Axle monthly payment": True,
        "Referral bonus": False,
        "Axle export event": True,
    }
    db.save_income(date(2026, 2, 1), "Axle bonus for a winter event", 5, None, regular=False)
    db.save_income(date(2026, 2, 2), "Octopus referral", 50, None)
    kinds = {row["description"]: row["regular"] for row in db.income_rows()}
    assert kinds["Axle bonus for a winter event"] is False and kinds["Octopus referral"] is False


def test_income_endpoint_and_roi_split_regular_from_one_off():
    yesterday = (datetime.now(api.local_timezone()) - timedelta(days=1)).date().isoformat()
    client.post("/api/income", json={"day": yesterday, "description": "Axle", "amount_gbp": 10})
    data = client.post(
        "/api/income",
        json={"day": yesterday, "description": "Referral", "amount_gbp": 50, "regular": False},
    ).json()
    assert (data["regular_gbp"], data["one_off_gbp"]) == (10, 50)
    body = {
        "install_date": "2026-01-01",
        "costs": [{"date": "2026-01-01", "amount": 9000}],
        "regular_income_minimum_gbp": 10,
    }
    saved = client.post("/api/roi/settings", json=body).json()
    assert saved["settings"]["regular_income_minimum_gbp"] == 10


def test_payback_setting_for_extra_income_is_kept():
    body = {
        "install_date": "2026-01-01",
        "costs": [{"date": "2026-01-01", "amount": 9000}],
        "project_extra_income": False,
    }
    saved = client.post("/api/roi/settings", json=body).json()
    assert saved["settings"]["project_extra_income"] is False
    assert client.post("/api/roi/settings", json={**body, "project_extra_income": True}).json()[
        "settings"
    ]["project_extra_income"]


def test_live_connection_signs_in_subscribes_and_takes_changes():
    """Against a stand-in for Home Assistant's websocket."""
    import contextlib
    import json
    import threading

    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    from energy_tracker.config import load_settings

    seen = []
    stop = threading.Event()

    def home_assistant(ws):
        ws.send(json.dumps({"type": "auth_required"}))
        seen.append(json.loads(ws.recv()))
        ws.send(json.dumps({"type": "auth_ok"}))
        request = json.loads(ws.recv())
        seen.append(request)
        # Refuse the narrow subscription, to check the fallback to all state changes.
        ws.send(json.dumps({"id": request["id"], "type": "result", "success": False, "error": {}}))
        seen.append(json.loads(ws.recv()))
        ws.send(json.dumps({"id": 2, "type": "result", "success": True}))
        state = {
            "entity_id": "sensor.pv",
            "state": "1.75",
            "attributes": {"unit_of_measurement": "kW"},
            "last_updated": "2026-06-01T12:00:00+00:00",
        }
        ws.send(json.dumps({"id": 2, "type": "event", "event": {"data": {"new_state": state}}}))
        for _ in range(100):
            if "pv_power" in live.snapshot():
                break
            threading.Event().wait(0.02)
        stop.set()

    with serve(home_assistant, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        settings = load_settings()
        object.__setattr__(settings, "ha_websocket_url", f"ws://127.0.0.1:{port}")
        object.__setattr__(settings, "ha_token", "token-for-test")
        by_entity = {m.entity: m for m in METRICS if m.kind in live.LIVE_KINDS}
        with contextlib.suppress(ConnectionClosed):  # the stand-in hangs up when done
            live.listen(settings, by_entity, stop)
        server.shutdown()

    assert seen[0] == {"type": "auth", "access_token": "token-for-test"}
    assert seen[1]["type"] == "subscribe_trigger"
    assert seen[1]["trigger"] == {"platform": "state", "entity_id": ["sensor.pv", "sensor.soc"]}
    assert seen[2] == {"id": 2, "type": "subscribe_events", "event_type": "state_changed"}
    assert live.snapshot()["pv_power"][1] == 1.75


# --- Tariff switch planner and reminders -------------------------------------------------------

from energy_tracker import planner  # noqa: E402


def test_cheap_times_join_a_window_that_runs_past_midnight():
    overnight = build_tariff(
        "Night",
        [
            {"start": "00:00", "end": "05:30", "p_per_kwh": 7.0},
            {"start": "05:30", "end": "23:30", "p_per_kwh": 28.0},
            {"start": "23:30", "end": "24:00", "p_per_kwh": 7.0},
        ],
        0,
        0,
    )
    assert planner.cheap_times(overnight) == [{"start": "23:30", "end": "05:30", "p_per_kwh": 7.0}]
    assert planner.cheap_times(build_tariff("Flat", [BANDS[0] | {"end": "24:00"}], 0, 0)) == []
    assert planner.cheap_times(build_tariff("Two", BANDS, 0, 0)) == [BANDS[0]]


def test_battery_size_is_worked_out_from_how_it_charges():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    soc, charged, total = [], [], 0.0
    for n in range(48 * 4):
        when = start + timedelta(minutes=30 * n)
        filling = n % 48 < 8  # four hours a night at 2.5 kW into a 10 kWh battery
        soc.append((when, 10.0 + 90.0 * min(n % 48, 8) / 8))
        charged.append((when, total))
        total += 1.25 / 0.9**0.5 if filling else 0.0
    capacity, speed = planner.estimate_battery(soc, Counter(charged, GAP), Counter([]))
    assert capacity == pytest.approx(11.1, abs=0.1)  # 10 kWh between 10% and 100%
    assert speed == pytest.approx(2.6, abs=0.1)
    assert planner.estimate_battery([], Counter([]), Counter([])) == (None, None)


def plan_counters(end, house_kw=1.0, solar=None, ev=None):
    return {
        "load": Counter(ramp(DAY, end, lambda t: house_kw + (ev(t) if ev else 0.0)), GAP),
        "solar": Counter(ramp(DAY, end, solar), GAP) if solar else Counter([]),
        "ev": Counter(ramp(DAY, end, ev), GAP) if ev else Counter([]),
    }


def test_battery_charged_in_the_cheap_window_runs_the_house_until_it_is_empty():
    end = DAY + timedelta(days=2)
    rates = Schedule([build_tariff("Night", BANDS, 0, 0)])  # 10p until 06:00, then 30p
    # A lossless 9 kWh battery: 9 hours of a 1 kW house. Cheap hours run from the grid.
    days = planner.simulate(plan_counters(end), DAY, end, LONDON, rates, planner.Battery(9, 5, 1))
    second = days[date(2026, 6, 2)]
    # Six cheap hours for the house plus 9 kWh into the battery, then nine dear hours short.
    assert second["import_kwh"] == pytest.approx(6 + 9 + 9)
    assert second["dear_kwh"] == pytest.approx(9.0) and second["empty_slots"] == 18
    assert second["gbp"] == pytest.approx((15 * 10 + 9 * 30) / 100)

    big = planner.simulate(plan_counters(end), DAY, end, LONDON, rates, planner.Battery(30, 5, 1))
    assert big[date(2026, 6, 2)]["dear_kwh"] == 0 and big[date(2026, 6, 2)]["empty_slots"] == 0
    assert planner.shortfall(days)["short_days"] == 2 and planner.shortfall(big)["short_days"] == 0
    assert planner.shortfall(days)["hours_empty_on_short_days"] > 8


def test_the_car_is_moved_to_the_cheap_hours_and_spare_solar_is_stored_then_exported():
    end = DAY + timedelta(days=1)
    rates = Schedule([build_tariff("Night", BANDS, 15.0, 0)])
    evening_car = lambda t: 7.0 if 18 <= t.hour < 20 else 0.0  # noqa: E731
    midday_sun = lambda t: 6.0 if 10 <= t.hour < 14 else 0.0  # noqa: E731
    counters = plan_counters(end, 0.0, midday_sun, evening_car)
    day = planner.simulate(counters, DAY, end, LONDON, rates, planner.Battery(10, 5, 1))[
        date(2026, 6, 1)
    ]
    # 14 kWh for the car bought at 10p, not 30p. The battery starts half full, takes 5 kWh
    # from the grid overnight, so all 24 kWh of solar is exported.
    assert day["dear_kwh"] == 0
    assert day["import_kwh"] == pytest.approx(14 + 5)
    assert day["export_kwh"] == pytest.approx(24.0)
    assert day["gbp"] == pytest.approx((19 * 10 - 24 * 15) / 100)


def test_planner_endpoint_gives_months_totals_and_a_charging_plan():
    assert client.get("/api/planner").json() == {"has_data": False, "tariffs": [], "settings": {}}
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows, imported, used, charged, given, level = [], 70000.0, 90000.0, 50000.0, 40000.0, 50.0
    for hours in range(24 * 40, -1, -1):
        when = now - timedelta(hours=hours)
        local_hour = when.astimezone(LONDON).hour
        charging = 2.5 if local_hour < 4 else 0.0
        discharging = 0.0 if local_hour < 6 else 0.5
        for name, value in (
            ("import_energy_total", imported),
            ("export_energy_total", 60000.0),
            ("load_energy_total", used),
            ("battery_charge_energy_total", charged),
            ("battery_discharge_energy_total", given),
            ("battery_soc", level),
        ):
            rows.append((when, name, value))
        imported += 1.0 - discharging + charging
        used += 1.0
        charged += charging
        given += discharging
        level = min(100.0, max(0.0, level + (charging * 0.95 - discharging / 0.95) * 10))
    api.database().insert_readings(rows)
    body = {
        "name": "Cheap afternoons",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 50,
        "vat_percent": 0,
        "import_bands": [
            {"start": "00:00", "end": "13:00", "p_per_kwh": 30.0},
            {"start": "13:00", "end": "16:00", "p_per_kwh": 5.0},
            {"start": "16:00", "end": "24:00", "p_per_kwh": 30.0},
        ],
    }
    saved = client.post("/api/compare/tariffs", json=body).json()["id"]
    data = client.get("/api/planner").json()
    assert data["has_data"] and data["tariff"] == {
        "id": saved,
        "name": "Cheap afternoons",
        "dynamic": False,
    }
    assert data["days"] == 39 and sum(m["days"] for m in data["months"]) == 39
    assert data["cheap_times"] == [{"start": "13:00", "end": "16:00", "p_per_kwh": 5.0}]
    assert 9 < data["battery"]["estimated_kwh"] < 11 and data["battery"]["estimated_kw"] == 2.5
    totals = data["totals"]
    assert totals["replayed_gbp"] > totals["moved_gbp"] and totals["planned_gbp"] is not None
    # A three-hour window at 2.5 kW cannot carry a 1 kW house for the other 21 hours.
    assert data["shortfall"]["short_days"] == 39 and data["shortfall"]["dear_kwh"] > 0

    # A bigger, faster battery entered by hand lasts, and the plan gets cheaper.
    assert client.post("/api/planner/settings", json={"battery_kwh": 30, "charge_kw": 10}).json()
    bigger = client.get(f"/api/planner?tariff={saved}").json()
    assert bigger["battery"]["used_kwh"] == 30 and bigger["shortfall"]["short_days"] <= 1
    assert bigger["totals"]["planned_gbp"] < totals["planned_gbp"]
    assert client.post("/api/planner/settings", json={"battery_kwh": 0.1}).status_code == 422


def test_a_fixed_price_about_to_end_raises_a_reminder_until_new_rates_are_entered():
    today = datetime.now(api.local_timezone()).date()
    body = {
        "name": "Fixed deal",
        "effective_from": "2026-01-01",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 50,
        "vat_percent": 5,
        "import_bands": BANDS,
        "fixed_until": (today + timedelta(days=45)).isoformat(),
    }
    saved = client.post("/api/tariffs", json=body).json()
    assert saved["reminders"] == []  # more than 30 days away
    assert next(p for p in saved["periods"] if p["name"] == "Fixed deal")["fixed_until"]

    ends = today + timedelta(days=12)
    soon = client.post("/api/tariffs", json={**body, "fixed_until": ends.isoformat()}).json()
    assert soon["reminders"] == [
        {"kind": "fixed_ending", "date": ends.isoformat(), "days": 12, "tariff": "Fixed deal"}
    ]
    past = (today - timedelta(days=3)).isoformat()
    over = client.post("/api/tariffs", json={**body, "fixed_until": past}).json()
    assert over["reminders"][0]["kind"] == "fixed_ended" and over["reminders"][0]["days"] == -3

    # Entering the rates that follow settles it.
    client.post("/api/tariffs", json={**body, "fixed_until": ends.isoformat()})
    follow = {**body, "name": "Next", "fixed_until": None}
    follow["effective_from"] = (ends + timedelta(days=1)).isoformat()
    assert client.post("/api/tariffs", json=follow).json()["reminders"] == []


def test_a_failed_price_fetch_raises_a_reminder():
    body = {
        "name": "Agile",
        "effective_from": "2026-01-01",
        "export_p_per_kwh": 15,
        "standing_charge_p_per_day": 50,
        "import_bands": BANDS,
        "dynamic": "AGILE-24-10-01/H",
    }
    client.post("/api/tariffs", json=body)
    prices.status["AGILE-24-10-01/H"] = {"error": "Could not reach the price list"}
    found = client.get("/api/tariffs").json()["reminders"]
    assert found == [
        {"kind": "prices_failed", "tariff": "Agile", "error": "Could not reach the price list"}
    ]


def test_old_figures_are_served_at_once_and_refreshed_in_the_background(monkeypatch):
    from energy_tracker import api

    kept, clock, calls = api.Kept(), [1000.0], []
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])

    def work():
        calls.append(clock[0])
        return len(calls)

    assert kept.get(("k",), work) == 1
    clock[0] += api.CACHE_SECONDS + 1
    assert kept.get(("k",), work) == 1  # old, but served without waiting
    assert kept.wake.is_set() and len(calls) == 1
    key, due = kept.next_due()
    assert kept.get(key, due, fresh=True) == 2 and kept.next_due() is None
    assert kept.get(("k",), work) == 2
    clock[0] += api.STALE_SECONDS + 1
    assert kept.get(("k",), work) == 3  # too old to serve: worked out there and then
    kept.wake.clear()
    kept.clear()
    assert kept.cleared and kept.wake.is_set()
    assert kept.get(("k",), work) == 4


def test_cost_today_and_this_month_are_shared_between_devices():
    data = client.get("/api/today").json()
    assert data["by_device"] is None  # nothing recorded
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    midnight = api.local_midnight(now, api.local_timezone())
    rows, used, heat, imported = [], 100.0, 40.0, 70.0
    when = midnight - timedelta(hours=2)
    while when <= now:
        rows += [
            (when, "load_energy_total", used),
            (when, "smart_load_energy_total", heat),
            (when, "import_energy_total", imported),
            (when, "export_energy_total", 5.0),
        ]
        used, heat, imported = used + 0.2, heat + 0.1, imported + 0.2
        when += timedelta(minutes=5)
    api.database().insert_readings(rows)
    api.clear_caches()
    checked = 0
    for body in (client.get("/api/today").json(), client.get("/api/month").json()):
        split, cost = body["by_device"], body["cost"]
        if cost is None or not cost["import_gbp"]:
            continue  # just after midnight there is nothing yet
        checked += 1
        parts = split["parts"]
        assert set(parts) == {"heat_pump", "rest"}  # no EV charger recorded
        assert sum(p["gbp"] for p in parts.values()) == pytest.approx(cost["import_gbp"], abs=0.02)
        assert parts["heat_pump"]["share_percent"] == pytest.approx(50, abs=2)
    assert checked or now - midnight < timedelta(minutes=10)


def test_heat_pump_true_cost_and_hot_water_split(monkeypatch):
    monkeypatch.setattr(api, "heat_pump_and_weather", lambda: {"warm_day_kwh": 0.5})
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    midnight = api.local_midnight(now, api.local_timezone())
    if now - midnight < timedelta(minutes=30):
        pytest.skip("too soon after midnight for a day's figures")
    tariff = {
        "name": "Flat",
        "effective_from": "2020-01-01",
        "export_p_per_kwh": 10.0,
        "standing_charge_p_per_day": 0.0,
        "vat_percent": 0,
        "import_bands": [{"start": "00:00", "end": "24:00", "p_per_kwh": 20.0}],
    }
    assert client.post("/api/tariffs", json=tariff).status_code == 200
    rows, used, heat, imported, solar, exported = [], 100.0, 40.0, 70.0, 10.0, 1.0
    when = midnight - timedelta(hours=1)
    while when <= now:
        rows += [
            (when, "load_energy_total", used),
            (when, "smart_load_energy_total", heat),
            (when, "import_energy_total", imported),
            (when, "export_energy_total", exported),
            (when, "pv_energy_total", solar),
        ]
        used, heat, imported = used + 0.2, heat + 0.1, imported + 0.1
        solar, exported = solar + 0.15, exported + 0.05  # 0.1 kWh of solar kept each step
        when += timedelta(minutes=5)
    api.database().insert_readings(rows)
    api.clear_caches()
    hp = client.get("/api/today").json()["by_device"]["parts"]["heat_pump"]
    # Half the house's use, so half the solar it kept, valued at 10p a unit.
    kept_kwh = (now - midnight) / timedelta(minutes=5) * 0.1
    assert hp["solar_gbp"] == pytest.approx(kept_kwh / 2 * 0.10, rel=0.1)
    assert hp["true_gbp"] == pytest.approx(hp["gbp"] + hp["solar_gbp"], abs=0.01)
    assert hp["hot_water"]["kwh"] == pytest.approx(min(0.5, hp["kwh"]), abs=0.05)
    assert hp["heating"]["kwh"] + hp["hot_water"]["kwh"] == pytest.approx(hp["kwh"], abs=0.1)
    assert hp["heating"]["gbp"] + hp["hot_water"]["gbp"] == pytest.approx(hp["gbp"], abs=0.02)


def test_device_figures_start_when_the_heat_pump_readings_do():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    zone = api.local_timezone()
    rows, used, heat, imported = [], 100.0, 40.0, 70.0
    heat_from = api.local_midnight(now, zone) - timedelta(days=3)
    when = heat_from - timedelta(days=10)
    while when <= now:
        rows += [(when, "load_energy_total", used), (when, "import_energy_total", imported)]
        if when >= heat_from:
            rows.append((when, "smart_load_energy_total", heat))
            heat += 0.5
        used, imported = used + 1.0, imported + 1.0
        when += timedelta(hours=1)
    api.database().insert_readings(rows)
    api.clear_caches()
    data = client.get("/api/devices").json()
    assert data["has_data"] and data["from"] == heat_from.astimezone(zone).date().isoformat()
    month = client.get("/api/month").json()["by_device"]
    if month and heat_from.astimezone(zone).day > 1:
        assert month["from"] and month["carried_gbp"] is None
