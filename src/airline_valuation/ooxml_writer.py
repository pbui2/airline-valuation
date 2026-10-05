"""Bounded, template-pinned OOXML input writer.

This changes only the existing <v> token of contracted direct input cells.
It does not evaluate formulas or modify calculation settings/caches.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

from .workbook import WorkbookContractError, _checked_zip, _resolve_target, _read_contract, _package_relationships

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[Ee][+-]?(?:0|[1-9][0-9]*))?\Z")
_REF = re.compile(r"([A-Z]+[1-9][0-9]*)\Z")


def _template_contract(template: str | Path):
    path = Path(template)
    contract = _read_contract(None)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != contract.get("sha256"):
        raise WorkbookContractError("writer accepts only the pinned pristine template SHA-256")
    return path, contract


def _normal_entries(entries, contract):
    if not isinstance(entries, dict):
        raise ValueError("entries must be a mapping of exact sheet!cell references to strings")
    allowed = set(contract.get("input_allowlist", []))
    if len(entries) != len(set(entries)):
        raise ValueError("duplicate input references")
    result = {}
    for ref, value in entries.items():
        if not isinstance(ref, str) or ref not in allowed:
            raise ValueError(f"input reference is not allowlisted: {ref!r}")
        if not isinstance(value, str):
            raise ValueError(f"input value must be a normalized string: {ref}")
        metadata = contract.get("input_metadata", {}).get(ref, {})
        if metadata.get("kind") == "switch":
            choices = metadata.get("allowed", [])
            if value not in choices or not isinstance(choices, list):
                raise ValueError(f"selector is not an approved switch choice: {ref}")
            result[ref] = (value, "selector")
            continue
        if metadata.get("kind") != "numeric":
            raise ValueError(f"input metadata does not approve numeric or switch values: {ref}")
        if not _NUMBER.fullmatch(value):
            raise ValueError(f"input must be a normalized numeric string or approved selector: {ref}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"numeric input must be finite: {ref}")
        result[ref] = (value, "numeric")
    return result


def _sheet_parts(zf, contract):
    _, package_overrides = _package_relationships(zf)
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    relroot = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rels = {}
    for rel in relroot.findall("p:Relationship", _NS):
        rid, typ, target = rel.attrib.get("Id"), rel.attrib.get("Type"), rel.attrib.get("Target")
        if not rid or rid in rels or not typ or not target or rel.attrib.get("TargetMode", "Internal") != "Internal":
            raise WorkbookContractError("invalid or duplicate workbook relationship")
        rels[rid] = (typ, _resolve_target("xl/workbook.xml", target))
    sheets = {}
    for sheet in wb.findall("m:sheets/m:sheet", _NS):
        rid = sheet.attrib.get("{" + _NS["r"] + "}id")
        record = rels.get(rid)
        if not record or not record[0].endswith("/worksheet"):
            raise WorkbookContractError("sheet has an invalid relationship type or target")
        name, part = sheet.attrib.get("name"), record[1]
        if part not in zf.namelist():
            raise WorkbookContractError(f"missing worksheet relationship target: {part}")
        if name in sheets:
            raise WorkbookContractError(f"duplicate worksheet name: {name}")
        sheets[name] = part
    overrides = package_overrides
    expected_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    expected = contract.get("sheet_metadata", {})
    names = ("Input sheet", "Cost of capital worksheet")
    for name in names:
        if name not in sheets or name not in expected or sheets[name] != expected[name].get("part"):
            raise WorkbookContractError(f"contracted worksheet relationship changed: {name}")
        if overrides.get(sheets[name]) != expected_type:
            raise WorkbookContractError(f"contracted worksheet content type changed: {name}")
    return sheets


def _replace_cell_xml(data: bytes, address: str, replacement: bytes, kind: str = "numeric") -> bytes:
    # Bounded byte-level edit: preserve tag/attribute/whitespace bytes and only
    # replace the single value token in the existing direct cell element.
    starts = []
    for opening_match in re.finditer(rb"<c\b[^<>]*>", data):
        opening = opening_match.group(0)
        coord = re.search(rb"\br=[\"']([^\"']+)[\"']", opening)
        if coord and coord.group(1).decode("ascii", "ignore") == address:
            if opening.rstrip().endswith(b"/>"):
                starts.append((opening_match.start(), opening_match.end(), opening_match.end()))
            else:
                close = re.search(rb"</c\s*>", data[opening_match.end():])
                if close:
                    starts.append((opening_match.start(), opening_match.end(), opening_match.end() + close.end()))
    if len(starts) != 1:
        raise WorkbookContractError(f"cell must occur exactly once as a direct cell: {address}")
    cell_start, opening_end, cell_end = starts[0]
    cell = data[cell_start:cell_end]
    if re.search(rb"<f(?:\s|>)", cell):
        raise WorkbookContractError(f"cannot write a formula cell: {address}")
    open_end = cell.find(b">") + 1
    opening = cell[:open_end]
    if kind == "numeric":
        if re.search(rb"\bt=[\"']s[\"']", opening):
            replacement_opening = re.sub(rb"\s+t=[\"']s[\"']", b" t=\"n\"", opening, count=1)
            cell = replacement_opening + cell[open_end:]
    vals = list(re.finditer(rb"<v(?:\s[^>]*)?>(.*?)</v\s*>", cell, re.DOTALL))
    if len(vals) == 1:
        start, end = vals[0].span(1)
        edited = cell[:start] + replacement + cell[end:]
    elif not vals and re.search(rb"/\s*>$", cell):
        edited = re.sub(rb"/\s*>$", b"><v>" + replacement + b"</v></c>", cell, count=1)
    else:
        raise WorkbookContractError(f"cell must have one value or be an existing empty cell: {address}")
    return data[:cell_start] + edited + data[cell_end:]


def write_prepared(template: str | Path, destination: str | Path, entries: dict[str, str]) -> Path:
    """Write allowlisted direct inputs while preserving all other member bytes.

    `entries` maps exact `Sheet name!A1` references to normalized decimal
    strings; the sole selector exception is `Cost of capital worksheet!B11`
    with value `I will input`.
    """
    template_path, contract = _template_contract(template)
    values = _normal_entries(entries, contract)
    with _checked_zip(template_path) as src:
        parts = _sheet_parts(src, contract)
        shared = ET.fromstring(src.read("xl/sharedStrings.xml"))
        shared_values = ["".join(node.itertext()) for node in shared.findall("m:si", _NS)]
        updates = {}
        for ref, (value, kind) in values.items():
            sheet, address = ref.rsplit("!", 1)
            if not _REF.fullmatch(address):
                raise ValueError(f"invalid cell reference: {ref}")
            part = parts[sheet]
            raw = updates.get(part, src.read(part))
            if kind == "selector":
                indexes = [i for i, text in enumerate(shared_values) if text == value]
                if len(indexes) != 1:
                    raise WorkbookContractError("approved selector must have one existing shared-string index")
                payload = str(indexes[0]).encode("ascii")
            else:
                payload = value.encode("ascii")
            updates[part] = _replace_cell_xml(raw, address, payload, kind)
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.name + ".tmp")
        try:
            with zipfile.ZipFile(temp, "w") as out:
                for info in src.infolist():
                    data = updates.get(info.filename)
                    if data is None:
                        data = src.read(info.filename)
                    out.writestr(info, data)
            with _checked_zip(temp):
                pass
            os.replace(temp, dest)
        finally:
            if temp.exists():
                temp.unlink()
    return dest


def verify_prepared_delta(template: str | Path, prepared: str | Path, entries: dict[str, str]) -> dict[str, object]:
    """Require an exact-cell-only delta from the pinned template."""
    template_path, contract = _template_contract(template)
    values = _normal_entries(entries, contract)
    expected_path = Path(prepared)
    with _checked_zip(template_path) as original, _checked_zip(expected_path) as changed:
        before = _sheet_parts(original, contract)
        after = _sheet_parts(changed, contract)
        if before != after:
            raise WorkbookContractError("worksheet relationships changed")
        if set(original.namelist()) != set(changed.namelist()):
            raise WorkbookContractError("package member inventory changed")
        editable = {}
        for ref in values:
            sheet, address = ref.rsplit("!", 1)
            editable.setdefault(before[sheet], set()).add(address)
        for part in original.namelist():
            old, new = original.read(part), changed.read(part)
            if old == new:
                continue
            if part not in editable:
                raise WorkbookContractError(f"unexpected package member change: {part}")
            expected = old
            for ref, (value, kind) in values.items():
                sheet, address = ref.rsplit("!", 1)
                if before[sheet] != part:
                    continue
                if kind == "selector":
                    shared = ET.fromstring(original.read("xl/sharedStrings.xml"))
                    texts = ["".join(n.itertext()) for n in shared.findall("m:si", _NS)]
                    idx = [i for i, text in enumerate(texts) if text == value]
                    if len(idx) != 1: raise WorkbookContractError("approved selector shared-string index is ambiguous")
                    payload = str(idx[0]).encode("ascii")
                else:
                    payload = value.encode("ascii")
                expected = _replace_cell_xml(expected, address, payload, kind)
            if expected != new:
                raise WorkbookContractError(f"worksheet contains changes outside the exact requested value tokens: {part}")
    # Also parse through the hardened reader; saved mode tolerates only these
    # requested input deltas while preserving formula inventories and metadata.
    from .workbook import Workbook
    with Workbook(expected_path, contract_path=Path(__file__).with_name("contract.json")) as book:
        book.verify_structure(mode="saved")
    return {"valid": True, "changed_cells": sorted(values)}
