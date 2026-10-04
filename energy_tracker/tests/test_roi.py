from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_tracker.roi import daily_costs, payback
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
    assert done["projection"] == []

    losing = {first + timedelta(days=n): -1.0 for n in range(30)}
    never = payback(losing, 100.0, first, first + timedelta(days=30))
    assert never["break_even"] is None and never["years_from_install"] is None
    assert payback({}, 100.0, first, first) == {"has_data": False}
