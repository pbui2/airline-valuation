"""Read-only, hashed guidance inventory for detailed-route workbook cells."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).with_name("detailed_contract.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PINNED_WORKBOOK_SHA256 = CONTRACT["sha256"]
DEFAULT_WORKBOOK = Path(__file__).resolve().parents[2] / CONTRACT["source_file"]
NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    "tc": "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments",
}
REF_RE = re.compile(r"(?<![A-Za-z0-9_])(?:'[^']*(?:''[^']*)*'|[A-Za-z_][A-Za-z0-9_. ]*)?!?\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?(?![A-Za-z0-9_(])")


class GuidanceInventoryError(ValueError):
    """The workbook or requested evidence does not match the pinned inventory."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(source: str, target: str) -> str:
    import posixpath
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root.findall("m:si", NS)]


def _cell_value(cell: ET.Element | None, strings: list[str]) -> Any:
    if cell is None:
        return None
    typ = cell.attrib.get("t")
    value = cell.find("m:v", NS)
    if typ == "inlineStr":
        inline = cell.find("m:is", NS)
        return "".join(inline.itertext()) if inline is not None else ""
    if value is None:
        return None
    raw = value.text
    if typ == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    return raw


def _comment_records(root: ET.Element, address: str, threaded: bool) -> list[str]:
    """Return text for matching comments, preserving existing empty comments."""
    tag = "threadedComment" if threaded else "comment"
    records = []
    for comment in root.iter():
        if comment.tag.rsplit("}", 1)[-1] != tag or comment.attrib.get("ref") != address:
            continue
        if threaded:
            text = next((node for node in comment if node.tag.rsplit("}", 1)[-1] == "text"), None)
            records.append(text.text or "" if text is not None else "")
        else:
            text = comment.find("m:text", NS)
            records.append("".join(node.text or "" for node in text.iter()
                                    if node.tag.rsplit("}", 1)[-1] == "t") if text is not None else "")
    return records


def _parts_comments(archive: zipfile.ZipFile, sheet_part: str) -> list[tuple[str, str, bytes]]:
    rel_part = "xl/worksheets/_rels/" + sheet_part.rsplit("/", 1)[1] + ".rels"
    found: list[tuple[str, str, bytes]] = []
    if rel_part not in archive.namelist():
        return found
    relroot = ET.fromstring(archive.read(rel_part))
    for rel in relroot.findall("p:Relationship", NS):
        typ = rel.attrib.get("Type", "")
        if typ.endswith("/comments") or typ.endswith("/threadedComment"):
            part = _target(sheet_part, rel.attrib["Target"])
            if part in archive.namelist():
                found.append(("threaded" if typ.endswith("/threadedComment") else "legacy",
                              part, archive.read(part)))
    return found


def _column(address: str) -> int:
    value = 0
    for char in re.match(r"[A-Z]+", address).group():
        value = value * 26 + ord(char) - 64
    return value


def _row(address: str) -> int:
    return int(re.search(r"[0-9]+$", address).group())


def _formula_dependencies(formula: str | None) -> list[str]:
    """Extract unique A1 dependency tokens in formula order, retaining sheet names."""
    dependencies = []
    for match in REF_RE.finditer(formula or ""):
        dependency = match.group().strip()
        if dependency not in dependencies:
            dependencies.append(dependency)
    return dependencies


def _in_sqref(address: str, sqref: str) -> bool:
    col, row = _column(address), _row(address)
    for token in sqref.split():
        bounds = token.replace("$", "").split(":")
        if len(bounds) == 1:
            bounds *= 2
        if len(bounds) != 2 or not all(re.fullmatch(r"[A-Z]+[1-9][0-9]*", item) for item in bounds):
            continue
        c1, c2 = _column(bounds[0]), _column(bounds[1])
        r1, r2 = _row(bounds[0]), _row(bounds[1])
        if min(c1, c2) <= col <= max(c1, c2) and min(r1, r2) <= row <= max(r1, r2):
            return True
    return False


