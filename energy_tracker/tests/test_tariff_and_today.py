from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from energy_tracker.costs import combine
from energy_tracker.tariff import Schedule, build_tariff, load_tariff
from energy_tracker.today import cost_since, energy_by_device

LONDON = ZoneInfo("Europe/London")
TARIFF = load_tariff(Path("config/tariff.toml"))
SCHEDULE = Schedule([TARIFF])
BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]


def local(hour, minute=0, day=4, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=LONDON)


def steady(start, end, start_value, kw):
    """A counter rising at a constant rate, sampled every 5 minutes."""
    out, t = [], start
    while t <= end:
        out.append((t, start_value + kw * (t - start).total_seconds() / 3600))
        t += timedelta(minutes=5)
    return out


def test_shipped_tariff_file_is_valid():
    assert [b.label for b in TARIFF.import_bands] == ["00:00–06:00", "06:00–24:00"]
    assert TARIFF.band_at(local(5, 59)).p_per_kwh == 7.62
    assert TARIFF.band_at(local(6, 0)).p_per_kwh == 25.39
    assert TARIFF.band_at(local(23, 59)).p_per_kwh == 25.39
    assert TARIFF.vat_percent == 5.0


@pytest.mark.parametrize(
    "bands",
    [
        [
            {"start": "00:00", "end": "06:00", "p_per_kwh": 8},
            {"start": "07:00", "end": "24:00", "p_per_kwh": 27},
        ],
        [
            {"start": "00:00", "end": "07:00", "p_per_kwh": 8},
            {"start": "06:00", "end": "24:00", "p_per_kwh": 27},
        ],
        [{"start": "00:00", "end": "23:00", "p_per_kwh": 8}],
        [
            {"start": "00:00", "end": "06:15", "p_per_kwh": 8},
            {"start": "06:15", "end": "24:00", "p_per_kwh": 27},
        ],
        [{"start": "00:00", "end": "24:00", "p_per_kwh": -1}],
        [],
    ],
)
def test_invalid_bands_are_rejected(bands):
    with pytest.raises(ValueError):
        build_tariff("Bad", bands, 10, 50)


def test_schedule_uses_the_rates_that_applied_on_each_day():
    old = build_tariff("Old", BANDS, 15, 50)
    new = build_tariff("New", BANDS, 12, 60, effective_from=date(2026, 11, 1))
    schedule = Schedule([new, old])
    assert schedule.on(date(2026, 10, 31)).name == "Old"
    assert schedule.on(date(2026, 11, 1)).name == "New"
    assert schedule.on(date(1999, 1, 1)).name == "Old"  # before everything: first period


def test_energy_by_device_for_a_full_day():
    before, now = local(23, 0, day=3), local(12, 0)
    samples = {
        "load_energy_total": steady(before, now, 9000, 3.0),
        "smart_load_energy_total": steady(before, now, 1800, 2.0),
        "ev_charger_energy_total": steady(before, now, 2200, 1.5),
    }

    energy = energy_by_device(samples, now, LONDON, inverter_used_today=36.5)

    assert energy["full_period"] is True
    assert energy["since"] == local(0)
    assert energy["estimated_hours"] == 0
    assert energy["total_kwh"] == 36.5  # the inverter's own figure wins for a full day
    assert energy["ev_charger_kwh"] == pytest.approx(18.0)
    assert energy["heat_pump_kwh"] == pytest.approx(6.0)  # circuit 24 - EV 18
    assert energy["rest_kwh"] == pytest.approx(12.5)  # 36.5 - circuit 24


def test_counter_that_started_today_gives_a_partial_day():
    now = local(16, 0)
    samples = {
        "load_energy_total": steady(local(23, 0, day=3), now, 9000, 1.0),
        "smart_load_energy_total": steady(local(10, 50), now, 1859, 0.5),
        "ev_charger_energy_total": steady(local(23, 0, day=3), now, 2200, 0.0),
    }

    energy = energy_by_device(samples, now, LONDON, inverter_used_today=99)

    assert energy["full_period"] is False
    assert energy["since"] == local(10, 50)
    assert energy["total_kwh"] == pytest.approx(5.167, abs=0.01)  # not the inverter's 99
    assert energy["heat_pump_kwh"] == pytest.approx(2.583, abs=0.01)
    assert energy["rest_kwh"] == pytest.approx(2.583, abs=0.01)


