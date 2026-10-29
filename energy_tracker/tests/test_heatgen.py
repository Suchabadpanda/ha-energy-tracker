from datetime import date, timedelta

import pytest

from energy_tracker import heatgen


def day_row(day: date, water: float, heat: float) -> dict:
    return {
        "day": day,
        "source": "modes",
        "hot_water_kwh": water / 3,
        "heating_kwh": heat / 3.5,
        "other_kwh": 0.1,
        "hot_water_heat_kwh": water,
        "heating_heat_kwh": heat,
    }


def weather_year(first: date, days: int) -> tuple[list[dict], dict]:
    """Days whose heating heat is exactly 2 kWh per degree day, with 6 kWh of hot water."""
    rows, temps = [], {}
    for i in range(days):
        day = first + timedelta(days=i)
        outside = 2.0 + (i % 20)  # 2 to 21 °C
        temps[day] = outside
        rows.append(day_row(day, 6.0, 2.0 * max(0.0, 15.5 - outside)))
    return rows, temps


def test_totals_by_day_week_month_and_year():
    today = date(2026, 10, 29)  # a Thursday
    rows = [day_row(today - timedelta(days=i), 5.0, 10.0) for i in range(1, 41)]
    found = heatgen.summary(rows, {}, today)
    assert found["has_data"]
    assert found["yesterday"]["heat_kwh"] == 15.0
    assert found["last_7_days"]["heat_kwh"] == 105.0
    assert found["this_week"]["name"] == "2026-10-26" and found["this_week"]["days"] == 3
    assert found["this_month"]["days"] == 28 and found["this_month"]["heat_kwh"] == 420.0
    assert found["this_year"]["heat_kwh"] == 600.0
    assert len(found["days"]) == heatgen.RECENT_DAYS
    assert found["days"][-1]["cop"] == pytest.approx(15 / (5 / 3 + 10 / 3.5 + 0.1), abs=0.01)
    assert found["prediction"] is None  # no temperatures to fit against


def test_today_is_left_out_and_whole_years_come_from_the_scop_figures():
    today = date(2026, 3, 2)
    rows = [day_row(today - timedelta(days=i), 5.0, 10.0) for i in range(0, 5)]
    whole = [
        {
            "name": "2025",
            "days": 300,
            "kwh": 2000.0,
            "scop": 3.2,
            "hot_water_heat_kwh": 1500.0,
            "heating_heat_kwh": 4900.0,
        },
        {
            "name": "2026",
            "days": 99,
            "kwh": 1.0,
            "scop": 1.0,
            "hot_water_heat_kwh": 1.0,
            "heating_heat_kwh": 1.0,
        },
    ]
    found = heatgen.summary(rows, {}, today, whole_years=whole)
    assert found["days"][-1]["name"] == "2026-03-01"
    years = {y["name"]: y for y in found["years"]}
    assert years["2025"]["heat_kwh"] == 6400.0 and years["2025"]["cop"] == 3.2
    assert years["2026"]["days"] == 4  # this year from the daily figures


def test_the_year_is_predicted_from_heat_against_degree_days():
    first = date(2025, 11, 1)
    today = date(2026, 6, 1)
    rows, temps = weather_year(first, (today - first).days)
    found = heatgen.summary(rows, temps, today)
    pred = found["prediction"]
    assert pred["per_degree_day_kwh"] == pytest.approx(2.0, abs=0.01)
    assert pred["hot_water_kwh_a_day"] == pytest.approx(6.0, abs=0.05)
    # Every remaining day is from an earlier year's reading or a typical month.
    assert pred["known_days"] == (today - date(2026, 1, 1)).days
    assert pred["known_days"] + pred["predicted_days"] == 365
    assert pred["temperature_sources"]["read"] == 0
    expected = pred["so_far_kwh"]
    day = today
    while day.year == 2026:
        outside, _ = heatgen._temperature_for(day, temps)
        expected += 6.0 + 2.0 * max(0.0, 15.5 - outside)
        day += timedelta(days=1)
    assert pred["heat_kwh"] == pytest.approx(expected, abs=2)
    assert pred["low_kwh"] <= pred["heat_kwh"] <= pred["high_kwh"]
    assert pred["high_kwh"] - pred["low_kwh"] < 0.02 * pred["heat_kwh"]  # an exact fit


def test_no_prediction_without_enough_days_or_cold_weather():
    today = date(2026, 7, 20)
    few, temps = weather_year(today - timedelta(days=10), 10)
    assert heatgen.summary(few, temps, today)["prediction"] is None
    # A summer: plenty of days, but none needing heating, so heating can't be predicted.
    warm = [day_row(today - timedelta(days=i), 6.0, 0.0) for i in range(1, 40)]
    summer = {r["day"]: 18.0 for r in warm}
    assert heatgen.summary(warm, summer, today)["prediction"] is None


def test_temperatures_for_days_without_a_reading():
    temps = {date(2025, 12, 10): 3.0}
    assert heatgen._temperature_for(date(2025, 12, 10), temps) == (3.0, "read")
    assert heatgen._temperature_for(date(2026, 12, 10), temps) == (3.0, "earlier year")
    assert heatgen._temperature_for(date(2026, 12, 11), temps) == (heatgen.TYPICAL_C[12], "typical")
    assert heatgen._temperature_for(date(2028, 2, 29), {})[1] == "typical"


def test_the_years_scop_is_predicted_from_heat_and_electricity():
    first = date(2025, 11, 1)
    today = date(2026, 6, 1)
    rows, temps = weather_year(first, (today - first).days)
    found = heatgen.summary(rows, temps, today)
    pred = found["prediction"]
    # Every day's electricity is its heat at fixed COPs (3 for hot water, 3.5 heating)
    # plus 0.1 kWh standby, so the year's SCOP sits between the standby-diluted figures.
    assert pred["electricity_kwh"] > 0
    assert pred["scop"] == pytest.approx(pred["heat_kwh"] / pred["electricity_kwh"], abs=0.01)
    assert 2.8 < pred["scop"] < 3.5
    month = found["months"][-1]
    assert month["hot_water_cop"] == pytest.approx(3.0, abs=0.01)
    assert month["heating_scop"] == pytest.approx(3.5, abs=0.01)
