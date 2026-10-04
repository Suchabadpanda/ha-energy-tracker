import pytest

from energy_tracker.normalise import normalise


@pytest.mark.parametrize(
    ("state", "unit", "kind", "expected"),
    [
        ("240.4", "W", "power", 0.2404),
        ("-3.668", "kW", "power", -3.668),
        ("4.36167", "MWh", "energy", 4361.67),
        ("7.24", "kWh", "energy", 7.24),
        ("500", "Wh", "energy", 0.5),
        ("8.9", "%", "percent", 8.9),
    ],
)
def test_converts_to_standard_units(state, unit, kind, expected):
    assert normalise(state, unit, kind) == pytest.approx(expected)


@pytest.mark.parametrize("state", ["unavailable", "unknown", "", None, "Unavailable", "abc", "nan"])
def test_unusable_states_return_none(state):
    assert normalise(state, "kW", "power") is None


def test_unit_that_does_not_match_kind_returns_none():
    assert normalise("5", "V", "power") is None
    assert normalise("5", None, "energy") is None
