"""Fill gaps in the readings from Home Assistant's own history.

Home Assistant runs around the clock and keeps about ten days of history for every sensor.
So when this app was not running (PC asleep, VM stopped), the missing readings can be
fetched afterwards instead of being estimated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from .config import Metric
from .db import Database
from .normalise import normalise

log = logging.getLogger("backfill")

Row = tuple[datetime, str, float]

GAP_THRESHOLD = timedelta(minutes=5)  # a break in collection longer than this gets filled
CHUNK = timedelta(hours=6)  # ask Home Assistant for this much history per request
KEEP_ONE_PER = 60  # seconds: thin history to one reading a minute per metric


def find_gaps(
    times: list[datetime], start: datetime, end: datetime, threshold: timedelta = GAP_THRESHOLD
) -> list[tuple[datetime, datetime]]:
    """Stretches of [start, end] with no readings, given the times readings were stored."""
    points = [start, *[t for t in sorted(times) if start < t < end], end]
    return [
        (earlier, later)
        for earlier, later in zip(points, points[1:], strict=False)
        if later - earlier > threshold
    ]


def parse_history(
    payload: list[list[dict]], metrics: list[Metric], start: datetime, end: datetime
) -> list[Row]:
    """Turn a Home Assistant history response into database rows.

    The response is one list per entity. The first item of each carries the entity id and
    unit; with `minimal_response` the rest carry only the state and when it changed.
    """
    by_entity = {m.entity: m for m in metrics}
    rows: dict[tuple[str, int], Row] = {}
    for states in payload:
        if not states:
            continue
        metric = by_entity.get(states[0].get("entity_id"))
        if metric is None:
            continue
        unit = states[0].get("attributes", {}).get("unit_of_measurement")
        for item in states:
            try:
                when = datetime.fromisoformat(item["last_changed"]).astimezone(UTC)
            except (KeyError, ValueError):
                continue
            if not start <= when <= end:
                continue  # outside the gap: already collected
            value = normalise(item.get("state"), unit, metric.kind)
            if value is None:
                continue
            when = when.replace(microsecond=0)
            # Later readings in the same minute replace earlier ones, leaving one per minute.
            rows[(metric.name, int(when.timestamp()) // KEEP_ONE_PER)] = (
                when,
                metric.name,
                value * metric.scale,
            )
    return sorted(rows.values())


def fetch_history(
    client: httpx.Client, entities: list[str], start: datetime, end: datetime
) -> list[list[dict]]:
    response = client.get(
        f"/api/history/period/{start.astimezone(UTC):%Y-%m-%dT%H:%M:%S}+00:00",
        params={
            "end_time": end.astimezone(UTC).isoformat(),
            "filter_entity_id": ",".join(entities),
            "minimal_response": "",
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def fill(
    client: httpx.Client, db: Database, metrics: list[Metric], start: datetime, end: datetime
) -> int:
    """Fetch and store history for one gap. Returns the number of readings added."""
    entities = [m.entity for m in metrics]
    added = 0
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + CHUNK)
        rows = parse_history(
            fetch_history(client, entities, cursor, chunk_end), metrics, cursor, chunk_end
        )
        if rows:
            added += db.insert_readings(rows)  # counts only readings that were new
        cursor = chunk_end
    return added


def fill_recent_gaps(
    client: httpx.Client, db: Database, metrics: list[Metric], days: int, now: datetime
) -> int:
    """Find every gap in the last `days` days and fill it from Home Assistant."""
    window_start = now - timedelta(days=days)
    # Every minute in which at least one reading of any metric was stored.
    times = db.minutes_with_readings([m.name for m in metrics], window_start)
    added = 0
    for gap_start, gap_end in find_gaps(times, window_start, now):
        count = fill(client, db, metrics, gap_start, gap_end)
        log.info(
            "Filled %s to %s from Home Assistant history: %d readings",
            f"{gap_start:%d %b %H:%M}",
            f"{gap_end:%d %b %H:%M}",
            count,
        )
        added += count
    return added
