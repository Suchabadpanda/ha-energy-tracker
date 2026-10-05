"""Keeps tariff periods in the database so they can be changed from the dashboard."""

from __future__ import annotations

from datetime import date

from . import prices
from .config import BUNDLED_TARIFF_FILE
from .db import Database
from .tariff import Schedule, Tariff, build_tariff, load_tariff


def _row(tariff: Tariff) -> dict:
    return {
        "effective_from": tariff.effective_from,
        "name": tariff.name,
        "export_p_per_kwh": tariff.export_p_per_kwh,
        "standing_charge_p_per_day": tariff.standing_charge_p_per_day,
        "vat_percent": tariff.vat_percent,
        "dynamic": tariff.dynamic,
        "import_bands": [
            {"start": b.start, "end": b.end, "p_per_kwh": b.p_per_kwh} for b in tariff.import_bands
        ],
    }


def load_periods(db: Database) -> list[Tariff]:
    """All tariff periods, oldest first. The first time, starts from config/tariff.toml."""
    rows = db.tariff_rows()
    if not rows:
        db.save_tariff_row(_row(load_tariff(BUNDLED_TARIFF_FILE)))
        rows = db.tariff_rows()
    return [build_tariff(**row, slot_prices=prices.for_row(db, row)) for row in rows]


def load_schedule(db: Database) -> Schedule:
    return Schedule(load_periods(db))


def save_period(db: Database, tariff: Tariff) -> None:
    """Add a period, or replace the one that starts on the same date."""
    load_periods(db)  # make sure the starting period exists first
    db.save_tariff_row(_row(tariff))


def delete_period(db: Database, effective_from: date) -> bool:
    """Remove a period. Refuses to remove the last one. Returns False if nothing matched."""
    if len(load_periods(db)) <= 1:
        raise ValueError("At least one tariff period must remain")
    return db.delete_tariff_row(effective_from)