def _selector_domains(archive: zipfile.ZipFile, sheet_parts: dict[str, str], entries: list[dict[str, Any]], strings: list[str], source_matches_pin: bool) -> dict[str, Any]:
    """Resolve exact list-validation domains only from the authenticated template."""
    if not source_matches_pin:
        return {}
    domains: dict[str, Any] = {}
    roots: dict[str, dict[str, ET.Element]] = {}
    for entry in entries:
        for validation in entry["validations"]:
            source = validation.get("formula1") or ""
            match = re.fullmatch(r"'((?:[^']|'')+)'!\$?([A-Z]+)\$?([1-9][0-9]*):\$?([A-Z]+)\$?([1-9][0-9]*)", source)
            if validation.get("type") != "list" or not match:
                continue
            sheet_name = match.group(1).replace("''", "'")
            if sheet_name not in sheet_parts:
                continue
            part = sheet_parts[sheet_name]
            if part not in roots:
                root = ET.fromstring(archive.read(part))
                roots[part] = {cell.attrib["r"]: cell for cell in root.findall(".//m:sheetData/m:row/m:c", NS)}
            c1, r1, c2, r2 = match.group(2), int(match.group(3)), match.group(4), int(match.group(5))
            if c1 != c2 or r2 < r1:
                continue
            values, evidence_cells = [], []
            for row in range(r1, r2 + 1):
                address = f"{c1}{row}"
                cell = roots[part].get(address)
                value = _cell_value(cell, strings)
                if value is None:
                    continue
                values.append(value)
                evidence_cells.append({"reference": f"{sheet_name}!{address}",
                                       "cell_xml_sha256": _sha(ET.tostring(cell, encoding="utf-8"))})
            if values:
                domains[entry["reference"]] = {
                    "allowed": values, "validation_source": source,
                    "validation_xml_sha256": validation["xml_sha256"],
                    "lookup_part": part, "lookup_worksheet_xml_sha256": _sha(archive.read(part)),
                    "lookup_cells": evidence_cells, "source_matches_pin": True,
                    "issuer_specific_decision": False,
                }
    return domains


