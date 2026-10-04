from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from energy_tracker.import_history import (
    EXPORT,
    IMPORT,
    LOAD,
    build_rows,
    first_local_midnight,
    parse_statistics,
    synthesise,
)
from energy_tracker.tariff import Schedule, build_tariff
from energy_tracker.today import cost_since

LONDON = ZoneInfo("Europe/London")


def day(year, month, number, totals):
    """One row of a daily export."""
    start = datetime(year, month, number, tzinfo=LONDON)
    return (start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC), totals)


BANDS = [
    {"start": "00:00", "end": "06:00", "p_per_kwh": 10.0},
    {"start": "06:00", "end": "24:00", "p_per_kwh": 30.0},
]


def test_parse_statistics_stamps_each_value_at_the_end_of_its_hour():
    start = datetime(2026, 4, 9, 7, tzinfo=UTC)
    result = {
        "sensor.a": [
            {"start": start.timestamp() * 1000, "state": 4466.97},
            {"start": "2026-04-09T08:00:00+00:00", "state": 4467.5},
            {"start": (start + timedelta(hours=2)).timestamp() * 1000, "state": None},
        ],
        "sensor.empty": [],
    }
    assert parse_statistics(result) == {
        "sensor.a": [(start + timedelta(hours=1), 4466.97), (start + timedelta(hours=2), 4467.5)]
    }


def test_first_local_midnight():
    assert first_local_midnight(datetime(2026, 4, 9, 7, tzinfo=UTC), LONDON) == datetime(
        2026, 4, 10, tzinfo=LONDON
    )
    exactly = datetime(2026, 4, 10, tzinfo=LONDON)
    assert first_local_midnight(exactly, LONDON) == exactly


def test_synthesised_days_join_the_later_readings_and_price_correctly():
    cutoff = datetime(2026, 1, 3, tzinfo=LONDON)
    days = [
        day(2026, 1, 1, {IMPORT: 20.0, LOAD: 18.0, EXPORT: 1.0}),
        day(2026, 1, 2, {IMPORT: 10.0, LOAD: 12.0, EXPORT: 3.0}),
        day(2026, 1, 3, {IMPORT: 99.0, LOAD: 99.0, EXPORT: 99.0}),
    ]  # on/after the cutoff: must be ignored
    anchors = {IMPORT: 130.0, LOAD: 230.0, EXPORT: 54.0}

    rows = synthesise(days, cutoff, anchors, LONDON, offpeak_share=0.9, cheap_until=time(6))

    imports = sorted((t, v) for t, m, v in rows if m == IMPORT)
    assert [round(v, 6) for _, v in imports] == [100.0, 118.0, 120.0, 129.0, 130.0]
    assert imports[1][0] == datetime(2026, 1, 1, 6, tzinfo=LONDON)
    assert sorted(v for _, m, v in rows if m == EXPORT) == [50.0, 51.0, 54.0]
    assert sorted(v for _, m, v in rows if m == LOAD) == [200.0, 218.0, 230.0]

    # Priced through the normal cost code: 90% of each day's import lands in the cheap band.
    samples = {
        IMPORT: imports,
        "export_energy_total": sorted((t, v) for t, m, v in rows if m == EXPORT),
    }
    cost = cost_since(
        samples,
        datetime(2026, 1, 1, tzinfo=LONDON),
        cutoff,
        LONDON,
        Schedule([build_tariff("T", BANDS, 15.0, 0.0)]),
    )
    bands = {b["label"]: b["kwh"] for b in cost["import_bands"]}
    assert bands["00:00–06:00"] == pytest.approx(27.0)
    assert bands["06:00–24:00"] == pytest.approx(3.0)
    assert cost["estimated_hours"] > 0  # daily-only days are flagged as estimated


def test_build_rows_uses_daily_totals_before_the_first_full_day_of_hourly_history():
    first = datetime(2026, 4, 9, 8, tzinfo=LONDON)
    hours = [first + timedelta(hours=n) for n in range(40)]
    stats = {
        "sensor.imp": [(t, 1000.0 + n) for n, t in enumerate(hours)],
        "sensor.load": [(t, 2000.0 + n) for n, t in enumerate(hours)],
        "sensor.exp": [(t, 500.0) for t in hours],
        "sensor.ev": [(t, 70.0 + n) for n, t in enumerate(hours)],
        "sensor.unknown": [(hours[0], 1.0)],
    }
    names = {
        "sensor.imp": IMPORT,
        "sensor.load": LOAD,
        "sensor.exp": EXPORT,
        "sensor.ev": "ev_charger_energy_total",
    }
    solar = "pv_energy_total"
    days = [
        day(2026, 4, 8, {IMPORT: 30.0, LOAD: 25.0, EXPORT: 5.0, solar: 9.0}),
        day(2026, 4, 9, {IMPORT: 40.0, LOAD: 35.0, EXPORT: 2.0, solar: 9.0}),
        day(2026, 4, 10, {IMPORT: 99.0, LOAD: 99.0, EXPORT: 99.0, solar: 9.0}),
    ]

    rows, summary = build_rows(stats, names, days, LONDON, 0.99, time(6))

    cutoff = datetime(2026, 4, 10, tzinfo=LONDON)
    assert summary["cutoff"] == cutoff and summary["sigen_rows"] == 2
    imports = sorted((t, v) for t, m, v in rows if m == IMPORT)
    # Hourly import readings from 9 April are dropped in favour of that day's daily total.
    assert not [t for t, _ in imports if datetime(2026, 4, 9, 6, tzinfo=LONDON) < t < cutoff]
    at_cutoff = dict(imports)[cutoff.astimezone(UTC)]
    assert at_cutoff == 1016.0  # 08:00 + 16 hours
    assert imports[0] == (datetime(2026, 4, 8, tzinfo=LONDON).astimezone(UTC), 1016.0 - 70.0)
    # Counters the daily export does not cover keep all their hourly history.
    assert len([1 for _, m, _ in rows if m == "ev_charger_energy_total"]) == 40
    assert "sensor.unknown" not in {m for _, m, _ in rows}
    # Solar is in the daily file but Home Assistant has no later history to join it to.
    assert summary["sigen_skipped"] == [solar]
    assert solar not in {m for _, m, _ in rows}


