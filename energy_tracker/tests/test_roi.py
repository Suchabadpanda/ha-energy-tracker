from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_tracker.roi import daily_costs, daily_solar_value, payback
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.usage import Counter

LONDON = ZoneInfo("Europe/London")
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]
SCHEDULE = Schedule([build_tariff("Test", BANDS, 15.0, 50.0)])


def steady(start, end, kw):
    out, t = [], start
    while t <= end:
        out.append((t, kw * (t - start).total_seconds() / 3600))
        t += timedelta(minutes=30)
    return out


def test_daily_costs_price_each_day_and_add_the_standing_charge():
    start, end = datetime(2026, 6, 1, tzinfo=LONDON), datetime(2026, 6, 3, tzinfo=LONDON)
    imports = Counter(steady(start, end, 1.0), timedelta(minutes=75))
    exports = Counter(steady(start, end, 0.5), timedelta(minutes=75))

    with_export = daily_costs(imports, exports, start, end, LONDON, SCHEDULE)
    assert list(with_export) == [date(2026, 6, 1), date(2026, 6, 2)]
    # 6 kWh at 10p + 18 kWh at 30p + 50p standing - 12 kWh exported at 15p
    assert with_export[date(2026, 6, 1)] == pytest.approx(0.60 + 5.40 + 0.50 - 1.80)
    assert daily_costs(imports, None, start, end, LONDON, SCHEDULE)[
        date(2026, 6, 2)
    ] == pytest.approx(6.50)


def test_payback_with_under_a_year_carries_the_average_forward_and_says_it_is_rough():
    first = date(2026, 1, 11)
    savings = {first + timedelta(days=n): 2.0 for n in range(100)}
    result = payback(savings, 1000.0, date(2026, 1, 1), first + timedelta(days=100))

    assert result["rough"] is True
    assert result["estimated_before_days"] == 10 and result["estimated_before_gbp"] == 20.0
    assert result["saved_gbp"] == 220.0 and result["percent"] == 22.0
    assert result["yearly_gbp"] == 730.0
    # 780 still to save at 2 a day = 390 days after the last reading
    assert result["break_even"] == first + timedelta(days=99 + 390)
    assert result["series"][-1] == (first + timedelta(days=99), 220.0)
    assert result["projection"][0] == result["series"][-1]
    assert result["projection"][-1][1] >= 1000.0


def test_payback_with_a_full_year_repeats_the_seasons():
    first = date(2025, 1, 1)
    # Summer half saves 4 a day, winter half 1 a day.
    savings = {first + timedelta(days=n): (4.0 if 90 <= n < 272 else 1.0) for n in range(365)}
    result = payback(savings, 1500.0, first, first + timedelta(days=365))

    assert result["rough"] is False
    assert result["yearly_gbp"] == pytest.approx(182 * 4 + 183 * 1)
    assert result["saved_gbp"] == pytest.approx(911.0)
    # The 589 still needed: 90 winter days (90), then summer at 4 a day.
    assert result["break_even"] == first + timedelta(days=365 + 90 + 124)


def test_payback_already_reached_and_never_reached():
    first = date(2026, 1, 1)
    earning = {first + timedelta(days=n): 10.0 for n in range(30)}
    done = payback(earning, 100.0, first, first + timedelta(days=30))
    assert done["already_reached"] and done["break_even"] == first + timedelta(days=9)
    assert done["remaining_gbp"] == -200.0  # 300 saved against a cost of 100
    # The line carries on to twenty years after the install, showing the profit by then.
    assert done["projection"][-1][0] == done["horizon"] == first + timedelta(days=7305)
    assert done["profit_at_horizon_gbp"] == pytest.approx(10 * 7306 - 100)  # both end days count
    short = payback(earning, 100.0, first, first + timedelta(days=30), horizon_years=5)
    assert short["horizon_years"] == 5 and short["profit_at_horizon_gbp"] == pytest.approx(
        10 * 1827 - 100
    )

    losing = {first + timedelta(days=n): -1.0 for n in range(30)}
    never = payback(losing, 100.0, first, first + timedelta(days=30))
    assert never["break_even"] is None and never["years_from_install"] is None
    assert payback({}, 100.0, first, first) == {"has_data": False}


def test_solar_value_is_direct_use_at_the_import_price_plus_export():
    start, end = datetime(2026, 6, 1, tzinfo=LONDON), datetime(2026, 6, 2, tzinfo=LONDON)
    gap = timedelta(minutes=75)
    solar = Counter(steady(start, end, 3.0), gap)  # 3 kW generated all day
    load = Counter(steady(start, end, 1.0), gap)  # the house uses 1 kW
    value = daily_solar_value(solar, load, start, end, LONDON, SCHEDULE, SCHEDULE)
    # 24 kWh used directly (6 at 10p, 18 at 30p) and 48 kWh exported at 15p
    assert value[date(2026, 6, 1)] == pytest.approx(0.60 + 5.40 + 7.20)


