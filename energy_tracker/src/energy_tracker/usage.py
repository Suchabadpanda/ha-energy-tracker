"""Turn lifetime energy counters (kWh that only ever go up) into energy used over a period.

Everything here is plain arithmetic on lists of (time, value) samples, so it can be tested
without a database.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta

Sample = tuple[datetime, float]

# Two samples further apart than this mean nothing was collected in between.
GAP = timedelta(minutes=15)
SLOT = timedelta(minutes=30)


def monotonic(samples: list[Sample]) -> list[Sample]:
    """Remove counter resets, so the series only ever rises.

    If a counter drops (device replaced, firmware reset), the drop is ignored and counting
    carries on from the new value.
    """
    cleaned: list[Sample] = []
    offset = 0.0
    previous: float | None = None
    for time, value in samples:
        if previous is not None and value < previous:
            offset += previous - value
        previous = value
        cleaned.append((time, value + offset))
    return cleaned


class Counter:
    """A lifetime kWh counter, as a series of samples, with lookups by time."""

    def __init__(self, samples: list[Sample], gap: timedelta = GAP) -> None:
        # `gap`: samples further apart than this mean nothing was collected in between.
        # It must be longer than the normal spacing of the samples passed in.
        self.gap = gap
        # Work in UTC throughout. Python subtracts two times that share a local time zone by
        # their clock readings, which goes wrong in the hour the clocks go back.
        in_utc = [(t.astimezone(UTC), v) for t, v in samples]
        cleaned = monotonic(sorted(in_utc))
        self.times = [t for t, _ in cleaned]
        self.values = [v for _, v in cleaned]

    def __bool__(self) -> bool:
        return bool(self.times)

    @property
    def first_time(self) -> datetime | None:
        return self.times[0] if self.times else None

    def at(self, when: datetime) -> float | None:
        """Counter value at a moment, interpolating in a straight line between samples.

        Before the first sample or after the last, the nearest sample is used (no energy is
        invented outside the period we have data for). Returns None if there are no samples.
        """
        if not self.times:
            return None
        index = bisect_right(self.times, when)
        if index == 0:
            return self.values[0]
        if index == len(self.times):
            return self.values[-1]
        t0, t1 = self.times[index - 1], self.times[index]
        v0, v1 = self.values[index - 1], self.values[index]
        return v0 + (v1 - v0) * ((when - t0) / (t1 - t0))

    def between(self, start: datetime, end: datetime) -> float | None:
        """kWh used between two moments, or None if there are no samples."""
        a, b = self.at(start), self.at(end)
        if a is None or b is None:
            return None
        return max(0.0, b - a)

    def uncovered(self, start: datetime, end: datetime) -> timedelta:
        """How much of the period has no readings (so its figures are interpolated)."""
        if not self.times:
            return max(timedelta(0), end - start)
        points = [start, *[t for t in self.times if start < t < end], end]
        missing = timedelta(0)
        for earlier, later in zip(points, points[1:], strict=False):
            if later - earlier > self.gap:
                missing += later - earlier
        return missing


def counter_at(samples: list[Sample], when: datetime) -> float | None:
    return Counter(samples).at(when)


def energy_between(samples: list[Sample], start: datetime, end: datetime) -> float | None:
    return Counter(samples).between(start, end)


def uncovered(samples: list[Sample], start: datetime, end: datetime) -> timedelta:
    return Counter(samples).uncovered(start, end)


def half_hour_slots(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split a period into pieces that each sit inside one half-hour settlement slot."""
    slots: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        as_utc = cursor.astimezone(UTC)
        slot_start = as_utc.replace(minute=0 if as_utc.minute < 30 else 30, second=0, microsecond=0)
        piece_end = min(end, slot_start + SLOT)
        slots.append((cursor, piece_end))
        cursor = piece_end
    return slots


def window_start(counters: list[Counter], period_start: datetime) -> datetime | None:
    """Start of the period we can report on: the period start, or later if a counter is newer.

    Returns None if any counter has no samples at all.
    """
    if not all(counters):
        return None
    return max(period_start, *[c.first_time for c in counters])
