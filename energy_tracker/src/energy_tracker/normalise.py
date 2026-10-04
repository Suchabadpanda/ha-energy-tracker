"""Convert raw Home Assistant states into numbers in standard units (kW, kWh, %)."""

from __future__ import annotations

import math

# Multiply the raw value by this factor to get the standard unit for each kind.
_FACTORS: dict[str, dict[str, float]] = {
    "power": {"W": 0.001, "kW": 1.0, "MW": 1000.0},
    "energy": {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0},
    "percent": {"%": 1.0},
}

_NOT_A_READING = {"", "unavailable", "unknown", "none"}


def normalise(state: str | None, unit: str | None, kind: str) -> float | None:
    """Return the reading in the standard unit, or None if it isn't usable.

    None is returned (rather than raising) for sensors that are offline,
    non-numeric, or reporting a unit we don't know how to convert.
    """
    if state is None or state.strip().lower() in _NOT_A_READING:
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None

    factor = _FACTORS.get(kind, {}).get(unit or "")
    if factor is None:
        return None
    return value * factor
