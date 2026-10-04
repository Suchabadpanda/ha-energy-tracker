"""One-off import of history from before this app was collecting.

Two sources are combined:

1. Home Assistant's long-term statistics: hourly values of every lifetime energy counter,
   kept indefinitely. Fetched over Home Assistant's WebSocket API.
2. A Sigenergy export (the spreadsheet from the mySigen app) for the time before Home
   Assistant's statistics begin. An HOURLY export is used as it is. A DAILY export has no
   times, so each day's import is split using an assumed off-peak share.

Normally run from the dashboard (Import history). From a command line:
    python -m energy_tracker.import_history --sigen stationData.xlsx
Safe to run more than once: readings that already exist are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings, load_metrics, load_settings
from .db import Database
from .usage import Counter, Sample

Row = tuple[datetime, str, float]
# One row of the export: when it starts and ends, and {counter name: kWh in that time}.
Interval = tuple[datetime, datetime, dict[str, float]]

IMPORT, LOAD, EXPORT = "import_energy_total", "load_energy_total", "export_energy_total"

# Column headings in the Sigenergy export and the counters they belong to. Units in
# brackets, e.g. "Grid Import (kWh)", are ignored.
SIGEN_COLUMNS = {
    "grid import": IMPORT,
    "grid export": EXPORT,
    "load": LOAD,
    "solar generation": "pv_energy_total",
    "battery charge": "battery_charge_energy_total",
    "battery discharge": "battery_discharge_energy_total",
}

# --- Home Assistant hourly statistics --------------------------------------------------------


async def fetch_hourly_statistics(url: str, token: str, entities: list[str]) -> dict:
    """Ask Home Assistant for the hourly value of each counter, converted to kWh."""
    import websockets  # imported here so the rest of the app does not need it

    async with websockets.connect(url, max_size=None) as ws:
        json.loads(await ws.recv())  # "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the access token")

        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/statistics_during_period",
                    "start_time": "2000-01-01T00:00:00+00:00",
                    "statistic_ids": entities,
                    "period": "hour",
                    "types": ["state"],
                    "units": {"energy": "kWh"},
                }
            )
        )
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("id") == 1 and reply.get("type") == "result":
                break
    if not reply.get("success"):
        raise RuntimeError(f"Home Assistant returned an error: {reply.get('error')}")
    return reply["result"]


def parse_statistics(result: dict) -> dict[str, list[Sample]]:
    """Turn the statistics reply into (time, kWh) samples per entity.

    Each row covers one hour and its `state` is the counter at the END of that hour, so the
    sample is stamped with the end time.
    """
    samples: dict[str, list[Sample]] = {}
    for entity, rows in result.items():
        series: list[Sample] = []
        for row in rows:
            if row.get("state") is None:
                continue
            start = row["start"]
            started = (
                datetime.fromtimestamp(start / 1000, UTC)
                if isinstance(start, int | float)
                else datetime.fromisoformat(start).astimezone(UTC)
            )
            series.append((started + timedelta(hours=1), float(row["state"])))
        if series:
            samples[entity] = series
    return samples


# --- Sigenergy daily export ------------------------------------------------------------------


def read_sigen(path: Path, timezone: ZoneInfo) -> tuple[str, list[Interval]]:
    """Read a Sigenergy export. Returns ("hourly" or "daily", its rows as intervals).

    The file's headings are shifted one column left (there is no heading over the dates),
    so heading N belongs to data column N+1. Columns are matched by heading, which copes
    with exports that have different columns or a different order.
    """
    from openpyxl import load_workbook

    # Not read_only: this export records its size wrongly, and read-only mode trusts that.
    rows = list(load_workbook(path, data_only=True).active.iter_rows(values_only=True))
    if not rows:
        raise ValueError(f"{path} is empty")

    columns: dict[int, str] = {}
    for index, heading in enumerate(rows[0]):
        name = str(heading or "").split("(")[0].strip().lower()
        name = "load" if name == "load consumption" else name
        if name in SIGEN_COLUMNS:
            columns[index + 1] = SIGEN_COLUMNS[name]
    if IMPORT not in columns.values() or EXPORT not in columns.values():
        raise ValueError(f"{path} needs 'Grid Import' and 'Grid Export' columns")

    kinds: set[str] = set()
    intervals: list[Interval] = []
    for row in rows[1:]:
        text = str(row[0] or "").strip()
        if text == "total" or not text:
            continue
        if len(text) == 10:  # 2026-01-31
            day = date.fromisoformat(text)
            start = datetime.combine(day, time(0), timezone)
            end = datetime.combine(day + timedelta(days=1), time(0), timezone)
            kinds.add("daily")
        elif len(text) == 19:  # 2026-01-31 14:00:00, local time, start of the hour
            # fold=1: on the night the clocks go forward the export labels the hours with
            # the new (summer) offset, including one clock time that does not exist.
            start = datetime.fromisoformat(text).replace(tzinfo=timezone, fold=1)
            end = start.astimezone(UTC) + timedelta(hours=1)
            kinds.add("hourly")
        else:
            raise ValueError(
                f"'{text}' is not a day or an hour. Use the daily or hourly export, "
                "not the monthly one."
            )
        try:
            # Tiny negative values appear in the first hour after commissioning.
            totals = {m: max(0.0, float(row[index])) for index, m in columns.items()}
        except (TypeError, ValueError, IndexError):
            continue  # "--": no data for this row
        intervals.append((start.astimezone(UTC), end.astimezone(UTC), totals))
    if not intervals:
        raise ValueError(f"No figures found in {path}")
    if len(kinds) > 1:
        raise ValueError(f"{path} mixes daily and hourly rows")
    return kinds.pop(), sorted(intervals, key=lambda i: i[0])


def synthesise(
    intervals: list[Interval],
    cutoff: datetime,
    anchors: dict[str, float],
    timezone: ZoneInfo,
    offpeak_share: float | None = None,
    cheap_until: time = time(6),
) -> list[Row]:
    """Build counter readings for the time before `cutoff` from the export's kWh figures.

    Works backwards from the known counter values at `cutoff` (one per counter in
    `anchors`), so the result joins up exactly with the readings that follow. Each row of
    the export gets a reading at its start and its end, so the counter stays flat across
    any hours missing from the file.

    `offpeak_share` is for daily exports only: the import counter gets an extra reading at
    the end of the cheap-rate window, placing that share of the day's import before it.
    """
    running = dict(anchors)
    readings: dict[tuple[str, datetime], float] = {
        (name, cutoff.astimezone(UTC)): value for name, value in anchors.items()
    }
    for start, end, totals in sorted((i for i in intervals if i[1] <= cutoff), reverse=True):
        for metric in anchors:
            readings.setdefault((metric, end), running[metric])
            running[metric] -= totals.get(metric, 0.0)
            readings[(metric, start)] = running[metric]
        if offpeak_share is not None and IMPORT in anchors:
            local_day = start.astimezone(timezone).date()
            cheap_end = datetime.combine(local_day, cheap_until, timezone).astimezone(UTC)
            readings[(IMPORT, cheap_end)] = running[IMPORT] + totals[IMPORT] * offpeak_share
    return [(when, metric, value) for (metric, when), value in readings.items()]


def first_local_midnight(when: datetime, timezone: ZoneInfo) -> datetime:
    """The first local midnight at or after `when`."""
    local = when.astimezone(timezone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if midnight == local else midnight + timedelta(days=1)


# --- Putting it together ---------------------------------------------------------------------


def build_rows(
    statistics: dict[str, list[Sample]],
    metric_by_entity: dict[str, str],
    sigen: list[Interval],
    timezone: ZoneInfo,
    offpeak_share: float | None,
    cheap_until: time,
) -> tuple[list[Row], dict]:
    """Combine both sources into rows to store, plus a summary of what was done."""
    by_metric = {metric_by_entity[e]: s for e, s in statistics.items() if e in metric_by_entity}
    summary: dict = {"hourly": {}, "sigen_rows": 0}
    rows: list[Row] = []

    cutoff = None
    covered: list[str] = []
    if sigen:
        missing = [m for m in (IMPORT, EXPORT) if m not in by_metric]
        if missing:
            raise RuntimeError(f"Home Assistant has no hourly history for: {', '.join(missing)}")
        # Counters in the export can only be joined on if Home Assistant has their later
        # history; any others in the file are left out.
        in_file = list(sigen[0][2])
        covered = [m for m in in_file if m in by_metric]
        # Use the export up to the first full day of Home Assistant history, so the two
        # sources never overlap within a day.
        earliest = max(by_metric[m][0][0] for m in covered)
        cutoff = first_local_midnight(earliest, timezone)
        anchors = {m: Counter(by_metric[m]).at(cutoff) for m in covered}
        used = [i for i in sigen if i[1] <= cutoff]
        rows += synthesise(sigen, cutoff, anchors, timezone, offpeak_share, cheap_until)
        summary.update(
            sigen_rows=len(used),
            sigen_from=used[0][0] if used else None,
            sigen_until=used[-1][1] if used else None,
            sigen_counters=covered,
            sigen_skipped=[m for m in in_file if m not in by_metric],
            cutoff=cutoff,
            # If the export covers the system's whole life, these should be close to zero.
            counters_at_start={
                m: round(anchors[m] - sum(i[2][m] for i in used), 2) for m in covered
            },
        )

    for metric, series in by_metric.items():
        # Counters covered by the daily export start at the cutoff; the others keep all
        # the hourly history they have.
        if cutoff and metric in covered:
            series = [s for s in series if s[0] >= cutoff]
        rows += [(when, metric, value) for when, value in series]
        if series:
            summary["hourly"][metric] = (series[0][0], series[-1][0], len(series))
    return rows, summary


def store(db: Database, rows: list[Row], replace: list[str], before: datetime | None) -> tuple:
    """Store the rows. Returns (readings removed, readings added).

    Readings of the `replace` counters from before `before` are removed first. That period
    is rebuilt from the export every time, so an hourly export cleanly replaces an earlier
    daily one. Nothing collected live is that old.
    """
    removed = db.delete_before(replace, before) if replace and before is not None else 0
    return removed, db.insert_readings(rows)


def run(
    settings: Settings,
    db: Database,
    sigen_path: Path | None = None,
    offpeak_share: float = 0.99,
    cheap_until: time = time(6, 0),
    dry_run: bool = False,
) -> list[str]:
    """Do the import. Returns a plain-English report, one line per item."""
    if not settings.collecting:
        raise RuntimeError("No Home Assistant connection is configured")
    timezone = settings.timezone
    counters = [m for m in load_metrics() if m.kind == "energy" and m.name.endswith("_total")]
    metric_by_entity = {m.entity: m.name for m in counters}

    result = asyncio.run(
        fetch_hourly_statistics(
            settings.ha_websocket_url, settings.ha_token, list(metric_by_entity)
        )
    )
    kind, sigen = read_sigen(sigen_path, timezone) if sigen_path else ("", [])
    rows, summary = build_rows(
        parse_statistics(result),
        metric_by_entity,
        sigen,
        timezone,
        offpeak_share if kind == "daily" else None,
        cheap_until,
    )

    def day(when: datetime, pattern: str = "%d %b %Y") -> str:
        return when.astimezone(timezone).strftime(pattern)

    report: list[str] = []
    if summary["sigen_rows"]:
        report.append(
            f"Sigenergy {kind} export: {summary['sigen_rows']} rows, "
            f"{day(summary['sigen_from'], '%d %b %Y %H:%M')} to "
            f"{day(summary['sigen_until'], '%d %b %Y %H:%M')}"
        )
        if kind == "daily":
            report.append(
                f"No times in a daily export: {offpeak_share:.0%} of each day's import "
                f"placed before {cheap_until:%H:%M}"
            )
        report.append("Covers: " + ", ".join(summary["sigen_counters"]))
        if summary["sigen_skipped"]:
            report.append(
                "Left out (no later history in Home Assistant): "
                + ", ".join(summary["sigen_skipped"])
            )
        report.append(
            f"Counters at the start of the export (expect about 0): {summary['counters_at_start']}"
        )
    for metric, (first, last, count) in sorted(summary["hourly"].items()):
        report.append(
            f"Home Assistant hourly {metric}: {day(first)} to {day(last)} ({count} readings)"
        )
    if not summary["hourly"]:
        report.append("Home Assistant has no hourly history for these counters yet.")

    if dry_run:
        report.append(f"Dry run: {len(rows)} readings would be imported. Nothing was changed.")
        return report
    removed, added = store(db, rows, summary.get("sigen_counters", []), summary.get("cutoff"))
    if removed:
        report.append(f"Replaced {removed} earlier readings from before {day(summary['cutoff'])}.")
    report.append(f"Imported {added} new readings ({len(rows) - added} were already there).")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Import older history into the energy tracker.")
    parser.add_argument("--sigen", type=Path, help="Sigenergy hourly or daily export (.xlsx)")
    parser.add_argument(
        "--offpeak-share",
        type=float,
        default=0.99,
        help="for a DAILY export only: share of each day's import assumed to be in the "
        "cheap window (default 0.99). Not used for an hourly export.",
    )
    parser.add_argument(
        "--cheap-until",
        default="06:00",
        help="for a DAILY export only: when the cheap window ends (default 06:00)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported")
    args = parser.parse_args()
    if not 0 <= args.offpeak_share <= 1:
        parser.error("--offpeak-share must be between 0 and 1")

    settings = load_settings()
    report = run(
        settings,
        Database(settings.database_path),
        args.sigen,
        args.offpeak_share,
        time.fromisoformat(args.cheap_until),
        args.dry_run,
    )
    print("\n".join(report))


if __name__ == "__main__":
    main()
