from datetime import UTC, datetime, timedelta

from energy_tracker.db import Database

START = datetime(2026, 9, 1, tzinfo=UTC)


def test_duplicate_readings_are_ignored(tmp_path):
    db = Database(tmp_path / "e.db")
    rows = [(START, "a", 1.0), (START + timedelta(minutes=1), "a", 2.0)]
    assert db.insert_readings(rows) == 2
    assert db.insert_readings(rows + [(START, "b", 5.0)]) == 1
    assert db.first_time("a") == START
    assert db.first_time("missing") is None


def test_last_reading_in_each_bucket(tmp_path):
    db = Database(tmp_path / "e.db")
    db.insert_readings([(START + timedelta(minutes=m), "a", float(m)) for m in range(60)])
    found = db.last_in_buckets(["a", "b"], START, START + timedelta(hours=1), 1800)
    assert found["a"] == [
        (START + timedelta(minutes=29), 29.0),
        (START + timedelta(minutes=59), 59.0),
    ]
    assert found["b"] == []


def test_series_averages_and_latest(tmp_path):
    db = Database(tmp_path / "e.db")
    db.insert_readings([(START + timedelta(minutes=m), "a", float(m)) for m in range(10)])
    assert db.series(["a"], START, 300)["a"] == [
        (START, 2.0),
        (START + timedelta(minutes=5), 7.0),
    ]
    assert db.latest(["a"], START)["a"] == (START + timedelta(minutes=9), 9.0)
    assert db.latest(["a"], START + timedelta(hours=1)) == {}
    assert len(db.minutes_with_readings(["a"], START)) == 10


def test_thinning_keeps_one_reading_per_five_minutes_and_only_for_old_data(tmp_path):
    db = Database(tmp_path / "e.db")
    db.insert_readings([(START + timedelta(seconds=10 * n), "a", float(n)) for n in range(360)])
    removed = db.thin(["a"], START + timedelta(minutes=30))
    assert removed == 180 - 6  # the first half hour goes from 180 readings to 6
    kept = db.last_in_buckets(["a"], START, START + timedelta(minutes=29, seconds=59), 1)
    assert [value for _, value in kept["a"]] == [29.0, 59.0, 89.0, 119.0, 149.0, 179.0]
    assert db.thin(["a"], START + timedelta(minutes=30)) == 0  # nothing new to thin
    assert db.delete_before(["a"], START + timedelta(minutes=30)) == 6


def test_readings_past_the_age_limit_are_deleted_for_every_metric(tmp_path):
    db = Database(tmp_path / "e.db")
    db.insert_readings(
        [(START + timedelta(days=d), name, 1.0) for d in range(10) for name in ("a", "b", "gone")]
    )
    assert db.delete_older_than(START + timedelta(days=4)) == 12
    assert db.first_time("a") == db.first_time("gone") == START + timedelta(days=4)
    assert db.delete_older_than(START + timedelta(days=4)) == 0
    assert db.size_bytes() > 0
