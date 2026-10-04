import os
import tempfile
from pathlib import Path

# The app reads its settings once, when first used: point it at a throwaway database and
# make sure it does not try to reach a real Home Assistant.
os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
for name in ("SUPERVISOR_TOKEN", "HA_URL", "HA_TOKEN", "ALLOWED_CLIENT", "METRICS_FILE"):
    os.environ.pop(name, None)

import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def empty_database():
    """Start every test from an empty database, so none depends on another or on the time
    of day it happens to run."""
    from energy_tracker import api, collector

    database = api.database()
    with closing(sqlite3.connect(database.path)) as conn, conn:
        for table in ("readings", "tariff_periods", "comparison_tariffs", "extra_income", "meta"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
    api.clear_caches()
    collector.missing.clear()
    yield
