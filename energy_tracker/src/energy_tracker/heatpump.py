"""What the heat pump is doing, and an estimated SCOP from it.

A heat pump does one job at a time: a valve sends its heat either to the hot water cylinder
or to the radiators. So if Home Assistant shows which mode it is in, each bit of its
electricity can be put down to hot water or heating exactly, rather than guessed from the
tank temperature.

Without a heat meter, the heat it gave out has to be estimated:

* Hot water: the cylinder's temperature rise in each reheat, times its size. Water drawn
  off during a reheat and the tank's own losses hide some heat, so this errs low.
* Heating: modelled. A heat pump's efficiency follows the "lift" between the outdoor air
  and the water it sends round the radiators, as a share (eta) of the ideal Carnot figure.
  Eta is calibrated from the hot water reheats, where both heat and electricity are known,
  so the model is grounded in this heat pump's own performance.

SCOP is all the heat divided by all the electricity, never an average of per-day figures.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import weather
from .db import Database
from .hotwater import MIN_COVER, Tank
from .usage import half_hour_slots

log = logging.getLogger("heatpump")

HOT_WATER_MODE = "hot_water_mode"  # 1 while heating the hot water, 0 otherwise
HEATING_MODE = "heating_mode"  # 1 while space heating is switched on
FLOW = "flow_temperature"  # °C of the water leaving the heat pump
METRICS = [HOT_WATER_MODE, HEATING_MODE, FLOW]

HOLD = timedelta(hours=2)  # a reading says nothing about the time this long after it
HISTORY_DAYS = 10  # what Home Assistant's recorder keeps by default
EVERY = 300  # seconds between the readings filled in from history

WATER_KWH_PER_LITRE_K = 0.001163  # heat to warm a litre of water by one degree
LEGIONELLA_C = 57.0  # a reheat above this is the weekly legionella cycle (partly immersion)
MIN_RUN_KWH = 0.2  # reheats using less tell little about efficiency
MIN_RUN_RISE_C = 2.0
MIN_RUNS = 3  # reheats needed to calibrate eta
DEFAULT_ETA = 0.45  # typical for a modern inverter heat pump
ETA_RANGE = (0.3, 0.65)
DEFAULT_HOT_WATER_COP = 2.5
HOT_WATER_COP_RANGE = (1.0, 5.0)  # outside this, a reading or setting is wrong
MIN_HEATING_KWH = 5.0  # below this a period's heating SCOP means little
MIN_HOT_WATER_KWH = 2.0
DAY_MINIMUM = 1.0  # kWh: a single day's COP needs less behind it

UNKNOWN = {"unavailable", "unknown", ""}
OFF = {"off", "idle", "standby", "none", "false", "0"}
HEATING_ON = {"heat", "heat_cool", "auto", "heating"}


# --- Reading Home Assistant ---------------------------------------------------------------------


def mode(state: dict | None, heating: bool = False) -> float | None:
    """1.0 if the entity shows that mode is on, 0.0 if not, None if it cannot be read.

    Hot water: anything but off (Samsung's water heater shows "heat_pump" only while it is
    reheating the tank). Heating: a climate entity set to heat.
    """
    if not state:
        return None
    value = str(state.get("state", "")).strip().lower()
    if value in UNKNOWN:
        return None
    if heating:
        return 1.0 if value in HEATING_ON or value == "on" else 0.0
    return 0.0 if value in OFF else 1.0


def flow_value(state: dict | None, from_attribute: bool) -> float | None:
    """The flow temperature: a sensor's state, or a climate entity's current temperature."""
    if not state:
        return None
    if from_attribute:
        value = weather.celsius(state.get("attributes", {}).get("current_temperature"), None)
    else:
        value = weather.reading(state)
    return value if value is not None and 5 <= value <= 85 else None


def sources(hot_water: str, heating: str, flow: str) -> list[tuple[str, str, Callable]]:
    """(entity, metric, how to read a state) for each entity named in the options."""
    found = []
    if hot_water:
        found.append((hot_water, HOT_WATER_MODE, mode))
    if heating:
        found.append((heating, HEATING_MODE, lambda s: mode(s, heating=True)))
    if flow:
        found.append((flow, FLOW, lambda s: flow_value(s, False)))
    elif heating.startswith("climate."):
        found.append((heating, FLOW, lambda s: flow_value(s, True)))
    return found


def rows(
    states: dict[str, dict], hot_water: str, heating: str, flow: str, now: datetime
) -> list[tuple[datetime, str, float]]:
    """Database rows for the heat pump's modes and flow temperature now."""
    found = []
    for entity, metric, read in sources(hot_water, heating, flow):
        value = read(states.get(entity))
        if value is not None:
            found.append((now, metric, value))
    return found


