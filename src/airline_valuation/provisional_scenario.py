"""Load an explicitly provisional Detailed-method scenario without calculations."""
from __future__ import annotations
import json
import math
import re
from pathlib import Path
from typing import Any

_INPUT = {f"Input sheet!B{x}" for x in (11,12,13,14,15,16,17,18,19,20,21,22,23,24,26,27,28,29,30,31,32,34,37,38,39,40,41,45,48,49,51,52,53,54,56,57,59,61,62,64,65,67,68,70,71,72)}
_METHOD = {f"Cost of capital worksheet!{x}" for x in ("B11","B21","B25","B32","B33","B35","B48","B49","B50")}
_REGIONS = {f"Cost of capital worksheet!H{x}" for x in range(21,32)}
_ALLOWED = _INPUT | _METHOD | _REGIONS | {"Input sheet!B8"}
_REQUIRED = {f"Input sheet!B{x}" for x in (11,12,13,14,15,16,17,18,19,20,21,22,23,24,26,27,28,29,30,31,32,34,37,45,48,51,53,56,59,61,64,67,70)} | {"Input sheet!B8", "Cost of capital worksheet!B11", "Cost of capital worksheet!B21", "Cost of capital worksheet!B25", "Cost of capital worksheet!B32", "Cost of capital worksheet!B33", "Cost of capital worksheet!B35", "Cost of capital worksheet!B48"} | _REGIONS
_SWITCHES = {f"Input sheet!B{x}" for x in (16,17,37,45,48,51,56,59,61,64,67,70)}
_UNITS = {**{f"Input sheet!B{x}": "USD millions" for x in (11,12,13,14,15,18,19,20)}, "Input sheet!B8":"industry key", "Input sheet!B21":"millions of shares", "Input sheet!B22":"USD/share", **{f"Input sheet!B{x}":"fraction" for x in (23,24,26,27,28,29,34)}, "Input sheet!B30":"years", "Input sheet!B31":"ratio", "Input sheet!B32":"ratio", "Cost of capital worksheet!B32":"years", "Cost of capital worksheet!B35":"rating category key", "Cost of capital worksheet!B48":"numeric switch (0/1)", **{ref:"USD millions" for ref in _REGIONS}}
_NUMERIC = set(_UNITS) - {"Input sheet!B8", "Cost of capital worksheet!B35"}


