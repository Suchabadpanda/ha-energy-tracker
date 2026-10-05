"""Published half-hourly prices, for tariffs whose price changes every half hour.

The prices are fetched from Octopus Energy's public price list and kept in the database, so
a day is always priced at what was published for it. Nothing about the user is sent: only
a tariff code and a region letter.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from . import lookup
from .db import Database

log = logging.getLogger("prices")

REFRESH_SECONDS = 3600
AHEAD = timedelta(days=2)  # tomorrow's prices are published the afternoon before

# Prices by source, held in memory: they are looked up for every half hour of every cost.
_cache: dict[tuple[str, str], dict[int, float]] = {}
# For the dashboard: how each source stood at the last refresh.
status: dict[str, dict] = {}
# Older stretches already asked for, so a tariff that did not exist then is not asked again.
_tried_back_to: dict[str, datetime] = {}


def forget() -> None:
    _cache.clear()


def slot_prices(db: Database, source: str) -> dict[int, float]:
    key = (str(db.path), source)
    if key not in _cache:
        _cache[key] = db.slot_prices(source)
    return _cache[key]


def for_row(db: Database, row: dict) -> dict[int, float] | None:
    """The prices for a stored tariff row, or None if it does not follow published prices."""
    return slot_prices(db, row["dynamic"]) if row.get("dynamic") else None


def sources(db: Database) -> set[str]:
    rows = [*db.tariff_rows(), *db.comparison_rows()]
    return {row["dynamic"] for row in rows if row.get("dynamic")}


def refresh(db: Database, client: httpx.Client, now: datetime, earliest: datetime) -> int:
    """Fetch whatever is missing for every tariff that follows published prices.

    `earliest` is how far back prices are wanted (when readings began). Returns how many
    half hours were stored.
    """
    added = 0
    wanted = sources(db)
    for source in sorted(wanted):
        product, region = source.split("/")
        first, last = db.slot_price_range(source)
        stretches: list[tuple[datetime, datetime]] = []
        if first is None:
            stretches.append((earliest, now + AHEAD))
        else:
            held_from = datetime.fromtimestamp(first, UTC)
            tried = _tried_back_to.get(source)
            if earliest < held_from - timedelta(days=1) and (tried is None or earliest < tried):
                stretches.append((earliest, held_from))
            stretches.append((datetime.fromtimestamp(last, UTC), now + AHEAD))
        try:
            for start, end in stretches:
                added += db.save_slot_prices(
                    source, lookup.fetch_slot_prices(client, product, region, start, end)
                )
            _tried_back_to[source] = earliest
            status[source] = {"checked": now, "error": None}
        except lookup.PriceLookupError as exc:
            log.warning("Could not fetch prices for %s: %s", source, exc)
            status[source] = {"checked": now, "error": str(exc)}
    db.delete_slot_prices_except(wanted)
    if added:
        forget()
    return added


def held(db: Database, source: str) -> tuple[datetime | None, datetime | None]:
    """The start of the first half hour a price is held for, and the end of the last."""
    first, last = db.slot_price_range(source)
    if first is None:
        return None, None
    return (
        datetime.fromtimestamp(first, UTC),
        datetime.fromtimestamp(last, UTC) + timedelta(minutes=30),
    )


def run(
    db: Database,
    stop: threading.Event,
    wake: threading.Event,
    earliest: Callable[[], datetime],
    changed: Callable[[], None],
) -> None:
    """Keep prices up to date until `stop` is set. `wake` asks for a refresh straight away."""
    while not stop.is_set():
        wake.clear()
        try:
            if sources(db):
                with httpx.Client(
                    timeout=30, headers={"User-Agent": "ha-energy-tracker"}
                ) as client:
                    if refresh(db, client, datetime.now(UTC), earliest()):
                        changed()
        except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
            log.error("Price refresh failed: %s", exc)
        wake.wait(REFRESH_SECONDS)