def fill_in(
    changes: list[tuple[datetime, float | None]], end: datetime
) -> list[tuple[datetime, float]]:
    """Readings every few minutes from a list of changes, as if they had been polled.

    History only records changes: a heat pump off for six hours has one entry. Repeating
    each value until the next change makes it look like the readings collected live.
    """
    filled = []
    ordered = sorted(changes, key=lambda c: c[0])
    for index, (when, value) in enumerate(ordered):
        if value is None:
            continue
        until = ordered[index + 1][0] if index + 1 < len(ordered) else end
        cursor = when
        while cursor < until:
            filled.append((cursor, value))
            cursor += timedelta(seconds=EVERY)
    return filled


def backfill(
    db: Database, client: httpx.Client, hot_water: str, heating: str, flow: str, now: datetime
) -> int:
    """Bring in the modes Home Assistant still has (about ten days), once for each entity."""
    added = 0
    for entity, metric, read in sources(hot_water, heating, flow):
        done = f"heatpump_backfilled:{entity}:{metric}"
        if db.get_setting(done):
            continue
        start = now - timedelta(days=HISTORY_DAYS)
        response = client.get(
            f"/api/history/period/{start.astimezone(UTC):%Y-%m-%dT%H:%M:%S}+00:00",
            params={
                "end_time": now.astimezone(UTC).isoformat(),
                "filter_entity_id": entity,
                "significant_changes_only": "0",
            },
            timeout=120,
        )
        response.raise_for_status()
        changes = []
        for series in response.json():
            for state in series:
                when = state.get("last_updated") or state.get("last_changed")
                if when:
                    moment = datetime.fromisoformat(when).astimezone(UTC)
                    changes.append((moment, read({**state, "entity_id": entity})))
        added += db.insert_readings((when, metric, value) for when, value in fill_in(changes, now))
        db.set_setting(done, now.isoformat())
    return added


# --- Working with the readings ------------------------------------------------------------------


