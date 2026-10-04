"""Extra income on top of the tariff, such as payments for exporting during grid events.

Entries are either typed in, or recorded automatically from an Axle Energy event sensor in
Home Assistant. Axle publishes when an event runs but not what it paid, so an automatic
entry is an estimate: the energy exported during the event times the rate per kWh. It can
be corrected by hand once the real payment is known.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .db import Database
from .usage import Counter

log = logging.getLogger("income")

EXPORT_COUNTER = "export_energy_total"
DEFAULT_RATE_P = 100.0  # pence per kWh exported during an event
SETTLE = timedelta(minutes=5)  # wait this long after an event before measuring it


def parse_event(state: dict | None) -> tuple[datetime, datetime] | None:
    """The start and end of the export event an Axle sensor is showing, if any."""
    attributes = (state or {}).get("attributes") or {}
    if str(attributes.get("import_export", "")).lower() != "export":
        return None
    try:
        start = datetime.fromisoformat(str(attributes["start_time"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(attributes["end_time"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None or not start < end <= start + timedelta(hours=12):
        return None
    return start.astimezone(UTC), end.astimezone(UTC)


def rate_p(db: Database) -> float:
    stored = db.get_setting("axle_rate_p")
    return float(stored) if stored else DEFAULT_RATE_P


def record(db: Database, state: dict | None, timezone: ZoneInfo) -> bool:
    """Note the event the sensor is showing. Returns True if it was new."""
    event = parse_event(state)
    if not event:
        return False
    return db.add_event(*event, event[0].astimezone(timezone).date())


def settle(db: Database, now: datetime) -> int:
    """Work out the estimated payment for every event that has finished. Returns how many."""
    done = 0
    for row in db.unsettled_events(now - SETTLE):
        start, end = row["event_start"], row["event_end"]
        samples = db.last_in_buckets(
            [EXPORT_COUNTER], start - timedelta(hours=1), end + timedelta(hours=1), 60
        )[EXPORT_COUNTER]
        counter = Counter(samples)
        # Readings must reach both ends of the event, or the figure would be too low.
        if not counter or counter.first_time > start or counter.times[-1] < end:
            if now - end > timedelta(days=12):  # Home Assistant's history will not fill it now
                db.settle_event(row["id"], None, 0.0)
            continue
        exported = counter.between(start, end)
        db.settle_event(row["id"], round(exported, 3), round(exported * rate_p(db) / 100, 2))
        done += 1
    return done


def backfill_events(
    client: httpx.Client,
    db: Database,
    entity: str,
    now: datetime,
    days: int,
    timezone: ZoneInfo,
) -> int:
    """Pick up events shown by the sensor while the app was not running."""
    since = now - timedelta(days=days)
    response = client.get(
        f"/api/history/period/{since:%Y-%m-%dT%H:%M:%S}+00:00",
        params={"end_time": now.isoformat(), "filter_entity_id": entity},
        timeout=60,
    )
    response.raise_for_status()
    added = 0
    for states in response.json():
        for state in states:
            added += record(db, state, timezone)
    return added
