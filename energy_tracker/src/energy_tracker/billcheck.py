"""Compare the rates printed on a bill with the rates the tracker holds for those dates.

A bill is the supplier's own statement of the prices charged, so where it disagrees with
what was typed in, the bill is taken to be right and a correction is offered.
"""

from __future__ import annotations

from datetime import date

from .tariff import BEGINNING, Schedule

TOLERANCE = 0.0005  # prices are compared to a thousandth of a penny


def _differs(a: float, b: float) -> bool:
    return abs(a - b) > TOLERANCE


def check(
    kind: str,
    first_day: date | None,
    rates: list[float],
    standing_charge: float | None,
    vat_percent: float | None,
    schedule: Schedule,
) -> dict | None:
    """What on the bill disagrees with the tracker's rates for the bill's first day.

    Returns None when the bill has no date to look rates up by. Otherwise the differences
    found, and the corrected rates ready to save (None if nothing needs changing or the
    change cannot be worked out).
    """
    if first_day is None:
        return None
    tariff = schedule.on(first_day)
    differences: list[dict] = []
    notes: list[str] = []
    bands = [
        {"start": b.start, "end": b.end, "p_per_kwh": b.p_per_kwh} for b in tariff.import_bands
    ]
    fixed = {
        "name": tariff.name,
        "export_p_per_kwh": tariff.export_p_per_kwh,
        "standing_charge_p_per_day": tariff.standing_charge_p_per_day,
        "vat_percent": tariff.vat_percent,
        "import_bands": bands,
        "dynamic": tariff.dynamic,
    }

    def note(what: str, held: float, billed: float, unit: str) -> None:
        differences.append({"what": what, "tracker": held, "bill": billed, "unit": unit})

    billed = sorted(set(rates))
    if kind == "export":
        if len(billed) == 1:
            if _differs(billed[0], tariff.export_p_per_kwh):
                note("Export rate", tariff.export_p_per_kwh, billed[0], "per kWh")
                fixed["export_p_per_kwh"] = billed[0]
        elif billed:
            notes.append(
                "The bill shows more than one export rate, so the rate changed during it. "
                "Check the export rates by hand."
            )
    else:
        held = sorted({b.p_per_kwh for b in tariff.import_bands})
        if tariff.dynamic:
            pass  # prices come from the published list, not from what was typed in
        elif billed and len(billed) == len(held):
            names = {1: ["Import rate"], 2: ["Cheap rate", "Day rate"]}.get(
                len(held), [f"Rate {n + 1} (cheapest first)" for n in range(len(held))]
            )
            replace = {}
            for name, ours, theirs in zip(names, held, billed, strict=True):
                if _differs(ours, theirs):
                    note(name, ours, theirs, "per kWh")
                    replace[ours] = theirs
            for band in bands:
                band["p_per_kwh"] = replace.get(band["p_per_kwh"], band["p_per_kwh"])
        elif billed:
            notes.append(
                f"The bill shows {len(billed)} import rate{'s' if len(billed) != 1 else ''} "
                f"but the tracker has {len(held)} for those dates, so they cannot be matched "
                "up. Check the import rates by hand."
            )
        if standing_charge is not None and _differs(
            standing_charge, tariff.standing_charge_p_per_day
        ):
            note("Standing charge", tariff.standing_charge_p_per_day, standing_charge, "a day")
            fixed["standing_charge_p_per_day"] = standing_charge
        if vat_percent is not None and _differs(vat_percent, tariff.vat_percent):
            note("VAT", tariff.vat_percent, vat_percent, "%")
            fixed["vat_percent"] = vat_percent

    later = [p.effective_from for p in schedule.periods if p.effective_from > first_day]
    return {
        "tariff_name": tariff.name,
        # None means the tracker's very first set of rates.
        "period_from": tariff.effective_from if tariff.effective_from != BEGINNING else None,
        "period_key": tariff.effective_from,
        "bill_from": first_day,
        "next_change": min(later) if later else None,
        "differences": differences,
        "notes": notes,
        "corrected": fixed if differences else None,
    }
