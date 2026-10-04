from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from energy_tracker import income
from energy_tracker.db import Database

LONDON = ZoneInfo("Europe/London")
START = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)  # 17:30 to 18:30 local
END = START + timedelta(hours=1)


def sensor(kind="export", start=START, end=END):
    return {
        "state": "ok",
        "attributes": {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "import_export": kind,
            "updated_at": "2026-10-01T09:00:00+00:00",
        },
    }


def exporting(db, kw, begin=START - timedelta(hours=1), finish=END + timedelta(hours=1)):
    rows, t = [], begin
    while t <= finish:
        inside = min(max((t - START).total_seconds(), 0), 3600) / 3600
        rows.append((t, "export_energy_total", 100 + kw * inside))
        t += timedelta(minutes=1)
    db.insert_readings(rows)


def test_only_export_events_with_sensible_times_are_read():
    assert income.parse_event(sensor()) == (START, END)
    assert income.parse_event(sensor("import")) is None
    assert income.parse_event(sensor(end=START)) is None
    assert income.parse_event({"state": "unknown", "attributes": {}}) is None
    assert income.parse_event(None) is None
    assert (
        income.parse_event({"attributes": {"import_export": "export", "start_time": "x"}}) is None
    )


def test_event_is_recorded_once_then_paid_at_the_rate_after_it_ends(tmp_path):
    db = Database(tmp_path / "e.db")
    assert income.record(db, sensor(), LONDON) is True
    assert income.record(db, sensor(), LONDON) is False  # the sensor shows it for hours
    entry = db.income_rows()[0]
    assert entry["day"] == "2026-10-01" and entry["amount_gbp"] is None and entry["estimated"]

    exporting(db, 4.0)
    assert income.settle(db, END + timedelta(minutes=1)) == 0  # too soon after the end
    assert income.settle(db, END + timedelta(minutes=10)) == 1
    entry = db.income_rows()[0]
    assert entry["kwh"] == 4.0 and entry["amount_gbp"] == 4.0  # 4 kWh at 100p
    assert income.settle(db, END + timedelta(hours=1)) == 0  # not worked out twice


def test_rate_can_be_changed_and_event_waits_for_readings(tmp_path):
    db = Database(tmp_path / "e.db")
    db.set_setting("axle_rate_p", "50")
    income.record(db, sensor(), LONDON)
    assert income.settle(db, END + timedelta(minutes=10)) == 0  # no readings yet: keep waiting
    assert db.income_rows()[0]["amount_gbp"] is None
    exporting(db, 3.0)
    income.settle(db, END + timedelta(hours=2))
    assert db.income_rows()[0]["amount_gbp"] == 1.5
    # An event whose readings never arrive is closed at nothing after a while.
    later = START + timedelta(days=1)
    income.record(db, sensor(start=later, end=later + timedelta(hours=1)), LONDON)
    income.settle(db, later + timedelta(days=14))
    assert db.income_rows()[0]["amount_gbp"] == 0.0


def test_manual_entries_corrections_and_removal(tmp_path):
    db = Database(tmp_path / "e.db")
    manual = db.save_income(date(2026, 9, 3), "Axle export event", 2.75, None)
    income.record(db, sensor(), LONDON)
    exporting(db, 4.0)
    income.settle(db, END + timedelta(minutes=10))
    event = next(r for r in db.income_rows() if r["source"] == "axle")

    db.save_income(date(2026, 10, 1), "Axle export event", 3.6, event["id"])  # the real payment
    corrected = next(r for r in db.income_rows() if r["id"] == event["id"])
    assert corrected["amount_gbp"] == 3.6 and corrected["estimated"] is False

    assert db.remove_income(event["id"]) and db.remove_income(manual)
    assert db.income_rows() == []
    assert income.record(db, sensor(), LONDON) is False  # a removed event does not come back
    assert db.remove_income(9999) is False


def test_events_shown_while_the_app_was_off_are_picked_up_from_history(tmp_path):
    db = Database(tmp_path / "e.db")
    earlier = START - timedelta(days=3)

    def handler(request):
        assert "filter_entity_id=sensor.axle_event" in str(request.url)
        return httpx.Response(
            200,
            json=[
                [
                    sensor(start=earlier, end=earlier + timedelta(hours=1)),
                    {"state": "unknown", "attributes": {}},
                    sensor(),
                ]
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ha") as client:
        assert income.backfill_events(client, db, "sensor.axle_event", END, 10, LONDON) == 2
    assert [r["day"] for r in db.income_rows()] == ["2026-10-01", "2026-09-28"]