class Steps:
    """A reading that holds until the next one (for at most HOLD): a mode, or a temperature."""

    def __init__(self, samples: list[tuple[datetime, float]]) -> None:
        points = sorted((t.astimezone(UTC).timestamp(), v) for t, v in samples)
        self.stamps = [t for t, _ in points]
        self.values = [v for _, v in points]
        self.hold = HOLD.total_seconds()

    def __bool__(self) -> bool:
        return bool(self.stamps)

    def _pieces(self, a: float, b: float):
        """(seconds, value) for each stretch of [a, b) a reading covers."""
        index = max(0, bisect_right(self.stamps, a) - 1)
        while index < len(self.stamps) and self.stamps[index] < b:
            begun = self.stamps[index]
            follows = self.stamps[index + 1] if index + 1 < len(self.stamps) else begun + self.hold
            low, high = max(begun, a), min(follows, begun + self.hold, b)
            if high > low:
                yield high - low, self.values[index]
            index += 1

    def mean(self, start: datetime, end: datetime, cover: float = 0.5) -> float | None:
        """Time-weighted average between two moments; None if under `cover` of it is known."""
        a, b = start.timestamp(), end.timestamp()
        seconds = weighted = 0.0
        for length, value in self._pieces(a, b):
            seconds += length
            weighted += length * value
        return weighted / seconds if b > a and seconds >= cover * (b - a) else None

    def runs(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Stretches during which the mode was on (an average of a half or more)."""
        found: list[list[float]] = []
        a, b = start.timestamp(), end.timestamp()
        index = max(0, bisect_right(self.stamps, a) - 1)
        while index < len(self.stamps) and self.stamps[index] < b:
            begun = self.stamps[index]
            follows = self.stamps[index + 1] if index + 1 < len(self.stamps) else begun + self.hold
            stop = min(follows, begun + self.hold, b)
            if self.values[index] >= 0.5 and stop > begun:
                if found and begun - found[-1][1] <= 1:
                    found[-1][1] = stop
                else:
                    found.append([max(begun, a), stop])
            index += 1
        return [(datetime.fromtimestamp(s, UTC), datetime.fromtimestamp(e, UTC)) for s, e in found]


def carnot(flow_c: float, outdoor_c: float) -> float:
    """The ideal efficiency between outdoor air and the flow water, allowing for the
    few degrees each heat exchanger needs (3 °C above the flow, 5 °C below outside)."""
    hot = flow_c + 3
    lift = max(5.0, hot - (outdoor_c - 5))
    return (hot + 273.15) / lift


def split(
    hot_water: Steps,
    heating: Steps,
    flow: Steps,
    outdoor: Tank,
    used: Callable[[datetime, datetime], float],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
) -> dict[date, dict]:
    """Hot water, heating and standby kWh for each day the hot water mode covers.

    Each half hour's electricity goes to hot water for the share of it the hot water mode
    was on; the rest to heating if heating was switched on, otherwise to standby. Also
    gathers, for heating, the electricity times the ideal efficiency at that half hour's
    flow and outdoor temperatures (multiplied by eta later, it gives the heat), and the
    (outdoor, flow) pairs for working out the weather compensation curve.
    """
    days: dict[date, dict] = {}
    for piece_start, piece_end in half_hour_slots(start, end):
        day = days.setdefault(
            piece_start.astimezone(timezone).date(),
            {
                "hot_water_kwh": 0.0,
                "heating_kwh": 0.0,
                "other_kwh": 0.0,
                "ideal": 0.0,
                "ideal_kwh": 0.0,
                "pairs": [],
                "covered_hours": 0.0,
                "hours": 0.0,
            },
        )
        hours = (piece_end - piece_start).total_seconds() / 3600
        day["hours"] += hours
        kwh = max(0.0, used(piece_start, piece_end) or 0.0)
        share = hot_water.mean(piece_start, piece_end)
        if share is None:
            day["heating_kwh"] += kwh  # not known: left out of the measured days below
            continue
        day["covered_hours"] += hours
        water = kwh * share
        rest = kwh - water
        day["hot_water_kwh"] += water
        heating_on = heating.mean(piece_start, piece_end) if heating else None
        if heating_on is not None and heating_on <= 0:
            day["other_kwh"] += rest
            continue
        day["heating_kwh"] += rest
        # The flow sensor shows the hot water's flow during a reheat: only use it when the
        # half hour was mostly heating.
        middle = piece_start + (piece_end - piece_start) / 2
        out = outdoor.at(middle) if outdoor else None
        temperature = flow.mean(piece_start, piece_end) if flow and share < 0.2 else None
        if rest > 0.01 and out is not None and temperature is not None:
            day["ideal"] += rest * carnot(temperature, out)
            day["ideal_kwh"] += rest
            day["pairs"].append((out, temperature, rest))

    return {
        when: {k: v for k, v in row.items() if k not in ("covered_hours", "hours")}
        for when, row in days.items()
        if row["hours"] and row["covered_hours"] / row["hours"] >= MIN_COVER
    }


def reheats(
    hot_water: Steps,
    tank: Steps,
    flow: Steps,
    outdoor: Tank,
    used: Callable[[datetime, datetime], float],
    litres: float,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
) -> list[dict]:
    """Each hot water reheat: electricity used, heat put into the cylinder, conditions."""
    found = []
    for begun, finished in hot_water.runs(start, end):
        kwh = max(0.0, used(begun, finished) or 0.0)

        # The coolest the tank was around the start (a draw-off may still be settling), and
        # its peak: it keeps reading higher for a few minutes after the reheat stops.
        def readings(a: datetime, b: datetime) -> list[float]:
            low = bisect_left(tank.stamps, a.timestamp())
            return tank.values[low : bisect_right(tank.stamps, b.timestamp(), low)]

        held = tank.mean(begun, begun + timedelta(minutes=1), 0.5)
        around = readings(begun - timedelta(minutes=10), begun + timedelta(minutes=10))
        before = min([*around, *([held] if held is not None else [])], default=None)
        peak = max(readings(begun, finished + timedelta(minutes=20)), default=None)
        if before is None or peak is None:
            continue
        rise = max(0.0, peak - before)
        middle = begun + (finished - begun) / 2
        out = outdoor.at(middle) if outdoor else None
        temperature = flow.mean(begun, finished, 0.3) if flow else None
        found.append(
            {
                "day": begun.astimezone(timezone).date(),
                "start": begun,
                "minutes": round((finished - begun).total_seconds() / 60),
                "kwh": kwh,
                "heat_kwh": litres * WATER_KWH_PER_LITRE_K * rise,
                "from_c": before,
                "to_c": peak,
                "rise_c": rise,
                "legionella": peak >= LEGIONELLA_C,
                # Without a flow reading, the flow runs a few degrees above the tank.
                "ideal": carnot(
                    temperature if temperature else peak + 5, out if out is not None else 10.0
                ),
            }
        )
    return found


def calibrate(runs: list[dict]) -> dict:
    """Eta and the hot water COP from the reheats; typical figures until there are enough."""
    useful = [r for r in runs if r["kwh"] >= MIN_RUN_KWH and r["rise_c"] >= MIN_RUN_RISE_C]
    normal = [r for r in useful if not r["legionella"]]
    result = {
        "reheats": len(useful),
        "legionella_reheats": sum(1 for r in useful if r["legionella"]),
        "eta": DEFAULT_ETA,
        "eta_source": "assumed",
        "hot_water_cop": DEFAULT_HOT_WATER_COP,
        "hot_water_cop_source": "assumed",
    }
    if len(normal) >= MIN_RUNS:
        heat = sum(r["heat_kwh"] for r in normal)
        ideal = sum(r["kwh"] * r["ideal"] for r in normal)
        if ideal > 0:
            low, high = ETA_RANGE
            if low <= heat / ideal <= high:
                result["eta"] = round(heat / ideal, 3)
                result["eta_source"] = "measured"
            else:  # something is off (the cylinder size, say): keep the typical figure
                result["eta_source"] = "out of range"
                result["eta_measured"] = round(heat / ideal, 3)
    if len(useful) >= MIN_RUNS:
        kwh = sum(r["kwh"] for r in useful)
        cop = sum(r["heat_kwh"] for r in useful) / kwh if kwh > 0 else 0.0
        if HOT_WATER_COP_RANGE[0] <= cop <= HOT_WATER_COP_RANGE[1]:
            result["hot_water_cop"] = round(cop, 2)
            result["hot_water_cop_source"] = "measured"
        elif kwh > 0:
            result["hot_water_cop_source"] = "out of range"
            result["hot_water_cop_measured"] = round(cop, 2)
    return result


def curve(pairs: list[tuple[float, float, float]]) -> dict:
    """The weather compensation curve: flow temperature against outdoor temperature.

    Fitted from the half hours of heating (weighted by the electricity used); a typical
    curve (50 °C at -3 °C outside, 30 °C at 15 °C) until there is enough to go on.
    """
    weight = sum(w for _, _, w in pairs)
    slope, source = -20 / 18, "assumed"
    middle_out, middle_flow = 15.0, 30.0
    if len(pairs) >= 24 and weight > 0:
        middle_out = sum(o * w for o, _, w in pairs) / weight
        middle_flow = sum(f * w for _, f, w in pairs) / weight
        spread = sum(w * (o - middle_out) ** 2 for o, _, w in pairs) / weight
        if spread >= 4:  # outdoor temperatures at least about 2 °C apart
            fitted = (
                sum(w * (o - middle_out) * (f - middle_flow) for o, f, w in pairs) / weight / spread
            )
            slope = min(0.0, max(-3.0, fitted))
            source = "measured"
        else:
            source = "partly measured"

    def at(outdoor: float) -> float:
        return round(min(60.0, max(25.0, middle_flow + slope * (outdoor - middle_out))), 1)

    return {"source": source, "at_minus_3": at(-3), "at_7": at(7), "at_15": at(15), "flow": at}


def summarise(name: str, members: list[dict], minimum: float = MIN_HEATING_KWH) -> dict:
    """Totals and efficiencies over some days (from `estimate`'s daily figures). A figure
    is only given with at least `minimum` kWh behind it (less for hot water)."""

    def total(key: str) -> float:
        return sum(m[key] for m in members)

    water, heating_kwh, other = total("hot_water_kwh"), total("heating_kwh"), total("other_kwh")
    water_heat, heating_heat = total("hot_water_heat_kwh"), total("heating_heat_kwh")
    electricity = water + heating_kwh + other
    water_minimum = min(MIN_HOT_WATER_KWH, minimum / 2)
    return {
        "name": name,
        "days": len(members),
        "measured_days": sum(1 for m in members if m["source"] == "modes"),
        "kwh": round(electricity, 1),
        "hot_water_kwh": round(water, 1),
        "heating_kwh": round(heating_kwh, 1),
        "other_kwh": round(other, 1),
        "heat_kwh": round(water_heat + heating_heat),
        "hot_water_cop": round(water_heat / water, 2) if water >= water_minimum else None,
        "heating_scop": round(heating_heat / heating_kwh, 2) if heating_kwh >= minimum else None,
        "scop": (
            round((water_heat + heating_heat) / electricity, 2) if electricity >= minimum else None
        ),
    }


def curve_from(saved: dict) -> dict:
    """A curve as `curve` returns it, from its saved points (to reuse a fitted curve)."""
    low, high = saved["at_minus_3"], saved["at_15"]

    def at(outdoor: float) -> float:
        return round(min(60.0, max(25.0, low + (high - low) * (outdoor + 3) / 18)), 1)

    return {**saved, "flow": at}


CALIBRATION = (
    "reheats",
    "legionella_reheats",
    "eta",
    "eta_source",
    "eta_measured",
    "hot_water_cop",
    "hot_water_cop_source",
    "hot_water_cop_measured",
)
DAILY_KEPT = 400  # days of daily figures returned, for the cost tiles


def estimate(
    used: dict[date, float],
    temperature: dict[date, float],
    measured: dict[date, dict],
    runs: list[dict],
    hot_water_fallback: dict[date, float],
    warm_day_kwh: float | None,
    today: date,
    litres: float | None,
    calibration: dict | None = None,
) -> dict:
    """Estimated SCOP by month and by year.

    Days with the heat pump's modes are split exactly and use their own flow temperatures;
    earlier days are split from the tank or the usual warm-day use, and take their flow
    temperature from the weather compensation curve. Every day uses the same eta and hot
    water COP, so the whole year can be estimated even though the modes only go back a
    little way.
    """
    if calibration:  # figures already worked out over a longer stretch
        tuned = {k: v for k, v in calibration.items() if k in CALIBRATION}
        weather_curve = curve_from(calibration["curve"])
    else:
        tuned = calibrate(runs) if litres else calibrate([])
        weather_curve = curve([p for row in measured.values() for p in row["pairs"]])
    eta, water_cop = tuned["eta"], tuned["hot_water_cop"]
    by_day_runs: dict[date, list[dict]] = {}
    for run in runs if litres else []:
        if run["kwh"] >= MIN_RUN_KWH:
            by_day_runs.setdefault(run["day"], []).append(run)

    days = []
    for day in sorted(set(used) | set(measured)):
        outside = temperature.get(day)
        if day in measured:
            row = measured[day]
            water, heating_kwh, other = row["hot_water_kwh"], row["heating_kwh"], row["other_kwh"]
            source = "modes"
        elif day in used:
            kwh = used[day]
            known = hot_water_fallback.get(day)
            water = min(kwh, known if known is not None else (warm_day_kwh or 0.0))
            heating_kwh, other = kwh - water, 0.0
            source = "tank" if known is not None else "estimated"
        else:
            continue
        if (
            heating_kwh > 0.05
            and outside is None
            and not (day in measured and measured[day]["ideal_kwh"] >= 0.5 * heating_kwh)
        ):
            continue  # no way to model this day's heating
        # Heating heat: measured flow temperatures where there are enough, else the curve.
        if day in measured and measured[day]["ideal_kwh"] >= 0.5 * heating_kwh > 0:
            ideal = measured[day]["ideal"] / measured[day]["ideal_kwh"]
        elif heating_kwh > 0 and outside is not None:
            ideal = carnot(weather_curve["flow"](outside), outside)
        else:
            ideal = 0.0
        heating_heat = heating_kwh * min(7.0, eta * ideal)
        # Hot water heat: that day's measured reheats where they cover most of it.
        today_runs = by_day_runs.get(day, [])
        run_kwh = sum(r["kwh"] for r in today_runs)
        day_cop = sum(r["heat_kwh"] for r in today_runs) / run_kwh if run_kwh else 0.0
        if (
            tuned["hot_water_cop_source"] == "measured"
            and water > 0
            and run_kwh >= 0.5 * water
            and HOT_WATER_COP_RANGE[0] <= day_cop <= HOT_WATER_COP_RANGE[1]
        ):
            water_heat = water * day_cop
        else:
            water_heat = water * water_cop
        days.append(
            {
                "day": day,
                "source": source,
                "hot_water_kwh": water,
                "heating_kwh": heating_kwh,
                "other_kwh": other,
                "hot_water_heat_kwh": water_heat,
                "heating_heat_kwh": heating_heat,
            }
        )

    def grouped(key) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for item in days:
            groups.setdefault(key(item["day"]), []).append(item)
        return [summarise(name, members) for name, members in sorted(groups.items())]

    months = grouped(lambda d: f"{d:%Y-%m}")
    years = grouped(lambda d: f"{d:%Y}")
    year_ago = [d for d in days if (today - d["day"]).days < 365]
    modes_from = min((d["day"] for d in days if d["source"] == "modes"), default=None)
    return {
        "has_data": bool(days),
        **{k: v for k, v in tuned.items()},
        "litres": litres,
        "curve": {k: v for k, v in weather_curve.items() if k != "flow"},
        "modes_from": modes_from,
        "months": months,
        "years": years,
        "this_month": next((m for m in months if m["name"] == f"{today:%Y-%m}"), None),
        "this_year": next((y for y in years if y["name"] == f"{today:%Y}"), None),
        "last_12_months": summarise("Last 12 months", year_ago) if year_ago else None,
        # Each day's figures: for the cost tiles, and a day's COP.
        "daily": [
            {k: (round(v, 3) if isinstance(v, float) else v) for k, v in item.items()}
            for item in days[-DAILY_KEPT:]
        ],
        "yesterday": next(
            (
                summarise(f"{item['day']}", [item], DAY_MINIMUM)
                for item in days[-1:]
                if item["day"] == today
            ),
            None,
        ),
        "recent_reheats": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "ideal"}
            for r in runs[-10:]
        ]
        if litres
        else [],
    }