def write_export(path, heading, rows):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(heading)
    for row in rows:
        book.active.append(row)
    book.save(path)
    return path


def test_read_daily_export_matches_columns_by_heading(tmp_path):
    from energy_tracker.import_history import read_sigen

    path = write_export(
        tmp_path / "daily.xlsx",
        ["Grid Import (kWh)", "Grid Export", "Load", "Solar Generation", "Something Else"],
        [
            ["2025-10-28", "--", "--", "--", "--", "--"],
            ["2025-10-29", "1.14", "0.05", "8.3", "0.11", "7"],
            ["2025-10-30", "22.25", "0.17", "19.43", "4.62", "7"],
            ["total", "23.39", "0.22", "27.73", "4.73", "14"],
        ],
    )

    kind, intervals = read_sigen(path, LONDON)

    assert kind == "daily"
    assert intervals == [
        day(2025, 10, 29, {IMPORT: 1.14, EXPORT: 0.05, LOAD: 8.3, "pv_energy_total": 0.11}),
        day(2025, 10, 30, {IMPORT: 22.25, EXPORT: 0.17, LOAD: 19.43, "pv_energy_total": 4.62}),
    ]


def test_read_hourly_export_including_the_night_the_clocks_go_forward(tmp_path):
    from energy_tracker.import_history import read_sigen

    path = write_export(
        tmp_path / "hourly.xlsx",
        ["Grid Import (kWh)", "Grid Export (kWh)", "Load Consumption (kWh)"],
        [
            ["2026-03-28 23:00:00", "0.2", "0.0", "0.5"],
            ["2026-03-29 01:00:00", "8.66", "0.0", "-0.01"],  # 00:00 GMT, labelled in BST
            ["2026-03-29 02:00:00", "6.16", "0.0", "0.4"],
            ["total", "15.02", "0.0", "0.89"],
        ],
    )

    kind, intervals = read_sigen(path, LONDON)

    assert kind == "hourly"
    assert [start for start, _, _ in intervals] == [
        datetime(2026, 3, 28, 23, tzinfo=UTC),
        datetime(2026, 3, 29, 0, tzinfo=UTC),
        datetime(2026, 3, 29, 1, tzinfo=UTC),
    ]
    assert all(end - start == timedelta(hours=1) for start, end, _ in intervals)
    assert intervals[1][2] == {IMPORT: 8.66, EXPORT: 0.0, LOAD: 0.0}  # negative clamped


def test_hourly_rows_keep_their_real_times_and_missing_hours_stay_flat():
    cutoff = datetime(2026, 1, 2, tzinfo=LONDON)

    def hour(h, kwh):
        start = datetime(2026, 1, 1, h, tzinfo=UTC)
        return (start, start + timedelta(hours=1), {IMPORT: kwh, EXPORT: 0.0})

    # 5 kWh at 01:00, 1 kWh at 07:00 (day rate), then nothing in the file until 23:00.
    rows = synthesise(
        [hour(1, 5.0), hour(7, 1.0), hour(23, 2.0)], cutoff, {IMPORT: 108.0, EXPORT: 9.0}, LONDON
    )

    imports = sorted((t, v) for t, m, v in rows if m == IMPORT)
    assert imports == [
        (datetime(2026, 1, 1, 1, tzinfo=UTC), 100.0),
        (datetime(2026, 1, 1, 2, tzinfo=UTC), 105.0),
        (datetime(2026, 1, 1, 7, tzinfo=UTC), 105.0),
        (datetime(2026, 1, 1, 8, tzinfo=UTC), 106.0),
        (datetime(2026, 1, 1, 23, tzinfo=UTC), 106.0),
        (datetime(2026, 1, 2, 0, tzinfo=UTC), 108.0),
    ]
    samples = {IMPORT: imports, EXPORT: sorted((t, v) for t, m, v in rows if m == EXPORT)}
    cost = cost_since(
        samples,
        datetime(2026, 1, 1, tzinfo=LONDON),
        cutoff,
        LONDON,
        Schedule([build_tariff("T", BANDS, 15.0, 0.0)]),
    )
    bands = {b["label"]: b["kwh"] for b in cost["import_bands"]}
    assert bands == {"00:00–06:00": pytest.approx(5.0), "06:00–24:00": pytest.approx(3.0)}


def test_monthly_export_is_refused(tmp_path):
    from energy_tracker.import_history import read_sigen

    path = write_export(
        tmp_path / "monthly.xlsx", ["Grid Import", "Grid Export"], [["2025-11", "616.37", "28.48"]]
    )
    with pytest.raises(ValueError, match="monthly"):
        read_sigen(path, LONDON)
