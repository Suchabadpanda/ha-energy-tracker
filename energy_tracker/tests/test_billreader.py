from datetime import date

import pytest
from fastapi.testclient import TestClient

from energy_tracker import api, billreader

# Made-up bills in the layout E.ON Next and Octopus Energy use. No real customer details.
IMPORT_BILL = """Example Energy Limited
 Page 1/4
Your account number: A-00000000
A Customer
1 Example Street
Your energy account.
8 Dec 2025 - 7 Jan 2026
We have charged you (VAT is included)
Electricity
6 Dec 2025 - 5 Jan 2026
 £61.82 DR
Gas
6 Dec 2025 - 5 Jan 2026
 £111.44 DR
Your charges in detail.
Electricity
Supply number
 S
Next Drive Fixed V7 (6th December 2025 - 5th January 2026)
Electricity charges for meter 00X0000000
Rate
Consumption
 Cost
6.381p/kWh
701.0 kWh
 £44.73
27.092p/kWh
3.1 kWh
 £0.83
Total consumption
704.0kWh @ 6.47p/kWh
 £45.56
Standing charge
31 days @ 42.978p/day
 £13.32
Subtotal of charges before VAT
 £58.88
VAT @ 5%
 £2.94
Total electricity charges
 £61.82
Your electricity tariff.
Unit rate
Gas
Meter point reference
Next Drive Fixed Gas V7 (6th December 2025 - 7th January 2026)
Energy used*
1613.7 kWh @ 5.985p/kWh
 £96.58
Standing charge
31 days @ 30.819p/day
Total gas charges
 £111.44
"""

EXPORT_BILL = """Your charges in detail.
Electricity
Next Export Exclusive v2 (17th Nov 2025 - 1st Apr 2026)
Electricity exported for meter 00X0000000
17 Nov 2025
9.0 Customer reading
2 Apr 2026
591.8 Smart meter reading
Electricity exported
582.8 kWh @ 16.500p/kWh
 £96.16
Standing charge
136 days @ 0.000p/day
 £0.00
VAT @ 0%
 £0.00
Total electricity credits
 £96.16
"""

SINGLE_RATE_BILL = """Your Charges In Detail
Electricity
Flexible Octopus (5th September 2026 - 11th September 2026)
Energy Charges for Meter 00X0000000
5th Sep 2026
5278.3 Smart meter reading
12th Sep 2026
5309.7 Estimated reading
Energy Used
31.4 kWh @ 25.16p/kWh
£7.91
Standing Charge
7 days @ 44.20p/day
£3.09
VAT @ 5.00%
£0.55
Total Electricity Charges
£11.55
"""


def test_two_rate_import_bill_ignores_the_gas_section():
    bill = billreader.read_text(IMPORT_BILL)
    assert bill["kind"] == "import"
    assert bill["found"] == {
        "first_day": date(2025, 12, 6),
        "last_day": date(2026, 1, 5),  # not the gas tariff's dates
        "import_kwh": 704.0,  # not the 1613.7 kWh of gas
        "charge_gbp": 61.82,
        "export_kwh": None,
        "export_gbp": None,
    }
    assert bill["rates"] == [{"p_per_kwh": 6.381, "kwh": 701.0}, {"p_per_kwh": 27.092, "kwh": 3.1}]
    assert bill["standing_charge_p_per_day"] == 42.978 and bill["vat_percent"] == 5.0
    assert bill["notes"] == []


def test_export_statement():
    bill = billreader.read_text(EXPORT_BILL)
    assert bill["kind"] == "export"
    assert bill["found"]["export_kwh"] == 582.8 and bill["found"]["export_gbp"] == 96.16
    assert bill["found"]["import_kwh"] is None and bill["found"]["charge_gbp"] is None
    assert (bill["found"]["first_day"], bill["found"]["last_day"]) == (
        date(2025, 11, 17),
        date(2026, 4, 1),
    )
    assert bill["rates"] == [{"p_per_kwh": 16.5, "kwh": 582.8}]


def test_single_rate_bill_with_an_estimated_reading():
    bill = billreader.read_text(SINGLE_RATE_BILL)
    assert bill["found"]["import_kwh"] == 31.4 and bill["found"]["charge_gbp"] == 11.55
    assert bill["found"]["first_day"] == date(2026, 9, 5) and bill["found"]["last_day"] == date(
        2026, 9, 11
    )
    assert bill["rates"] == [{"p_per_kwh": 25.16, "kwh": 31.4}]
    assert any("estimated" in note for note in bill["notes"])


def test_a_price_change_mid_bill_spans_both_periods_and_adds_up():
    text = (
        SINGLE_RATE_BILL.replace(
            "Energy Charges",
            "Flexible Octopus (12th September 2026 - 30th September 2026)\nEnergy Charges",
        )
        + "Energy Used\n100.0 kWh @ 26.00p/kWh\n£26.00\nTotal Electricity Charges\n£30.10\n"
    )
    bill = billreader.read_text(text)
    assert bill["found"]["first_day"] == date(2026, 9, 5) and bill["found"]["last_day"] == date(
        2026, 9, 30
    )
    assert bill["found"]["import_kwh"] == 131.4 and bill["found"]["charge_gbp"] == pytest.approx(
        41.65
    )
    assert any("more than one tariff period" in note for note in bill["notes"])


def test_unrecognised_text_is_refused_not_guessed():
    with pytest.raises(billreader.BillError, match="No electricity figures"):
        billreader.read_text(
            "Dear customer, thank you for your payment of £50.00 on 3rd May 2026." * 3
        )
    dated = billreader.read_text("Total electricity charges\n£10.00\n")
    assert dated["found"]["charge_gbp"] == 10.0 and any("dates" in note for note in dated["notes"])


def pdf_with(lines: list[str]) -> bytes:
    """A minimal one-page PDF showing the given lines of text."""

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    shown = " ".join(f"({escape(line)}) Tj T*" for line in lines)
    content = f"BT /F1 10 Tf 12 TL 40 800 Td {shown} ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


def test_reading_a_real_pdf_through_the_api():
    client = TestClient(api.app)
    response = client.post("/api/bills/read", content=pdf_with(IMPORT_BILL.splitlines()))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["found"]["import_kwh"] == 704.0 and data["found"]["first_day"] == "2025-12-06"
    assert client.get("/api/bills").json() == {"bills": []}  # reading saves nothing

    assert client.post("/api/bills/read", content=b"this is not a pdf").status_code == 422
    assert client.post("/api/bills/read", content=b"").status_code == 422
    blank = client.post("/api/bills/read", content=pdf_with([""]))
    assert blank.status_code == 422 and "No text" in blank.json()["detail"]
