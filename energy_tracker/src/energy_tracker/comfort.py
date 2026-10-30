"""Room comfort and balance: how warm each room is against a target, and how evenly the
heating reaches them.

Each room is a temperature sensor in Home Assistant (with a humidity sensor if it has one),
chosen on the dashboard and read at every poll like any other sensor. Readings are stored
by entity, so renaming a room keeps its history.

Figures are worked out from hourly averages. "Comfort hours" (07:00 to 22:00 by default)
leave out the night, when the heating is usually set back on purpose; "all day" counts
every hour. Within those hours each room-hour is in the comfort band (the target, give or
take the band), below it or above it. The spread is how far apart the warmest and coldest
rooms were in the same hour: the balance between rooms, whatever the overall level.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .weather import celsius

TEMPERATURE = "room_temperature:"  # + entity id
HUMIDITY = "room_humidity:"
HUMID_PERCENT = 60  # above this, damp and mould become likely
DRY_PERCENT = 40
DEFAULT_TARGET = 21.0
DEFAULT_BAND = 1.0
DEFAULT_HOURS = ("07:00", "22:00")
MAX_ROOMS = 12
ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
SUMMARY_DAYS = 7  # the tiles and the room table cover this many days to yesterday


@dataclass(frozen=True)
class Room:
    key: str  # the temperature entity: what the room's readings are stored under
    name: str
    temperature: str
    humidity: str = ""


# The rooms being read, kept in step with the saved settings (see load).
rooms: list[Room] = []
# Temperature and humidity sensors seen at the last poll, to choose rooms from.
seen: list[dict] = []


def parse(settings: dict) -> list[Room]:
    found = []
    for item in settings.get("rooms", [])[:MAX_ROOMS]:
        entity = item.get("temperature", "")
        if not ENTITY.match(entity):
            continue
        humidity = item.get("humidity") or ""
        found.append(
            Room(
                entity,
                (item.get("name") or entity).strip()[:40],
                entity,
                humidity if ENTITY.match(humidity) else "",
            )
        )
    return found


def load(db) -> list[Room]:
    """Read the rooms from the saved settings, for the collector to follow."""
    global rooms
    try:
        rooms = parse(json.loads(db.get_setting("comfort") or "{}"))
    except ValueError:
        rooms = []
    return rooms


def metrics(chosen: list[Room] | None = None) -> list[str]:
    """Every metric the rooms are stored under."""
    names = []
    for room in rooms if chosen is None else chosen:
        names.append(TEMPERATURE + room.temperature)
        if room.humidity:
            names.append(HUMIDITY + room.humidity)
    return names


def percent(value: object, unit: str | None) -> float | None:
    """A relative humidity, or None if it is not a believable one."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if unit not in (None, "%"):
        return None
    return round(number, 1) if 0 <= number <= 100 else None


def kind_of(state: dict) -> str | None:
    """'temperature' or 'humidity' for a sensor that reads one, else None."""
    attributes = state.get("attributes", {})
    device_class = attributes.get("device_class")
    unit = attributes.get("unit_of_measurement")
    if device_class == "temperature" or unit in ("°C", "°F"):
        return "temperature"
    if device_class == "humidity":
        return "humidity"
    return None


def rows(states: dict[str, dict], now: datetime) -> list[tuple]:
    """Readings for the rooms being followed, and note the sensors there are to choose from."""
    global seen
    found = []
    for entity, state in states.items():
        if not entity.startswith("sensor."):
            continue
        kind = kind_of(state)
        if kind:
            found.append(
                {
                    "entity": entity,
                    "name": state.get("attributes", {}).get("friendly_name") or entity,
                    "kind": kind,
                    "value": value_of(state, kind),
                }
            )
    seen = sorted(found, key=lambda s: (s["kind"], s["name"].lower()))
    out = []
    for room in rooms:
        for entity, prefix, kind in (
            (room.temperature, TEMPERATURE, "temperature"),
            (room.humidity, HUMIDITY, "humidity"),
        ):
            if entity and (value := value_of(states.get(entity), kind)) is not None:
                out.append((now, prefix + entity, value))
    return out


def value_of(state: dict | None, kind: str) -> float | None:
    if not state:
        return None
    unit = state.get("attributes", {}).get("unit_of_measurement")
    reading = state.get("state")
    return celsius(reading, unit) if kind == "temperature" else percent(reading, unit)


# --- Working the figures out ------------------------------------------------------------------


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _in_hours(local: datetime, hours: tuple[str, str] | None) -> bool:
    if hours is None:
        return True
    start, end = _minutes(hours[0]), _minutes(hours[1])
    now = local.hour * 60 + local.minute
    return start <= now < end if start <= end else now >= start or now < end


