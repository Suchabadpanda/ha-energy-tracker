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


def test_eta_comes_from_the_design_not_the_reheats_and_scop_estimated_by_month_and_year():
    ideal = heatpump.carnot(55, 5)
    runs = [
        {
            "day": date(2026, 10, 10 + n),
            "kwh": 1.5,
            "heat_kwh": 1.5 * 0.5 * ideal,  # hot water COP of 0.5 x ideal
            "rise_c": 20.0,
            "legionella": False,
            "ideal": ideal,
        }
        for n in range(4)
    ]
    tuned = heatpump.calibrate(runs)
    # The reheats give the hot water COP, but not the heating model's share.
    assert tuned["eta"] == heatpump.DEFAULT_ETA and tuned["eta_source"] == "assumed"
    assert tuned["hot_water_cop"] == pytest.approx(0.5 * ideal, abs=0.01)
    assert tuned["hot_water_cop_source"] == "measured"
    # Too few reheats: the typical hot water COP.
    assert heatpump.calibrate(runs[:2])["hot_water_cop_source"] == "assumed"
    # A cylinder size far too big, say: the typical figure stands, and says why.
    wrong = heatpump.calibrate([{**r, "heat_kwh": r["heat_kwh"] * 4} for r in runs])
    assert wrong["hot_water_cop_source"] == "out of range"
    # From the design SCOP at its flow temperature (an MCS certificate: 3.78 at 50 °C, -3.4 °C).
    design = {"design_scop": 3.78, "design_flow_c": 50, "outdoor_c": -3.4, "indoor_c": 20}
    tuned = heatpump.calibrate(runs, design)
    assert tuned["eta_source"] == "design" and tuned["eta"] == pytest.approx(0.558, abs=0.005)
    # Without the flow temperature the SCOP can't be worked back.
    assert heatpump.calibrate(runs, {"design_scop": 3.78})["eta_source"] == "assumed"
    # A share entered by hand takes precedence.
    tuned = heatpump.calibrate(runs, {**design, "heating_share_pct": 60})
    assert tuned["eta"] == pytest.approx(0.6) and tuned["eta_source"] == "set"
    # A mistyped design SCOP: the typical share stands, and says why.
    tuned = heatpump.calibrate(runs, {**design, "design_scop": 7.9})
    assert tuned["eta_source"] == "out of range" and tuned["eta"] == heatpump.DEFAULT_ETA

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
    found = heatpump.estimate(
        used,
        temperature,
        measured,
        runs,
        {},
        2.5,
        date(2026, 10, 13),
        250,
        design={"heating_share_pct": 50},
    )
    october = found["this_month"]
    assert october["measured_days"] == 4 and october["kwh"] == pytest.approx(48.0)
    assert october["heating_scop"] == pytest.approx(0.5 * heatpump.carnot(40, 5), abs=0.01)
    september = next(m for m in found["months"] if m["name"] == "2026-09")
    assert september["measured_days"] == 0 and september["hot_water_kwh"] == pytest.approx(75.0)
    # The fitted curve: flow = 45 - outdoor.
    assert found["curve"]["source"] == "measured" and found["curve"]["at_7"] == pytest.approx(38)
    year = found["this_year"]
    assert year["days"] == 34 and 1 < year["scop"] < 5


def test_a_reheat_above_cop_5_is_used_not_swapped_for_the_pooled_figure():
    # Booster-heater reheats at COP ~3.2, then one without the booster at 5.3 (10 Nov 2026:
    # 21.5 -> 50.5 °C in a 250 litre cylinder from 1.58 kWh). That day's own figure stands.
    def run(day: int, kwh: float, cop: float) -> dict:
        return {
            "day": date(2026, 11, day),
            "kwh": kwh,
            "heat_kwh": kwh * cop,
            "rise_c": 25.0,
            "legionella": False,
            "ideal": heatpump.carnot(45, 8),
        }

    runs = [run(day, 2.4, 3.2) for day in (6, 7, 8, 9)] + [run(10, 1.58, 5.3)]
    measured = {
        date(2026, 11, day): {
            "hot_water_kwh": r["kwh"],
            "heating_kwh": 0.0,
            "other_kwh": 0.0,
            "ideal": 0.0,
            "ideal_kwh": 0.0,
            "pairs": [],
        }
        for day, r in zip((6, 7, 8, 9, 10), runs, strict=True)
    }
    found = heatpump.estimate({}, {}, measured, runs, {}, None, date(2026, 11, 10), 250)
    assert found["hot_water_cop_source"] == "measured"
    today = next(d for d in found["daily"] if d["day"] == date(2026, 11, 10))
    assert today["hot_water_heat_kwh"] / today["hot_water_kwh"] == pytest.approx(5.3, abs=0.01)
    # Above 7 is still not believable: the pooled figure is used instead.
    runs[-1] = run(10, 1.58, 8.0)
    found = heatpump.estimate({}, {}, measured, runs, {}, None, date(2026, 11, 10), 250)
    today = next(d for d in found["daily"] if d["day"] == date(2026, 11, 10))
    assert today["hot_water_heat_kwh"] / today["hot_water_kwh"] == pytest.approx(
        found["hot_water_cop"], abs=0.01
    )


