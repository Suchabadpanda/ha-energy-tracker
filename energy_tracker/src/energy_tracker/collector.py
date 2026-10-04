"""Collector: polls Home Assistant on a schedule and stores readings in the database.

Runs in the background inside the app (started by api.py).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import httpx

from . import backfill
from .config import Metric, Settings
from .db import Database
from .normalise import normalise

log = logging.getLogger("collector")

Row = tuple[datetime, str, float]

THIN_EVERY = timedelta(hours=24)

# Which sensors Home Assistant does not have, as of the last poll. The dashboard uses this
# to leave out tiles for equipment that is not installed.
missing: set[str] = set()
polled = False


def fetch_states(client: httpx.Client) -> dict[str, dict]:
    """Fetch every entity state from Home Assistant in one request."""
    response = client.get("/api/states")
    response.raise_for_status()
    return {item["entity_id"]: item for item in response.json()}


def build_rows(states: dict[str, dict], metrics: list[Metric], now: datetime) -> list[Row]:
    """Turn a Home Assistant snapshot into database rows, skipping unusable readings."""
    global polled
    rows: list[Row] = []
    for metric in metrics:
        state = states.get(metric.entity)
        if state is None:
            if metric.name not in missing:  # say so once, not at every poll
                log.warning(
                    "Not found in Home Assistant, so left out: %s (%s)", metric.entity, metric.label
                )
                missing.add(metric.name)
            continue
        missing.discard(metric.name)
        unit = state.get("attributes", {}).get("unit_of_measurement")
        value = normalise(state.get("state"), unit, metric.kind)
        if value is None:
            log.debug("Skipping %s (state=%r unit=%r)", metric.name, state.get("state"), unit)
            continue
        rows.append((now, metric.name, value * metric.scale))
    polled = True
    return rows


def run(settings: Settings, db: Database, metrics: list[Metric], stop: threading.Event) -> None:
    """Collect until `stop` is set. A failed cycle is logged and tried again next time."""
    log.info(
        "Collecting %d metrics from %s every %ds",
        len(metrics),
        settings.ha_url,
        settings.poll_seconds,
    )
    names = [m.name for m in metrics]
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    with httpx.Client(base_url=settings.ha_url, headers=headers, timeout=15) as client:
        # Anything missed while this app was not running is fetched from Home Assistant's
        # history: at start-up, and again after any break in polling.
        def fill_gaps(now: datetime) -> None:
            if settings.backfill_days == 0:
                return
            try:
                added = backfill.fill_recent_gaps(client, db, metrics, settings.backfill_days, now)
                if added:
                    log.info("Backfilled %d readings from Home Assistant history", added)
            except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
                log.error("Backfill failed (live collection continues): %s", exc)

        fill_gaps(datetime.now(UTC).replace(microsecond=0))
        last_stored = datetime.now(UTC)
        last_thinned: datetime | None = None

        while not stop.is_set():
            try:
                now = datetime.now(UTC).replace(microsecond=0)
                if now - last_stored > backfill.GAP_THRESHOLD:
                    fill_gaps(now)  # polling was interrupted
                rows = build_rows(fetch_states(client), metrics, now)
                if rows:
                    db.insert_readings(rows)
                    last_stored = now
                log.debug("Stored %d of %d readings", len(rows), len(metrics))

                # Once a day, thin old readings so the database stays small.
                if last_thinned is None or now - last_thinned > THIN_EVERY:
                    removed = db.thin(names, now - timedelta(days=settings.detail_days))
                    # ...and drop whatever has passed the age limit. Run daily, this removes
                    # a day's worth at a time.
                    expired = db.delete_older_than(now - timedelta(days=365 * settings.keep_years))
                    last_thinned = now
                    if expired:
                        log.info(
                            "Deleted %d readings older than %d years", expired, settings.keep_years
                        )
                    if removed:
                        log.info(
                            "Thinned readings older than %d days: removed %d",
                            settings.detail_days,
                            removed,
                        )
            except httpx.HTTPStatusError as exc:
                log.error("Home Assistant returned HTTP %s", exc.response.status_code)
            except (httpx.HTTPError, sqlite3.Error) as exc:
                log.error("Collection cycle failed: %s", exc)
            stop.wait(settings.poll_seconds)

    log.info("Collector stopped")
