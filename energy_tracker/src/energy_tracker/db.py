"""All database access. Readings and tariff rates live in one SQLite file.

SQLite needs no server and suits a small always-on box. Times are stored as whole seconds
since 1970 (UTC), which keeps the file small and time arithmetic simple.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

Row = tuple[datetime, str, float]
Sample = tuple[datetime, float]

SCHEMA = """
    CREATE TABLE IF NOT EXISTS readings (
        metric TEXT    NOT NULL,
        time   INTEGER NOT NULL,
        value  REAL    NOT NULL,
        PRIMARY KEY (metric, time)
    ) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS tariff_periods (
        effective_from            TEXT PRIMARY KEY,
        name                      TEXT NOT NULL,
        export_p_per_kwh          REAL NOT NULL,
        standing_charge_p_per_day REAL NOT NULL,
        vat_percent               REAL NOT NULL DEFAULT 0,
        import_bands              TEXT NOT NULL
    );

    -- Published half-hourly prices (pence per kWh including VAT), by where they came from.
    CREATE TABLE IF NOT EXISTS slot_prices (
        source TEXT    NOT NULL,
        time   INTEGER NOT NULL,
        price  REAL    NOT NULL,
        PRIMARY KEY (source, time)
    ) WITHOUT ROWID;

    CREATE TABLE IF NOT EXISTS comparison_tariffs (
        id                        INTEGER PRIMARY KEY,
        name                      TEXT NOT NULL,
        export_p_per_kwh          REAL NOT NULL,
        standing_charge_p_per_day REAL NOT NULL,
        vat_percent               REAL NOT NULL DEFAULT 0,
        import_bands              TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS extra_income (
        id          INTEGER PRIMARY KEY,
        day         TEXT NOT NULL,            -- local date the income belongs to
        description TEXT NOT NULL,
        amount_gbp  REAL,                     -- empty until an event has been measured
        source      TEXT NOT NULL DEFAULT 'manual',
        event_start INTEGER UNIQUE,           -- for recorded events: when it ran
        event_end   INTEGER,
        kwh         REAL,
        estimated   INTEGER NOT NULL DEFAULT 0,
        hidden      INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS bills (
        id          INTEGER PRIMARY KEY,
        first_day   TEXT NOT NULL,
        last_day    TEXT NOT NULL,
        import_kwh  REAL,
        charge_gbp  REAL,
        export_kwh  REAL,
        export_gbp  REAL
    );

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


def _seconds(when: datetime) -> int:
    return int(when.timestamp())


def _when(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            # WAL lets the dashboard read while the collector writes.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            # Added in 0.16: tariffs that follow published half-hourly prices.
            for table in ("tariff_periods", "comparison_tariffs"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "dynamic" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN dynamic TEXT NOT NULL DEFAULT ''")
            # Added in 0.19: the day a fixed-price deal ends.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tariff_periods)")}
            if "fixed_until" not in columns:
                conn.execute("ALTER TABLE tariff_periods ADD COLUMN fixed_until TEXT")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    # --- readings --------------------------------------------------------------------------

    def insert_readings(self, rows: Iterable[Row]) -> int:
        """Store readings, leaving any that already exist. Returns how many were new."""
        data = [(metric, _seconds(when), value) for when, metric, value in rows]
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO readings VALUES (?, ?, ?)", data)
            return conn.total_changes - before

    def delete_before(self, metrics: list[str], before: datetime) -> int:
        """Remove all readings of these metrics older than `before`."""
        with closing(self._connect()) as conn, conn:
            start = conn.total_changes
            for metric in metrics:
                conn.execute(
                    "DELETE FROM readings WHERE metric = ? AND time < ?", (metric, _seconds(before))
                )
            return conn.total_changes - start

    def latest(self, metrics: list[str], since: datetime) -> dict[str, Sample]:
        """The most recent reading of each metric, ignoring anything older than `since`."""
        result: dict[str, Sample] = {}
        with closing(self._connect()) as conn:
            for metric in metrics:
                row = conn.execute(
                    "SELECT time, value FROM readings WHERE metric = ? AND time >= ? "
                    "ORDER BY time DESC LIMIT 1",
                    (metric, _seconds(since)),
                ).fetchone()
                if row:
                    result[metric] = (_when(row[0]), row[1])
        return result

    def series(self, metrics: list[str], since: datetime, bucket_seconds: int) -> dict[str, list]:
        """Average of each metric in every time bucket since `since`."""
        result: dict[str, list] = {}
        with closing(self._connect()) as conn:
            for metric in metrics:
                rows = conn.execute(
                    "SELECT (time / ?) * ? AS bucket, avg(value) FROM readings "
                    "WHERE metric = ? AND time >= ? GROUP BY bucket ORDER BY bucket",
                    (bucket_seconds, bucket_seconds, metric, _seconds(since)),
                ).fetchall()
                result[metric] = [(_when(bucket), value) for bucket, value in rows]
        return result

    def last_in_buckets(
        self, metrics: list[str], since: datetime, until: datetime, bucket_seconds: int
    ) -> dict[str, list[Sample]]:
        """The last reading of each metric in every time bucket between `since` and `until`.

        One reading per bucket is plenty for energy totals, and keeps this fast however
        often readings are collected.
        """
        result: dict[str, list[Sample]] = {}
        with closing(self._connect()) as conn:
            for metric in metrics:
                # SQLite returns `value` from the same row as max(time).
                rows = conn.execute(
                    "SELECT max(time), value FROM readings "
                    "WHERE metric = ? AND time >= ? AND time <= ? GROUP BY time / ? ORDER BY 1",
                    (metric, _seconds(since), _seconds(until), bucket_seconds),
                ).fetchall()
                result[metric] = [(_when(t), value) for t, value in rows]
        return result

    def first_time(self, metric: str) -> datetime | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT min(time) FROM readings WHERE metric = ?", (metric,)
            ).fetchone()
        return _when(row[0]) if row and row[0] is not None else None

    def minutes_with_readings(self, metrics: list[str], since: datetime) -> list[datetime]:
        """Every minute since `since` in which at least one reading was stored."""
        minutes: set[int] = set()
        with closing(self._connect()) as conn:
            for metric in metrics:
                minutes.update(
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT time / 60 FROM readings WHERE metric = ? AND time >= ?",
                        (metric, _seconds(since)),
                    )
                )
        return [_when(minute * 60) for minute in sorted(minutes)]

    def thin(self, metrics: list[str], older_than: datetime, keep_every_seconds: int = 300) -> int:
        """Keep only the last reading in each 5 minutes for readings older than `older_than`.

        Remembers how far it got, so each run only looks at newly aged readings.
        """
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'thinned_until'").fetchone()
            start, end = int(row[0]) if row else 0, _seconds(older_than)
            # Stop on a bucket boundary so no bucket is split across two runs.
            end -= end % keep_every_seconds
            if end <= start:
                return 0
            before = conn.total_changes
            for metric in metrics:
                conn.execute(
                    "DELETE FROM readings WHERE metric = :m AND time >= :a AND time < :b "
                    "AND time NOT IN (SELECT max(time) FROM readings WHERE metric = :m "
                    "AND time >= :a AND time < :b GROUP BY time / :every)",
                    {"m": metric, "a": start, "b": end, "every": keep_every_seconds},
                )
            conn.execute(
                "INSERT INTO meta VALUES ('thinned_until', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(end),),
            )
            return conn.total_changes - before - 1

    def delete_older_than(self, cutoff: datetime) -> int:
        """Remove every reading older than `cutoff`, whichever metric it belongs to."""
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            # One metric at a time, so each delete can use the (metric, time) index.
            metrics = [row[0] for row in conn.execute("SELECT DISTINCT metric FROM readings")]
            for metric in metrics:
                conn.execute(
                    "DELETE FROM readings WHERE metric = ? AND time < ?", (metric, _seconds(cutoff))
                )
            return conn.total_changes - before

    def size_bytes(self) -> int:
        """Space the database takes on disk, including its write-ahead log."""
        files = (self.path, self.path.with_name(self.path.name + "-wal"))
        return sum(f.stat().st_size for f in files if f.exists())

    # --- tariff periods ---------------------------------------------------------------------

    def tariff_rows(self) -> list[dict]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM tariff_periods ORDER BY effective_from").fetchall()
        return [
            {
                **dict(row),
                "effective_from": date.fromisoformat(row["effective_from"]),
                "import_bands": json.loads(row["import_bands"]),
            }
            for row in rows
        ]

    def save_tariff_row(self, row: dict) -> None:
        """Add a tariff period, or replace the one that starts on the same date."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO tariff_periods (effective_from, name, export_p_per_kwh, "
                "standing_charge_p_per_day, vat_percent, import_bands, dynamic, fixed_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["effective_from"].isoformat(),
                    row["name"],
                    row["export_p_per_kwh"],
                    row["standing_charge_p_per_day"],
                    row["vat_percent"],
                    json.dumps(row["import_bands"]),
                    row.get("dynamic") or "",
                    row["fixed_until"].isoformat() if row.get("fixed_until") else None,
                ),
            )

    def delete_tariff_row(self, effective_from: date) -> bool:
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            conn.execute(
                "DELETE FROM tariff_periods WHERE effective_from = ?", (effective_from.isoformat(),)
            )
            return conn.total_changes > before

    # --- published half-hourly prices --------------------------------------------------------

    def save_slot_prices(self, source: str, rows: Iterable[tuple[int, float]]) -> int:
        """Store prices, replacing any already held for the same half hours."""
        data = [(source, int(time), float(price)) for time, price in rows]
        with closing(self._connect()) as conn, conn:
            conn.executemany("INSERT OR REPLACE INTO slot_prices VALUES (?, ?, ?)", data)
        return len(data)

    def slot_prices(self, source: str) -> dict[int, float]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT time, price FROM slot_prices WHERE source = ?", (source,)
            ).fetchall()
        return dict(rows)

    def slot_price_range(self, source: str) -> tuple[int | None, int | None]:
        """The first and last half hour held for a source."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT min(time), max(time) FROM slot_prices WHERE source = ?", (source,)
            ).fetchone()
        return row[0], row[1]

    def delete_slot_prices_except(self, sources: Iterable[str]) -> int:
        """Drop prices no tariff uses any more."""
        keep = list(sources)
        marks = ",".join("?" * len(keep))
        with closing(self._connect()) as conn, conn:
            return conn.execute(
                f"DELETE FROM slot_prices WHERE source NOT IN ({marks})",  # noqa: S608
                keep,
            ).rowcount

    # --- small settings ---------------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO meta VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- tariffs to compare against ----------------------------------------------------------

    def comparison_rows(self) -> list[dict]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM comparison_tariffs ORDER BY id").fetchall()
        return [{**dict(row), "import_bands": json.loads(row["import_bands"])} for row in rows]

    def save_comparison_row(self, row: dict, row_id: int | None = None) -> int:
        """Add a tariff to compare against, or replace the one with this id."""
        values = (
            row["name"],
            row["export_p_per_kwh"],
            row["standing_charge_p_per_day"],
            row["vat_percent"],
            json.dumps(row["import_bands"]),
            row.get("dynamic") or "",
        )
        with closing(self._connect()) as conn, conn:
            if row_id is not None:
                cursor = conn.execute(
                    "UPDATE comparison_tariffs SET name = ?, export_p_per_kwh = ?, "
                    "standing_charge_p_per_day = ?, vat_percent = ?, import_bands = ?, "
                    "dynamic = ? WHERE id = ?",
                    (*values, row_id),
                )
                if cursor.rowcount:
                    return row_id
            cursor = conn.execute(
                "INSERT INTO comparison_tariffs (name, export_p_per_kwh, "
                "standing_charge_p_per_day, vat_percent, import_bands, dynamic) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            return cursor.lastrowid

    def delete_comparison_row(self, row_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            return (
                conn.execute("DELETE FROM comparison_tariffs WHERE id = ?", (row_id,)).rowcount > 0
            )

    # --- extra income ------------------------------------------------------------------------

    def income_rows(self) -> list[dict]:
        """Every entry that has not been removed, newest first."""
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM extra_income WHERE hidden = 0 ORDER BY day DESC, id DESC"
            ).fetchall()
        return [
            {
                **dict(row),
                "estimated": bool(row["estimated"]),
                "event_start": _when(row["event_start"]) if row["event_start"] else None,
                "event_end": _when(row["event_end"]) if row["event_end"] else None,
            }
            for row in rows
        ]

    def save_income(self, day: date, description: str, amount: float, row_id: int | None) -> int:
        """Add an entry typed in by hand, or correct an existing one (which stops it being
        an estimate)."""
        with closing(self._connect()) as conn, conn:
            if row_id is not None:
                cursor = conn.execute(
                    "UPDATE extra_income SET day = ?, description = ?, amount_gbp = ?, "
                    "estimated = 0 WHERE id = ? AND hidden = 0",
                    (day.isoformat(), description, amount, row_id),
                )
                if cursor.rowcount:
                    return row_id
            cursor = conn.execute(
                "INSERT INTO extra_income (day, description, amount_gbp) VALUES (?, ?, ?)",
                (day.isoformat(), description, amount),
            )
            return cursor.lastrowid

    def remove_income(self, row_id: int) -> bool:
        """Remove an entry. A recorded event is hidden, not deleted, so that the sensor
        still showing it does not bring it straight back."""
        with closing(self._connect()) as conn, conn:
            hidden = conn.execute(
                "UPDATE extra_income SET hidden = 1 WHERE id = ? AND source != 'manual'", (row_id,)
            ).rowcount
            deleted = conn.execute(
                "DELETE FROM extra_income WHERE id = ? AND source = 'manual'", (row_id,)
            ).rowcount
            return bool(hidden or deleted)

    def add_event(self, start: datetime, end: datetime, day: date, source: str = "axle") -> bool:
        """Record a grid event on the given local date. Returns False if already known."""
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO extra_income (day, description, source, event_start, "
                "event_end, estimated) VALUES (?, ?, ?, ?, ?, 1)",
                (
                    day.isoformat(),
                    "Axle export event",
                    source,
                    _seconds(start),
                    _seconds(end),
                ),
            )
            return cursor.rowcount > 0

    def unsettled_events(self, ended_before: datetime) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, event_start, event_end FROM extra_income "
                "WHERE source != 'manual' AND amount_gbp IS NULL AND hidden = 0 AND event_end <= ?",
                (_seconds(ended_before),),
            ).fetchall()
        return [{"id": r[0], "event_start": _when(r[1]), "event_end": _when(r[2])} for r in rows]

    def settle_event(self, row_id: int, kwh: float | None, amount: float) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE extra_income SET kwh = ?, amount_gbp = ? "
                "WHERE id = ? AND amount_gbp IS NULL",
                (kwh, amount, row_id),
            )

    # --- bills entered for checking -------------------------------------------------------------

    def bill_rows(self) -> list[dict]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM bills ORDER BY first_day DESC, id DESC").fetchall()
        return [dict(row) for row in rows]

    def save_bill(self, row: dict, row_id: int | None = None) -> int:
        values = (
            row["first_day"].isoformat(),
            row["last_day"].isoformat(),
            row["import_kwh"],
            row["charge_gbp"],
            row["export_kwh"],
            row["export_gbp"],
        )
        with closing(self._connect()) as conn, conn:
            if row_id is not None:
                cursor = conn.execute(
                    "UPDATE bills SET first_day = ?, last_day = ?, import_kwh = ?, charge_gbp = ?, "
                    "export_kwh = ?, export_gbp = ? WHERE id = ?",
                    (*values, row_id),
                )
                if cursor.rowcount:
                    return row_id
            cursor = conn.execute(
                "INSERT INTO bills (first_day, last_day, import_kwh, charge_gbp, export_kwh, "
                "export_gbp) VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            return cursor.lastrowid

    def delete_bill(self, row_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM bills WHERE id = ?", (row_id,)).rowcount > 0
