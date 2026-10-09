from datetime import UTC, datetime, timedelta

import pytest

from energy_tracker.usage import (
    counter_at,
    energy_between,
    half_hour_slots,
    monotonic,
    uncovered,
)


def at(hour, minute=0):
    return datetime(2026, 10, 4, hour, minute, tzinfo=UTC)


SAMPLES = [(at(0), 100.0), (at(1), 102.0), (at(2), 102.0), (at(4), 110.0)]


def test_counter_is_interpolated_between_samples():
    assert counter_at(SAMPLES, at(0, 30)) == pytest.approx(101.0)
    assert counter_at(SAMPLES, at(3)) == pytest.approx(106.0)


def test_counter_is_clamped_outside_the_data():
    assert counter_at(SAMPLES, at(0) - timedelta(hours=5)) == 100.0
    assert counter_at(SAMPLES, at(9)) == 110.0
    assert counter_at([], at(1)) is None


def test_energy_between_two_moments():
    assert energy_between(SAMPLES, at(0), at(4)) == pytest.approx(10.0)
    assert energy_between(SAMPLES, at(1), at(2)) == 0.0
    assert energy_between([], at(1), at(2)) is None


def test_counter_reset_is_ignored():
    cleaned = monotonic([(at(0), 500.0), (at(1), 505.0), (at(2), 1.0), (at(3), 4.0)])
    assert [v for _, v in cleaned] == [500.0, 505.0, 505.0, 508.0]


def test_half_hour_slots_split_on_settlement_boundaries():
    slots = list(half_hour_slots(at(5, 50), at(7, 10)))
    assert slots == [
        (at(5, 50), at(6, 0)),
        (at(6, 0), at(6, 30)),
        (at(6, 30), at(7, 0)),
        (at(7, 0), at(7, 10)),
    ]


def test_uncovered_counts_only_long_gaps():
    samples = [(at(0), 0.0), (at(0, 10), 1.0), (at(3), 2.0), (at(3, 5), 3.0)]
    assert uncovered(samples, at(0), at(3, 5)) == timedelta(hours=2, minutes=50)
    assert uncovered([], at(0), at(2)) == timedelta(hours=2)


def test_clock_change_night_does_not_confuse_the_counter():
    # 25 Oct 2026: UK clocks go back, so 01:00-02:00 local happens twice.
    from zoneinfo import ZoneInfo

    from energy_tracker.usage import Counter

    london = ZoneInfo("Europe/London")
    start = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    samples = [
        ((start + timedelta(minutes=30 * n)).astimezone(london), 100.0 + n) for n in range(10)
    ]
    counter = Counter(samples)
    assert counter.between(start, start + timedelta(hours=4.5)) == pytest.approx(9.0)
    assert counter.at(start + timedelta(hours=2, minutes=15)) == pytest.approx(104.5)
