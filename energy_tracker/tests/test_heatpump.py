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
            (now, heatpump.FLOW_TARGET, 45.0),
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

    # Heat generated: the same heat as the SCOP figures, by day, week, month and year.
    generated = data["heat_generated"]
    assert generated["has_data"] and generated["days"] and generated["weeks"] and generated["years"]
    yesterday = generated["yesterday"]
    assert yesterday["heat_kwh"] == pytest.approx(
        yesterday["hot_water_heat_kwh"] + yesterday["heating_heat_kwh"], abs=0.2
    )
    assert generated["prediction"] is None  # under two weeks of figures

    # The tiles under the costs: today so far, and a stretch of whole days.
    midnight = api.local_midnight(now, LONDON)
    today = api._heat_pump_efficiency(midnight, now)
    if now - midnight > timedelta(hours=3):  # enough of today to go on
        assert today["days"] == 1 and today["kwh"] > 0
    week = api._heat_pump_efficiency(midnight - timedelta(days=4), now)
    assert week["days"] >= 4 and week["scop"] and 1.5 < week["scop"] < 6


def test_stale_modes_during_a_catch_up_count_as_hot_water():
    """Water heater "off" and heating off all night, as SmartThings left them; the tank froze
    then jumped. The reheat's electricity should be hot water, not standby or heating."""
    from test_hotwater import NIGHT, REHEAT_KW, frozen_night

    used, tank = frozen_night()
    stamps = [t for t, _ in used]
    off = heatpump.Steps([(t, 0.0) for t in stamps])
    counter = Counter(used, timedelta(minutes=5))
    found = heatpump.split(
        off,
        off,
        heatpump.Steps([]),
        hotwater.Tank([]),
        lambda a, b: counter.between(a, b),
        NIGHT,
        NIGHT + timedelta(hours=5),
        LONDON,
        spells=hotwater.catch_ups(tank),
    )
    day = found[NIGHT.date()]
    assert day["hot_water_kwh"] == pytest.approx(sum(REHEAT_KW.values()) / 4 + 0.025, abs=0.005)
    assert day["heating_kwh"] == pytest.approx(0.0)

    # Heating switched on in the spell: left as heating (it can't be told apart)
    on = heatpump.Steps([(t, 1.0) for t in stamps])
    found = heatpump.split(
        off,
        on,
        heatpump.Steps([]),
        hotwater.Tank([]),
        lambda a, b: counter.between(a, b),
        NIGHT,
        NIGHT + timedelta(hours=5),
        LONDON,
        spells=hotwater.catch_ups(tank),
    )
    assert found[NIGHT.date()]["hot_water_kwh"] == pytest.approx(0.0)


def reheat_morning():
    """2 kWh an hour, read every minute, 04:00–08:00. The heating warms the flow slowly from
    30 °C, a reheat climbs it 43 → 54 °C 06:00–06:22, then it cools back to 40 °C."""
    used, flow, total = [], [], 0.0
    t = DAY + timedelta(hours=4)
    while t <= DAY + timedelta(hours=8):
        minutes = (t - DAY).total_seconds() / 60 - 360
        if minutes < 0:
            temp = 30 + (minutes + 120) * 0.1  # 30 → 42 °C over two hours
        elif minutes <= 22:
            temp = 43 + minutes * 0.5
        else:
            temp = max(40.0, 54 - (minutes - 22) * 0.25)
        used.append((t, total))
        flow.append((t, round(temp * 2) / 2))  # SmartThings reports half degrees
        total += 2 / 60
        t += timedelta(minutes=1)
    return Counter(used, timedelta(minutes=5)), flow


def test_a_flow_climb_past_48_c_is_hot_water_when_the_modes_froze():
    """28 Oct 2026: SmartThings left heating "on" and the water heater "off" through a
    reheat. The flow's climb past 48 °C should count as hot water; the slow warm-up before
    it and the cooling after it stay heating."""
    counter, flow = reheat_morning()
    climbs = heatpump.flow_reheats(heatpump.Steps(flow))
    assert len(climbs) == 1
    start, end, rise = climbs[0]
    assert abs(start - (DAY + timedelta(hours=6))) <= timedelta(minutes=2)
    assert abs(end - (DAY + timedelta(hours=6, minutes=22))) <= timedelta(minutes=1)
    assert rise == pytest.approx(11.0, abs=1.0)  # may start a reading early, at 42 °C

    stamps = [t for t, _ in flow]
    found = heatpump.split(
        heatpump.Steps([(t, 0.0) for t in stamps]),
        heatpump.Steps([(t, 1.0) for t in stamps]),
        heatpump.Steps(flow),
        hotwater.Tank([]),
        lambda a, b: counter.between(a, b),
        DAY + timedelta(hours=4),
        DAY + timedelta(hours=8),
        LONDON,
    )
    day = found[DAY.date()]
    assert day["hot_water_kwh"] == pytest.approx(2 * 22 / 60, abs=0.1)
    assert day["heating_kwh"] == pytest.approx(8 - 2 * 22 / 60, abs=0.1)


