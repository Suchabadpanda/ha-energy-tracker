"""Turn lifetime energy counters (kWh that only ever go up) into energy used over a period.

Everything here is plain arithmetic on lists of (time, value) samples, so it can be tested
without a database.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from functools import lru_cache

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
        in_utc = [(t if t.tzinfo is UTC else t.astimezone(UTC), v) for t, v in samples]
        cleaned = monotonic(sorted(in_utc))
        self.times = [t for t, _ in cleaned]
        self.values = [v for _, v in cleaned]
        # The same times as seconds: quicker to search and to work with than datetimes.
        self.stamps = [t.timestamp() for t in self.times]
        # Values already worked out, by moment. Neighbouring half hours share a boundary and
        # most figures walk the same slots, so most lookups are repeats.
        self._known: dict[float, float] = {}

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
        return self.at_stamp(when.timestamp())

    def at_stamp(self, stamp: float) -> float:
        """`at`, for a moment given in seconds since 1970. Needs at least one sample."""
        known = self._known.get(stamp)
        if known is not None:
            return known
        stamps = self.stamps
        index = bisect_right(stamps, stamp)
        if index == 0:
            value = self.values[0]
        elif index == len(stamps):
            value = self.values[-1]
        else:
            s0, s1 = stamps[index - 1], stamps[index]
            v0, v1 = self.values[index - 1], self.values[index]
            value = v0 + (v1 - v0) * ((stamp - s0) / (s1 - s0))
        self._known[stamp] = value
        return value

    def between(self, start: datetime, end: datetime) -> float | None:
        """kWh used between two moments, or None if there are no samples."""
        if not self.times:
            return None
        return max(0.0, self.at_stamp(end.timestamp()) - self.at_stamp(start.timestamp()))

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


def half_hour_slots(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    """Split a period into pieces that each sit inside one half-hour settlement slot."""
    # Many figures walk the same year of slots: work each period out once.
    return _half_hour_slots(start.astimezone(UTC), end.astimezone(UTC))


@lru_cache(maxsize=64)
def _half_hour_slots(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    slots: list[tuple[datetime, datetime]] = []
    seconds = int(SLOT.total_seconds())
    cursor = start
    while cursor < end:
        # The next half-hour boundary, worked out on the clock in seconds: much quicker than
        # converting between time zones for every piece.
        boundary = (int(cursor.timestamp()) // seconds + 1) * seconds
        piece_end = min(end, datetime.fromtimestamp(boundary, UTC))
        slots.append((cursor, piece_end))
        cursor = piece_end
    return tuple(slots)


def window_start(counters: list[Counter], period_start: datetime) -> datetime | None:
    """Start of the period we can report on: the period start, or later if a counter is newer.

    Returns None if any counter has no samples at all.
    """
    if not all(counters):
        return None
    return max(period_start, *[c.first_time for c in counters])
