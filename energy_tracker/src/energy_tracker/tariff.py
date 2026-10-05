"""Electricity tariffs: import rates by time of day, export rate and standing charge.

Rates change over time, so they are kept as a schedule of periods, each with the date it
takes effect. A day is always priced at the rates that applied on that day.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

MINUTES_PER_DAY = 24 * 60
BEGINNING = date(2000, 1, 1)  # "from the start": used for the first period
SLOT_SECONDS = 30 * 60
# How many of a day's cheapest half hours count as its "cheap rate" on half-hourly prices.
CHEAP_SLOTS = 8


@lru_cache(maxsize=512)  # called for every half hour of every cost
def _minutes(clock: str) -> int:
    """'06:30' -> 390. '24:00' is allowed and means the end of the day."""
    try:
        hours, minutes = (int(part) for part in clock.split(":"))
    except ValueError:
        raise ValueError(f"Time '{clock}' must look like 06:30") from None
    total = hours * 60 + minutes
    if not 0 <= total <= MINUTES_PER_DAY or minutes not in (0, 30):
        raise ValueError(f"Time '{clock}' must be on the hour or half hour, 00:00 to 24:00")
    return total


@dataclass(frozen=True)
class Band:
    start: str
    end: str
    p_per_kwh: float

    @property
    def label(self) -> str:
        return f"{self.start}–{self.end}"

    def contains(self, minute_of_day: int) -> bool:
        return _minutes(self.start) <= minute_of_day < _minutes(self.end)


@dataclass(frozen=True)
class Tariff:
    name: str
    import_bands: tuple[Band, ...]
    export_p_per_kwh: float
    standing_charge_p_per_day: float
    effective_from: date = BEGINNING
    # Added to the import price and the standing charge (not to export). Use 0 if the
    # prices entered already include VAT.
    vat_percent: float = 0.0
    # For a tariff whose price changes every half hour: where its prices are published, as
    # "PRODUCT/REGION" (Octopus Energy). The bands above are then only a fallback.
    dynamic: str = ""
    # Published prices in pence per kWh including VAT, by the start of each half hour
    # (seconds since 1970).
    slot_prices: Mapping[int, float] | None = field(default=None, compare=False, repr=False)
    _by_day: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def vat_multiplier(self) -> float:
        return 1 + self.vat_percent / 100

    def published_price(self, when: datetime) -> float | None:
        """The published price for the half hour containing `when`, if there is one."""
        if not self.slot_prices:
            return None
        return self.slot_prices.get(int(when.timestamp()) // SLOT_SECONDS * SLOT_SECONDS)

    def import_price(self, local_time: datetime) -> float:
        """Pence per kWh to import at this moment, including VAT."""
        published = self.published_price(local_time)
        if published is not None:
            return published
        return self.band_at(local_time).p_per_kwh * self.vat_multiplier

    def _published_on(self, local_time: datetime) -> list[float]:
        """That local day's published prices, cheapest first (empty if there are none)."""
        if not self.slot_prices:
            return []
        day = local_time.date()
        if day not in self._by_day:
            midnight = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
            first = int(midnight.timestamp())
            found = (self.slot_prices.get(first + n * SLOT_SECONDS) for n in range(48))
            self._by_day[day] = sorted(p for p in found if p is not None)
        return self._by_day[day]

    def day_rate(self, local_time: datetime) -> float:
        """The price, with VAT, that import is measured against to show what cheaper times
        saved: the dearest band, or the day's average on half-hourly prices."""
        prices = self._published_on(local_time)
        if prices:
            return sum(prices) / len(prices)
        return max(b.p_per_kwh for b in self.import_bands) * self.vat_multiplier

    def cheap_rate(self, local_time: datetime) -> float:
        """The cheapest price that day, with VAT: what charging a battery would cost."""
        prices = self._published_on(local_time)
        if prices:
            cheapest = prices[:CHEAP_SLOTS]
            return sum(cheapest) / len(cheapest)
        return min(b.p_per_kwh for b in self.import_bands) * self.vat_multiplier

    def band_at(self, local_time: datetime) -> Band:
        minute_of_day = local_time.hour * 60 + local_time.minute
        for band in self.import_bands:
            if band.contains(minute_of_day):
                return band
        raise ValueError(f"No import band covers {local_time:%H:%M}")  # unreachable if validated


def build_tariff(
    name: str,
    import_bands: list[dict],
    export_p_per_kwh: float,
    standing_charge_p_per_day: float,
    effective_from: date = BEGINNING,
    vat_percent: float = 0.0,
    dynamic: str = "",
    slot_prices: Mapping[int, float] | None = None,
    **_: object,
) -> Tariff:
    """Create a Tariff, checking that the import bands cover the whole day exactly once."""
    bands = tuple(sorted((Band(**item) for item in import_bands), key=lambda b: _minutes(b.start)))
    if not bands:
        raise ValueError("At least one import band is needed")

    expected = 0
    for band in bands:
        if _minutes(band.start) != expected or _minutes(band.end) <= _minutes(band.start):
            raise ValueError("Import bands must cover 00:00 to 24:00 without gaps or overlaps")
        if band.p_per_kwh < 0:
            raise ValueError("Prices cannot be negative")
        expected = _minutes(band.end)
    if expected != MINUTES_PER_DAY:
        raise ValueError("Import bands must cover 00:00 to 24:00 without gaps or overlaps")
    if export_p_per_kwh < 0 or standing_charge_p_per_day < 0:
        raise ValueError("Prices cannot be negative")
    if not 0 <= vat_percent <= 100:
        raise ValueError("VAT must be between 0 and 100 percent")

    return Tariff(
        name=name,
        import_bands=bands,
        export_p_per_kwh=float(export_p_per_kwh),
        standing_charge_p_per_day=float(standing_charge_p_per_day),
        effective_from=effective_from,
        vat_percent=float(vat_percent),
        dynamic=dynamic or "",
        slot_prices=slot_prices if dynamic else None,
    )


def load_tariff(path: Path) -> Tariff:
    """Read the starting tariff from config/tariff.toml."""
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return build_tariff(
        name=raw.get("name", "Current tariff"),
        import_bands=raw.get("import_band", []),
        export_p_per_kwh=raw.get("export_p_per_kwh", 0.0),
        standing_charge_p_per_day=raw.get("standing_charge_p_per_day", 0.0),
        vat_percent=raw.get("vat_percent", 0.0),
    )


class Schedule:
    """Tariff periods in date order. Looks up the rates that applied on a given day."""

    def __init__(self, periods: list[Tariff]) -> None:
        if not periods:
            raise ValueError("A schedule needs at least one tariff period")
        self.periods = sorted(periods, key=lambda p: p.effective_from)

    def on(self, day: date) -> Tariff:
        current = self.periods[0]  # days before the first period use the first period's rates
        for period in self.periods:
            if period.effective_from <= day:
                current = period
        return current