def load_provisional_scenario(path: str | Path) -> dict[str, Any]:
    """Return ``cells`` (normalized strings), exact ``units``, provenance, and uncertainty labels.

    JSON must contain ``status``, ``cells``, ``units``, and ``provenance``. Each
    provenance record requires nonempty status/basis/uncertainty. No formulas are evaluated.
    """
    with Path(path).open(encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict) or raw.get("status") != "illustrative draft only; not approved":
        raise ValueError("scenario status must be exactly 'illustrative draft only; not approved'")
    cells0, units, provenance = raw.get("cells"), raw.get("units"), raw.get("provenance")
    if not all(isinstance(x, dict) for x in (cells0, units, provenance)):
        raise ValueError("scenario requires cells, units, and provenance objects")
    conversions = raw.get("formula_to_numeric_inputs")
    expected_conversions = {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"}
    if not isinstance(conversions, dict) or set(conversions) != expected_conversions:
        raise ValueError("formula_to_numeric_inputs must separately declare exactly B27, B28, and B32 conversions")
    for ref, record in conversions.items():
        if not isinstance(record, dict) or record.get("target") != ref or not isinstance(record.get("reason"), str) or not record["reason"].strip():
            raise ValueError(f"{ref}: formula_to_numeric_input requires an exact target and non-empty reason")
    refs = set(cells0)
    if refs - _ALLOWED:
        raise ValueError(f"unknown or unsupported cell references: {sorted(refs - _ALLOWED)}")
    if _REQUIRED - refs:
        raise ValueError(f"missing required scenario cells: {sorted(_REQUIRED - refs)}")
    if refs != set(units) or refs != set(provenance):
        raise ValueError("cells, units, and provenance must have identical reference sets")
    cell_re = re.compile(r"^[A-Za-z][A-Za-z0-9 ]*![A-Z]+[1-9][0-9]*$")
    cells: dict[str,str] = {}
    for ref, value in cells0.items():
        if not cell_re.fullmatch(ref): raise ValueError(f"invalid cell reference: {ref}")
        if isinstance(value, bool) or value is None or isinstance(value,(dict,list)): raise ValueError(f"{ref}: invalid scalar")
        if isinstance(value,(int,float)):
            if not math.isfinite(float(value)): raise ValueError(f"{ref}: value must be finite")
            cells[ref] = str(value) if isinstance(value,int) else format(value,".15g")
        elif isinstance(value,str) and value.strip(): cells[ref]=value.strip()
        else: raise ValueError(f"{ref}: empty value")
        record=provenance[ref]
        if not isinstance(record,dict) or any(not isinstance(record.get(k),str) or not record[k].strip() for k in ("status","basis","uncertainty")):
            raise ValueError(f"{ref}: provenance requires status, basis, uncertainty")
        if not isinstance(units[ref],str) or not units[ref].strip(): raise ValueError(f"{ref}: unit required")
        if ref in _UNITS and units[ref] != _UNITS[ref]: raise ValueError(f"{ref}: unit must be {_UNITS[ref]!r}")
    selectors={"Input sheet!B8":"Air Transport", "Cost of capital worksheet!B11":"Detailed", "Cost of capital worksheet!B21":"Single Business(US)", "Cost of capital worksheet!B25":"Operating regions", "Cost of capital worksheet!B33":"Actual rating", "Cost of capital worksheet!B35":"Baa2/BBB"}
    for ref,want in selectors.items():
        if cells[ref] != want: raise ValueError(f"{ref} must be {want!r} for this scenario")
    if cells["Input sheet!B53"] != "V": raise ValueError("Input sheet!B53 must be explicit 'V'")
    constrained_values = {
        "Input sheet!B15": "5981", "Input sheet!B17": "No", "Input sheet!B18": "3231",
        "Input sheet!B19": "0", "Input sheet!B20": "0",
        "Cost of capital worksheet!H24": "0", "Cost of capital worksheet!H28": "28063",
    }
    for ref, expected in constrained_values.items():
        if cells[ref] != expected:
            raise ValueError(f"{ref} is constrained to {expected!r} in the published Southwest scenario")
    if any(cells.get(ref) != "No" for ref in _SWITCHES):
        raise ValueError("all provisional override switches, including lease switch B17, must explicitly be No")
    for row in set(range(21, 32)) - {28}:
        ref = f"Cost of capital worksheet!H{row}"
        if cells[ref] != "0":
            raise ValueError(f"{ref} must be zero in the published North-America-only scenario")

    def provenance_contains(ref: str, *terms: str) -> bool:
        record = provenance[ref]
        narrative = " ".join(record[key] for key in ("status", "basis", "uncertainty")).casefold()
        return all(term.casefold() in narrative for term in terms)

    provenance_requirements = {
        "Input sheet!B15": ("4,901", "1,080", "78", "not added again"),
        "Input sheet!B17": ("B15", "1,080", "double count"),
        "Input sheet!B18": ("3,231", "short-term investments"),
        "Input sheet!B19": ("457", "deferred compensation", "analyst"),
        "Input sheet!B20": ("analyst", "separate", "proof"),
        "Cost of capital worksheet!H24": ("Caribbean", "scenario"),
        "Cost of capital worksheet!H28": ("28,063", "North America", "not issuer"),
    }
    for ref, terms in provenance_requirements.items():
        if not provenance_contains(ref, *terms):
            raise ValueError(f"{ref} provenance is inconsistent with the published Southwest scenario")
    for row in set(range(21, 32)) - {28}:
        ref = f"Cost of capital worksheet!H{row}"
        if cells[ref] == "0" and not provenance_contains(ref, "zero"):
            raise ValueError(f"{ref} provenance must describe its zero allocation")
    for ref in _NUMERIC & refs:
        try: n=float(cells[ref])
        except ValueError as exc: raise ValueError(f"{ref}: expected numeric value") from exc
        if not math.isfinite(n) or n < 0: raise ValueError(f"{ref}: must be finite and nonnegative")
    if float(cells["Input sheet!B21"]) <= 0 or float(cells["Input sheet!B22"]) <= 0 or float(cells["Input sheet!B30"]) <= 0 or float(cells["Input sheet!B31"]) <= 0:
        raise ValueError("shares, price, convergence years, and sales-to-capital ratio must be positive")
    maturity = float(cells["Cost of capital worksheet!B32"])
    if maturity < 0 or (maturity == 0 and not (
        cells["Cost of capital worksheet!B11"] == "Detailed"
        and cells["Cost of capital worksheet!B21"] == "Single Business(US)"
        and cells["Cost of capital worksheet!B25"] == "Operating regions"
        and cells["Cost of capital worksheet!B33"] == "Actual rating"
    )):
        raise ValueError("B32 zero is permitted only for the selected Detailed / Single Business(US) / Operating regions / Actual rating route; otherwise maturity must be positive")
    for ref in ("Input sheet!B23","Input sheet!B24","Input sheet!B34"):
        if float(cells[ref]) > 1: raise ValueError(f"{ref}: fraction must be in [0,1]")
    preferred_switch=cells["Cost of capital worksheet!B48"]
    if preferred_switch not in {"0","0.0","1","1.0"}:
        raise ValueError("preferred switch Cost of capital worksheet!B48 must be numeric 0 or 1")
    pref_price,pref_dividend="Cost of capital worksheet!B49","Cost of capital worksheet!B50"
    if preferred_switch in {"0","0.0"}:
        if pref_price in refs or pref_dividend in refs:
            raise ValueError("preferred switch zero requires B49 and B50 to be absent")
    else:
        if pref_price not in refs or pref_dividend not in refs:
            raise ValueError("preferred switch one requires B49 price and B50 dividend")
        try:
            price,dividend=float(cells[pref_price]),float(cells[pref_dividend])
        except ValueError as exc:
            raise ValueError("preferred B49 and B50 must be numeric") from exc
        if not math.isfinite(price) or price <= 0 or not math.isfinite(dividend) or dividend < 0:
            raise ValueError("preferred B49 must be positive finite and B50 nonnegative finite")
        if units[pref_price] != "preferred price per share" or units[pref_dividend] != "preferred dividend per share":
            raise ValueError("preferred B49/B50 units must identify price and dividend per share")
    if any(float(cells[ref]) > 1 for ref in ("Input sheet!B26", "Input sheet!B27", "Input sheet!B28", "Input sheet!B29")):
        raise ValueError("forecast growth and margin fractions B26:B29 must be in [0,1]")
    if float(cells["Input sheet!B30"]) != int(float(cells["Input sheet!B30"])):
        raise ValueError("Input sheet!B30 convergence years must be a whole number")
    if float(cells["Input sheet!B32"]) <= 0:
        raise ValueError("Input sheet!B32 years 6–10 sales-to-capital ratio must be positive")
    if not cells["Input sheet!B8"] or not cells["Cost of capital worksheet!B35"]:
        raise ValueError("industry and rating keys must be explicit non-empty strings")
    if any(float(cells[f"Cost of capital worksheet!H{row}"]) != 0 for row in (30, 31)):
        raise ValueError("aggregate region rows H30/H31 must remain zero to avoid overlap")
    if abs(sum(float(cells[r]) for r in _REGIONS)-float(cells["Input sheet!B11"])) > 1e-8:
        raise ValueError("regional revenue cells Cost of capital worksheet!H21:H31 must sum to Input sheet!B11")
    if "Cost of capital worksheet!B12" in refs: raise ValueError("do not supply direct cost of capital or assert WACC")
    uncertainties=raw.get("uncertainties")
    if not isinstance(uncertainties,dict) or not all(isinstance(uncertainties.get(k),str) and uncertainties[k].strip() for k in ("rating","regions","leases","preferred")):
        raise ValueError("uncertainties must label rating, regions, leases, and preferred")
    candidate_dispositions=raw.get("candidate_dispositions")
    if not isinstance(candidate_dispositions,dict) or not candidate_dispositions or not all(isinstance(k,str) and k.strip() and isinstance(v,str) and v.strip() for k,v in candidate_dispositions.items()):
        raise ValueError("candidate_dispositions must be a non-empty object of non-empty text labels")
    disposition = raw.get("candidate_inventory")
    alternatives = {"Cost of capital worksheet!B12", "Cost of capital worksheet!B49", "Cost of capital worksheet!B50", "Input sheet!B46", "Input sheet!B15 debt-only", "Cost of capital worksheet!B35 BBB", "Input sheet!B8 Airlines"}
    if not isinstance(disposition, dict) or set(disposition) != refs | alternatives:
        raise ValueError("candidate inventory must reconcile entered cells and excluded/superseded alternatives")
    for ref, item in disposition.items():
        expected = {"entered"} if ref in refs else {"excluded", "superseded"}
        if not isinstance(item, dict) or item.get("disposition") not in expected or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError(f"candidate inventory has invalid disposition: {ref}")
    return {"cells":cells,"units":units,"provenance":provenance,"uncertainties":uncertainties,"candidate_dispositions":candidate_dispositions,"candidate_inventory":disposition,"formula_to_numeric_inputs":conversions}
