from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, lookup

LONDON = ZoneInfo("Europe/London")
client = TestClient(api.app)

PRODUCTS = {
    "next": None,
    "results": [
        {"code": "GO-VAR-22-10-14", "full_name": "Octopus Go", "direction": "IMPORT"},
        {"code": "VAR-22-11-01", "full_name": "Flexible Octopus", "direction": "IMPORT"},
        {"code": "AGILE-24-10-01", "full_name": "Agile Octopus", "direction": "IMPORT"},
        {"code": "OUTGOING-VAR-24-10-26", "full_name": "Outgoing Octopus", "direction": "EXPORT"},
        {
            "code": "PREPAY-VAR-18-09-21",
            "full_name": "Key",
            "direction": "IMPORT",
            "is_prepay": True,
        },
    ],
}
RATES_URL = "https://api.octopus.energy/v1/products/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-H/standard-unit-rates/"
GO_DETAIL = {
    "full_name": "Octopus Go",
    "single_register_electricity_tariffs": {
        "_H": {
            "direct_debit_monthly": {
                "code": "E-1R-GO-VAR-22-10-14-H",
                "standing_charge_exc_vat": 47.0544,
                "standing_charge_inc_vat": 47.0544,
                "links": [{"href": RATES_URL, "rel": "standard_unit_rates"}],
            }
        }
    },
}
# As published: newest first, times in UTC. In British Summer Time 23:30Z is 00:30 local.
GO_RATES = {
    "results": [
        {
            "value_inc_vat": 29.7953,
            "valid_from": "2026-10-05T04:30:00Z",
            "valid_to": "2026-10-05T23:30:00Z",
        },
        {
            "value_inc_vat": 8.2143,
            "valid_from": "2026-10-04T23:30:00Z",
            "valid_to": "2026-10-05T04:30:00Z",
        },
        {
            "value_inc_vat": 29.7953,
            "valid_from": "2026-10-04T04:30:00Z",
            "valid_to": "2026-10-04T23:30:00Z",
        },
        {
            "value_inc_vat": 8.2143,
            "valid_from": "2026-10-03T23:30:00Z",
            "valid_to": "2026-10-04T04:30:00Z",
        },
        {
            "value_inc_vat": 29.7953,
            "valid_from": "2026-10-03T04:30:00Z",
            "valid_to": "2026-10-03T23:30:00Z",
        },
    ]
}


