from datetime import UTC, datetime, timedelta

from energy_tracker.backfill import find_gaps, parse_history
from energy_tracker.config import Metric


def at(hour, minute=0, second=0):
    return datetime(2026, 10, 4, hour, minute, second, tzinfo=UTC)


METRICS = [
    Metric("import_energy_total", "sensor.import", "energy", "Import"),
    Metric("grid_power", "sensor.grid", "power", "Grid"),
]


def test_find_gaps_includes_before_the_first_and_after_the_last_reading():
    times = [at(2), at(2, 1), at(2, 2), at(9), at(9, 1)]
    assert find_gaps(times, at(0), at(9, 30)) == [
        (at(0), at(2)),
        (at(2, 2), at(9)),
        (at(9, 1), at(9, 30)),
    ]
    assert find_gaps([], at(0), at(1)) == [(at(0), at(1))]
    assert find_gaps([at(0, 2), at(0, 4)], at(0), at(0, 6)) == []


def test_parse_history_converts_units_and_keeps_one_reading_a_minute():
    payload = [
        [
            {
                "entity_id": "sensor.import",
                "state": "9.9",
                "last_changed": "2026-10-04T02:00:00+00:00",
                "attributes": {"unit_of_measurement": "MWh"},
            },
            {"state": "9.91", "last_changed": "2026-10-04T02:00:20.5+00:00"},
            {"state": "9.92", "last_changed": "2026-10-04T02:00:50+00:00"},
            {"state": "unavailable", "last_changed": "2026-10-04T02:01:10+00:00"},
            {"state": "9.95", "last_changed": "2026-10-04T03:01:00+01:00"},
            {"state": "9.99", "last_changed": "2026-10-04T08:00:00+00:00"},
        ],
        [
            {
                "entity_id": "sensor.grid",
                "state": "13500",
                "last_changed": "2026-10-04T02:30:00+00:00",
                "attributes": {"unit_of_measurement": "W"},
            },
        ],
        [
            {
                "entity_id": "sensor.something_else",
                "state": "1",
                "last_changed": "2026-10-04T02:30:00+00:00",
            }
        ],
        [],
    ]

    rows = parse_history(payload, METRICS, at(2), at(6))

    assert rows == [
        (at(2, 0, 50), "import_energy_total", 9920.0),  # last of the three in 02:00
        (at(2, 1), "import_energy_total", 9950.0),  # 03:01+01:00 is 02:01 UTC
        (at(2, 30), "grid_power", 13.5),
    ]


def test_parse_history_ignores_readings_outside_the_gap():
    payload = [
        [
            {
                "entity_id": "sensor.grid",
                "state": "1",
                "last_changed": "2026-10-04T01:00:00+00:00",
                "attributes": {"unit_of_measurement": "kW"},
            },
            {"state": "2", "last_changed": "2026-10-04T02:30:00+00:00"},
        ]
    ]
    assert parse_history(payload, METRICS, at(2), at(3)) == [(at(2, 30), "grid_power", 2.0)]
    assert at(2) + timedelta(hours=1) == at(3)
