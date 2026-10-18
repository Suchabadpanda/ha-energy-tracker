"""The heat pump's modes: an exact split, its reheats and an estimated SCOP."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, devices, heatpump, hotwater, weather
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 12, tzinfo=LONDON)
client = TestClient(api.app)


def test_modes_and_flow_are_read_from_home_assistant_states():
    assert heatpump.mode({"state": "heat_pump"}) == 1.0
    assert heatpump.mode({"state": "off"}) == 0.0
    assert heatpump.mode({"state": "unavailable"}) is None
    assert heatpump.mode({"state": "heat"}, heating=True) == 1.0
    assert heatpump.mode({"state": "off"}, heating=True) == 0.0
    climate = {"state": "heat", "attributes": {"current_temperature": 38.5, "temperature": 45}}
    states = {
        "water_heater.heat_pump": {"state": "off"},
        "climate.heat_pump_indoor1": climate,
    }
    now = datetime(2026, 10, 18, tzinfo=UTC)
    rows = heatpump.rows(states, "water_heater.heat_pump", "climate.heat_pump_indoor1", "", now)
    assert sorted(rows) == sorted(
        [
            (now, heatpump.HOT_WATER_MODE, 0.0),
            (now, heatpump.HEATING_MODE, 1.0),
            (now, heatpump.FLOW, 38.5),
        ]
    )


def test_changes_from_history_are_filled_in_like_polled_readings():
    start = datetime(2026, 10, 12, tzinfo=UTC)
    filled = heatpump.fill_in(
        [(start, 0.0), (start + timedelta(hours=1), 1.0), (start + timedelta(hours=2), None)],
        start + timedelta(hours=3),
    )
    assert len(filled) == 24  # every five minutes for two hours; nothing once unknown
    assert filled[12] == (start + timedelta(hours=1), 1.0)


def test_steps_hold_each_reading_and_find_the_runs():
    start = datetime(2026, 10, 12, tzinfo=UTC)
    steps = heatpump.Steps(
        [(start, 0.0), (start + timedelta(minutes=30), 1.0), (start + timedelta(minutes=90), 0.0)]
    )
    assert steps.mean(start, start + timedelta(hours=1)) == pytest.approx(0.5)
    runs = steps.runs(start, start + timedelta(hours=3))
    assert runs == [(start + timedelta(minutes=30), start + timedelta(minutes=90))]
    # A reading only holds for two hours.
    assert steps.mean(start + timedelta(hours=4), start + timedelta(hours=5)) is None


def a_day():
    """1 kWh an hour; hot water 02:00–03:00 (tank 30 → 50 °C); heating on, flow 40 °C."""
    used, tank, modes, flow, outdoor = [], [], [], [], []
    total, temp = 0.0, 45.0
    t = DAY - timedelta(hours=1)
    while t <= DAY + timedelta(days=1, hours=1):
        local = t.astimezone(LONDON)
        on = local.date() == DAY.date() and 2 <= local.hour < 3
        used.append((t, total))
        modes.append((t, 1.0 if on else 0.0))
        flow.append((t, 55.0 if on else 40.0))
        outdoor.append((t, 5.0))
        if local.date() == DAY.date() and local.hour == 2 and local.minute == 0:
            temp = 30.0
        tank.append((t, temp))
        temp += 5.0 if on else -0.01  # every 15 minutes while reheating
        total += 0.25
        t += timedelta(minutes=15)
    return used, tank, modes, flow, outdoor


def test_split_by_mode_and_the_reheat_it_finds():
    used, tank, modes, flow, outdoor = a_day()
    counter = Counter(used, timedelta(minutes=75))
    hot_water, temps = heatpump.Steps(modes), hotwater.Tank(outdoor)
    heat_on = heatpump.Steps([(t, 1.0) for t, _ in modes])
    found = heatpump.split(
        hot_water,
        heat_on,
        heatpump.Steps(flow),
        temps,
        counter.between,
        DAY,
        DAY + timedelta(days=1),
        LONDON,
    )
    day = found[DAY.date()]
    assert day["hot_water_kwh"] == pytest.approx(1.0)
    assert day["heating_kwh"] == pytest.approx(23.0)
    assert day["ideal_kwh"] == pytest.approx(23.0)
    assert day["ideal"] / day["ideal_kwh"] == pytest.approx(heatpump.carnot(40, 5))

    runs = heatpump.reheats(
        hot_water,
        heatpump.Steps(tank),
        heatpump.Steps(flow),
        temps,
        counter.between,
        200,
        DAY,
        DAY + timedelta(days=1),
        LONDON,
    )
    assert len(runs) == 1
    run = runs[0]
    assert run["kwh"] == pytest.approx(1.0)
    assert run["rise_c"] == pytest.approx(20.0, abs=0.1)
    assert run["heat_kwh"] == pytest.approx(200 * 0.001163 * 20, abs=0.05)
    assert not run["legionella"]


def test_eta_is_calibrated_from_reheats_and_scop_estimated_by_month_and_year():
    ideal = heatpump.carnot(55, 5)
    runs = [
        {
            "day": date(2026, 10, 10 + n),
            "kwh": 1.5,
            "heat_kwh": 1.5 * 0.5 * ideal,  # eta of 0.5
            "rise_c": 20.0,
            "legionella": False,
            "ideal": ideal,
        }
        for n in range(4)
    ]
    tuned = heatpump.calibrate(runs)
    assert tuned["eta"] == pytest.approx(0.5) and tuned["eta_source"] == "measured"
    assert tuned["hot_water_cop"] == pytest.approx(0.5 * ideal, abs=0.01)
    # Too few reheats: typical figures.
    assert heatpump.calibrate(runs[:2])["eta_source"] == "assumed"
    # A cylinder size far too big, say: the typical figures stand, and say why.
    wrong = heatpump.calibrate([{**r, "heat_kwh": r["heat_kwh"] * 4} for r in runs])
    assert wrong["eta_source"] == "out of range" and wrong["eta"] == heatpump.DEFAULT_ETA
    assert wrong["hot_water_cop_source"] == "out of range"

    measured, used, temperature = {}, {}, {}
    for n in range(4):
        day = date(2026, 10, 10 + n)
        pairs = [(o, 45 - o, 0.5) for o in (2.0, 4.0, 6.0, 8.0, 10.0, 12.0)] * 5
        measured[day] = {
            "hot_water_kwh": 1.5,
            "heating_kwh": 10.0,
            "other_kwh": 0.5,
            "ideal": 10.0 * heatpump.carnot(40, 5),
            "ideal_kwh": 10.0,
            "pairs": pairs,
        }
        temperature[day] = 5.0
    for n in range(30):  # September: earlier days, only the total known
        day = date(2026, 9, 1) + timedelta(days=n)
        used[day], temperature[day] = 3.0, 14.0
    found = heatpump.estimate(used, temperature, measured, runs, {}, 2.5, date(2026, 10, 13), 250)
    october = found["this_month"]
    assert october["measured_days"] == 4 and october["kwh"] == pytest.approx(48.0)
    assert october["heating_scop"] == pytest.approx(0.5 * heatpump.carnot(40, 5), abs=0.01)
    september = next(m for m in found["months"] if m["name"] == "2026-09")
    assert september["measured_days"] == 0 and september["hot_water_kwh"] == pytest.approx(75.0)
    # The fitted curve: flow = 45 - outdoor.
    assert found["curve"]["source"] == "measured" and found["curve"]["at_7"] == pytest.approx(38)
    year = found["this_year"]
    assert year["days"] == 34 and 1 < year["scop"] < 5


def test_heating_endpoint_gives_the_estimated_scop_from_the_modes():
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = (now - timedelta(days=6)).replace(minute=0)
    rows, heat, used, imported, tank = [], 100.0, 1000.0, 800.0, 45.0
    t = start
    while t <= now:
        local = t.astimezone(LONDON)
        on = local.hour == 3
        if local.hour == 3 and local.minute == 0:
            tank = 30.0
        rows += [
            (t, devices.LOAD, used),
            (t, devices.IMPORT, imported),
            (t, devices.HEAT_PUMP, heat),
            (t, heatpump.HOT_WATER_MODE, 1.0 if on else 0.0),
            (t, heatpump.HEATING_MODE, 1.0),
            (t, heatpump.FLOW, 55.0 if on else 38.0),
            (t, hotwater.TANK, tank),
            (t, weather.METRIC, 6.0),
        ]
        tank += 1.0 if on else -0.01
        heat += 0.1  # 1.2 kWh an hour
        used += 0.15
        imported += 0.15
        t += timedelta(minutes=5)
    api.database().insert_readings(rows)
    api.database().set_setting("heating_design", '{"cylinder_litres": 250}')
    api.clear_caches()
    data = client.get("/api/heating").json()
    found = data["efficiency"]
    assert found["has_data"] and found["this_month"] or found["months"]
    assert found["reheats"] >= 3 and found["eta_source"] == "measured"
    latest = found["months"][-1]
    assert latest["measured_days"] >= 4
    assert latest["hot_water_kwh"] == pytest.approx(1.2 * latest["measured_days"], rel=0.15)
    assert latest["scop"] and 1.5 < latest["scop"] < 6
    assert found["yesterday"]["scop"] and found["daily"]

    # The tiles under the costs: today so far, and a stretch of whole days.
    midnight = api.local_midnight(now, LONDON)
    today = api._heat_pump_efficiency(midnight, now)
    if now - midnight > timedelta(hours=3):  # enough of today to go on
        assert today["days"] == 1 and today["kwh"] > 0
    week = api._heat_pump_efficiency(midnight - timedelta(days=4), now)
    assert week["days"] >= 4 and week["scop"] and 1.5 < week["scop"] < 6