def fake_octopus(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/products/":
        return httpx.Response(200, json=PRODUCTS)
    if path == "/v1/products/GO-VAR-22-10-14/":
        return httpx.Response(200, json=GO_DETAIL)
    if path.endswith("/standard-unit-rates/"):
        return httpx.Response(200, json=GO_RATES)
    return httpx.Response(404)


@pytest.fixture
def octopus(monkeypatch):
    monkeypatch.setattr(
        api, "lookup_client", lambda: httpx.Client(transport=httpx.MockTransport(fake_octopus))
    )


def test_only_tariffs_with_a_daily_pattern_are_offered(octopus):
    data = client.get("/api/lookup/octopus").json()
    assert [p["code"] for p in data["products"]] == ["VAR-22-11-01", "GO-VAR-22-10-14"]
    assert {"code": "H", "name": "Southern England"} in data["regions"]


def test_rates_become_local_time_bands():
    bands = lookup.bands_from_rates(GO_RATES["results"], date(2026, 10, 4), LONDON)
    assert bands == [
        {"start": "00:00", "end": "00:30", "p_per_kwh": 29.7953},
        {"start": "00:30", "end": "05:30", "p_per_kwh": 8.2143},
        {"start": "05:30", "end": "24:00", "p_per_kwh": 29.7953},
    ]


def test_a_flat_tariff_is_one_band_and_prefers_direct_debit_prices():
    rates = [
        {"value_inc_vat": 30.0, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None,
         "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 27.5, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None,
         "payment_method": "DIRECT_DEBIT"},
    ]  # fmt: skip
    assert lookup.bands_from_rates(rates, date(2026, 10, 4), LONDON) == [
        {"start": "00:00", "end": "24:00", "p_per_kwh": 27.5}
    ]


def test_missing_prices_are_reported_not_guessed():
    with pytest.raises(lookup.PriceLookupError):
        lookup.bands_from_rates(GO_RATES["results"], date(2026, 10, 20), LONDON)


class DayOfTheSamplePrices(datetime):
    """The sample prices above cover 3 to 5 October 2026: look them up as if on the 5th."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 5, 12, tzinfo=tz)


def test_lookup_fills_the_form_and_can_be_saved_for_comparison(octopus, monkeypatch):
    monkeypatch.setattr(api, "datetime", DayOfTheSamplePrices)
    found = client.get(
        "/api/lookup/octopus/tariff", params={"product": "GO-VAR-22-10-14", "region": "H"}
    )
    assert found.status_code == 200
    tariff = found.json()
    assert tariff["name"] == "Octopus Go (Southern England)"
    assert tariff["standing_charge_p_per_day"] == 47.0544 and tariff["vat_percent"] == 0
    assert len(tariff["import_bands"]) == 3

    assert (
        client.get("/api/lookup/octopus/tariff", params={"product": "x", "region": "H"}).status_code
        == 422
    )
    assert (
        client.get(
            "/api/lookup/octopus/tariff", params={"product": "NOPE-1", "region": "H"}
        ).status_code
        == 502
    )


def test_comparison_replays_the_same_usage_on_other_tariffs():
    now = datetime.now(UTC).replace(microsecond=0)
    rows = []
    for half_hours in range(0, 48 * 12):
        when = now - timedelta(minutes=30 * half_hours)
        rows.append((when, "import_energy_total", 5000 - half_hours * 0.5))  # 1 kW all day
        rows.append((when, "export_energy_total", 800.0))
    api.database().insert_readings(rows)

    def add(name, bands, standing=0.0):
        body = {"name": name, "export_p_per_kwh": 10, "standing_charge_p_per_day": standing,
                "vat_percent": 0, "import_bands": bands}  # fmt: skip
        response = client.post("/api/compare/tariffs", json=body)
        assert response.status_code == 200, response.text
        return response.json()["id"]

    flat = add("Flat 20p", [{"start": "00:00", "end": "24:00", "p_per_kwh": 20}])
    add("Flat 40p", [{"start": "00:00", "end": "24:00", "p_per_kwh": 40}])

    data = client.get("/api/compare", params={"period": "30d"}).json()
    by_name = {c["name"]: c for c in data["candidates"]}
    kwh = data["actual"]["import_kwh"]
    assert kwh > 100
    assert by_name["Flat 20p"]["cost"]["import_gbp"] == pytest.approx(kwh * 0.20, abs=0.02)
    assert by_name["Flat 40p"]["cost"]["import_gbp"] == pytest.approx(
        2 * by_name["Flat 20p"]["cost"]["import_gbp"], abs=0.05
    )
    assert by_name["Flat 40p"]["difference_gbp"] > by_name["Flat 20p"]["difference_gbp"]
    assert by_name["Flat 20p"]["difference_gbp"] == pytest.approx(
        by_name["Flat 20p"]["cost"]["net_gbp"] - data["actual"]["net_gbp"], abs=0.011
    )

    # Editing replaces in place; removing takes it off the list.
    client.post(
        f"/api/compare/tariffs?id={flat}",
        json={
            "name": "Flat 25p",
            "export_p_per_kwh": 10,
            "standing_charge_p_per_day": 0,
            "vat_percent": 0,
            "import_bands": [{"start": "00:00", "end": "24:00", "p_per_kwh": 25}],
        },
    )
    names = [c["name"] for c in client.get("/api/compare?period=30d").json()["candidates"]]
    assert "Flat 25p" in names and "Flat 20p" not in names
    assert client.delete(f"/api/compare/tariffs/{flat}").status_code == 200
    assert client.delete(f"/api/compare/tariffs/{flat}").status_code == 404

    gap = [{"start": "00:00", "end": "12:00", "p_per_kwh": 1}]
    bad = {
        "name": "Bad",
        "export_p_per_kwh": 0,
        "standing_charge_p_per_day": 0,
        "import_bands": gap,
    }
    assert client.post("/api/compare/tariffs", json=bad).status_code == 422
