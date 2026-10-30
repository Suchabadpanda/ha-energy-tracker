"""Collector: polls Home Assistant on a schedule and stores readings in the database.

Runs in the background inside the app (started by api.py).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import httpx

from . import backfill, comfort, heatpump, hotwater, income, live, weather
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
# How the Axle Energy event sensors stood at the last poll: "ok", "unavailable" or "missing".
axle_status = "missing"
# When a poll last went all the way through (None until the first one does).
last_poll_ok: datetime | None = None
# The home zone's latitude as last stored.
home_latitude: float | None = None


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


def note_home(db: Database, states: dict[str, dict]) -> None:
    """Keep the home's latitude, from Home Assistant's home zone, for working out how much
    sun a clear day gives (Before and after an upgrade). Stored only when it changes."""
    global home_latitude
    latitude = states.get("zone.home", {}).get("attributes", {}).get("latitude")
    if not isinstance(latitude, int | float) or not -90 <= latitude <= 90:
        return
    if latitude != home_latitude:
        db.set_setting("home_latitude", str(round(float(latitude), 4)))
        home_latitude = latitude


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
        try:  # events shown while the app was not running
            found = income.backfill_events(
                client,
                db,
                settings.axle_event_entity,
                datetime.now(UTC).replace(microsecond=0),
                max(1, settings.backfill_days),
                settings.timezone,
            )
            if found:
                log.info("Recorded %d earlier Axle export events from history", found)
        except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
            log.debug("No Axle event history: %s", exc)
        last_stored = datetime.now(UTC)
        last_thinned: datetime | None = None
        weather_checked = False
        comfort.load(db)
        rooms_checked: set[str] = set()  # room sensors whose history has been brought in
        tank_checked = False
        modes_checked = False

        while not stop.is_set():
            try:
                now = datetime.now(UTC).replace(microsecond=0)
                if now - last_stored > backfill.GAP_THRESHOLD:
                    fill_gaps(now)  # polling was interrupted
                states = fetch_states(client)
                rows = build_rows(states, metrics, now)
                outdoor = weather.row(states, settings.outdoor_temperature_entity, now)
                if outdoor:
                    rows.append(outdoor)
                if not weather_checked and weather.entity_in_use:
                    weather_checked = True  # once per start: bring in what Home Assistant has
                    try:
                        found = weather.backfill(
                            db,
                            client,
                            settings.ha_websocket_url,
                            settings.ha_token,
                            weather.entity_in_use,
                            now,
                            settings.backfill_days,
                        )
                        if found:
                            log.info("Brought in %d earlier outdoor temperatures", found)
                    except (httpx.HTTPError, sqlite3.Error, ValueError, OSError) as exc:
                        log.info("Could not bring in earlier outdoor temperatures: %s", exc)
                # The hot water cylinder's temperature, if one is named.
                if settings.hot_water_tank_entity:
                    tank = weather.reading(states.get(settings.hot_water_tank_entity))
                    if tank is not None:
                        rows.append((now, hotwater.TANK, tank))
                    if not tank_checked:
                        tank_checked = True
                        try:
                            found = weather.backfill(
                                db,
                                client,
                                settings.ha_websocket_url,
                                settings.ha_token,
                                settings.hot_water_tank_entity,
                                now,
                                settings.backfill_days,
                                metric=hotwater.TANK,
                                detail_first=True,
                            )
                            if found:
                                log.info("Brought in %d earlier tank temperatures", found)
                        except (httpx.HTTPError, sqlite3.Error, ValueError, OSError) as exc:
                            log.info("Could not bring in earlier tank temperatures: %s", exc)
                # What the heat pump is doing (hot water or heating), if those are named.
                mode_entities = (
                    settings.hot_water_mode_entity,
                    settings.heating_mode_entity,
                    settings.flow_temperature_entity,
                )
                if any(mode_entities):
                    rows.extend(heatpump.rows(states, *mode_entities, now))
                    if not modes_checked:
                        modes_checked = True
                        try:
                            found = heatpump.backfill(db, client, *mode_entities, now)
                            if found:
                                log.info("Brought in %d earlier heat pump mode readings", found)
                        except (httpx.HTTPError, sqlite3.Error, ValueError, OSError) as exc:
                            log.info("Could not bring in earlier heat pump modes: %s", exc)
                # The rooms' temperatures and humidities, and their history once.
                rows.extend(comfort.rows(states, now))
                for room in comfort.rooms:
                    for entity, prefix, humid in (
                        (room.temperature, comfort.TEMPERATURE, False),
                        (room.humidity, comfort.HUMIDITY, True),
                    ):
                        if not entity or entity in rooms_checked:
                            continue
                        rooms_checked.add(entity)
                        try:
                            found = weather.backfill(
                                db,
                                client,
                                settings.ha_websocket_url,
                                settings.ha_token,
                                entity,
                                now,
                                settings.backfill_days,
                                metric=prefix + entity,
                                humidity=humid,
                            )
                            if found:
                                log.info("Brought in %d earlier readings of %s", found, entity)
                        except (httpx.HTTPError, sqlite3.Error, ValueError, OSError) as exc:
                            log.info("Could not bring in earlier readings of %s: %s", entity, exc)
                note_home(db, states)
                if rows:
                    db.insert_readings(rows)
                    last_stored = now
                    live.note_rows(rows, metrics)
                log.debug("Stored %d of %d readings", len(rows), len(metrics))

                # Note any grid event the Axle sensor is showing, and measure finished ones.
                global axle_status
                event, axle_status = income.event_state(states, settings.axle_event_entity)
                if income.record(db, event, settings.timezone):
                    log.info("Recorded an Axle export event")
                if income.settle(db, now):
                    log.info("Worked out the estimated payment for a finished Axle event")

                # Once a day, thin old readings so the database stays small.
                if last_thinned is None or now - last_thinned > THIN_EVERY:
                    removed = db.thin(
                        [
                            *names,
                            weather.METRIC,
                            hotwater.TANK,
                            *heatpump.METRICS,
                            *comfort.metrics(),
                        ],
                        now - timedelta(days=settings.detail_days),
                    )
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
                global last_poll_ok
                last_poll_ok = now
            except httpx.HTTPStatusError as exc:
                log.error("Home Assistant returned HTTP %s", exc.response.status_code)
            except (httpx.HTTPError, sqlite3.Error) as exc:
                log.error("Collection cycle failed: %s", exc)
            except Exception:  # noqa: BLE001 - anything else must not stop collecting for good
                # A sensor in an unexpected state, say. Logged in full, and tried again next
                # time: the thread carrying on matters more than this one cycle.
                log.exception("Collection cycle failed unexpectedly")
            stop.wait(settings.poll_seconds)

    log.info("Collector stopped")
