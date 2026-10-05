"""Live readings for the "right now" tiles.

The collector stores a reading every poll. Between polls, this listens to Home Assistant
for changes to the power and battery-level sensors and keeps the newest value of each in
memory, so the tiles can follow them within a second or two. Nothing here is stored.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime

from .config import Metric, Settings
from .normalise import normalise

log = logging.getLogger("live")

LIVE_KINDS = {"power", "percent"}
RETRY_SECONDS = 15

# The newest value of each metric: name -> (time, value).
values: dict[str, tuple[datetime, float]] = {}
connected = False
_lock = threading.Lock()


def note(name: str, when: datetime, value: float) -> None:
    with _lock:
        held = values.get(name)
        if held is None or when >= held[0]:
            values[name] = (when, value)


def note_rows(rows: list[tuple[datetime, str, float]], metrics: list[Metric]) -> None:
    """Take in a poll's readings, so tiles are right even for sensors that rarely change."""
    wanted = {m.name for m in metrics if m.kind in LIVE_KINDS}
    for when, name, value in rows:
        if name in wanted:
            note(name, when, value)


def snapshot() -> dict[str, tuple[datetime, float]]:
    with _lock:
        return dict(values)


def take_state(state: dict | None, by_entity: dict[str, Metric]) -> bool:
    """Record one Home Assistant state, if it is a usable reading of a live metric."""
    if not state:
        return False
    metric = by_entity.get(state.get("entity_id", ""))
    if metric is None:
        return False
    unit = state.get("attributes", {}).get("unit_of_measurement")
    value = normalise(state.get("state"), unit, metric.kind)
    if value is None:
        return False
    try:
        when = datetime.fromisoformat(state.get("last_updated") or "").astimezone(UTC)
    except ValueError:
        when = datetime.now(UTC)
    note(metric.name, when, value * metric.scale)
    return True


def listen(settings: Settings, by_entity: dict[str, Metric], stop: threading.Event) -> None:
    """One connection: sign in, ask for changes to the live sensors, and take them in."""
    from websockets.sync.client import connect  # imported here: only needed when collecting

    global connected
    with connect(settings.ha_websocket_url, max_size=None, open_timeout=15) as ws:
        json.loads(ws.recv(timeout=15))  # "auth_required"
        ws.send(json.dumps({"type": "auth", "access_token": settings.ha_token}))
        if json.loads(ws.recv(timeout=15)).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the access token")
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "subscribe_trigger",
                    "trigger": {"platform": "state", "entity_id": sorted(by_entity)},
                }
            )
        )
        connected = True
        log.info("Following %d sensors live", len(by_entity))
        while not stop.is_set():
            try:
                message = json.loads(ws.recv(timeout=5))
            except TimeoutError:
                continue
            if message.get("type") == "result" and not message.get("success", True):
                if message.get("id") != 1:
                    raise RuntimeError(f"Home Assistant refused: {message.get('error')}")
                # Could not watch just these sensors: watch every change and pick them out.
                ws.send(
                    json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"})
                )
            if message.get("type") == "event":
                event = message.get("event", {})
                trigger = event.get("variables", {}).get("trigger", {})
                take_state(
                    trigger.get("to_state") or event.get("data", {}).get("new_state"), by_entity
                )


def run(settings: Settings, metrics: list[Metric], stop: threading.Event) -> None:
    """Follow the live sensors until `stop` is set, reconnecting after any break."""
    global connected
    by_entity = {m.entity: m for m in metrics if m.kind in LIVE_KINDS}
    if not by_entity or not settings.ha_websocket_url:
        return
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            listen(settings, by_entity, stop)
        except Exception as exc:  # noqa: BLE001 - any failure just means trying again
            # Say so once, not every few seconds while Home Assistant is away.
            failures = 1 if time.monotonic() - started > 60 else failures + 1
            level = logging.WARNING if failures == 1 else logging.DEBUG
            log.log(level, "Live connection ended (%s); the tiles follow the stored readings", exc)
        finally:
            connected = False
        stop.wait(RETRY_SECONDS)