def test_ageing_delays_break_even_and_rising_prices_bring_it_forward():
    first = date(2026, 1, 1)
    savings = {first + timedelta(days=n): 4.0 for n in range(100)}
    solar = {day: 3.0 for day in savings}  # panels 3 a day, battery the other 1
    args = (savings, 10000.0, first, first + timedelta(days=100), solar)

    plain = payback(*args)
    assert plain["adjusted"] is None
    assert plain["yearly_solar_gbp"] == 1095.0 and plain["yearly_battery_gbp"] == 365.0

    aged = payback(*args, panel_ageing=0.005, battery_ageing=0.02)
    assert aged["break_even"] == plain["break_even"]  # the plain estimate is unchanged
    assert aged["adjusted"]["break_even"] > plain["break_even"]
    assert aged["adjusted"]["projection"][-1][1] >= 10000.0

    dearer = payback(*args, price_change=0.03)
    assert dearer["adjusted"]["break_even"] < plain["break_even"]

    # Only the battery part ages when only battery ageing is set.
    battery_only = payback(*args, battery_ageing=0.02)
    assert (
        plain["break_even"]
        < battery_only["adjusted"]["break_even"]
        < aged["adjusted"]["break_even"]
    )


def test_savings_that_shrink_away_never_reach_the_cost():
    first = date(2026, 1, 1)
    savings = {first + timedelta(days=n): 1.0 for n in range(50)}
    result = payback(savings, 50000.0, first, first + timedelta(days=50), price_change=-0.10)
    assert result["adjusted"]["break_even"] is None
    # The line is still drawn to the horizon, where the system has not paid for itself.
    assert result["adjusted"]["projection"][-1][0] == result["horizon"]
    assert result["adjusted"]["profit_at_horizon_gbp"] < 0
    # The plain estimate would take over a century: no date, and a loss at the horizon.
    assert result["break_even"] is None and result["projection"][-1][0] == result["horizon"]
    assert result["profit_at_horizon_gbp"] == pytest.approx(7306 - 50000)

    # A break-even after the horizon is still found, and the line runs on to it.
    late = payback(savings, 10000.0, first, first + timedelta(days=50))
    assert late["break_even"] == first + timedelta(days=9999) > late["horizon"]
    assert late["projection"][-1] == (late["break_even"], 10000.0)
    assert late["profit_at_horizon_gbp"] == pytest.approx(7306 - 10000)


def test_billed_line_uses_bill_charges_where_there_are_bills():
    from energy_tracker.roi import billed_line

    first = date(2026, 1, 1)
    days = [first + timedelta(days=n) for n in range(20)]
    otherwise = {d: 5.0 for d in days}  # without the system: £5 a day
    paid = {d: 1.0 for d in days}  # the tracker's net cost: £1.50 import less 50p export
    before_export = {d: 1.5 for d in days}
    bills = [
        # Days 3 to 12: the bill charged £20 over 10 days, £2 a day.
        {
            "first_day": "2026-01-03",
            "last_day": "2026-01-12",
            "charge_gbp": 20.0,
            "export_gbp": None,
        },
        # An export bill for days 6 to 10 paid £10, £2 a day.
        {
            "first_day": "2026-01-06",
            "last_day": "2026-01-10",
            "charge_gbp": None,
            "export_gbp": 10.0,
        },
    ]
    line = billed_line(otherwise, paid, before_export, bills, {first: 3.0}, 7.0)
    assert line["first_billed"] == date(2026, 1, 3) and line["last_billed"] == date(2026, 1, 12)
    assert line["billed_days"] == 10 and line["series"][-1][0] == date(2026, 1, 12)
    # Unbilled days 1-2 save £4 each; billed days with the tracker's 50p export save £3.50,
    # and those with the export bill's £2 save £5. Plus £3 of extra income and £7 before.
    assert line["saved_gbp"] == pytest.approx(7 + 3 + 2 * 4 + 5 * 3.5 + 5 * 5)
    assert line["tracker_saved_gbp"] == pytest.approx(7 + 3 + 12 * 4)
    assert sum(line["savings"].values()) == pytest.approx(line["saved_gbp"] - 7)
    assert list(line["savings"]) == days[:12]  # up to the last billed day
    assert billed_line(otherwise, paid, before_export, bills[1:], {}, 0) is None
    assert billed_line(otherwise, paid, before_export, [], {}, 0) is None
