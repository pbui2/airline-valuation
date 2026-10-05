"""Pinned, read-only evidence inventory for the Detailed cost-of-capital route."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).with_name("detailed_fixture.json")
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
PINNED_WORKBOOK_SHA256 = _FIXTURE["workbook"]["sha256"]
WORKSHEET_PART = _FIXTURE["worksheet"]["part"]
PINNED_WORKSHEET_XML_SHA256 = _FIXTURE["worksheet"]["xml_sha256"]
WORKSHEET_NAME = _FIXTURE["worksheet"]["name"]
PINNED_FORMULA_SHA256 = {
    "B13": "b6fad7932c3e476be888a4b2c178e4598aa72b679bdac7f3f384aec2af3786ce",
    "B23": "90afb31ac2c26af44c20fce0ada67639cc96754ed2367ae8ec62e8f81ac926b3",
    "B27": "08f0d38d9ad738e7326cbfa1d5f43a69420e2d9af55ef8e305e60a944c5d118f",
    "B37": "04343eb2f19de171c541b7e79f5930e4e8a97a32b220ace2b196abe469acf546",
    "B60": "8276b6489067dd1cd382a4eb9760f0df79b1d2b672383ff0224cac15609ece52",
    "C57": "1a2fc8240e205167ce61a199b16afdec0fdf17349a3d9fba245c2bff74cf073f",
    "B62": "bf3cef340ccdc3ceaf2e06877335dd58d1e769d2b0a5f59c7c5bfb23ee03cf0a",
    "C62": "0002ae384592af4e0c25a8527e2b6455327c3c9dcdc5e8e6344cdd2ab935e695",
    "D62": "267622be255be17913230cf5ad04ebdec435a55f538ecbee1dc7ff8d79e9dba7",
    "E62": "d38e6256a2d8b063c7fc76ed3815d54da4911214d3487c68d35e48edce18fee9",
}
_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "package_rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_CELL_REF = re.compile(r"(?<![A-Za-z0-9_])(?:'[^']*(?:''[^']*)*'|[A-Za-z_][A-Za-z0-9_. ]*)?!?\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?(?![A-Za-z0-9_(])")
_RANGE = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*):\$?([A-Z]{1,3})\$?([1-9][0-9]*)$")
_COL = re.compile(r"[A-Z]+")
_MAX_EXPANSION = 512

def _text(cell: ET.Element | None, path: str) -> str | None:
    if cell is None:
        return None
    node = cell.find(path, _NS)
    return node.text if node is not None else None

def _column_number(label: str) -> int:
    value = 0
    for char in label:
        value = value * 26 + ord(char) - 64
    return value

def _column_label(value: int) -> str:
    result = ""
    while value:
        value, rem = divmod(value - 1, 26)
        result = chr(65 + rem) + result
    return result

def _expand_reference(reference: str) -> dict[str, Any]:
    """Expand only bounded local A1 ranges; preserve cross-sheet refs as ranges."""
    if "!" in reference:
        return {"reference": reference, "cells": None, "expanded": False}
    match = _RANGE.fullmatch(reference)
    if not match:
        return {"reference": reference, "cells": [reference.replace("$", "")], "expanded": True}
    c1, r1, c2, r2 = match.groups()
    width, height = _column_number(c2) - _column_number(c1) + 1, int(r2) - int(r1) + 1
    if width <= 0 or height <= 0 or width * height > _MAX_EXPANSION:
        return {"reference": reference, "cells": None, "expanded": False, "reason": "reference range exceeds bounded expansion"}
    cells = [f"{_column_label(c)}{r}" for r in range(int(r1), int(r2) + 1) for c in range(_column_number(c1), _column_number(c2) + 1)]
    return {"reference": reference, "cells": cells, "expanded": True}

def _formula_references(formula: str) -> tuple[list[dict[str, Any]], bool]:
    refs = []
    unknown = False
    scrubbed = re.sub(r'"(?:[^"]|"")*"', '""', formula)
    matches = list(_CELL_REF.finditer(scrubbed))
    for match in matches:
        item = _expand_reference(match.group().strip())
        refs.append(item)
        unknown = unknown or not item["expanded"]
    # Spreadsheet constructs beyond A1 references are intentionally not interpreted.
    residue = _CELL_REF.sub("", scrubbed)
    residue = re.sub(r'""|[A-Za-z_][A-Za-z0-9_.]*|[0-9]+(?:\.[0-9]+)?|[+*/^&%=<>(),;:{}!$\- ]', "", residue)
    unknown = unknown or bool(residue)
    return refs, unknown

def _route_audit(archive: zipfile.ZipFile, workbook_root: ET.Element,
                 relationship_map: dict[str, str | None], cells: dict[str, ET.Element],
                 shared_strings: list[str], formulas: dict[str, dict[str, Any]], *, candidate: bool = False) -> dict[str, Any]:
    """Audit the selected branch, not every formula elsewhere on the worksheet."""
    def sheet_cells(name: str) -> dict[str, ET.Element]:
        sheet = next((s for s in workbook_root.findall("main:sheets/main:sheet", _NS)
                      if s.attrib.get("name") == name), None)
        if sheet is None:
            raise ValueError(f"missing lookup worksheet {name}")
        target = relationship_map.get(sheet.attrib.get("{" + _NS["rel"] + "}id"))
        if not target:
            raise ValueError(f"missing worksheet relationship for {name}")
        part = target.lstrip("/") if target.startswith("/") else "xl/" + target
        expected = next((row["part"] for row in (_FIXTURE["route_audit"]["industry"],
                                               _FIXTURE["route_audit"]["rating"])
                         if row["sheet"] == name), None)
        if expected is not None and part != expected:
            raise ValueError(f"{name} resolves to {part}, expected {expected}")
        return {c.attrib["r"]: c for c in ET.fromstring(archive.read(part)).findall(".//main:c", _NS)}

    def raw(cell: ET.Element | None) -> str | None:
        return _text(cell, "main:v")

    def label(cell: ET.Element | None) -> str | None:
        index = raw(cell)
        if cell is not None and cell.attrib.get("t") == "s" and index is not None:
            try:
                return shared_strings[int(index)]
            except (ValueError, IndexError):
                return None
        return index

    issues: list[str] = []
    rows = {}
    for kind in ("industry", "rating"):
        pin = _FIXTURE["route_audit"][kind]
        source = sheet_cells(pin["sheet"])
        key, result = source.get(pin["key_cell"]), source.get(pin["value_cell"])
        valid = (key is not None and key.attrib.get("t") == "s"
                 and raw(key) == str(pin["key_shared_index"]) and label(key) == pin["key"]
                 and result is not None and result.find("main:f", _NS) is None
                 and raw(result) == pin["value"])
        rows[kind] = {"sheet": pin["sheet"], "key_cell": pin["key_cell"],
                      "value_cell": pin["value_cell"], "key": label(key),
                      "key_shared_index": raw(key), "value": raw(result), "matches_pin": valid}
        if not valid:
            issues.append(f"{kind} selected lookup row differs from pin")
    selectors = {ref: label(cells.get(ref)) for ref in ("B11", "B21", "B25", "B33")}
    expected = {"B11": "Detailed", "B21": "Single Business(US)",
                "B25": "Operating regions", "B33": "Actual rating"}
    for ref, wanted in expected.items():
        if selectors[ref] != wanted:
            issues.append(f"{ref} branch selector is not {wanted}")
    hashes = {ref: formulas.get(ref, {}).get("formula_sha256") == pin
              for ref, pin in _FIXTURE["route_audit"]["formula_hashes"].items()}
    if candidate:
        from .detailed_workflow import load_contract
        allowed_formula = load_contract()["d62"]["provisional_formula"]
        hashes["D62"] = formulas.get("D62", {}).get("formula") == allowed_formula

    branch_text = {"B13": ('IF(B11="I will input",B12,IF(B11="Detailed",E62,',),
                   "B23": ('IF(B21="Single Business(US)",VLOOKUP(\'Input sheet\'!B8,\'Industry Averages(US)\'!A2:G95,7)',),
                   "B27": ('IF(B25="Will Input",B26,IF(B25="Country of Incorporation",',
                           'IF(B25="Operating regions",\'Cost of capital worksheet\'!K32,'),
                   "B37": ('IF(B33="Direct Input",B34,IF(B33="Synthetic Rating",',
                           "B24+VLOOKUP('Cost of capital worksheet'!B35,'Synthetic rating'!G42:H56,2)"),
                   "C57": ('IF(B21="Direct Input",B22,B23*(1+(1-B38)*(C60/B60)))',),
                   "B45": ('IF(\'Input sheet\'!B17="Yes",\'Operating lease converter\'!F33,0)',)}
    for ref, fragments in branch_text.items():
        text = formulas.get(ref, {}).get("formula", "")
        if not all(fragment in text for fragment in fragments):
            issues.append(f"selected branch formula {ref} differs from audited expression")
    issues.extend(f"route formula {ref} differs from pin" for ref, valid in hashes.items() if not valid)
    # An IF's unselected arms are not dependencies of the chosen Detailed branch.
    selected = {"B13": "E62", "B23": "'Input sheet'!B8 'Industry Averages(US)'!A4:G4",
                "B27": "K32", "B37": "B24 B35 'Synthetic rating'!G52:H52",
                "C57": "B21 B23 B38 C60 B60"}
    # This matcher rejects named ranges and foreign references instead of silently dropping them.
    references = re.compile(r"(?:(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_ ]*))!)?(\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?)")
    sheet_cache: dict[str, dict[str, ET.Element]] = {}
    def external_cells(name: str) -> dict[str, ET.Element]:
        if name not in sheet_cache:
            sheet_cache[name] = sheet_cells(name)
        return sheet_cache[name]
    lease_selector = label(external_cells("Input sheet").get("B17"))
    if lease_selector != "No":
        issues.append("Input sheet!B17 must select No before lease converter can be excluded")
    selected["B45"] = "'Input sheet'!B17"
    visited: set[str] = set()
    followers: list[str] = []
    dependencies: dict[str, list[str]] = {}
    pending = ["B13"]
    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        external_sheet = None
        if "!" in ref:
            external_sheet, address = ref.split("!", 1)
            source = external_cells(external_sheet).get(address)
            if source is None:
                issues.append(f"reachable external cell {ref} is absent")
                continue
            formula_node = source.find("main:f", _NS)
            if formula_node is None:
                continue
            text = formula_node.text or ""
            attrs = dict(formula_node.attrib)
            if attrs.get("t") == "shared" and not text:
                head = next(((name, cell.find("main:f", _NS)) for name, cell in external_cells(external_sheet).items()
                             if (cell.find("main:f", _NS) is not None
                                 and cell.find("main:f", _NS).attrib.get("si") == attrs.get("si")
                                 and cell.find("main:f", _NS).attrib.get("ref")
                                 and address in (_expand_reference(cell.find("main:f", _NS).attrib["ref"])["cells"] or []))), None)
                if head is None or not head[1].text:
                    issues.append(f"reachable external shared follower {ref} is unresolved")
                    continue
                offset = int(re.search(r"\d+", address).group()) - int(re.search(r"\d+", head[0]).group())
                text = re.sub(r"(?<![A-Za-z0-9_])(\$?)([A-Z]+)(\$?)(\d+)",
                              lambda m: f"{m[1]}{m[2]}{m[3]}{int(m[4]) if m[3] else int(m[4]) + offset}",
                              head[1].text)
                followers.append(ref)
        else:
            record = formulas.get(ref)
            if record is None:
                if ref not in cells:
                    issues.append(f"reachable cell {ref} is absent")
                continue
            text = record["formula"]
            attrs = record["formula_attributes"]
        if attrs.get("t") == "shared" and not text:
            head = next((h for h, item in formulas.items()
                         if item["formula_attributes"].get("si") == attrs.get("si")
                         and item["formula_attributes"].get("ref")
                         and ref in (_expand_reference(item["formula_attributes"]["ref"])["cells"] or [])), None)
            if head is None:
                issues.append(f"reachable shared follower {ref} has no head")
                continue
            offset = int(re.search(r"\d+", ref).group()) - int(re.search(r"\d+", head).group())
            text = re.sub(r"(?<![A-Za-z0-9_])(\$?)([A-Z]+)(\$?)(\d+)",
                          lambda m: f"{m[1]}{m[2]}{m[3]}{int(m[4]) if m[3] else int(m[4]) + offset}",
                          formulas[head]["formula"])
            followers.append(ref)
        if not text:
            issues.append(f"reachable formula {ref} has no resolvable text")
            continue
        expression = selected.get(ref, text)
        expression = re.sub(r'"(?:[^"]|"")*"', '""', expression)
        found = []
        external = []
        def consume(match: re.Match[str]) -> str:
            sheet = match[1] or match[2]
            expanded = _expand_reference(match[3].replace("$", ""))
            if not expanded["expanded"]:
                issues.append(f"unresolved reference {ref}: {match.group()}")
            elif sheet and sheet != WORKSHEET_NAME:
                external.append(f"{sheet}!{match[3]}")
            elif external_sheet and not sheet:
                external.extend(f"{external_sheet}!{cell}" for cell in expanded["cells"])
            else:
                found.extend(expanded["cells"])
            return " "
        residue = references.sub(consume, expression)
        residue = re.sub(r'""|\b(?:IF|SUM|VLOOKUP|COUNTIF|AVERAGE|ROUND|ABS|MIN|MAX|AND|OR|IFERROR)\b|\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?|[+*/^&%=<>(),;:{}!$\- ]', "", residue, flags=re.I)
        if residue:
            issues.append(f"unknown reachable formula syntax {ref}: {residue[:40]}")
        for item in external:
            sheet, address = item.split("!", 1)
            if item in ("Industry Averages(US)!A4:G4", "Synthetic rating!G52:H52"):
                continue
            expanded = _expand_reference(address.replace("$", ""))
            if not expanded["expanded"]:
                issues.append(f"unresolved external range {ref}: {item}")
            else:
                pending.extend(f"{sheet}!{cell}" for cell in expanded["cells"])
        dependencies[ref] = sorted(set(found) | set(external))
        pending.extend(found)
    return {"selectors": selectors, "lookup_rows": rows,
            "selected_lookup_status": {"B23": "pinned industry row; scenario key required",
                                       "B27": "selected operating-region weighted route; scenario region inputs required",
                                       "B37": "pinned rating row; scenario B35 key required"},
            "formula_hashes_match_pins": hashes,
            "reachable_cells": sorted(visited), "reachable_formulas": sorted(visited & formulas.keys()),
            "shared_followers": sorted(followers), "dependencies": dependencies,
            "unknown_dependencies": issues, "valid": not issues}


def validate_input_formula_dependents(path: str | Path, *, staged_values: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate pinned B27/B28/B32 cells and their direct formula dependents."""
    pins = _FIXTURE["input_formula_dependents"]
    target_refs = ["Input sheet!B27", "Input sheet!B28", "Input sheet!B32"]
    if staged_values is not None and (not isinstance(staged_values, dict) or set(staged_values) != set(target_refs)):
        raise ValueError("staged_values must contain exactly Input sheet!B27, B28, and B32")
    numeric = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[Ee][+-]?(?:0|[1-9][0-9]*))?\Z")
    # Cell/range references with optional quoted or ordinary worksheet names.
    ref_re = re.compile(r"(?<![A-Za-z0-9_.])(?:(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_. ]*))!)?(\$?[A-Z]{1,3}\$?[1-9][0-9]*)(?::(\$?[A-Z]{1,3}\$?[1-9][0-9]*))?(?![A-Za-z0-9_(])")
    def col_row(address: str) -> tuple[int, int]:
        match = re.fullmatch(r"\$?([A-Z]{1,3})\$?([1-9][0-9]*)", address)
        if not match:
            raise ValueError(f"unsupported cell reference: {address}")
        column = 0
        for character in match.group(1):
            column = column * 26 + ord(character) - ord("A") + 1
        return column, int(match.group(2))

    def rectangle_contains_target(first: str, last: str) -> bool:
        c1, r1 = col_row(first)
        c2, r2 = col_row(last)
        left, right = sorted((c1, c2))
        top, bottom = sorted((r1, r2))
        return any(left <= col <= right and top <= row <= bottom for col, row in targets)
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {item.attrib.get("Id"): item.attrib.get("Target") for item in relroot.findall("package_rel:Relationship", _NS)}
        sheet_parts = {}
        for sheet in workbook.findall("main:sheets/main:sheet", _NS):
            target = rels.get(sheet.attrib.get("{" + _NS["rel"] + "}id"))
            if not target:
                raise ValueError(f"missing worksheet relationship for {sheet.attrib.get('name')}")
            sheet_parts[sheet.attrib["name"]] = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if "Input sheet" not in sheet_parts:
            raise ValueError("Input sheet relationship missing")
        input_xml = archive.read(sheet_parts["Input sheet"])
        input_root = ET.fromstring(input_xml)
        input_cells = {c.attrib.get("r"): c for c in input_root.findall(".//main:c", _NS)}
        raw_cells = {}
        for address in ("B27", "B28", "B32"):
            matches = re.findall(rb"<c\b(?=[^>]*\br=\"" + address.encode() + rb"\")[^>]*>.*?</c>", input_xml)
            if len(matches) == 1:
                raw_cells[address] = matches[0].decode("utf-8")
        target_memberships = []
        for ref in target_refs:
            address = ref.rsplit("!", 1)[1]
            cell = input_cells.get(address)
            if cell is None:
                raise ValueError(f"pinned forecast target missing: {ref}")
            formula = cell.find("main:f", _NS)
            if formula is not None and formula.attrib.get("t") in {"shared", "array"}:
                target_memberships.append(ref)
            if staged_values is None:
                pin = _FIXTURE["target_formula_cells"].get(ref)
                if not pin or raw_cells.get(address) != pin["xml"] or formula is None:
                    raise ValueError(f"pristine target raw XML differs from formula pin: {ref}")
            else:
                expected = staged_values[ref]
                raw = _text(cell, "main:v")
                pin = _FIXTURE.get("target_cells", {}).get(ref, {}).get("staged_xml", "").replace("{value}", expected if isinstance(expected, str) else "")
                if (not isinstance(expected, str) or not numeric.fullmatch(expected) or formula is not None
                        or cell.attrib.get("t") is not None or raw != expected
                        or raw_cells.get(address) != pin):
                    raise ValueError(f"staged target raw XML differs from numeric value/style pin: {ref}")
        if target_memberships:
            raise ValueError(f"forecast target belongs to shared/array formula range: {target_memberships}")
        targets = {col_row(ref.rsplit("!", 1)[1]) for ref in pins["targets"]}
        target_addresses = {ref.rsplit("!", 1)[1] for ref in pins["targets"]}
        found: dict[str, str] = {}
        memberships: list[str] = []
        for sheet_name, part in sheet_parts.items():
            root = ET.fromstring(archive.read(part))
            for cell in root.findall(".//main:c", _NS):
                formula = cell.find("main:f", _NS)
                if formula is None:
                    continue
                attrs = formula.attrib
                if attrs.get("t") in {"shared", "array"}:
                    formula_range = attrs.get("ref")
                    if formula_range:
                        range_match = re.fullmatch(r"(\$?[A-Z]{1,3}\$?[1-9][0-9]*):(\$?[A-Z]{1,3}\$?[1-9][0-9]*)", formula_range)
                        if range_match is None:
                            raise ValueError(f"unsupported shared/array formula range: {sheet_name}!{cell.attrib.get('r')}")
                        if sheet_name == "Input sheet" and rectangle_contains_target(range_match.group(1), range_match.group(2)):
                            raise ValueError(f"shared/array formula range intersects a target dependency: {sheet_name}!{cell.attrib.get('r')}")
                if sheet_name == "Input sheet" and cell.attrib.get("r") in target_addresses:
                    continue
                text = formula.text or ""
                clean = re.sub(r'"(?:[^"]|"")*"', '""', text)
                references = list(ref_re.finditer(clean))
                residue = ref_re.sub(" ", clean)
                # Strip formula syntax/names only after references; unsupported external, 3-D,
                # structured, or R1C1 references are not safe to treat as independent.
                if re.search(r"\[[^\]]+\]|(?:^|[=,(])\s*[A-Z]{1,3}\[|#REF!|#NAME\?", residue, re.I):
                    raise ValueError(f"unsupported formula reference syntax in {sheet_name}!{cell.attrib.get('r')}")
                dependent = False
                for match in references:
                    sheet_ref = (match.group(1) or match.group(2) or "").replace("''", "'")
                    if sheet_ref and sheet_ref != "Input sheet":
                        continue
                    if not sheet_ref and sheet_name != "Input sheet":
                        continue
                    if rectangle_contains_target(match.group(3), match.group(4) or match.group(3)):
                        dependent = True
                if not dependent:
                    continue
                ref = f"{sheet_name}!{cell.attrib['r']}"
                found[ref] = text
                if attrs.get("t") in {"shared", "array"}:
                    memberships.append(ref)
        if sorted(found) != sorted(pins["direct_formula_cells"]):
            raise ValueError("direct formula dependents of Input sheet B26:B32 differ from fixture")
        if sorted(memberships) != sorted(pins["shared_or_array_formula_cells"]):
            raise ValueError("shared/array formula membership of forecast dependents differs from fixture")
        for ref, digest in pins["formula_hashes"].items():
            if ref not in found or hashlib.sha256(found[ref].encode("utf-8")).hexdigest() != digest:
                raise ValueError(f"pinned forecast dependent formula hash differs: {ref}")
        return {"direct_formula_cells": sorted(found), "shared_or_array_formula_cells": sorted(memberships),
                "mode": "pristine" if staged_values is None else "staged"}