def _inventory(path: Path, *, verify_pin: bool) -> dict[str, Any]:
    raw_package = path.read_bytes()
    package_hash = _sha(raw_package)
    if verify_pin and package_hash != PINNED_WORKBOOK_SHA256:
        raise GuidanceInventoryError(f"workbook SHA-256 {package_hash} does not match pinned {PINNED_WORKBOOK_SHA256}")
    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in relationships.findall("p:Relationship", NS)}
        sheet_parts: dict[str, str] = {}
        for sheet in wb.findall("m:sheets/m:sheet", NS):
            rid = sheet.attrib["{" + NS["r"] + "}id"]
            sheet_parts[sheet.attrib["name"]] = _target("xl/workbook.xml", targets[rid])
        strings = _strings(archive)
        contract = CONTRACT
        refs: list[str] = contract["input_allowlist"]
        entries: list[dict[str, Any]] = []
        xml_hashes: dict[str, str] = {}
        for ref in refs:
            sheet_name, address = ref.split("!", 1)
            part = sheet_parts[sheet_name]
            sheet_bytes = archive.read(part)
            xml_hashes.setdefault(part, _sha(sheet_bytes))
            root = ET.fromstring(sheet_bytes)
            cellmap = {c.attrib["r"]: c for c in root.findall(".//m:sheetData/m:row/m:c", NS)}
            cell = cellmap.get(address)
            comment_records = _parts_comments(archive, part)
            comments = []
            for kind, comment_part, comment_bytes in comment_records:
                cr = ET.fromstring(comment_bytes)
                texts = _comment_records(cr, address, kind == "threaded")
                comments.extend({"kind": kind, "text": text,
                                 "part": comment_part, "part_sha256": _sha(comment_bytes)}
                                for text in texts)
            rnum = _row(address)
            context = []
            target_col = _column(address)
            for rr in (rnum - 1, rnum, rnum + 1):
                values = [{"cell": ref_cell, "value": _cell_value(c, strings)} for ref_cell, c in cellmap.items()
                          if _row(ref_cell) == rr and _cell_value(c, strings) is not None
                          and (abs(_column(ref_cell) - target_col) <= 2 or _column(ref_cell) <= 3)]
                if values:
                    context.append({"row": rr, "cells": sorted(values, key=lambda x: _column(x["cell"]))})
            formula_node = cell.find("m:f", NS) if cell is not None else None
            formula = formula_node.text if formula_node is not None else None
            dependencies = _formula_dependencies(formula)
            validations = []
            for node in root.iter():
                if not node.tag.endswith("dataValidation"):
                    continue
                # Extension (x14) validations hold their target in xm:sqref and
                # their list source in a nested xm:f rather than an attribute.
                sqref = node.attrib.get("sqref") or next(
                    (child.text for child in node if child.tag.rsplit("}", 1)[-1] == "sqref"), "")
                if _in_sqref(address, sqref):
                    formulas = {}
                    for child in node:
                        kind = child.tag.rsplit("}", 1)[-1]
                        if kind in {"formula1", "formula2"}:
                            list_source = next((item.text for item in child.iter()
                                                if item.tag.rsplit("}", 1)[-1] == "f"), None)
                            formulas[kind] = list_source if list_source is not None else child.text
                    validations.append({"sqref": sqref, "type": node.attrib.get("type"),
                                        "operator": node.attrib.get("operator"),
                                        "formula1": formulas.get("formula1"),
                                        "formula2": formulas.get("formula2"),
                                        "xml_sha256": _sha(ET.tostring(node, encoding="utf-8"))})
            guard = None
            if ref in {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"}:
                guard = "scenario_conversion_guard: formula-to-numeric exception only; no broader formula edits"
            elif ref == "Cost of capital worksheet!D62":
                guard = "non_input_formula_guard: formula cell; pinned formula text only, never an input"
            entries.append({
                "reference": ref, "worksheet_part": part, "worksheet_xml_sha256": xml_hashes[part],
                "cell_present": cell is not None,
                "cell_type": cell.attrib.get("t") if cell is not None else None,
                "cell_style": cell.attrib.get("s") if cell is not None else None,
                "cell_value": _cell_value(cell, strings),
                "formula": formula, "formula_sha256": _sha((formula or "").encode()),
                "dependencies": dependencies,
                "cell_xml_sha256": _sha(ET.tostring(cell, encoding="utf-8")) if cell is not None else None,
                "comments": comments,
                "comment_status": "present" if comments else "absent in related comment parts",
                "nearby_headers_and_labels": context,
                "validations": validations,
                "guard_classification": guard,
            })
        # D62 is deliberately outside the input allowlist: surface its pin as a
        # separate guard record rather than suggesting it is a guidance input.
        guard_ref = "Cost of capital worksheet!D62"
        guard_sheet, guard_address = guard_ref.split("!", 1)
        guard_part = sheet_parts[guard_sheet]
        guard_bytes = archive.read(guard_part)
        guard_root = ET.fromstring(guard_bytes)
        guard_cell = next((c for c in guard_root.findall(".//m:sheetData/m:row/m:c", NS)
                           if c.attrib.get("r") == guard_address), None)
        guard_formula_node = guard_cell.find("m:f", NS) if guard_cell is not None else None
        guard_formula = guard_formula_node.text if guard_formula_node is not None else None
        guard_record = {"reference": guard_ref, "worksheet_part": guard_part,
                        "worksheet_xml_sha256": _sha(guard_bytes),
                        "cell_xml_sha256": _sha(ET.tostring(guard_cell, encoding="utf-8")) if guard_cell is not None else None,
                        "cell_type": guard_cell.attrib.get("t") if guard_cell is not None else None,
                        "formula": guard_formula,
                        "formula_sha256": _sha((guard_formula or "").encode()),
                        "dependencies": _formula_dependencies(guard_formula),
                        "guard_classification": "non_input_formula_guard: excluded by detailed contract; do not treat as an input"}
        selector_domains = _selector_domains(archive, sheet_parts, entries, strings,
                                             package_hash == PINNED_WORKBOOK_SHA256)
    return {"schema_version": "trial-guidance-inventory/1", "source_file": path.name,
            "pinned_source_sha256": PINNED_WORKBOOK_SHA256, "observed_source_sha256": package_hash,
            "source_matches_pin": package_hash == PINNED_WORKBOOK_SHA256,
            "reference_count": len(refs), "entries": entries, "excluded_formula_guard": guard_record,
            "selector_domains": selector_domains}


def inspect_trial_guidance(path: str | Path | None = None, *, verify_pin: bool = True) -> dict[str, Any]:
    """Return cell-level guidance evidence; never calculate or modify the workbook."""
    return _inventory(Path(path) if path is not None else DEFAULT_WORKBOOK, verify_pin=verify_pin)