def test_devices_that_are_not_installed_are_left_out():
    before, now = local(23, 0, day=3), local(12, 0)
    load = steady(before, now, 9000, 3.0)
    circuit = steady(before, now, 1800, 2.0)
    ev = steady(before, now, 2200, 1.5)

    neither = energy_by_device({"load_energy_total": load}, now, LONDON)
    assert "heat_pump_kwh" not in neither and "ev_charger_kwh" not in neither
    assert neither["rest_kwh"] == neither["total_kwh"] == pytest.approx(36.0)

    only_smart_load = energy_by_device(
        {"load_energy_total": load, "smart_load_energy_total": circuit}, now, LONDON
    )
    assert only_smart_load["heat_pump_kwh"] == pytest.approx(24.0)
    assert "ev_charger_kwh" not in only_smart_load
    assert only_smart_load["rest_kwh"] == pytest.approx(12.0)

    only_ev = energy_by_device(
        {"load_energy_total": load, "ev_charger_energy_total": ev}, now, LONDON
    )
    assert only_ev["ev_charger_kwh"] == pytest.approx(18.0)
    assert only_ev["rest_kwh"] == pytest.approx(18.0)


def test_ev_charger_on_its_own_circuit_is_not_taken_off_the_smart_load():
    before, now = local(23, 0, day=3), local(12, 0)
    samples = {
        "load_energy_total": steady(before, now, 9000, 4.0),
        "smart_load_energy_total": steady(before, now, 1800, 1.0),
        "ev_charger_energy_total": steady(before, now, 2200, 1.5),
    }
    energy = energy_by_device(samples, now, LONDON, ev_on_smart_load=False)
    assert energy["heat_pump_kwh"] == pytest.approx(12.0)
    assert energy["ev_charger_kwh"] == pytest.approx(18.0)
    assert energy["rest_kwh"] == pytest.approx(48.0 - 12.0 - 18.0)


def test_missing_counter_means_no_figures_rather_than_wrong_ones():
    assert energy_by_device({"load_energy_total": []}, local(12), LONDON) is None
    assert cost_since({}, local(0), local(12), LONDON, SCHEDULE) is None


def test_cost_today_splits_import_by_band_and_adds_standing_charge():
    before, now = local(23, 0, day=3), local(12, 0)
    samples = {
        "import_energy_total": steady(before, now, 9900, 4.0),
        "export_energy_total": steady(before, now, 4300, 0.5),
    }
    schedule = Schedule([build_tariff("Test", BANDS, 15.0, 60.0)])

    cost = cost_since(samples, local(0), now, LONDON, schedule)

    bands = {b["label"]: b for b in cost["import_bands"]}
    assert bands["00:00–06:00"]["kwh"] == pytest.approx(24.0)
    assert bands["06:00–24:00"]["kwh"] == pytest.approx(24.0)
    assert cost["import_gbp"] == pytest.approx(24 * 0.10 + 24 * 0.30)
    assert cost["export_credit_gbp"] == pytest.approx(6 * 0.15)
    assert cost["standing_charge_days"] == 1
    assert cost["standing_charge_gbp"] == 0.60
    assert cost["net_gbp"] == pytest.approx(9.60 - 0.90 + 0.60)
    # All 48 kWh at the 30p day rate would be 14.40; importing 24 of them at 10p saved 4.80.
    assert cost["all_day_rate_gbp"] == pytest.approx(14.40)
    assert cost["saved_gbp"] == pytest.approx(4.80)
    assert cost["full_period"] is True and cost["estimated_hours"] == 0


