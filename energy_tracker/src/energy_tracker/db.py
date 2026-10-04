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

    CREATE TABLE IF NOT EXISTS comparison_tariffs (
        id                        INTEGER PRIMARY KEY,
        name                      TEXT NOT NULL,
        export_p_per_kwh          REAL NOT NULL,
        standing_charge_p_per_day REAL NOT NULL,
        vat_percent               REAL NOT NULL DEFAULT 0,
        import_bands              TEXT NOT NULL
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
                "standing_charge_p_per_day, vat_percent, import_bands) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["effective_from"].isoformat(),
                    row["name"],
                    row["export_p_per_kwh"],
                    row["standing_charge_p_per_day"],
                    row["vat_percent"],
                    json.dumps(row["import_bands"]),
                ),
            )

    def delete_tariff_row(self, effective_from: date) -> bool:
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            conn.execute(
                "DELETE FROM tariff_periods WHERE effective_from = ?", (effective_from.isoformat(),)
            )
            return conn.total_changes > before

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
        )
        with closing(self._connect()) as conn, conn:
            if row_id is not None:
                cursor = conn.execute(
                    "UPDATE comparison_tariffs SET name = ?, export_p_per_kwh = ?, "
                    "standing_charge_p_per_day = ?, vat_percent = ?, import_bands = ? WHERE id = ?",
                    (*values, row_id),
                )
                if cursor.rowcount:
                    return row_id
            cursor = conn.execute(
                "INSERT INTO comparison_tariffs (name, export_p_per_kwh, "
                "standing_charge_p_per_day, vat_percent, import_bands) VALUES (?, ?, ?, ?, ?)",
                values,
            )
            return cursor.lastrowid

    def delete_comparison_row(self, row_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            return (
                conn.execute("DELETE FROM comparison_tariffs WHERE id = ?", (row_id,)).rowcount > 0
            )