def inspect_detailed_workbook(path: str | Path, *, candidate: bool = False) -> dict[str, Any]:
    """Inspect pinned OOXML and cached values; never calculates or modifies the workbook.

    Candidate mode is only for a candidate already bound by the separate exact-delta
    validator; it does not waive formula, lookup, relationship, or route checks.
    """
    path = Path(path)
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        with zipfile.ZipFile(path) as archive:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            relationship_map = {item.attrib.get("Id"): item.attrib.get("Target") for item in relationships_root.findall("package_rel:Relationship", _NS)}
            selected = next((sheet for sheet in workbook_root.findall("main:sheets/main:sheet", _NS) if sheet.attrib.get("name") == WORKSHEET_NAME), None)
            rid = selected.attrib.get("{" + _NS["rel"] + "}id") if selected is not None else None
            target = relationship_map.get(rid)
            relationship_part = ("xl/" + target.lstrip("/")) if target else None
            if relationship_part != WORKSHEET_PART:
                raise ValueError(f"worksheet relationship resolves to {relationship_part!r}, expected {WORKSHEET_PART!r}")
            xml = archive.read(relationship_part)
            root = ET.fromstring(xml)
            shared_strings = ["".join(node.text or "" for node in item.findall(".//main:t", _NS)) for item in shared_root.findall("main:si", _NS)]
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"cannot inspect pinned workbook package: {exc}") from exc
    cells = {cell.attrib["r"]: cell for cell in root.findall(".//main:c", _NS)}
    def value(ref: str) -> str | None:
        cell = cells.get(ref)
        raw = _text(cell, "main:v")
        if cell is not None and cell.attrib.get("t") == "s" and raw is not None:
            try: return shared_strings[int(raw)]
            except (IndexError, ValueError): return None
        return raw
    formula_cells = {}
    shared_formulas = []
    canonical_records = []
    unknown_dependencies = []
    for ref, cell in cells.items():
        f = cell.find("main:f", _NS)
        if f is None: continue
        formula = f.text or ""
        attrs = dict(f.attrib)
        record = {"formula": formula, "formula_sha256": hashlib.sha256(formula.encode("utf-8")).hexdigest(), "cached_value": _text(cell, "main:v"), "cached_type": cell.attrib.get("t"), "formula_attributes": attrs}
        if attrs.get("t") == "shared":
            record["shared_formula"] = {"shared_index": attrs.get("si"), "range": attrs.get("ref"), "follower": not bool(formula)}
            shared_formulas.append({"cell": ref, "shared_index": attrs.get("si"), "range": attrs.get("ref"), "formula": formula or None, "follower": not bool(formula)})
        refs, unknown = _formula_references(formula) if formula else ([], True)
        record["references"] = refs
        record["dependency_status"] = "unknown" if unknown else "extracted"
        if unknown: unknown_dependencies.append(ref)
        formula_cells[ref] = record
        canonical_records.append([ref, formula, sorted(attrs.items())])
    canonical_hash = hashlib.sha256(json.dumps(canonical_records, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with zipfile.ZipFile(path) as archive:
        route_audit = _route_audit(archive, workbook_root, relationship_map, cells, shared_strings, formula_cells, candidate=candidate)
    selector = value("B11")
    selector_cell = cells.get("B11")
    selector_index = _text(selector_cell, "main:v") if selector_cell is not None else None
    # Hash the exact worksheet cell byte slice, not a serializer-dependent XML rendering.
    d62_xml = None
    raw_d62_xml = None
    d62_hash = None
    with zipfile.ZipFile(path) as archive:
        worksheet_bytes = archive.read(WORKSHEET_PART)
    match = re.search(rb'<c\b(?=[^>]*\br="D62")[^>]*>.*?</c>', worksheet_bytes)
    if match:
        raw_d62_xml = match.group(0).decode("utf-8")
        d62_xml = raw_d62_xml
        d62_hash = hashlib.sha256(match.group(0)).hexdigest()
    d62_exact = d62_hash == _FIXTURE["detailed_route"]["d62_cell_xml_sha256"] and _text(cells.get("D62"), "main:f") == "B50/B49"
    if candidate:
        from .detailed_workflow import load_contract
        d62_exact = _text(cells.get("D62"), "main:f") == load_contract()["d62"]["provisional_formula"]
    hashes_match = {ref: formula_cells.get(ref, {}).get("formula_sha256") == pinned for ref, pinned in PINNED_FORMULA_SHA256.items()}
    blockers = []
    if not candidate and source_sha != PINNED_WORKBOOK_SHA256: blockers.append("workbook source SHA-256 differs from pinned template")
    worksheet_hash = hashlib.sha256(xml).hexdigest()
    if not candidate and worksheet_hash != PINNED_WORKSHEET_XML_SHA256: blockers.append("worksheet XML SHA-256 differs from pinned worksheet")
    if not candidate and (len(formula_cells) != _FIXTURE["worksheet"]["formula_count"] or canonical_hash != _FIXTURE["worksheet"]["canonical_formula_inventory_sha256"]): blockers.append("complete formula inventory differs from fixture")
    if not d62_exact:
        blockers.append("D62 candidate formula differs from Detailed contract" if candidate else "D62 exact XML/formula differs from fixture")
    blockers.extend(f"formula hash differs from pin for {ref}" for ref, ok in hashes_match.items()
                    if not ok and not (candidate and ref == "D62"))
    blockers.extend(route_audit["unknown_dependencies"])
    if value("B8") is not None:
        blockers.append("B8 must remain the pinned blank worksheet cell; industry key comes from Input sheet!B8")
    if candidate:
        from .detailed_workflow import load_contract
        expected_rating = load_contract()["detailed_route_contract"]["route_selectors"]["Cost of capital worksheet!B35"]
        if value("B35") != expected_rating:
            blockers.append("B35 candidate rating selector differs from Detailed contract")
    elif value("B35") is not None:
        blockers.append("B35 must remain the pinned blank rating input cell")
    if value("B49") is not None or value("B50") is not None:
        blockers.append("B49/B50 no longer match the pinned blank preferred-share driver cells")
    return {
        "fixture_schema_version": _FIXTURE["schema_version"], "source_sha256": source_sha,
        "source_sha256_matches_pin": source_sha == PINNED_WORKBOOK_SHA256, "worksheet_name": WORKSHEET_NAME,
        "worksheet_part": WORKSHEET_PART, "worksheet_relationship_id": rid, "worksheet_relationship_part": relationship_part,
        "worksheet_relationship_matches_pin": relationship_part == WORKSHEET_PART,
        "worksheet_xml_sha256": worksheet_hash, "worksheet_xml_sha256_matches_pin": worksheet_hash == PINNED_WORKSHEET_XML_SHA256,
        "selector": {"cell": "B11", "shared_string_index": selector_index, "observed_label": selector},
        "route": {"cell": "B13", "formula": formula_cells.get("B13", {}).get("formula"), "detailed_branch": "E62"},
        "formulas": formula_cells, "formula_cells": sorted(formula_cells), "formula_count": len(formula_cells),
        "canonical_formula_inventory_sha256": canonical_hash, "formula_hashes_match_pins": hashes_match,
        "shared_formulas": shared_formulas, "dependency_extraction": {"range_limit": _MAX_EXPANSION, "unknown_cells": unknown_dependencies, "fail_closed": True},
        "d62_xml": d62_xml, "d62_xml_sha256": d62_hash, "d62_exact_xml_matches_pin": d62_exact,
        "driver_candidates": {"B21": {"observed": value("B21"), "status": "unverified lookup route"}, "B25": {"observed": value("B25"), "status": "unverified lookup route"}, "B33": {"observed": value("B33"), "status": "unverified lookup route"}, "B32": {"observed": value("B32"), "status": "blank maturity-year candidate; not a B49 denominator"}, "B35": {"observed": value("B35"), "status": "blank lookup-key candidate"}, "B49": {"observed": value("B49"), "status": "blank denominator cell; units unknown"}, "B50": {"observed": value("B50"), "status": "blank numerator cell; units unknown"}},
        "lookup_status": {ref: info["status"] + ("; cached " + str(info["cached"]) if info.get("cached") else "") for ref, info in _FIXTURE["lookups"].items()},
        "lookup_routes": _FIXTURE["lookups"], "units_and_drivers": _FIXTURE["units_and_drivers"],
        "route_audit": route_audit, "blocked": bool(blockers), "blockers": blockers,
        "cached_errors": {ref: item["cached_value"] for ref, item in formula_cells.items()
                          if ref in route_audit["reachable_formulas"] and item["cached_type"] == "e"},
        "scenario_requirements": {"B35": "rating key Baa2/BBB; source/authority must be recorded",
                                  "B49": "preferred share market price; currency per share and source required",
                                  "B50": "preferred annual dividend; same currency per share per year and source required",
                                  "D62": "guard: exact pinned B50/B49 formula currently has #DIV/0! cache; rebuild/review before reliance"},
    }

def validate_verified_driver_evidence(evidence: dict[str, Any], *, candidate: bool = False) -> None:
    """Accept only the pinned structural Detailed route; never certify stale cached values."""
    if not candidate and (evidence.get("source_sha256") != PINNED_WORKBOOK_SHA256 or evidence.get("source_sha256_matches_pin") is not True):
        raise ValueError("workbook source does not match pinned SHA-256")
    if not candidate and (evidence.get("worksheet_xml_sha256") != PINNED_WORKSHEET_XML_SHA256 or evidence.get("worksheet_xml_sha256_matches_pin") is not True):
        raise ValueError("worksheet XML does not match pinned SHA-256")
    if evidence.get("worksheet_relationship_id") != _FIXTURE["worksheet"]["relationship_id"] or evidence.get("worksheet_relationship_part") != WORKSHEET_PART:
        raise ValueError("worksheet relationship does not match pin")
    if not candidate and (evidence.get("formula_count") != _FIXTURE["worksheet"]["formula_count"] or evidence.get("canonical_formula_inventory_sha256") != _FIXTURE["worksheet"]["canonical_formula_inventory_sha256"]):
        raise ValueError("worksheet formula inventory does not match pin")
    if candidate:
        from .detailed_workflow import load_contract
        expected_d62 = load_contract()["d62"]["provisional_formula"]
        if evidence.get("d62_exact_xml_matches_pin") is not True or evidence.get("formulas", {}).get("D62", {}).get("formula") != expected_d62:
            raise ValueError("D62 candidate formula differs from Detailed contract")
    elif evidence.get("d62_exact_xml_matches_pin") is not True or evidence.get("d62_xml_sha256") != _FIXTURE["detailed_route"]["d62_cell_xml_sha256"] or evidence.get("formulas", {}).get("D62", {}).get("formula") != "B50/B49":
        raise ValueError("D62 exact source/formula does not match pin")
    audit = evidence.get("route_audit", {})
    if audit.get("valid") is not True or audit.get("unknown_dependencies"):
        raise ValueError("selected Detailed route has unknown dependencies")
    if audit.get("selectors") != {"B11": "Detailed", "B21": "Single Business(US)", "B25": "Operating regions", "B33": "Actual rating"}:
        raise ValueError("selected branch differs from audited branch")
    for ref, pin in _FIXTURE["route_audit"]["formula_hashes"].items():
        if candidate and ref == "D62":
            continue
        if evidence.get("formulas", {}).get(ref, {}).get("formula_sha256") != pin or audit.get("formula_hashes_match_pins", {}).get(ref) is not True:
            raise ValueError(f"route formula {ref} differs from pin")
    if candidate:
        from .detailed_workflow import load_contract
        if audit.get("formula_hashes_match_pins", {}).get("D62") is not True:
            raise ValueError("route D62 formula differs from Detailed contract")
    for kind in ("industry", "rating"):
        row = audit.get("lookup_rows", {}).get(kind, {})
        pin = _FIXTURE["route_audit"][kind]
        if row.get("matches_pin") is not True or any(row.get(field) != pin[field] for field in ("sheet", "key_cell", "value_cell", "key", "value")) or row.get("key_shared_index") != str(pin["key_shared_index"]):
            raise ValueError(f"selected {kind} lookup row differs from pin")
    if not audit.get("reachable_formulas") or not audit.get("shared_followers") or "Country equity risk premiums!B209" not in audit.get("shared_followers", []):
        raise ValueError("shared formula route is not fully resolved")
    if evidence.get("blocked") is not False or evidence.get("blockers"):
        raise ValueError("Detailed route audit remains blocked")
    candidates = evidence.get("driver_candidates", {})
    for ref, candidate in candidates.items():
        if isinstance(candidate, dict) and candidate.get("offered_as_input") is True and ref in evidence.get("formula_cells", []):
            raise ValueError(f"formula cell {ref} cannot be offered as an input driver")

def blocked_report(path: str | Path) -> dict[str, Any]:
    try:
        evidence = inspect_detailed_workbook(path)
        reasons = evidence["blockers"]
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        reasons = [f"pinned workbook inventory could not be validated: {exc}"]
    return {"status": "blocked", "published": False, "reasons": reasons, "reason": "No provisional output was created or modified."}
