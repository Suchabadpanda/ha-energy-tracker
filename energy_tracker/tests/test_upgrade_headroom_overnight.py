"""Before and after an upgrade, supply headroom, and the overnight charge."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from energy_tracker import api, collector, headroom, overnight, upgrade
from energy_tracker.performance import EXPORT, IMPORT, LOAD, SOLAR
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

client = TestClient(api.app)
LONDON = ZoneInfo("Europe/London")


# --- Before and after an upgrade -----------------------------------------------------------


def test_a_cloudless_day_gives_far_more_sun_in_summer_than_in_winter():
    june, december = (
        upgrade.clear_sky_kwh(date(2026, 6, 21), 51),
        upgrade.clear_sky_kwh(date(2026, 12, 21), 51),
    )
    assert 6.5 < june < 8.5
    assert 0.4 < december < 1.2
    # Further north, a winter day is shorter still.
    assert upgrade.clear_sky_kwh(date(2026, 12, 21), 58) < december


def days_with(first: date, count: int, per_sky: float, latitude: float = 51.0) -> dict:
    """Days whose solar is `per_sky` times what a cloudless sky gives, a flat 20 kWh used."""
    rows = {}
    for n in range(count):
        day = first + timedelta(days=n)
        solar = per_sky * upgrade.clear_sky_kwh(day, latitude)
        rows[day] = {SOLAR: solar, LOAD: 20.0, IMPORT: 20.0 - solar / 2, EXPORT: solar / 4}
    return rows


def test_the_season_is_allowed_for():
    # The same panels either side of an upgrade in late October: solar falls with the
    # daylight, but allowing for the season there is no change.
    upgrade_day = date(2026, 10, 30)
    rows = days_with(upgrade_day - timedelta(days=60), 121, 3.0)
    found = upgrade.compare(rows, upgrade_day, date(2026, 12, 31), 60, 51.0)
    assert found["after"]["solar_kwh"] < found["before"]["solar_kwh"] * 0.7  # darker after
    assert abs(found["changes"]["solar_percent"]) < 1
    assert abs(found["changes"]["clear_day_percent"]) < 1
    assert found["before"]["days"] == 60 and found["after"]["days"] == 60
    assert upgrade_day not in (found["before"]["first_day"], found["after"]["first_day"])


def test_more_panels_show_as_more_solar_for_the_season():
    upgrade_day = date(2026, 11, 20)
    before = days_with(upgrade_day - timedelta(days=30), 30, 3.0)
    after = days_with(upgrade_day + timedelta(days=1), 30, 4.8)
    found = upgrade.compare(
        {**before, **after},
        upgrade_day,
        date(2027, 1, 1),
        30,
        51.0,
        kwp_before=6.4,
        kwp_after=10.24,
    )
    assert found["changes"]["solar_percent"] == 60.0
    assert found["changes"]["clear_day_percent"] == 60.0
    assert found["expected_solar_change_percent"] == 60.0
    assert not found["early"]


def test_only_days_already_over_count_and_a_few_are_shown_as_early():
    upgrade_day = date(2026, 11, 20)
    rows = days_with(upgrade_day - timedelta(days=30), 60, 3.0)
    found = upgrade.compare(rows, upgrade_day, upgrade_day + timedelta(days=5), 30, 51.0)
    assert found["after"]["days"] == 4  # the 21st to the 24th: today is not over
    assert found["early"]
    nothing_yet = upgrade.compare(rows, upgrade_day, upgrade_day + timedelta(days=1), 30, 51.0)
    assert nothing_yet["after"] is None and nothing_yet["before"]["days"] == 30


def counters_for_days(now: datetime, days: int) -> None:
    names = {
        "pv_energy_total": 0.4,
        "load_energy_total": 1.0,
        "import_energy_total": 0.7,
        "export_energy_total": 0.1,
        "battery_charge_energy_total": 0.2,
        "battery_discharge_energy_total": 0.18,
    }
    rows = []
    for minutes in range(days * 24 * 60, 0, -10):
        when = now - timedelta(minutes=minutes)
        for name, rate in names.items():
            rows.append((when, name, 1000 + rate * (days * 24 * 60 - minutes) / 60))
    api.database().insert_readings(rows)


def test_the_upgrade_page_waits_for_a_day_then_compares():
    assert client.get("/api/upgrade").json() == {"settings": {}, "has_data": False}
    now = datetime.now(UTC).replace(microsecond=0)
    counters_for_days(now, 12)
    day = (now.astimezone(api.local_timezone()) - timedelta(days=5)).date()
    body = {"day": day.isoformat(), "label": "West panels", "kwp_before": 6, "kwp_after": 9}
    found = client.post("/api/upgrade/settings", json=body).json()
    assert found["has_data"] and found["settings"]["label"] == "West panels"
    assert found["before"]["days"] >= 5 and 3 <= found["after"]["days"] <= 4
    assert found["early"] and found["expected_solar_change_percent"] == 50.0
    assert found["latitude"] == upgrade.DEFAULT_LATITUDE and not found["latitude_known"]
    # With the home zone's latitude known, that is used.
    api.database().set_setting("home_latitude", "50.87")
    api.clear_caches()
    assert client.get("/api/upgrade?days=30").json()["latitude"] == 50.87
    assert client.post("/api/upgrade/settings", json={"kwp_before": 0}).status_code == 422


def test_the_home_zone_latitude_is_kept_when_it_changes(monkeypatch):
    monkeypatch.setattr(collector, "home_latitude", None)
    states = {"zone.home": {"state": "0", "attributes": {"latitude": 50.8712, "longitude": -1.2}}}
    collector.note_home(api.database(), states)
    assert api.database().get_setting("home_latitude") == "50.8712"
    collector.note_home(api.database(), {})  # no home zone: left as it was
    collector.note_home(api.database(), {"zone.home": {"attributes": {"latitude": "x"}}})
    assert api.database().get_setting("home_latitude") == "50.8712"


# --- Supply headroom -------------------------------------------------------------------------


def minute(n: int) -> datetime:
    return datetime(2026, 11, 20, 0, 0, tzinfo=UTC) + timedelta(minutes=n)  # GMT: local = UTC


def test_peaks_are_split_into_what_was_running():
    series = {
        headroom.GRID: [(minute(0), 1.0), (minute(10), 9.0), (minute(11), 8.8), (minute(90), 6.0)],
        headroom.LOAD: [(minute(10), 9.5), (minute(90), 6.0)],
        headroom.SMART: [(minute(10), 9.0), (minute(90), 2.0)],  # heat pump + car
        headroom.EV: [(minute(10), 7.0), (minute(90), 0.0)],
        headroom.BATTERY: [(minute(10), -0.5), (minute(90), 0.0)],
        headroom.SOLAR: [(minute(10), 0.0)],
    }
    found = headroom.analyse(series, LONDON, 23.0, None, ev_on_smart_load=True)
    first, second = found["peaks"]  # minute 11 is part of the minute-10 peak
    assert first["import_kw"] == 9.0 and second["import_kw"] == 6.0
    assert first["heat_pump_kw"] == 2.0 and first["ev_kw"] == 7.0
    assert first["house_kw"] == 0.5 and first["battery_charging_kw"] == 0.0
    assert found["highest_percent"] == 39
    assert found["above"][0] == {"percent": 50, "kw": 11.5, "minutes": 0}
    # Without the car wired through the smart load, all of that circuit is the heat pump.
    alone = headroom.analyse(series, LONDON, None, None, ev_on_smart_load=False)
    assert alone["peaks"][0]["heat_pump_kw"] == 9.0 and alone["above"] is None


def test_the_day_by_hour_and_time_at_the_limits():
    series = {headroom.GRID: []}
    for day in range(3):
        base = day * 24 * 60
        series[headroom.GRID] += [
            (minute(base + 60), 2.0 + day),  # 01:00 local (GMT)
            (minute(base + 61), 1.0),
            (minute(base + 12 * 60), -3.6),  # exporting at a 3.68 kW limit at noon
            (minute(base + 12 * 60 + 1), -2.0),
        ]
    found = headroom.analyse(series, LONDON, 4.0, 3.68, ev_on_smart_load=True)
    one = next(h for h in found["hours"] if h["hour"] == 1)
    assert one == {"hour": 1, "highest_kw": 4.0, "typical_kw": 3.0}
    assert found["days"] == 3
    assert found["above"][-1] == {"percent": 90, "kw": 3.6, "minutes": 1}
    assert found["export"]["minutes_at_limit"] == 3 and found["export"]["days_at_limit"] == 3
    assert found["export"]["highest_kw"] == 3.6


def test_the_busiest_half_hour_from_the_import_counter():
    samples = [
        (minute(0), 100.0),
        (minute(30), 100.5),  # 1 kW
        (minute(45), 103.5),  # 15 minutes: not a half hour, so not counted
        (minute(75), 104.0),
        (minute(105), 106.5),  # 5 kW over the half hour from minute 75
        (minute(165), 112.5),  # an hour: not a half hour
    ]
    assert headroom.busiest_half_hour(samples) == {"import_kw": 5.0, "start": minute(75)}
    assert headroom.busiest_half_hour([]) is None


def test_supply_headroom_from_the_readings():
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    rows = []
    for n in range(240, 0, -1):
        when = now - timedelta(minutes=n)
        rows += [(when, "grid_power", 7.0 if n == 100 else 1.5), (when, "load_power", 2.0)]
    api.database().insert_readings(rows)
    found = client.post(
        "/api/headroom/settings", json={"fuse_amps": 80, "export_limit_kw": 3.68}
    ).json()
    assert found["has_data"] and found["supply_kw"] == 18.4
    assert found["highest_import_kw"] == 7.0 and found["peaks"][0]["house_kw"] == 2.0
    assert found["settings"] == {"fuse_amps": 80.0, "export_limit_kw": 3.68}
    assert client.post("/api/headroom/settings", json={"fuse_amps": 5}).status_code == 422


# --- The overnight charge --------------------------------------------------------------------


def cheap_nights() -> Schedule:
    bands = [
        {"start": "00:00", "end": "06:00", "p_per_kwh": 8},
        {"start": "06:00", "end": "24:00", "p_per_kwh": 26},
    ]
    return Schedule([build_tariff("EV", bands, 15, 50)])


def at(day: date, hour: float) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=LONDON) + timedelta(hours=hour)


def test_days_that_ran_short_or_had_charge_to_spare():
    short_day, spare_day = date(2026, 11, 2), date(2026, 11, 3)
    levels, imports, total = [], [], 0.0
    for step in range(0, 2 * 24 * 4 + 1):  # every 15 minutes for two days
        when = at(short_day, step / 4)
        hours = step / 4 % 24
        if when.date() == short_day:
            # Charged to 90% by 06:00, empty (10%) by 18:00, then buying 1 kW at the dear rate.
            level = 90.0 if hours <= 6 else max(10.0, 90 - (hours - 6) * 8)
        else:
            level = 90.0 if hours <= 6 else 90 - (hours - 6) * 2  # down to 54% at midnight
        if hours < 6:
            level = 54.0 + hours * 6  # charging in the cheap window, from 54% to 90%
        # What was bought in the quarter hour up to this reading.
        before = when - timedelta(minutes=15)
        earlier = before.hour + before.minute / 60
        if earlier < 6:
            total += 2.0
        elif before.date() == short_day and earlier >= 18:
            total += 0.25
        levels.append((when, level))
        imports.append((when, total))
    rows = overnight.nights(
        overnight.Levels(levels),
        None,
        None,
        Counter(imports, timedelta(minutes=75)),
        cheap_nights(),
        LONDON,
        short_day,
        spare_day,
        capacity=20.0,
        reserve=10.0,
        now=at(spare_day + timedelta(days=2), 0),
    )
    short, spare = rows
    assert short["verdict"] == "short" and short["lowest"] == 10.0
    assert short["dear_kwh"] == 6.0  # 18:00 to midnight at 1 kW
    assert short["needed_percent"] == 120.0  # 90% plus 6 kWh of a 20 kWh battery
    assert short["extra_cost_gbp"] == round(6.0 * (26 - 8) / 100, 2)
    assert spare["verdict"] == "spare" and spare["spare_kwh"] == round(0.44 * 20, 1)
    assert spare["needed_percent"] == 90 - 44.0 + 0  # it never used the bottom 44 points
    found = overnight.summarise(rows, 20.0)
    assert found["short_days"] == 1 and found["spare_days"] == 1
    assert found["over_full"] and found["suggested_percent"] == 100


def test_a_day_not_over_yet_or_without_readings_is_left_out():
    day = date(2026, 11, 2)
    levels = overnight.Levels([(at(day, h), 50.0) for h in range(0, 30)])
    imports = Counter([(at(day, 0), 1.0), (at(day, 30), 2.0)], timedelta(hours=40))
    args = (None, None, imports, cheap_nights(), LONDON, day, day + timedelta(days=3))
    assert overnight.nights(levels, *args, 20.0, 5.0, now=at(day, 23)) == []
    found = overnight.nights(levels, *args, 20.0, 5.0, now=at(day + timedelta(days=9), 0))
    assert [row["day"] for row in found] == [day]  # the 3rd has no readings at 06:00


def test_a_window_past_midnight_runs_to_the_next_one_the_same_day():
    bands = [
        {"start": "00:00", "end": "05:30", "p_per_kwh": 8},
        {"start": "05:30", "end": "23:30", "p_per_kwh": 26},
        {"start": "23:30", "end": "24:00", "p_per_kwh": 8},
    ]
    schedule = Schedule([build_tariff("Go", bands, 15, 50)])
    ends, begins, cheap = overnight._window(schedule, date(2026, 11, 2), LONDON)  # noqa: SLF001
    assert ends == at(date(2026, 11, 2), 5.5) and begins == at(date(2026, 11, 2), 23.5)
    assert cheap == 8


def test_the_reserve_is_about_the_lowest_level_seen():
    assert overnight.estimate_reserve([50.0] * 50) is None
    assert overnight.estimate_reserve([0.0] + [10.4] * 300 + [60.0] * 400) == 10.0


def test_overnight_charge_from_the_readings():
    assert client.get("/api/overnight").json()["why"] == "battery"
    client.post("/api/planner/settings", json={"battery_kwh": 27.11})
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    zone = api.local_timezone()
    rows, total = [], 1000.0
    for n in range(6 * 24 * 12, 0, -1):  # every 5 minutes for six days
        when = now - timedelta(minutes=5 * n)
        hours = when.astimezone(zone).hour + when.astimezone(zone).minute / 60
        level = 30 + hours * 10 if hours < 6 else max(8.0, 90 - (hours - 6) * 5)
        total += 0.2 if hours < 6 else 0.01
        rows += [(when, "battery_soc", level), (when, "import_energy_total", total)]
    api.database().insert_readings(rows)
    api.clear_caches()
    found = client.get("/api/overnight").json()
    assert found["has_data"] and found["capacity_kwh"] == 27.11
    assert found["reserve_percent"] == 8.0  # the lowest seen
    assert 4 <= found["summary"]["days"] <= 6
    assert found["days"][0]["day"] > found["days"][-1]["day"]  # newest first
    assert all(88 <= day["charged_to"] <= 90 for day in found["days"])  # read near 06:00
    assert found["months"] == []
    saved = client.post("/api/overnight/settings", json={"reserve_percent": 5}).json()
    assert saved["reserve_percent"] == 5 and saved["estimated_reserve_percent"] == 8.0
    assert client.get("/api/overnight?period=12m").json()["has_data"]


def test_the_new_settings_are_backed_up():
    client.post("/api/upgrade/settings", json={"day": "2026-12-01"})
    client.post("/api/headroom/settings", json={"fuse_amps": 100})
    client.post("/api/overnight/settings", json={"reserve_percent": 10})
    backup = client.get("/api/backup").json()
    assert {"upgrade", "supply", "overnight"} <= set(backup["settings"])