class _Totals:
    """Sums for a group of hours, to be added together for weeks, months and years."""

    def __init__(self, keys: list[str]) -> None:
        self.rooms = {
            key: {
                "sum": 0.0,
                "n": 0,
                "min": None,
                "max": None,
                "hum": 0.0,
                "hum_n": 0,
                "humid": 0,
                "in": 0,
                "below": 0,
                "above": 0,
            }
            for key in keys
        }
        self.spread = self.spread_n = 0.0
        self.outdoor = self.outdoor_n = 0.0
        self.days: set[date] = set()

    def add(self, other: _Totals) -> None:
        for key, mine in self.rooms.items():
            theirs = other.rooms[key]
            for name in ("sum", "n", "hum", "hum_n", "humid", "in", "below", "above"):
                mine[name] += theirs[name]
            for name, pick in (("min", min), ("max", max)):
                values = [v for v in (mine[name], theirs[name]) if v is not None]
                mine[name] = pick(values) if values else None
        self.spread += other.spread
        self.spread_n += other.spread_n
        self.outdoor += other.outdoor
        self.outdoor_n += other.outdoor_n
        self.days |= other.days

    def result(self, name: str) -> dict:
        out_rooms = {}
        all_in = all_n = 0
        for key, r in self.rooms.items():
            if not r["n"]:
                out_rooms[key] = None
                continue
            all_in += r["in"]
            all_n += r["n"]
            out_rooms[key] = {
                "average": round(r["sum"] / r["n"], 1),
                "min": round(r["min"], 1),
                "max": round(r["max"], 1),
                "in_band_percent": round(r["in"] / r["n"] * 100),
                "below_percent": round(r["below"] / r["n"] * 100),
                "above_percent": round(r["above"] / r["n"] * 100),
                "humidity": round(r["hum"] / r["hum_n"]) if r["hum_n"] else None,
                "humid_percent": round(r["humid"] / r["hum_n"] * 100) if r["hum_n"] else None,
                "hours": r["n"],
            }
        averages = [room["average"] for room in out_rooms.values() if room]
        return {
            "name": name,
            "days": len(self.days),
            "rooms": out_rooms,
            "house_average": round(sum(averages) / len(averages), 1) if averages else None,
            "spread": round(self.spread / self.spread_n, 1) if self.spread_n else None,
            "in_band_percent": round(all_in / all_n * 100) if all_n else None,
            "outdoor": round(self.outdoor / self.outdoor_n, 1) if self.outdoor_n else None,
        }


def analyse(
    chosen: list[Room],
    temperatures: dict[str, list[tuple[datetime, float]]],
    humidities: dict[str, list[tuple[datetime, float]]],
    outdoor: list[tuple[datetime, float]],
    timezone: ZoneInfo,
    target: float,
    band: float,
    hours: tuple[str, str] | None,
    today: date,
) -> dict:
    """Each room's comfort and the balance between rooms, by day, week, month and year.

    The readings are hourly averages, keyed by the room's key (its temperature entity).
    `hours` is the comfort hours, or None for all day. Today is left out until it is over.
    """
    keys = [room.key for room in chosen]
    by_hour: dict[datetime, dict] = {}

    def slot(when: datetime) -> dict | None:
        local = when.astimezone(timezone)
        if local.date() >= today or not _in_hours(local, hours):
            return None
        return by_hour.setdefault(when, {"local": local, "t": {}, "h": {}, "out": None})

    for key in keys:
        for when, value in temperatures.get(key, []):
            if (s := slot(when)) is not None:
                s["t"][key] = value
        for when, value in humidities.get(key, []):
            if (s := slot(when)) is not None:
                s["h"][key] = value
    for when, value in outdoor:
        if when in by_hour:
            by_hour[when]["out"] = value

    days: dict[date, _Totals] = {}
    for item in by_hour.values():
        if not item["t"]:
            continue
        day = item["local"].date()
        totals = days.get(day)
        if totals is None:
            totals = days[day] = _Totals(keys)
            totals.days.add(day)
        for key, value in item["t"].items():
            r = totals.rooms[key]
            r["sum"] += value
            r["n"] += 1
            r["min"] = value if r["min"] is None else min(r["min"], value)
            r["max"] = value if r["max"] is None else max(r["max"], value)
            if value < target - band:
                r["below"] += 1
            elif value > target + band:
                r["above"] += 1
            else:
                r["in"] += 1
        for key, value in item["h"].items():
            r = totals.rooms[key]
            r["hum"] += value
            r["hum_n"] += 1
            r["humid"] += value > HUMID_PERCENT
        if len(item["t"]) >= 2:
            totals.spread += max(item["t"].values()) - min(item["t"].values())
            totals.spread_n += 1
        if item["out"] is not None:
            totals.outdoor += item["out"]
            totals.outdoor_n += 1

    if not days:
        return {"has_data": False}

    def grouped(key_of) -> list[dict]:
        groups: dict[str, _Totals] = {}
        for day in sorted(days):
            name = key_of(day)
            if name not in groups:
                groups[name] = _Totals(keys)
            groups[name].add(days[day])
        return [totals.result(name) for name, totals in groups.items()]

    recent = _Totals(keys)
    for day, totals in days.items():
        if day >= today - timedelta(days=SUMMARY_DAYS):
            recent.add(totals)
    summary = recent.result("recent") if recent.days else None
    if summary and summary["house_average"] is not None:
        for room in summary["rooms"].values():
            if room:
                room["offset"] = round(room["average"] - summary["house_average"], 1)
        present = {k: v for k, v in summary["rooms"].items() if v}
        if len(present) >= 2:
            summary["coldest"] = min(present, key=lambda k: present[k]["average"])
            summary["warmest"] = max(present, key=lambda k: present[k]["average"])

    return {
        "has_data": True,
        "first_day": min(days),
        "last_day": max(days),
        "summary": summary,
        "days": grouped(lambda d: d.isoformat()),
        "weeks": grouped(lambda d: (d - timedelta(days=d.weekday())).isoformat()),
        "months": grouped(lambda d: f"{d:%Y-%m}"),
        "years": grouped(lambda d: f"{d:%Y}"),
    }
