"""Outdoor temperature, for comparing the heat pump's use with the weather.

Read from Home Assistant: a temperature sensor if one is named in the options, otherwise
the first weather entity (Home Assistant sets one up for its home location by default).
Stored in °C like any other reading.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx

from .db import Database

log = logging.getLogger("weather")

METRIC = "outdoor_temperature"
# The entity being read, for the dashboard to show. None until the first poll finds one.
entity_in_use: str | None = None


def pick_entity(states: dict[str, dict], chosen: str) -> str | None:
    """The entity to read: the one named in the options, or else the first weather entity."""
    if chosen:
        return chosen if chosen in states else None
    weather = sorted(name for name in states if name.startswith("weather."))
    return weather[0] if weather else None


def celsius(value: object, unit: str | None) -> float | None:
    """A temperature in °C, or None if it is not a believable reading."""
    try:
        degrees = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if unit in ("°F", "F"):
        degrees = (degrees - 32) * 5 / 9
    elif unit not in (None, "°C", "C"):
        return None
    return round(degrees, 2) if -60 <= degrees <= 60 else None


def reading(state: dict | None) -> float | None:
    """The temperature in a Home Assistant state, from a weather entity or a sensor."""
    if not state:
        return None
    attributes = state.get("attributes", {})
    if state.get("entity_id", "").startswith("weather."):
        return celsius(attributes.get("temperature"), attributes.get("temperature_unit"))
    return celsius(state.get("state"), attributes.get("unit_of_measurement"))


def row(states: dict[str, dict], chosen: str, now: datetime) -> tuple | None:
    """A database row for the temperature now, if there is one to read."""
    global entity_in_use
    entity = pick_entity(states, chosen)
    entity_in_use = entity
    value = reading(states.get(entity)) if entity else None
    return (now, METRIC, value) if value is not None else None


def statistics(url: str, token: str, entity: str) -> list[tuple[datetime, float]]:
    """Hourly mean temperatures Home Assistant has kept for a sensor, oldest first."""
    from websockets.sync.client import connect  # only needed for this one-off fetch

    with connect(url, max_size=None, open_timeout=15) as ws:
        json.loads(ws.recv(timeout=15))  # "auth_required"
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=15)).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the access token")
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/statistics_during_period",
                    "start_time": "2000-01-01T00:00:00+00:00",
                    "statistic_ids": [entity],
                    "period": "hour",
                    "types": ["mean"],
                    "units": {"temperature": "°C"},
                }
            )
        )
        while True:
            reply = json.loads(ws.recv(timeout=120))
            if reply.get("id") == 1 and reply.get("type") == "result":
                break
    found = []
    for item in (reply.get("result") or {}).get(entity, []):
        value = celsius(item.get("mean"), "°C")
        when = item.get("start")
        if value is None or when is None:
            continue
        moment = (
            datetime.fromtimestamp(when / 1000, UTC)
            if isinstance(when, int | float)
            else datetime.fromisoformat(when).astimezone(UTC)
        )
        found.append((moment, value))
    return found


def recent_history(
    client: httpx.Client, entity: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Temperatures in Home Assistant's recent history (about ten days, by default)."""
    response = client.get(
        f"/api/history/period/{start.astimezone(UTC):%Y-%m-%dT%H:%M:%S}+00:00",
        params={"end_time": end.astimezone(UTC).isoformat(), "filter_entity_id": entity},
        timeout=120,
    )
    response.raise_for_status()
    found = []
    for series in response.json():
        for state in series:
            value = reading({**state, "entity_id": entity})
            when = state.get("last_updated") or state.get("last_changed")
            if value is not None and when:
                found.append((datetime.fromisoformat(when).astimezone(UTC), value))
    return found


def backfill(
    db: Database,
    client: httpx.Client,
    websocket_url: str,
    token: str,
    entity: str,
    now: datetime,
    days: int,
) -> int:
    """Bring in the temperatures Home Assistant already has, once for each entity.

    A sensor's long-term statistics can go back years; a weather entity only has recent
    history. Returns how many readings were stored.
    """
    done = f"weather_backfilled:{entity}"
    if db.get_setting(done):
        return 0
    found: list[tuple[datetime, float]] = []
    if not entity.startswith("weather."):
        try:
            found = statistics(websocket_url, token, entity)
        except Exception as exc:  # noqa: BLE001 - recent history is still worth having
            log.info("No long-term temperature statistics for %s (%s)", entity, exc)
    if not found and days:
        found = recent_history(client, entity, now - timedelta(days=days), now)
    added = db.insert_readings((when, METRIC, value) for when, value in found)
    db.set_setting(done, now.isoformat())
    return added
