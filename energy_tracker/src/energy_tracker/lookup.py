"""Look up published tariff prices, to save typing them in.

Only Octopus Energy is supported: it is the one UK supplier with a public price list that
needs no account. Nothing about the user is sent: only a region letter and a tariff code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://api.octopus.energy/v1"
MINUTES_PER_DAY = 24 * 60

# Electricity regions (the letter ends every regional tariff code).
REGIONS = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and North Wales",
    "E": "West Midlands",
    "F": "North East England",
    "G": "North West England",
    "H": "Southern England",
    "J": "South East England",
    "K": "South Wales",
    "L": "South West England",
    "M": "Yorkshire",
    "N": "Southern Scotland",
    "P": "Northern Scotland",
}


class PriceLookupError(Exception):
    """The lookup could not be completed; the message is fit to show to the user."""


def _get(client: httpx.Client, url: str, **params: object) -> dict:
    try:
        response = client.get(url, params=params or None)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PriceLookupError(f"Could not reach Octopus Energy's price list ({exc})") from exc


def _all_products(client: httpx.Client) -> list[dict]:
    products: list[dict] = []
    url: str | None = f"{BASE_URL}/products/"
    params: dict = {"brand": "OCTOPUS_ENERGY", "is_business": "false"}
    for _ in range(5):  # a handful of pages at most
        page = _get(client, url, **params)
        products += page.get("results", [])
        url, params = page.get("next"), {}
        if not url:
            break
    return [p for p in products if p.get("direction") == "IMPORT" and not p.get("is_prepay")]


def _named(products: list[dict]) -> list[dict]:
    named = [{"code": p["code"], "name": p.get("full_name") or p["code"]} for p in products]
    return sorted(named, key=lambda p: p["name"])


def product_lists(client: httpx.Client) -> tuple[list[dict], list[dict]]:
    """Import tariffs on sale now: those with the same prices every day, and those whose
    price changes every half hour (Agile)."""
    products = _all_products(client)
    half_hourly = [p for p in products if p["code"].startswith("AGILE")]
    # Agile has no fixed daily pattern, and a tracker's price changes daily: neither can be
    # typed in as a set of time windows.
    fixed = [p for p in products if p not in half_hourly and not p.get("is_tracker")]
    return _named(fixed), _named(half_hourly)


def list_products(client: httpx.Client) -> list[dict]:
    """Import tariffs on sale now that have the same prices every day."""
    return product_lists(client)[0]


def fetch_slot_prices(
    client: httpx.Client, product: str, region: str, start: datetime, end: datetime
) -> list[tuple[int, float]]:
    """Every half hour's price between two moments, as (start in seconds since 1970, pence
    per kWh including VAT)."""
    if region not in REGIONS:
        raise PriceLookupError("Choose a region")
    stamp = "%Y-%m-%dT%H:%MZ"
    url: str | None = (
        f"{BASE_URL}/products/{product}/electricity-tariffs/"
        f"E-1R-{product}-{region}/standard-unit-rates/"
    )
    params: dict = {
        "period_from": start.astimezone(UTC).strftime(stamp),
        "period_to": end.astimezone(UTC).strftime(stamp),
        "page_size": 1500,
    }
    rates: list[dict] = []
    for _ in range(400):  # 1,500 half hours a page: far more than ten years
        page = _get(client, url, **params)
        rates += page.get("results", [])
        url, params = page.get("next"), {}
        if not url:
            break
    preferred = [r for r in rates if r.get("payment_method") in (None, "DIRECT_DEBIT")] or rates
    first, last = int(start.timestamp()), int(end.timestamp())
    prices: dict[int, float] = {}
    for rate in preferred:
        begins = int(_moment(rate["valid_from"]).timestamp())
        finish = _moment(rate.get("valid_to"))
        ends = int(finish.timestamp()) if finish else last
        slot = -(-max(begins, first) // 1800) * 1800  # the next half hour boundary
        while slot < min(ends, last):
            prices[slot] = round(float(rate["value_inc_vat"]), 4)
            slot += 1800
    return sorted(prices.items())


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC) if text else None


def bands_from_rates(rates: list[dict], day: date, timezone: ZoneInfo) -> list[dict]:
    """Turn a list of dated unit rates into one day's price bands, in local time.

    Looks up the price for each half hour of `day`, then joins neighbouring half hours that
    cost the same.
    """
    # Where a tariff has different prices by payment method, use the direct debit one.
    preferred = [r for r in rates if r.get("payment_method") in (None, "DIRECT_DEBIT")] or rates
    periods = [(_moment(r["valid_from"]), _moment(r.get("valid_to")), r) for r in preferred]

    def price_at(when: datetime) -> float:
        for start, end, rate in periods:
            if start <= when and (end is None or when < end):
                return round(float(rate["value_inc_vat"]), 4)
        raise PriceLookupError("Octopus Energy has not published prices for the whole of today")

    midnight = datetime.combine(day, time(0), timezone)
    bands: list[dict] = []
    for minute in range(0, MINUTES_PER_DAY, 30):
        price = price_at((midnight + timedelta(minutes=minute)).astimezone(UTC))
        if bands and bands[-1]["p_per_kwh"] == price:
            bands[-1]["end_minute"] = minute + 30
        else:
            bands.append({"start_minute": minute, "end_minute": minute + 30, "p_per_kwh": price})

    def clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    return [
        {
            "start": clock(b["start_minute"]),
            "end": clock(b["end_minute"]),
            "p_per_kwh": b["p_per_kwh"],
        }
        for b in bands
    ]


def fetch_tariff(
    client: httpx.Client, product: str, region: str, timezone: ZoneInfo, day: date
) -> dict:
    """Today's prices for one tariff in one region, in the shape the comparison form uses.

    Prices include VAT as published, so VAT to add is 0.
    """
    if region not in REGIONS:
        raise PriceLookupError("Choose a region")
    detail = _get(client, f"{BASE_URL}/products/{product}/")
    by_payment = detail.get("single_register_electricity_tariffs", {}).get(f"_{region}")
    if not by_payment:
        raise PriceLookupError("That tariff has no single-meter prices for this region")
    tariff = by_payment.get("direct_debit_monthly") or next(iter(by_payment.values()))
    rates_url = next(
        (
            link["href"]
            for link in tariff.get("links", [])
            if link.get("rel") == "standard_unit_rates"
        ),
        None,
    )
    if not rates_url:
        raise PriceLookupError("Octopus Energy did not list unit rates for that tariff")
    # Newest first; one page comfortably covers today.
    rates = _get(client, rates_url, page_size=200).get("results", [])
    if not rates:
        raise PriceLookupError("Octopus Energy did not list unit rates for that tariff")
    try:
        bands = bands_from_rates(rates, day, timezone)
    except PriceLookupError:
        # Today's prices are not all published yet: yesterday's show the same daily pattern.
        day -= timedelta(days=1)
        bands = bands_from_rates(rates, day, timezone)
    return {
        "name": f"{detail.get('full_name', product)} ({REGIONS[region]})",
        "import_bands": bands,
        "standing_charge_p_per_day": round(float(tariff["standing_charge_inc_vat"]), 4),
        "vat_percent": 0.0,
        "source": f"Octopus Energy {tariff.get('code', product)}: prices including VAT, "
        f"as published for {day:%d %b %Y}",
    }