def test_the_hot_water_cop_comes_from_the_latest_reheats_only():
    # Months of booster-heater reheats at COP 2.7, then ten without it at 5.0.
    def run(when: datetime, cop: float) -> dict:
        return {
            "day": when.date(),
            "start": when,
            "kwh": 2.0,
            "heat_kwh": 2.0 * cop,
            "rise_c": 25.0,
            "legionella": False,
            "ideal": 7.0,
        }

    first = datetime(2026, 10, 1, 2, tzinfo=UTC)
    old = [run(first + timedelta(days=n), 2.7) for n in range(30)]
    new = [run(first + timedelta(days=30 + n), 5.0) for n in range(10)]
    tuned = heatpump.calibrate(new[5:] + old + new[:5])  # in any order
    assert tuned["reheats"] == heatpump.RECENT_RUNS
    assert tuned["hot_water_cop"] == pytest.approx(5.0)


def a_frozen_morning(off_at: tuple[int, int] = (5, 30)):
    """11 Nov 2026, every two minutes: the reheat runs 03:46 to 04:38 (tank 26.5 → 50.5 °C,
    1.6 kW), then the readings freeze until SmartThings is reloaded at `off_at` (05:30),
    while the heat pump goes back to heating (1.2 kW from 04:50)."""
    at = lambda h, m: datetime(2026, 11, 11, h, m, tzinfo=UTC)  # noqa: E731
    begun, ended, back = at(3, 46), at(4, 38), at(*off_at)
    modes = {m: [] for m in heatpump.METRICS}
    used, tank, total = [], [], 0.0
    t = at(3, 0)
    while t <= at(6, 30):
        done = min(1.0, max(0.0, (t - begun) / (ended - begun)))
        stale = ended <= t < back
        modes[heatpump.HOT_WATER_MODE].append((t, 1.0 if begun <= t < back else 0.0))
        modes[heatpump.HEATING_MODE].append((t, 0.0 if begun <= t < back else 1.0))
        flow = 35.0 if t < begun else 41.0 if t >= back else 57.0 if stale else 35 + 24 * done
        modes[heatpump.FLOW].append((t, flow))
        modes[heatpump.FLOW_TARGET].append((t, 36.0))
        tank.append((t, 26.5 + 24 * done))
        used.append((t, total))
        kw = 0.7 if t < begun else 1.6 if t < ended else 0.1 if t < at(4, 50) else 1.2
        total += kw * 2 / 60
        t += timedelta(minutes=2)
    return modes, tank, used


def test_a_reheat_whose_readings_froze_ends_where_they_froze():
    at = lambda h, m: datetime(2026, 11, 11, h, m, tzinfo=UTC)  # noqa: E731
    modes, tank, used = a_frozen_morning()
    counter = Counter(used, timedelta(minutes=75))
    outdoor = hotwater.Tank([(at(3, 0), 7.0), (at(6, 30), 7.0)])

    def split(readings: dict) -> dict:
        return heatpump.split(
            heatpump.Steps(readings[heatpump.HOT_WATER_MODE]),
            heatpump.Steps(readings[heatpump.HEATING_MODE]),
            heatpump.Steps(readings[heatpump.FLOW]),
            outdoor,
            counter.between,
            at(3, 0),
            at(6, 30),
            LONDON,
            target=heatpump.Steps(readings[heatpump.FLOW_TARGET]),
        )[date(2026, 11, 11)]

    # As read: hot water "on" for 104 minutes, with 0.8 kWh of heating in it.
    assert split(modes)["hot_water_kwh"] == pytest.approx(1.6 * 52 / 60 + 0.82, abs=0.03)

    hot_water, cylinder = heatpump.Steps(modes[heatpump.HOT_WATER_MODE]), heatpump.Steps(tank)
    flow = heatpump.Steps(modes[heatpump.FLOW])
    assert heatpump.frozen_ends(hot_water, cylinder, flow, at(3, 0), at(6, 30)) == [
        (at(4, 38), at(5, 30))
    ]
    fixed = heatpump.thawed(modes, tank)
    assert heatpump.Steps(fixed[heatpump.HOT_WATER_MODE]).runs(at(3, 0), at(6, 30)) == [
        (at(3, 46), at(4, 38))
    ]
    day = split(fixed)
    assert day["hot_water_kwh"] == pytest.approx(1.6 * 52 / 60, abs=0.03)
    # The rest is heating, not standby, and the frozen 57 °C flow is not taken as heating's.
    assert day["other_kwh"] == pytest.approx(0.0, abs=0.01)
    assert day["heating_kwh"] == pytest.approx(split(modes)["heating_kwh"] + 0.82, abs=0.05)
    assert all(flow_c < 45 for _, flow_c, _ in day["pairs"])
    runs = heatpump.reheats(
        heatpump.Steps(fixed[heatpump.HOT_WATER_MODE]),
        cylinder,
        heatpump.Steps(fixed[heatpump.FLOW]),
        outdoor,
        counter.between,
        250,
        at(3, 0),
        at(6, 30),
        LONDON,
    )
    assert len(runs) == 1 and runs[0]["minutes"] == 52
    assert runs[0]["kwh"] == pytest.approx(1.6 * 52 / 60, abs=0.03)
    assert runs[0]["rise_c"] == pytest.approx(24.0, abs=0.1)