def test_month_cost_prices_each_day_at_that_days_rates():
    # 1 kW imported flat out from 30 Oct 00:00 to 2 Nov 00:00; rates change on 1 Nov.
    start, end = local(0, day=30), local(0, day=2, month=11)
    samples = {
        "import_energy_total": steady(start - timedelta(hours=1), end, 100, 1.0),
        "export_energy_total": steady(start - timedelta(hours=1), end, 50, 0.0),
    }
    october = build_tariff("October", BANDS, 15.0, 50.0)
    november = build_tariff(
        "November",
        [{"start": "00:00", "end": "24:00", "p_per_kwh": 20.0}],
        12.0,
        70.0,
        effective_from=date(2026, 11, 1),
    )

    cost = cost_since(samples, start, end, LONDON, Schedule([october, november]))

    # 30 and 31 Oct: 6 h at 10p + 18 h at 30p = 600p a day. 1 Nov: 24 h at 20p = 480p.
    assert cost["import_gbp"] == pytest.approx((600 + 600 + 480) / 100)
    # Standing charge for 30 Oct, 31 Oct and 1 Nov. The period ends at midnight, so 2 Nov
    # is not included.
    assert cost["standing_charge_days"] == 3
    assert cost["standing_charge_gbp"] == pytest.approx((50 + 50 + 70) / 100)
    # At the day rate throughout: 2 days x 24 kWh x 30p, then 24 kWh x 20p.
    assert cost["all_day_rate_gbp"] == pytest.approx((720 + 720 + 480) / 100)
    assert cost["saved_gbp"] == pytest.approx((1920 - 1680) / 100)
    assert cost["tariff"] == "November"
    assert {(b["label"], b["p_per_kwh"]) for b in cost["import_bands"]} == {
        ("00:00–06:00", 10.0),
        ("06:00–24:00", 30.0),
        ("00:00–24:00", 20.0),
    }


def test_overnight_gap_is_reported_as_estimated():
    now = local(12, 0)
    series = steady(local(22, 0, day=3), local(23, 0, day=3), 100, 1.0) + steady(
        local(8, 0), now, 110, 1.0
    )
    samples = {"import_energy_total": series, "export_energy_total": series}

    cost = cost_since(samples, local(0), now, LONDON, SCHEDULE)

    assert cost["full_period"] is True
    assert cost["estimated_hours"] == 8.0


def test_period_before_any_readings_has_no_cost():
    samples = {
        "import_energy_total": steady(local(0), local(12), 100, 1.0),
        "export_energy_total": steady(local(0), local(12), 50, 0.0),
    }
    september = (local(0, day=1, month=9), local(0, day=1, month=10))
    assert cost_since(samples, *september, LONDON, SCHEDULE) is None


def test_combine_adds_months_into_a_year():
    schedule = Schedule([build_tariff("Test", BANDS, 15.0, 60.0)])
    samples = {
        "import_energy_total": steady(
            local(0, day=1) - timedelta(hours=1), local(0, day=3), 100, 1.0
        ),
        "export_energy_total": steady(
            local(0, day=1) - timedelta(hours=1), local(0, day=3), 50, 0.5
        ),
    }
    first = cost_since(samples, local(0, day=1), local(0, day=2), LONDON, schedule)
    second = cost_since(samples, local(0, day=2), local(0, day=3), LONDON, schedule)

    total = combine([first, second])

    assert total["import_kwh"] == pytest.approx(48.0)
    assert total["net_gbp"] == pytest.approx(first["net_gbp"] + second["net_gbp"])
    assert total["saved_gbp"] == pytest.approx(2 * 6 * 0.20)  # 6 kWh a day at 10p instead of 30p
    assert total["standing_charge_days"] == 2
    assert total["since"] == local(0, day=1)
    assert total["full_period"] is True
    assert combine([]) is None


def test_vat_is_added_to_import_and_standing_charge_but_not_export():
    before, now = local(23, 0, day=3), local(12, 0)
    samples = {
        "import_energy_total": steady(before, now, 9900, 4.0),
        "export_energy_total": steady(before, now, 4300, 0.5),
    }
    without = cost_since(
        samples, local(0), now, LONDON, Schedule([build_tariff("T", BANDS, 15.0, 60.0)])
    )
    with_vat = cost_since(
        samples,
        local(0),
        now,
        LONDON,
        Schedule([build_tariff("T", BANDS, 15.0, 60.0, vat_percent=5)]),
    )

    assert with_vat["import_gbp"] == pytest.approx(without["import_gbp"] * 1.05)
    assert with_vat["standing_charge_gbp"] == pytest.approx(0.63)
    assert with_vat["all_day_rate_gbp"] == pytest.approx(without["all_day_rate_gbp"] * 1.05)
    assert with_vat["export_credit_gbp"] == without["export_credit_gbp"]
    assert with_vat["vat_percent"] == 5


def test_vat_outside_zero_to_one_hundred_is_rejected():
    with pytest.raises(ValueError, match="VAT"):
        build_tariff("Bad", BANDS, 10, 50, vat_percent=120)