def test_flow_that_stays_below_48_c_or_only_cools_is_not_a_reheat():
    start = DAY + timedelta(hours=1)
    warm_up = [(start + timedelta(minutes=n), 20 + n * 0.5) for n in range(55)]  # to 47 °C
    cooling = [(start + timedelta(hours=2, minutes=n), 56 - n * 0.5) for n in range(20)]
    assert heatpump.flow_reheats(heatpump.Steps(warm_up + cooling)) == []


def test_a_flow_climb_the_heating_asked_for_is_not_a_reheat():
    """27 Oct 2026: a plumber set the heating's flow to 50 then 64 °C to test a radiator,
    with the hot water off, and the flow climbed past 48 °C. That is heating, not a reheat.
    On 28 Oct the heating was asking for 43 °C when a reheat climbed the flow past 50 °C."""
    counter, flow = reheat_morning()
    stamps = [t for t, _ in flow]
    asked_for_heat = heatpump.Steps(
        [
            (DAY + timedelta(hours=4), 43.0),
            (DAY + timedelta(hours=5, minutes=58), 50.0),
            (DAY + timedelta(hours=6, minutes=10), 64.0),
        ]
    )
    assert heatpump.flow_reheats(heatpump.Steps(flow), asked_for_heat) == []
    found = heatpump.split(
        heatpump.Steps([(t, 0.0) for t in stamps]),
        heatpump.Steps([(t, 1.0) for t in stamps]),
        heatpump.Steps(flow),
        hotwater.Tank([]),
        lambda a, b: counter.between(a, b),
        DAY + timedelta(hours=4),
        DAY + timedelta(hours=8),
        LONDON,
        target=asked_for_heat,
    )
    assert found[DAY.date()]["hot_water_kwh"] == pytest.approx(0.0)

    weather_compensated = heatpump.Steps([(DAY + timedelta(hours=4), 43.0)])
    assert len(heatpump.flow_reheats(heatpump.Steps(flow), weather_compensated)) == 1


def test_a_climate_entity_also_gives_the_flow_the_heating_asks_for():
    state = {"state": "heat", "attributes": {"current_temperature": 40, "temperature": 44}}
    metrics = {metric: read(state) for _, metric, read in heatpump.sources("", "climate.hp", "")}
    assert metrics[heatpump.FLOW] == 40
    assert metrics[heatpump.FLOW_TARGET] == 44
    assert heatpump.FLOW_TARGET not in {m for _, m, _ in heatpump.sources("", "switch.hp", "")}


def test_hot_water_gets_the_electricity_used_in_its_own_minutes():
    """28 Oct 2026: a reheat 06:00-06:21 drew about 1.5 kW, then 4 kW in its last six
    minutes, while the heating drew 0.5 kW the rest of the half hour. Hot water should get
    what it used, not 21/30 of the half hour's electricity."""
    used, modes, total = [], [], 0.0
    t = DAY + timedelta(hours=6)
    while t <= DAY + timedelta(hours=6, minutes=30):
        minute = (t - DAY - timedelta(hours=6)).total_seconds() / 60
        used.append((t, total))
        modes.append((t, 1.0 if minute < 21 else 0.0))
        total += (1.5 if minute < 15 else 4.0 if minute < 21 else 0.5) / 60
        t += timedelta(minutes=1)
    counter = Counter(used, timedelta(minutes=5))
    stamps = [w for w, _ in modes]
    found = heatpump.split(
        heatpump.Steps(modes),
        heatpump.Steps([(w, 1.0) for w in stamps]),
        heatpump.Steps([]),
        hotwater.Tank([]),
        lambda a, b: counter.between(a, b),
        DAY + timedelta(hours=6),
        DAY + timedelta(hours=6, minutes=30),
        LONDON,
    )
    day = found[DAY.date()]
    reheat = 1.5 * 15 / 60 + 4.0 * 6 / 60  # 0.775 kWh
    assert day["hot_water_kwh"] == pytest.approx(reheat, abs=0.03)
    assert day["heating_kwh"] == pytest.approx(0.5 * 9 / 60, abs=0.03)


def test_used_in_counts_overlapping_spans_once():
    start = DAY
    end = DAY + timedelta(minutes=30)
    spans = [
        (DAY - timedelta(minutes=10), DAY + timedelta(minutes=10)),
        (DAY + timedelta(minutes=5), DAY + timedelta(minutes=20)),
    ]
    minutes = lambda a, b: (b - a).total_seconds() / 60  # noqa: E731
    assert heatpump.used_in(spans, minutes, start, end) == pytest.approx(20)
    assert heatpump.used_in([], minutes, start, end) is None
