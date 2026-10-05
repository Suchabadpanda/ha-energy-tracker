"""Read the figures off an electricity bill PDF, to save typing them into Bill check.

Works on the text inside the PDF, entirely on this machine: nothing is sent anywhere and
the file is not kept. Built around the layout E.ON Next and Octopus Energy share (both use
the same billing system); other suppliers' bills may give some figures or none. Whatever is
found is only a starting point for the person to check against the bill.
"""

from __future__ import annotations

import io
import re
from datetime import date

MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
NUMBER = r"([\d,]+(?:\.\d+)?)"
DAY = r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(\d{4})"
# A tariff heading such as "Next Drive Fixed V7 (6th December 2025 - 5th January 2026)".
PERIOD = re.compile(rf"\(\s*{DAY}\s*-\s*{DAY}\s*\)")


class BillError(Exception):
    """The bill could not be read; the message is fit to show to the user."""


def pdf_text(data: bytes) -> str:
    """All the text in a PDF, page after page."""
    from pypdf import PdfReader  # imported here: only needed when a bill is uploaded
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise BillError("That PDF is password-protected")
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:20])
    except BillError:
        raise
    except (PyPdfError, ValueError, KeyError, OSError) as exc:
        raise BillError("That file could not be read as a PDF") from exc
    if len(text.strip()) < 50:
        raise BillError(
            "No text was found in that PDF. A scan or a photo cannot be read: "
            "use the PDF downloaded from your supplier."
        )
    return text


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _day(day: str, month: str, year: str) -> date | None:
    number = MONTHS.get(month[:3].lower())
    try:
        return date(int(year), number, int(day)) if number else None
    except ValueError:
        return None


def electricity_section(text: str) -> str:
    """The part of the bill that itemises electricity, leaving out gas and the small print."""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*your charges in detail\.?\s*", line, re.IGNORECASE):
            start = index
            break
    end = len(lines)
    for index in range(start + 1, len(lines)):
        # A line saying just "Gas" opens the gas charges.
        if lines[index].strip().lower() == "gas":
            end = index
            break
    return "\n".join(lines[start:end])


def read_text(text: str) -> dict:
    """Pick the Bill check figures out of a bill's text.

    Returns the fields found (others are None), the unit rates and standing charge quoted on
    the bill, and notes about anything the reader should look at.
    """
    section = electricity_section(text)
    found: dict = {
        "first_day": None,
        "last_day": None,
        "import_kwh": None,
        "charge_gbp": None,
        "export_kwh": None,
        "export_gbp": None,
    }
    notes: list[str] = []

    periods = [(_day(*m.groups()[:3]), _day(*m.groups()[3:])) for m in PERIOD.finditer(section)]
    periods = [(a, b) for a, b in periods if a and b and a <= b]
    if periods:
        found["first_day"] = min(a for a, _ in periods)
        found["last_day"] = max(b for _, b in periods)
        if len(periods) > 1:
            notes.append("The bill covers more than one tariff period; the dates span all of them.")

    def first(pattern: str) -> float | None:
        match = re.search(pattern, section, re.IGNORECASE)
        return _number(match.group(1)) if match else None

    def total(pattern: str) -> float | None:
        values = [_number(m.group(1)) for m in re.finditer(pattern, section, re.IGNORECASE)]
        return round(sum(values), 3) if values else None

    exported = total(rf"electricity exported\s*{NUMBER}\s*kWh")
    if exported is not None:
        found["export_kwh"] = exported
        found["export_gbp"] = total(rf"total electricity credits\s*£\s*{NUMBER}")
    else:
        found["import_kwh"] = total(rf"total consumption\s*{NUMBER}\s*kWh") or total(
            rf"energy used\s*{NUMBER}\s*kWh"
        )
        found["charge_gbp"] = total(rf"total electricity charges\s*£\s*{NUMBER}")

    # Each price with the units charged at it, as "6.381p/kWh 701.0 kWh" or "31.4 kWh @ 25.16p/kWh".
    rates = [
        {"p_per_kwh": _number(m.group(1)), "kwh": _number(m.group(2))}
        for m in re.finditer(rf"{NUMBER}p/kWh\s*{NUMBER}\s*kWh", section)
    ]
    if not rates:
        rates = [
            {"p_per_kwh": _number(m.group(2)), "kwh": _number(m.group(1))}
            for m in re.finditer(
                rf"(?:energy used|electricity exported)\s*{NUMBER}\s*kWh\s*@\s*{NUMBER}p/kWh",
                section,
                re.IGNORECASE,
            )
        ]
    if re.search(r"estimated reading|\(estimated\)", text, re.IGNORECASE):
        notes.append("The bill uses an estimated meter reading, so its units may not match.")
    if all(value is None for key, value in found.items() if key.endswith(("kwh", "gbp"))):
        raise BillError(
            "No electricity figures could be found on that bill. Its layout is not one this "
            "reader knows; please type the figures in."
        )
    missing = [key for key in ("first_day", "last_day") if found[key] is None]
    if missing:
        notes.append("The billing dates could not be found; please enter them.")
    return {
        "found": found,
        "kind": "export" if exported is not None else "import",
        "rates": rates,
        "standing_charge_p_per_day": first(rf"days\s*@\s*{NUMBER}p/day"),
        "vat_percent": first(rf"VAT\s*@\s*{NUMBER}\s*%"),
        "notes": notes,
    }


def read_pdf(data: bytes) -> dict:
    return read_text(pdf_text(data))
