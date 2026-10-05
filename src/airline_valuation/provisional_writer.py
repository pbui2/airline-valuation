"""Byte-bounded provisional OOXML writer for the audited Detailed route."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

from .workbook import WorkbookContractError, _checked_zip

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[Ee][+-]?(?:0|[1-9][0-9]*))?\Z")
_CELL_REF = re.compile(r"[A-Z]+[1-9][0-9]*\Z")


def _contract() -> dict:
    return json.loads(Path(__file__).with_name("provisional_contract.json").read_text(encoding="utf-8"))


def _pinned_template(path: str | Path, contract: dict) -> Path:
    template = Path(path)
    if hashlib.sha256(template.read_bytes()).hexdigest() != contract["sha256"]:
        raise WorkbookContractError("provisional writer accepts only the pinned pristine template SHA-256")
    return template


def _parts(archive: zipfile.ZipFile, contract: dict) -> dict[str, str]:
    for name, digest in {**contract["pinned_members"], **contract["relationship_pins"]}.items():
        if hashlib.sha256(archive.read(name)).hexdigest() != digest:
            raise WorkbookContractError(f"pinned workbook dependency changed: {name}")
    for name, pin in contract["x14_validation_pins"].items():
        matches = re.findall(rb"<x14:dataValidations\b.*?</x14:dataValidations>", archive.read(name))
        if len(matches) != 1 or hashlib.sha256(matches[0]).hexdigest() != pin["sha256"] or len(re.findall(rb"<x14:dataValidation\b", matches[0])) != pin["count"]:
            raise WorkbookContractError(f"pinned x14 validations changed: {name}")
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rels = {}
    for rel in relroot.findall("p:Relationship", _NS):
        rid, target = rel.attrib.get("Id"), rel.attrib.get("Target")
        if not rid or rid in rels or not target or rel.attrib.get("TargetMode", "Internal") != "Internal":
            raise WorkbookContractError("invalid workbook relationship inventory")
        rels[rid] = (rel.attrib.get("Type", ""), "xl/" + target.lstrip("/"))
    parts = {}
    for node in workbook.findall("m:sheets/m:sheet", _NS):
        rid = node.attrib.get("{" + _NS["r"] + "}id")
        relationship = rels.get(rid)
        if not relationship or not relationship[0].endswith("/worksheet"):
            raise WorkbookContractError("invalid worksheet relationship")
        parts[node.attrib["name"]] = relationship[1]
    for sheet, part in contract["worksheet_parts"].items():
        if parts.get(sheet) != part or part not in archive.namelist():
            raise WorkbookContractError(f"contracted worksheet relationship changed: {sheet}")
    # The package hash pins the full relationship tables; these checks make the
    # reviewed formula and lookup routes explicit and independently inspectable.
    formulas = contract.get("formula_fingerprints", {})
    lookups = contract.get("lookup_fingerprints", {})
    if formulas or lookups:
        part = parts["Cost of capital worksheet"]
        root = ET.fromstring(archive.read(part))
        cells = {cell.attrib.get("r"): cell for cell in root.findall(".//m:sheetData/m:row/m:c", _NS)}
        for address, expected in {**formulas, **lookups}.items():
            cell = cells.get(address)
            formula = cell.find("m:f", _NS) if cell is not None else None
            if formula is None or hashlib.sha256((formula.text or "").encode("utf-8")).hexdigest() != expected:
                raise WorkbookContractError(f"pinned formula/lookup fingerprint changed: {address}")
    shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    shared_values = ["".join(item.itertext()) for item in shared_root.findall("m:si", _NS)]
    for label, row in contract["lookup_rows"].items():
        root = ET.fromstring(archive.read(row["part"]))
        key = root.find(f".//m:sheetData/m:row/m:c[@r='{row['key_cell']}']", _NS)
        key_value = key.find("m:v", _NS) if key is not None else None
        if key is None or key.get("t") != "s" or key_value is None or key_value.text != str(row["key_index"]) or shared_values[row["key_index"]] != ("Baa2/BBB" if label == "rating_dropdown" else label):
            raise WorkbookContractError(f"pinned lookup key changed: {label}")
        if "value_cell" in row:
            value = root.find(f".//m:sheetData/m:row/m:c[@r='{row['value_cell']}']/m:v", _NS)
            if value is None or value.text != row["value"]:
                raise WorkbookContractError(f"pinned lookup value changed: {label}")
    for ref, metadata in contract["input_metadata"].items():
        if metadata.get("kind") != "selector":
            continue
        sheet, address = ref.rsplit("!", 1)
        root = ET.fromstring(archive.read(parts[sheet]))
        cells = [cell for cell in root.findall(".//m:sheetData/m:row/m:c", _NS) if cell.attrib.get("r") == address]
        if len(cells) != 1 or cells[0].attrib.get("t") != "s":
            raise WorkbookContractError(f"selector cell lacks an existing shared-string representation: {ref}")
        value = cells[0].find("m:v", _NS)
        if value is None or not value.text or int(value.text) >= len(shared_values):
            raise WorkbookContractError(f"selector cell has invalid shared-string index: {ref}")
        if any(choice not in shared_values or shared_values.count(choice) != 1 for choice in metadata.get("allowed", [])):
            raise WorkbookContractError(f"selector choices are not unique existing shared strings: {ref}")
    return parts


def _entries(entries: dict[str, str], contract: dict) -> dict[str, tuple[str, str]]:
    if not isinstance(entries, dict):
        raise ValueError("entries must be a mapping of exact sheet!cell references to normalized strings")
    allowed, metadata = set(contract["input_allowlist"]), contract["input_metadata"]
    if allowed != set(metadata):
        raise WorkbookContractError("provisional input allowlist and metadata differ")
    result = {}
    for ref, value in entries.items():
        if not isinstance(ref, str) or ref not in allowed:
            raise ValueError(f"input reference is not allowlisted: {ref!r}")
        if not isinstance(value, str):
            raise ValueError(f"input value must be a normalized string: {ref}")
        kind = metadata[ref]["kind"]
        if kind in {"selector", "dropdown_string"}:
            choices = metadata[ref].get("allowed")
            if not isinstance(choices, list) or value not in choices:
                raise ValueError(f"selector is not an approved choice: {ref}")
        elif kind == "numeric":
            if value and (not _NUMBER.fullmatch(value) or not math.isfinite(float(value))):
                raise ValueError(f"input must be a finite normalized number or blank: {ref}")
        else:
            raise ValueError(f"unsupported provisional input kind: {ref}")
        result[ref] = (value, kind)
    switch = result.get("Cost of capital worksheet!B48")
    price = result.get("Cost of capital worksheet!B49")
    dividend = result.get("Cost of capital worksheet!B50")
    if switch is not None:
        if switch[0] not in {"0", "0.0", "1", "1.0"}:
            raise ValueError("preferred switch B48 must be numeric 0 or 1")
        if switch[0] in {"0", "0.0"} and (price is not None or dividend is not None):
            raise ValueError("preferred switch zero requires B49 and B50 to be absent")
        if switch[0] in {"1", "1.0"} and (price is None or dividend is None or not price[0] or not dividend[0] or float(price[0]) <= 0 or float(dividend[0]) < 0):
            raise ValueError("preferred switch one requires positive B49 and nonnegative B50")
    elif price is not None or dividend is not None:
        raise ValueError("preferred B49 and B50 require explicit B48 switch")
    return result


def _replace_input(xml: bytes, address: str, value: str, kind: str) -> bytes:
    openings = []
    for opening_match in re.finditer(rb"<c\b[^<>]*>", xml):
        coordinate = re.search(rb"\br=[\"']([^\"']+)[\"']", opening_match.group(0))
        if coordinate and coordinate.group(1) == address.encode("ascii"):
            openings.append(opening_match)
    if len(openings) != 1:
        raise WorkbookContractError(f"input cell must occur exactly once: {address}")
    opening_match = openings[0]
    opening = opening_match.group(0)
    if opening.rstrip().endswith(b"/>"):
        match_start, match_end = opening_match.start(), opening_match.end()
    else:
        close = re.search(rb"</c\s*>", xml[opening_match.end():])
        if close is None:
            raise WorkbookContractError(f"malformed direct cell element: {address}")
        between = xml[opening_match.end():opening_match.end() + close.start()]
        if re.search(rb"<c\b", between):
            raise WorkbookContractError(f"malformed nested cell element: {address}")
        match_start, match_end = opening_match.start(), opening_match.end() + close.end()
    cell = xml[match_start:match_end]
    if re.search(rb"<f(?:\s|>)", cell):
        raise WorkbookContractError(f"cannot write a formula cell: {address}")
    open_end = cell.find(b">") + 1
    opening = cell[:open_end]
    shared = re.search(rb"\bt=[\"']s[\"']", opening) is not None
    if kind == "selector" and not shared:
        raise WorkbookContractError(f"selector cell is not an existing shared-string cell: {address}")
    if kind == "numeric" and shared:
        raise WorkbookContractError(f"numeric input cannot overwrite a shared-string cell: {address}")
    if kind == "numeric" and re.search(rb"\bt=[\"'][^\"']+[\"']", opening):
        raise WorkbookContractError(f"numeric input has a protected nonnumeric cell type: {address}")
    payload = b""
    if value:
        if kind == "selector":
            payload = value.encode("utf-8")
        else:
            payload = value.encode("ascii")
    vals = list(re.finditer(rb"<v(?:\s[^>]*)?>(.*?)</v\s*>", cell, re.DOTALL))
    if len(vals) == 1:
        span = vals[0].span(1)
        newcell = cell[:span[0]] + payload + cell[span[1]:]
    elif not vals and re.search(rb"/\s*>$", cell):
        if not value:
            return xml
        newcell = re.sub(rb"/\s*>$", b"><v>" + payload + b"</v></c>", cell, count=1)
    else:
        raise WorkbookContractError(f"input must be an existing empty cell or have one value: {address}")
    return xml[:match_start] + newcell + xml[match_end:]


def _replace_dropdown(xml: bytes, address: str, index: int, original_cell: str) -> bytes:
    original = original_cell.encode("ascii")
    if xml.count(original) != 1 or f'r="{address}"'.encode("ascii") not in original:
        raise WorkbookContractError(f"blank dropdown cell does not match exact pin: {address}")
    replacement = original[:-2] + b' t="s"><v>' + str(index).encode("ascii") + b"</v></c>"
    return xml.replace(original, replacement, 1)


def _selector_token(value: str, shared_values: list[str]) -> bytes:
    indexes = [index for index, text in enumerate(shared_values) if text == value]
    if len(indexes) != 1:
        raise WorkbookContractError("approved selector must have exactly one existing shared-string index")
    return str(indexes[0]).encode("ascii")


def _replace_formula(xml: bytes, contract: dict) -> bytes:
    d62 = contract["d62"]
    original = d62["cell_xml"].encode("ascii")
    if hashlib.sha256(original).hexdigest() != d62["cell_xml_sha256"]:
        raise WorkbookContractError("invalid static D62 cell pin")
    if xml.count(original) != 1:
        raise WorkbookContractError("D62 original exact XML cell does not match its pin")
    changed = original.replace(b"<f>" + d62["original_formula"].encode("ascii") + b"</f>", b"<f>" + d62["provisional_formula"].encode("ascii") + b"</f>")
    return xml.replace(original, changed, 1)


def _replace_formula_input(xml: bytes, address: str, value: str, pin: dict) -> bytes:
    original = pin["cell_xml"].encode("ascii")
    if hashlib.sha256(original).hexdigest() != pin.get("cell_xml_sha256"):
        raise WorkbookContractError(f"invalid static formula-to-numeric XML pin: {address}")
    if xml.count(original) != 1:
        raise WorkbookContractError(f"formula-to-numeric input does not match exact pristine XML pin: {address}")
    formula = b"<f>" + pin["formula"].encode("ascii") + b"</f>"
    cached = b"<v>" + pin["cached_value"].encode("ascii") + b"</v>"
    if original.count(formula) != 1 or original.count(cached) != 1:
        raise WorkbookContractError(f"malformed formula-to-numeric pin tokens: {address}")
    opening_match = re.match(rb"<c\b([^>]*)>", original)
    if opening_match is None:
        raise WorkbookContractError(f"malformed formula-to-numeric cell opening: {address}")
    attrs = opening_match.group(1)
    if not re.search(rb"(?:^|\s)r=\"" + address.encode("ascii") + rb"\"(?:\s|$)", attrs):
        raise WorkbookContractError(f"formula-to-numeric cell coordinate pin differs: {address}")
    style = re.search(rb"(?:^|\s)s=\"([^\"]+)\"", attrs)
    if style is None or style.group(1).decode("ascii") != pin.get("style") or (b't="e"' in attrs) != (pin.get("type") == "e"):
        raise WorkbookContractError(f"formula-to-numeric style/type pin differs: {address}")
    replacement, count = re.subn(re.escape(formula), b"", original, count=1)
    if count != 1:
        raise WorkbookContractError(f"formula token replacement failed: {address}")
    replacement, count = re.subn(rb'\s+t="e"', b"", replacement, count=1) if pin.get("type") == "e" else (replacement, 1)
    if count != 1:
        raise WorkbookContractError(f"error type replacement failed: {address}")
    replacement, count = re.subn(re.escape(cached), b"<v>" + value.encode("ascii") + b"</v>", replacement, count=1)
    expected = f'<c r="{address}" s="{pin["style"]}"><v>{value}</v></c>'.encode("ascii")
    if count != 1 or replacement != expected or xml.count(original) != 1:
        raise WorkbookContractError(f"formula-to-numeric output encoding differs from exact expectation: {address}")
    changed, count = re.subn(re.escape(original), replacement, xml, count=1)
    if count != 1:
        raise WorkbookContractError(f"formula-to-numeric cell replacement failed: {address}")
    return changed


def _expected_members(archive: zipfile.ZipFile, values: dict, contract: dict,
                      formula_to_numeric_inputs: dict[str, str] | None = None) -> dict[str, bytes]:
    parts = _parts(archive, contract)
    shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    shared_values = ["".join(item.itertext()) for item in shared_root.findall("m:si", _NS)]
    expected = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    conversion_pins = contract.get("formula_to_numeric_inputs", {})
    conversions = formula_to_numeric_inputs or {}
    if set(conversions) - set(conversion_pins) or (set(conversions) & set(values)) != (set(conversions) & set(conversion_pins)):
        raise ValueError("formula_to_numeric_input declarations must reference pinned numeric targets")
    required_conversions = set(values) & set(conversion_pins)
    if set(conversions) != required_conversions:
        raise ValueError("each supplied formula-to-numeric input needs a separate declaration")
    for ref, (value, kind) in values.items():
        if ref in conversion_pins:
            continue
        sheet, address = ref.rsplit("!", 1)
        if not _CELL_REF.fullmatch(address):
            raise ValueError(f"invalid exact cell coordinate: {ref}")
        part = parts[sheet]
        if kind == "dropdown_string":
            metadata = contract["input_metadata"][ref]
            index = metadata["shared_string_index"]
            if shared_values[index] != value or shared_values.count(value) != 1:
                raise WorkbookContractError(f"pinned dropdown string changed: {ref}")
            expected[part] = _replace_dropdown(expected[part], address, index, metadata["original_cell_xml"])
        else:
            payload_value = _selector_token(value, shared_values).decode("ascii") if kind == "selector" else value
            expected[part] = _replace_input(expected[part], address, payload_value, kind)
    for ref, value in conversions.items():
        if ref not in values or not value or not _NUMBER.fullmatch(value) or not math.isfinite(float(value)):
            raise ValueError(f"formula-to-numeric target must also be an explicit finite numeric input: {ref}")
        if values[ref][0] != value:
            raise ValueError(f"formula-to-numeric declaration and scenario cell value differ: {ref}")
        sheet, address = ref.rsplit("!", 1)
        expected[parts[sheet]] = _replace_formula_input(expected[parts[sheet]], address, value, conversion_pins[ref])
    d62part = parts["Cost of capital worksheet"]
    expected[d62part] = _replace_formula(expected[d62part], contract)
    return expected


def _write_with_contract(template: str | Path, dest: str | Path, entries: dict[str, str],
                         formula_to_numeric_inputs: dict[str, str] | None, contract: dict) -> Path:
    """Shared exact-delta writer for separately pinned route contracts."""
    if not isinstance(contract, dict) or contract.get("sha256") is None:
        raise ValueError("explicit writer contract must be a pinned contract object")
    template_path = _pinned_template(template, contract)
    values = _entries(entries, contract)
    with _checked_zip(template_path) as source:
        expected = _expected_members(source, values, contract, formula_to_numeric_inputs)
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            with zipfile.ZipFile(temporary, "w") as output:
                for info in source.infolist():
                    output.writestr(info, expected[info.filename])
            with _checked_zip(temporary):
                pass
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    return destination


def write_provisional(template: str | Path, dest: str | Path, entries: dict[str, str],
                      formula_to_numeric_inputs: dict[str, str] | None = None,
                      *, contract: dict | None = None) -> Path:
    """Write direct inputs and explicitly declared pinned formula conversions."""
    if contract is None:
        contract = _contract()
    return _write_with_contract(template, dest, entries, formula_to_numeric_inputs, contract)


def verify_provisional_delta(template: str | Path, dest: str | Path, entries: dict[str, str],
                             formula_to_numeric_inputs: dict[str, str] | None = None,
                             *, contract: dict | None = None) -> dict[str, object]:
    """Require exact package bytes except declared inputs and pinned formula conversions."""
    if contract is None:
        contract = _contract()
    template_path = _pinned_template(template, contract)
    values = _entries(entries, contract)
    with _checked_zip(template_path) as original, _checked_zip(dest) as changed:
        if original.namelist() != changed.namelist():
            raise WorkbookContractError("package member inventory changed")
        expected = _expected_members(original, values, contract, formula_to_numeric_inputs)
        # Compare the entire ZIP container with a canonical rendering, not only
        # decompressed members: local headers, central directory, flags and any
        # leading/trailing bytes must be exactly those our writer produced.
        from io import BytesIO
        canonical_buffer = BytesIO()
        with zipfile.ZipFile(canonical_buffer, "w") as canonical:
            for info in original.infolist():
                canonical.writestr(info, expected[info.filename])
        if Path(dest).read_bytes() != canonical_buffer.getvalue():
            raise WorkbookContractError("package ZIP container differs outside exact provisional edits")
        for name in original.namelist():
            if changed.read(name) != expected[name]:
                raise WorkbookContractError(f"package member differs outside exact provisional edits: {name}")
    from .detailed_inventory import validate_input_formula_dependents
    structural = validate_input_formula_dependents(template_path)
    return {"valid": True, "changed_cells": sorted([*values, "Cost of capital worksheet!D62"]),
            "input_formula_dependents": structural}