def test_a_reheat_that_ends_normally_or_is_read_sparsely_is_left_alone():
    # Seen to switch off within minutes of the last change (or caught by a five minute
    # watchdog): nothing to put right.
    for off_at in ((4, 38), (4, 42), (4, 46)):
        modes, tank, _ = a_frozen_morning(off_at)
        assert heatpump.thawed(modes, tank) is modes
    # Readings every 15 minutes: the tank's last change is one whole gap before the end.
    _, tank, hot_water, flow, _ = a_day()
    modes = {heatpump.HOT_WATER_MODE: hot_water, heatpump.FLOW: flow}
    assert heatpump.thawed(modes, tank) is modes


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
    assert found["reheats"] >= 3 and found["hot_water_cop_source"] == "measured"
    assert found["eta_source"] == "assumed"  # no design SCOP entered
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


# --- The tank in layers about its sensor -----------------------------------------------------


def test_without_the_sensor_height_the_whole_tank_rises_as_before():
    assert heatpump.reheat_heat(250, 21.5, 50.5) == pytest.approx(250 * 0.001163 * 29)


def test_after_showers_the_hot_top_is_not_counted_again():
    # 10 Nov 2026: the sensor, 40% up, read 21.5 °C, the top still at the last peak of 50.5,
    # mains about 12.3 °C. The whole tank rising 29 °C would be 8.4 kWh, COP 5.4 on 1.55 kWh.
    heat = heatpump.reheat_heat(250, 21.5, 50.5, 0.4, top=50.5, cold=12.3)
    assert heat == pytest.approx(6.3, abs=0.1)
    assert heat / 1.55 == pytest.approx(4.1, abs=0.1)


def test_a_cold_sensor_after_a_big_draw_counts_nearly_the_whole_tank():
    # 9 Nov 2026: the sensor read 13.5 °C, almost the mains: most of the tank was cold.
    heat = heatpump.reheat_heat(250, 13.5, 50.5, 0.4, top=50.5, cold=12.3)
    assert heat == pytest.approx(7.5, abs=0.1)
    assert heat < 250 * 0.001163 * (50.5 - 13.5)


def test_a_reheat_after_standing_losses_only_is_near_the_whole_tank_rise():
    # No water drawn: the sensor has only cooled 50.5 -> 45, so little cold water is assumed.
    whole = 250 * 0.001163 * 5.5
    heat = heatpump.reheat_heat(250, 45.0, 50.5, 0.4, top=50.5, cold=12.0)
    assert 0.8 * whole <= heat <= whole


def test_the_top_is_never_taken_hotter_than_the_reheat_ends():
    # A legionella peak of 60 °C the day before does not make the top hotter than now.
    assert heatpump.reheat_heat(250, 21.5, 50.5, 0.4, top=60.0, cold=12.3) == pytest.approx(
        heatpump.reheat_heat(250, 21.5, 50.5, 0.4, top=50.5, cold=12.3)
    )


def test_mains_water_follows_the_seasons():
    assert heatpump.mains_c(date(2026, 2, 15)) < 9.5
    assert heatpump.mains_c(date(2026, 8, 8)) == pytest.approx(17.0, abs=0.1)
    assert heatpump.mains_c(date(2026, 11, 10)) == pytest.approx(12.3, abs=0.3)


def test_a_reheat_is_capped_at_the_heat_pumps_output_and_uses_the_sensor_height():
    used, tank, modes, flow, outdoor = a_day()
    counter = Counter(used, timedelta(minutes=75))
    hot_water, temps = heatpump.Steps(modes), hotwater.Tank(outdoor)

    def found(cylinder):
        return heatpump.reheats(
            hot_water,
            heatpump.Steps(tank),
            heatpump.Steps(flow),
            temps,
            counter.between,
            200,
            DAY,
            DAY + timedelta(days=1),
            LONDON,
            cylinder=cylinder,
        )[0]

    plain = found(None)
    assert not plain["capped"]
    layered = found({"sensor_height_pct": 40, "cold_water_c": 10})
    assert layered["heat_kwh"] <= plain["heat_kwh"]
    minutes = plain["minutes"]
    tight = found({"max_output_kw": 1.0})
    assert tight["capped"]
    assert tight["heat_kwh"] == pytest.approx(minutes / 60, abs=0.02)
    assert tight["rise_c"] == plain["rise_c"]  # the sensor's own rise is kept as it was
