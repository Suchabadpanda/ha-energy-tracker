"""Alerts: tell the household, through Home Assistant, when something looks wrong.

Checks run every few minutes alongside the collector. When one starts failing, a
notification is sent once: to the phones chosen (Home Assistant's mobile app) and as a
notification inside Home Assistant. It is not sent again that day.

This is the one place the app asks Home Assistant to do something, and all it asks for is
a notification. It never changes a device or a setting.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

import httpx

from . import planner
from .db import Database
from .tariff import Schedule
from .usage import Counter, half_hour_slots

log = logging.getLogger("alerts")

CHECK_EVERY = timedelta(minutes=5)
LOG_LENGTH = 30

DEFAULTS = {
    "enabled": False,
    # Home Assistant notify services to use, such as "mobile_app_my_phone". Empty means
    # every phone with the Home Assistant app.
    "services": [],
    "battery_not_charged": True,
    "battery_expected_percent": 30,
    "dear_import": True,
    "dear_import_kwh": 5.0,
}
TITLES = {
    "battery_not_charged": "Battery did not charge in the cheap period",
    "dear_import": "Buying a lot at the dearer rate today",
    "test": "Energy Tracker test alert",
}


def settings(db: Database) -> dict:
    stored = json.loads(db.get_setting("alerts") or "{}")
    return {**DEFAULTS, **{key: value for key, value in stored.items() if key in DEFAULTS}}


def history(db: Database) -> list[dict]:
    return json.loads(db.get_setting("alerts_log") or "[]")


def _state(db: Database) -> dict:
    return json.loads(db.get_setting("alerts_state") or "{}")


def phone_services(client: httpx.Client) -> list[str]:
    """The notify services Home Assistant offers, phones first."""
    response = client.get("/api/services")
    response.raise_for_status()
    names: list[str] = []
    for domain in response.json():
        if domain.get("domain") == "notify":
            names = sorted(domain.get("services", {}))
    skip = {"persistent_notification", "send_message"}
    phones = [name for name in names if name.startswith("mobile_app_")]
    return phones + [name for name in names if name not in phones and name not in skip]


def send(client: httpx.Client, services: list[str], check: str, message: str) -> list[str]:
    """Send a notification. Returns the problems met, one line each (empty if all went)."""
    title = TITLES.get(check, "Energy Tracker")
    problems: list[str] = []
    try:
        targets = services or [s for s in phone_services(client) if s.startswith("mobile_app_")]
    except httpx.HTTPError as exc:
        targets = []
        problems.append(f"Could not list Home Assistant's notification services ({exc})")
    calls = [(f"notify/{name}", {"title": title, "message": message}) for name in targets]
    # Also inside Home Assistant itself, where it stays until dismissed.
    calls.append(
        (
            "persistent_notification/create",
            {"title": title, "message": message, "notification_id": f"energy_tracker_{check}"},
        )
    )
    for path, body in calls:
        try:
            client.post(f"/api/services/{path}", json=body).raise_for_status()
        except httpx.HTTPError as exc:
            problems.append(f"{path.split('/')[-1]}: {exc}")
    if not targets and not problems:
        problems.append(
            "No phone with the Home Assistant app was found, so it was only shown inside "
            "Home Assistant"
        )
    return problems


def _counter(db: Database, name: str, start: datetime, end: datetime) -> Counter:
    samples = db.last_in_buckets([name], start - timedelta(hours=2), end, 300)[name]
    return Counter(samples, timedelta(minutes=20))


def evaluate(
    db: Database,
    schedule: Schedule,
    now: datetime,
    timezone: ZoneInfo,
    options: dict,
) -> dict[str, tuple[str, str]]:
    """The checks failing now, as check -> (key, message).

    The key says which occurrence this is (a date, usually), so each is announced once.
    """
    failing: dict[str, tuple[str, str]] = {}
    local = now.astimezone(timezone)
    today = local.date()
    midnight = datetime.combine(today, clock(0), timezone)
    tariff = schedule.on(today)

    if options["battery_not_charged"] and not tariff.dynamic:
        for window in planner.cheap_times(tariff):
            ends = clock.fromisoformat("00:00" if window["end"] == "24:00" else window["end"])
            window_end = datetime.combine(today, ends, timezone)
            starts = datetime.combine(today, clock.fromisoformat(window["start"]), timezone)
            if starts >= window_end:  # runs past midnight: it began yesterday
                starts -= timedelta(days=1)
            # Looked at once, in the three hours after the cheap period ends.
            if not timedelta(minutes=10) <= now - window_end <= timedelta(hours=3):
                continue
            charge = _counter(db, "battery_charge_energy_total", starts, window_end)
            level = db.last_in_buckets(
                ["battery_soc"], window_end - timedelta(minutes=30), window_end, 1800
            )["battery_soc"]
            if not charge or not level:
                continue
            charged = charge.between(starts, window_end)
            if charged < 0.5 and level[-1][1] < options["battery_expected_percent"]:
                failing["battery_not_charged"] = (
                    f"{today} {window['end']}",
                    f"The battery took {charged:.1f} kWh in the cheap period "
                    f"({window['start']} to {window['end']}) and is at {level[-1][1]:.0f}%.",
                )

    if options["dear_import"] and not tariff.dynamic and planner.cheap_times(tariff):
        imports = _counter(db, "import_energy_total", midnight, now)
        if imports:
            cheap = tariff.cheap_rate(local)
            dear = sum(
                imports.between(a, b)
                for a, b in half_hour_slots(midnight, now)
                if tariff.import_price(a.astimezone(timezone)) > cheap + 1e-9
            )
            if dear > options["dear_import_kwh"]:
                failing["dear_import"] = (
                    str(today),
                    f"{dear:.1f} kWh has been bought outside the cheap rate so far today "
                    f"(your limit is {options['dear_import_kwh']:g} kWh).",
                )

    return failing


def run(
    db: Database,
    client: httpx.Client,
    schedule: Schedule,
    now: datetime,
    timezone: ZoneInfo,
) -> list[str]:
    """Run the checks and send a notification for each that has newly started failing.

    Returns the checks announced.
    """
    options = settings(db)
    if not options["enabled"]:
        return []
    failing = evaluate(db, schedule, now, timezone, options)
    state = _state(db)
    announced: list[str] = []
    for check, (key, message) in failing.items():
        if state.get(check) == key:
            continue  # already said
        problems = send(client, options["services"], check, message)
        if problems:
            log.warning("Alert '%s' not fully delivered: %s", check, "; ".join(problems))
        state[check] = key
        announced.append(check)
        entries = history(db)
        entries.insert(
            0,
            {
                "time": now.astimezone(UTC).isoformat(),
                "check": check,
                "title": TITLES[check],
                "message": message,
                "problems": problems,
            },
        )
        db.set_setting("alerts_log", json.dumps(entries[:LOG_LENGTH]))
    db.set_setting("alerts_state", json.dumps(state))
    return announced
