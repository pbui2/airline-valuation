"""Read-only access to the reviewed airline valuation XLSX template.

This module reads package XML and cached values only. It never calculates,
rewrites, or saves a workbook.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "officeRel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "packageRel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_CELL = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_ERROR_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?", "#NUM!", "#NULL!"}


def _stable_xml_bytes(root: ET.Element) -> bytes:
    """Serialize XML with generated prefixes independent of ElementTree's registry."""
    namespace_uris: list[str] = []

    def collect(element: ET.Element) -> None:
        for name in (element.tag, *element.attrib):
            if isinstance(name, str) and name.startswith("{"):
                uri = name[1:].split("}", 1)[0]
                if uri not in namespace_uris:
                    namespace_uris.append(uri)
        for child in element:
            collect(child)

    collect(root)
    prefixes = {uri: f"ns{index}" for index, uri in enumerate(namespace_uris)}
    serialized = ET.tostring(root, encoding="unicode")
    declarations = re.findall(r'xmlns:([^=\s]+)="([^"]+)"', serialized)
    current_prefixes = {prefix: uri for prefix, uri in declarations}
    for prefix, uri in current_prefixes.items():
        canonical = prefixes.get(uri)
        if canonical is not None and canonical != prefix:
            serialized = re.sub(rf"(?<![\w.-]){re.escape(prefix)}(?=[:=])", canonical, serialized)
    declared = "".join(f' xmlns:{prefixes[uri]}="{uri}"' for uri in namespace_uris)
    serialized = re.sub(r"(\s+xmlns:[^=\s]+=\"[^\"]+\")+", "", serialized, count=1)
    serialized = re.sub(r"^<([^ >]+)", lambda match: match.group(0) + declared, serialized, count=1)
    return serialized.encode("utf-8")


def _read_contract(contract_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(contract_path) if contract_path is not None else Path(__file__).with_name("contract.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _checked_zip(path: str | Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path, "r")
        names = [item.filename for item in archive.infolist()]
        if len(names) != len(set(names)):
            archive.close()
            raise WorkbookContractError("duplicate ZIP member names")
        for name in names:
            if name.startswith("/") or "\\\\" in name or any(piece in ("", ".", "..") for piece in name.rstrip("/").split("/")):
                archive.close()
                raise WorkbookContractError(f"unsafe ZIP member path: {name}")
        bad = archive.testzip()
        if bad is not None:
            archive.close()
            raise WorkbookContractError(f"ZIP CRC failure: {bad}")
        return archive
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise WorkbookContractError("malformed or corrupt XLSX ZIP package") from exc


def _resolve_target(source_part: str, target: str) -> str:
    from urllib.parse import unquote, urlsplit
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        raise WorkbookContractError(f"invalid relationship target: {target}")
    decoded = unquote(parsed.path)
    if "\\\\" in decoded or decoded.startswith("//"):
        raise WorkbookContractError(f"invalid relationship target: {target}")
    part = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/") else posixpath.join(posixpath.dirname(source_part), decoded))
    if part in ("", ".", "..") or part.startswith("../") or part.startswith("/"):
        raise WorkbookContractError(f"relationship target escapes package: {target}")
    return part


def _package_relationships(archive: zipfile.ZipFile) -> tuple[dict[str, str], dict[str, str]]:
    """Validate package root relationship and content-type declarations."""
    try:
        root = ET.fromstring(archive.read("_rels/.rels"))
        types = ET.fromstring(archive.read("[Content_Types].xml"))
    except (KeyError, ET.ParseError) as exc:
        raise WorkbookContractError("missing or malformed package root metadata") from exc
    root_rels = {}
    for rel in root.findall("packageRel:Relationship", _NS):
        rid, typ, target = rel.attrib.get("Id"), rel.attrib.get("Type"), rel.attrib.get("Target")
        if not rid or rid in root_rels or not typ or not target:
            raise WorkbookContractError("invalid or duplicate package root relationship")
        if rel.attrib.get("TargetMode", "Internal") != "Internal":
            raise WorkbookContractError("external package root relationships are disallowed")
        root_rels[rid] = (typ, _resolve_target("", target))
    office = [(typ, target) for typ, target in root_rels.values() if typ == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"]
    if len(office) != 1 or office[0][1] != "xl/workbook.xml":
        raise WorkbookContractError("package officeDocument relationship must target xl/workbook.xml exactly once")
    ct_ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    defaults, overrides = {}, {}
    permitted_missing = set(_read_contract().get("feature_policy", {}).get("permitted_missing_parts", []))
    allowed_types = {
        "application/xml", "application/vnd.openxmlformats-package.relationships+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.theme+xml",
    }
    for item in types.findall("ct:Default", ct_ns):
        ext, content_type = item.attrib.get("Extension"), item.attrib.get("ContentType")
        if not ext or not content_type or ext in defaults:
            raise WorkbookContractError("invalid or duplicate content type default")
        defaults[ext] = content_type
    for item in types.findall("ct:Override", ct_ns):
        name, content_type = item.attrib.get("PartName"), item.attrib.get("ContentType")
        if not name or not name.startswith("/") or not content_type or name in overrides:
            raise WorkbookContractError("invalid or duplicate content type override")
        part = name.lstrip("/")
        if part not in archive.namelist() and part not in permitted_missing:
            raise WorkbookContractError(f"content type override targets missing part: {part}")
        if content_type not in allowed_types and not content_type.startswith(("application/vnd.openxmlformats-officedocument.", "application/vnd.openxmlformats-package.", "application/vnd.ms-office.", "application/vnd.ms-excel.", "application/xml")):
            raise WorkbookContractError(f"disallowed content type: {content_type}")
        overrides[part] = content_type
    if overrides.get("xl/workbook.xml") != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml":
        raise WorkbookContractError("workbook content type declaration is missing or invalid")
    contract = _read_contract()
    pinned_path = Path(__file__).resolve().parents[2] / str(contract.get("source_file", ""))
    if pinned_path.is_file() and hashlib.sha256(pinned_path.read_bytes()).hexdigest() == contract.get("sha256") and Path(archive.filename).resolve() != pinned_path.resolve():
        with zipfile.ZipFile(pinned_path) as pinned:
            pinned_root = ET.fromstring(pinned.read("_rels/.rels"))
            pinned_types = ET.fromstring(pinned.read("[Content_Types].xml"))
        pinned_rels = sorted((r.attrib.get("Id"), r.attrib.get("Type"), r.attrib.get("Target"), r.attrib.get("TargetMode", "Internal")) for r in pinned_root.findall("packageRel:Relationship", _NS))
        current_rels = sorted((r.attrib.get("Id"), r.attrib.get("Type"), r.attrib.get("Target"), r.attrib.get("TargetMode", "Internal")) for r in root.findall("packageRel:Relationship", _NS))
        if current_rels != pinned_rels:
            raise WorkbookContractError("package root relationship inventory changed")
        pinned_defaults = sorted((e.attrib.get("Extension"), e.attrib.get("ContentType")) for e in pinned_types.findall("ct:Default", ct_ns))
        pinned_overrides = sorted((e.attrib.get("PartName"), e.attrib.get("ContentType")) for e in pinned_types.findall("ct:Override", ct_ns))
        current_defaults = sorted((e.attrib.get("Extension"), e.attrib.get("ContentType")) for e in types.findall("ct:Default", ct_ns))
        current_overrides = sorted((e.attrib.get("PartName"), e.attrib.get("ContentType")) for e in types.findall("ct:Override", ct_ns))
        if current_defaults != pinned_defaults or current_overrides != pinned_overrides:
            raise WorkbookContractError("content type declarations changed from pinned package")
    return defaults, overrides


class WorkbookContractError(ValueError):
    """Workbook package does not match the reviewed template contract."""


class Workbook:
    """Read cached cell values from an XLSX without invoking a spreadsheet engine."""

    def __init__(self, path: str | Path, contract_path: str | Path | None = None):
        self.path = Path(path)
        if contract_path is None:
            contract_path = Path(__file__).with_name("contract.json")
        self.contract_path = Path(contract_path)
        self.contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        self._zip = _checked_zip(self.path)
        self._parts = set(self._zip.namelist())
        self._sheets: dict[str, dict[str, Any]] = {}
        self._cells: dict[str, dict[str, dict[str, Any]]] = {}
        self._formula_fingerprints: dict[str, str] = {}
        self._worksheet_metadata_fingerprints: dict[str, str] = {}
        self.relationship_fingerprint = ""
        self._parse()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "Workbook":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _parse(self) -> None:
        self._content_type_defaults, self._content_type_overrides = _package_relationships(self._zip)
        try:
            workbook = ET.fromstring(self._zip.read("xl/workbook.xml"))
            relroot = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ET.ParseError) as exc:
            raise WorkbookContractError("Invalid XLSX workbook metadata") from exc
        relationship_records = sorted((r.attrib.get("Id"), r.attrib.get("Type"), r.attrib.get("Target"), r.attrib.get("TargetMode")) for r in relroot.findall("packageRel:Relationship", _NS))
        optional_rel_types = set(self.contract.get("feature_policy", {}).get("relationship_optional_types", []))
        stable_relationships = [record for record in relationship_records if record[1] not in optional_rel_types]
        self.relationship_fingerprint = hashlib.sha256(json.dumps(stable_relationships, separators=(",", ":")).encode()).hexdigest()
        rels = {}
        for record in relroot.findall("packageRel:Relationship", _NS):
            rid, typ, target = record.attrib.get("Id"), record.attrib.get("Type"), record.attrib.get("Target")
            if not rid or rid in rels or not typ or not target or record.attrib.get("TargetMode", "Internal") != "Internal":
                raise WorkbookContractError("invalid or duplicate workbook relationship")
            rels[rid] = (typ, target)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in self._parts:
            strings = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
            shared = ["".join(t.itertext()) for t in strings.findall("main:si", _NS)]
        self.calc_settings = dict(workbook.find("main:calcPr", _NS).attrib) if workbook.find("main:calcPr", _NS) is not None else {}
        self.defined_names = []
        for name in workbook.findall("main:definedNames/main:definedName", _NS):
            self.defined_names.append({"attributes": dict(name.attrib), "formula": name.text or ""})
        for sheet in workbook.findall("main:sheets/main:sheet", _NS):
            rid = sheet.attrib.get("{" + _NS["officeRel"] + "}id")
            record = rels.get(rid)
            if record is None or not record[0].endswith("/worksheet"):
                raise WorkbookContractError("sheet has an invalid relationship type or target")
            part = _resolve_target("xl/workbook.xml", record[1])
            if part not in self._parts:
                raise WorkbookContractError(f"Missing sheet relationship target: {part}")
            if self._content_type_overrides.get(part) != "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml":
                raise WorkbookContractError(f"worksheet content type declaration is missing or invalid: {part}")
            name = sheet.attrib["name"]
            if name in self._sheets:
                raise WorkbookContractError(f"Duplicate worksheet name: {name}")
            self._sheets[name] = {"sheet_id": sheet.attrib["sheetId"], "relationship_id": rid, "part": part, "state": sheet.attrib.get("state", "visible")}
            root = ET.fromstring(self._zip.read(part))
            metadata_root = ET.fromstring(self._zip.read(part))
            for volatile_tag in ("sheetData", "sheetViews", "pageMargins", "pageSetup", "printOptions", "headerFooter", "extLst"):
                for node in list(metadata_root.findall(f"main:{volatile_tag}", _NS)):
                    metadata_root.remove(node)
            metadata_xml = _stable_xml_bytes(metadata_root)
            self._worksheet_metadata_fingerprints[sheet.attrib["name"]] = hashlib.sha256(metadata_xml).hexdigest()
            cells: dict[str, dict[str, Any]] = {}
            formula_records = []
            for cell in root.findall(".//main:sheetData/main:row/main:c", _NS):
                address = cell.attrib.get("r")
                if not address or address in cells or not _CELL.fullmatch(address):
                    raise WorkbookContractError(f"invalid or duplicate cell coordinate: {address!r} in {part}")
                value = cell.find("main:v", _NS)
                formula = cell.find("main:f", _NS)
                raw = value.text if value is not None else None
                typ = cell.attrib.get("t", "n")
                if typ == "s" and raw is not None:
                    raw = shared[int(raw)]
                elif typ == "inlineStr":
                    inline = cell.find("main:is", _NS)
                    raw = "".join(inline.itertext()) if inline is not None else ""
                elif typ == "b" and raw is not None:
                    raw = raw == "1"
                elif typ == "n" and raw is not None:
                    try:
                        raw = float(raw)
                    except ValueError:
                        pass
                cells[cell.attrib["r"]] = {"value": raw, "formula": formula.text if formula is not None else None, "type": typ, "formula_attributes": dict(formula.attrib) if formula is not None else {}}
                if formula is not None:
                    formula_records.append((cell.attrib["r"], (formula.text or "").strip(), sorted(formula.attrib.items())))
            canonical = json.dumps(formula_records, separators=(",", ":"))
            self._formula_fingerprints[sheet.attrib["name"]] = hashlib.sha256(canonical.encode()).hexdigest()
            self._cells[sheet.attrib["name"]] = cells
        self.sheet_order = list(self._sheets)
        prefixes = tuple(self.contract.get("feature_policy", {}).get("feature_prefixes", []))
        self.package_features = sorted(p for p in self._parts if p.startswith(prefixes) or p == "xl/calcChain.xml")
        self.feature_hashes = {part: hashlib.sha256(self._zip.read(part)).hexdigest() for part in self.package_features}
        self.feature_hashes.update({part: hashlib.sha256(self._zip.read(part)).hexdigest() for part in self._parts if part.startswith("xl/media/")})
        self.feature_hashes.update({part: hashlib.sha256(self._zip.read(part)).hexdigest() for part in self._parts if part.startswith("xl/worksheets/_rels/") or part.startswith("xl/drawings/_rels/") or part.startswith("xl/webextensions/_rels/")})
        allowlist = set(self.contract.get("input_allowlist", []))
        self._static_records: dict[str, list[tuple[str, str, Any]]] = {}
        self._static_fingerprints: dict[str, str] = {}
        for sheet, cells in self._cells.items():
            records = [(address, item["type"], item["value"]) for address, item in cells.items() if item["formula"] is None and f"{sheet}!{address}" not in allowlist]
            self._static_records[sheet] = records
            self._static_fingerprints[sheet] = hashlib.sha256(json.dumps(records, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self._shared_formula_groups, self._formula_followers = self._inventory_pinned_formula_followers()

    def _pinned_static_records(self, sheet_name: str) -> list[tuple[str, str, Any]]:
        template = Path(__file__).resolve().parents[2] / str(self.contract.get("source_file", ""))
        if not template.is_file() or hashlib.sha256(template.read_bytes()).hexdigest() != self.contract.get("sha256"):
            return self._static_records.get(sheet_name, [])
        with _checked_zip(template) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rels = {r.attrib.get("Id"): r.attrib for r in relroot.findall("packageRel:Relationship", _NS)}
            sheets = {s.attrib.get("name"): s for s in workbook.findall("main:sheets/main:sheet", _NS)}
            sheet = sheets.get(sheet_name)
            if sheet is None:
                return []
            rel = rels.get(sheet.attrib.get("{" + _NS["officeRel"] + "}id"), {})
            part = _resolve_target("xl/workbook.xml", rel.get("Target", ""))
            shared = []
            if "xl/sharedStrings.xml" in self._parts:
                strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in strings.findall("main:si", _NS)]
            allowlist = set(self.contract.get("input_allowlist", []))
            root = ET.fromstring(archive.read(part))
            records = []
            for cell in root.findall(".//main:sheetData/main:row/main:c", _NS):
                address = cell.attrib.get("r")
                formula = cell.find("main:f", _NS)
                if formula is not None or f"{sheet_name}!{address}" in allowlist:
                    continue
                value = cell.find("main:v", _NS)
                raw = value.text if value is not None else None
                typ = cell.attrib.get("t", "n")
                if typ == "s" and raw is not None: raw = shared[int(raw)]
                elif typ == "inlineStr":
                    inline = cell.find("main:is", _NS); raw = "".join(inline.itertext()) if inline is not None else ""
                elif typ == "b" and raw is not None: raw = raw == "1"
                elif typ == "n" and raw is not None:
                    try: raw = float(raw)
                    except ValueError: pass
                records.append((address, typ, raw))
            return records

    def _inventory_pinned_formula_followers(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
        """Inventory shared/array formula ranges from the hash-pinned template."""
        template = Path(__file__).resolve().parents[2] / str(self.contract.get("source_file", ""))
        if not template.is_file() or hashlib.sha256(template.read_bytes()).hexdigest() != self.contract.get("sha256"):
            return {}, {}
        groups: dict[str, list[dict[str, Any]]] = {}
        followers: dict[str, set[str]] = {}
        with _checked_zip(template) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rels = {r.attrib.get("Id"): r.attrib for r in relroot.findall("packageRel:Relationship", _NS)}
            for sheet in workbook.findall("main:sheets/main:sheet", _NS):
                name = sheet.attrib.get("name")
                rel = rels.get(sheet.attrib.get("{" + _NS["officeRel"] + "}id"), {})
                if not rel.get("Type", "").endswith("/worksheet"):
                    continue
                part = _resolve_target("xl/workbook.xml", rel.get("Target", ""))
                root = ET.fromstring(archive.read(part))
                cells = {c.attrib.get("r"): c for c in root.findall(".//main:sheetData/main:row/main:c", _NS)}
                for address, cell in cells.items():
                    formula = cell.find("main:f", _NS)
                    if formula is None or formula.attrib.get("t") not in {"shared", "array"}:
                        continue
                    range_ref = formula.attrib.get("ref")
                    if not range_ref or ":" not in range_ref:
                        continue
                    first, last = range_ref.split(":", 1)
                    a, b = _CELL.fullmatch(first), _CELL.fullmatch(last)
                    if not a or not b:
                        continue
                    def col_number(text: str) -> int:
                        value = 0
                        for char in text: value = value * 26 + ord(char) - 64
                        return value
                    def col_letters(value: int) -> str:
                        letters = ""
                        while value:
                            value, rem = divmod(value - 1, 26); letters = chr(65 + rem) + letters
                        return letters
                    min_col, max_col = col_number(a.group(1)), col_number(b.group(1))
                    min_row, max_row = int(a.group(2)), int(b.group(2))
                    group_followers = []
                    for row_num in range(min_row, max_row + 1):
                        for col_num in range(min_col, max_col + 1):
                            coord = col_letters(col_num) + str(row_num)
                            if coord == address:
                                continue
                            follower = cells.get(coord)
                            follower_formula = follower.find("main:f", _NS) if follower is not None else None
                            if follower_formula is None or not (follower_formula.text or "").strip():
                                group_followers.append(coord)
                    groups.setdefault(name, []).append({"kind": formula.attrib["t"], "anchor": address, "range": range_ref, "shared_index": formula.attrib.get("si"), "formula": formula.text or "", "followers": group_followers})
                    followers.setdefault(name, set()).update(group_followers)
        return groups, followers

    def _verify_follower_structure(self) -> list[str]:
        template = Path(__file__).resolve().parents[2] / str(self.contract.get("source_file", ""))
        if not template.is_file() or hashlib.sha256(template.read_bytes()).hexdigest() != self.contract.get("sha256"):
            return ["pinned template unavailable for follower structure validation"]
        failures = []
        with _checked_zip(template) as original:
            current_workbook = ET.fromstring(self._zip.read("xl/workbook.xml"))
            pinned_workbook = ET.fromstring(original.read("xl/workbook.xml"))
            current_rels_root = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
            pinned_rels_root = ET.fromstring(original.read("xl/_rels/workbook.xml.rels"))
            current_rels = {r.attrib.get("Id"): r.attrib for r in current_rels_root.findall("packageRel:Relationship", _NS)}
            pinned_rels = {r.attrib.get("Id"): r.attrib for r in pinned_rels_root.findall("packageRel:Relationship", _NS)}
            def parts(book, rels):
                output = {}
                for sheet in book.findall("main:sheets/main:sheet", _NS):
                    rel = rels.get(sheet.attrib.get("{" + _NS["officeRel"] + "}id"), {})
                    if rel.get("Type", "").endswith("/worksheet"):
                        output[sheet.attrib["name"]] = _resolve_target("xl/workbook.xml", rel.get("Target", ""))
                return output
            current_parts, pinned_parts = parts(current_workbook, current_rels), parts(pinned_workbook, pinned_rels)
            def structural_signature(cell):
                if cell is None:
                    return None
                attrs = tuple(sorted((key, value) for key, value in cell.attrib.items() if key != "t"))
                children = []
                for child in list(cell):
                    if child.tag == "{" + _NS["main"] + "}v":
                        continue
                    children.append((child.tag, tuple(sorted(child.attrib.items())), child.text or "", tuple((sub.tag, tuple(sorted(sub.attrib.items())), sub.text or "") for sub in list(child))))
                return attrs, tuple(children)
            for sheet_name, addresses in self._formula_followers.items():
                if sheet_name not in current_parts or sheet_name not in pinned_parts:
                    failures.append(f"follower worksheet missing: {sheet_name}"); continue
                current_root = ET.fromstring(self._zip.read(current_parts[sheet_name]))
                pinned_root = ET.fromstring(original.read(pinned_parts[sheet_name]))
                current_cells = {c.attrib.get("r"): c for c in current_root.findall(".//main:sheetData/main:row/main:c", _NS)}
                pinned_cells = {c.attrib.get("r"): c for c in pinned_root.findall(".//main:sheetData/main:row/main:c", _NS)}
                for address in addresses:
                    if structural_signature(current_cells.get(address)) != structural_signature(pinned_cells.get(address)):
                        failures.append(f"shared/array formula follower structure changed: {sheet_name}!{address}")
        return failures

    @staticmethod
    def normalize_formula(formula: str | None) -> str | None:
        """Normalize insignificant whitespace and leading '=' for comparison."""
        if formula is None:
            return None
        return re.sub(r"\s+", "", formula.lstrip("=")).upper()

    def verify_identity(self) -> dict[str, Any]:
        """Check whether this is the byte-identical pristine template."""
        actual_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        expected_hash = self.contract.get("sha256")
        return {"matches_template": actual_hash == expected_hash, "expected_sha256": expected_hash, "sha256": actual_hash}

    def verify_structure(self, mode: str = "strict") -> dict[str, Any]:
        """Validate structure; saved mode explicitly permits allowlisted value edits.

        Both modes retain identical formula, metadata, relationship, feature, and
        non-input cell checks. Allowlisted cells are excluded from the static
        value fingerprint in either mode, as they are template inputs.
        """
        if mode not in {"strict", "saved"}:
            raise ValueError("mode must be 'strict' or 'saved'")
        failures: list[str] = []
        if mode == "saved":
            failures.extend(self._verify_follower_structure())
        if self.sheet_order != self.contract.get("sheet_order"):
            failures.append("worksheet order changed")
        expected_sheets = self.contract.get("sheet_metadata", {})
        for name, expected in expected_sheets.items():
            actual = self._sheets.get(name)
            if actual is None:
                failures.append(f"sheet missing: {name}")
                continue
            for field in ("sheet_id", "state", "relationship_id", "part"):
                if actual.get(field) != expected.get(field):
                    failures.append(f"sheet {field} changed: {name}")
        if self.defined_names != self.contract.get("defined_names", []):
            failures.append("defined names changed")
        for sheet, expected in self.contract.get("worksheet_metadata_fingerprints", {}).items():
            if self._worksheet_metadata_fingerprints.get(sheet) != expected:
                failures.append(f"worksheet metadata changed: {sheet}")
        expected_settings = self.contract.get("calc_settings", {})
        permitted = self.contract.get("permitted_calc_settings", {})
        for key in set(expected_settings) | set(self.calc_settings):
            policy = permitted.get(key, "must-match")
            if policy == "any":
                continue
            if self.calc_settings.get(key) != expected_settings.get(key):
                failures.append(f"calculation setting changed: {key}")
        expected_fingerprints = self.contract.get("formula_fingerprints", {})
        expected_counts = self.contract.get("formula_counts", {})
        for sheet, fingerprint in expected_fingerprints.items():
            if self._formula_fingerprints.get(sheet) != fingerprint:
                failures.append(f"formula inventory changed: {sheet}")
            actual_count = sum(1 for item in self._cells.get(sheet, {}).values() if item.get("formula") is not None)
            if expected_counts.get(sheet) != actual_count:
                failures.append(f"formula count changed: {sheet}")
        expected_relationships = self.contract.get("relationship_fingerprint")
        if expected_relationships and self.relationship_fingerprint != expected_relationships:
            failures.append("workbook relationship inventory changed")
        feature_policy = self.contract.get("feature_policy", {})
        template_path = Path(__file__).resolve().parents[2] / str(self.contract.get("source_file", ""))
        if mode == "saved" and template_path.is_file() and hashlib.sha256(template_path.read_bytes()).hexdigest() == self.contract.get("sha256"):
            with _checked_zip(template_path) as pinned:
                pinned_parts = set(pinned.namelist())
            permitted_missing_parts = set(feature_policy.get("permitted_missing_parts", []))
            unexpected_members = self._parts - pinned_parts
            missing_members = (pinned_parts - self._parts) - permitted_missing_parts
            if unexpected_members:
                failures.append("unexpected package members: " + ", ".join(sorted(unexpected_members)))
            if missing_members:
                failures.append("missing package members: " + ", ".join(sorted(missing_members)))
        expected_features = feature_policy.get("required_part_hashes", {})
        for part, digest in expected_features.items():
            if part == "xl/calcChain.xml" and part not in self._parts:
                continue
            if part not in self._parts:
                failures.append(f"required feature part missing: {part}")
            elif self.feature_hashes.get(part) != digest:
                failures.append(f"feature content changed: {part}")
        allowed_missing = set(feature_policy.get("permitted_missing_parts", []))
        actual_features = set(self.package_features) | {p for p in self._parts if p.startswith(("xl/worksheets/_rels/", "xl/drawings/_rels/", "xl/webextensions/_rels/"))}
        for part in actual_features - set(expected_features) - allowed_missing:
            failures.append(f"unexpected feature part: {part}")
        for sheet, expected in self.contract.get("static_cell_fingerprints", {}).items():
            actual_records = self._static_records.get(sheet, [])
            if mode == "saved":
                excluded = self._formula_followers.get(sheet, set())
                actual_records = [record for record in actual_records if record[0] not in excluded]
                template_records = self._pinned_static_records(sheet)
                template_records = [record for record in template_records if record[0] not in excluded]
                expected_fingerprint = hashlib.sha256(json.dumps(template_records, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            else:
                expected_fingerprint = expected
            actual_fingerprint = hashlib.sha256(json.dumps(actual_records, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            if actual_fingerprint != expected_fingerprint:
                failures.append(f"static cell inventory changed: {sheet}")
        for key in self.contract.get("input_allowlist", []):
            sheet, cell = key.split("!", 1)
            item = self._cells.get(sheet, {}).get(cell)
            if item is None or item.get("formula") is not None:
                failures.append(f"allowlisted input is not a direct cell: {key}")
        for key, formula in self.contract.get("protected_formulas", {}).items():
            sheet, cell = key.split("!", 1)
            actual = self._cells.get(sheet, {}).get(cell, {}).get("formula")
            if self.normalize_formula(actual) != self.normalize_formula(formula):
                failures.append(f"protected formula changed: {key}")
        for part in self.contract.get("required_parts", []):
            if part not in self._parts:
                failures.append(f"required package feature missing: {part}")
        for part in self.contract.get("forbidden_parts", []):
            if part in self._parts:
                failures.append(f"unexpected package feature: {part}")
        if failures:
            raise WorkbookContractError("; ".join(failures))
        return {"valid": True, "sheet_order": list(self.sheet_order), "formula_fingerprints": dict(self._formula_fingerprints)}

    def verify_contract(self) -> dict[str, Any]:
        """Compatibility alias for structural verification of a saved copy."""
        return self.verify_structure()

    def read_input(self, cell: str, sheet: str = "Input sheet") -> Any:
        """Read a cached input only when its exact sheet/cell is allowlisted."""
        key = f"{sheet}!{cell}"
        if key not in self.contract.get("input_allowlist", []):
            raise PermissionError(f"Input cell is not allowlisted: {key}")
        item = self._cells.get(sheet, {}).get(cell)
        if item is None or item.get("formula") is not None:
            raise WorkbookContractError(f"Input cell is not a direct editable value: {key}")
        return item.get("value")

    def read_output(self, cell: str, sheet: str = "Valuation output") -> Any:
        """Read a cached output; formula results are never evaluated here."""
        if sheet not in self.contract.get("output_sheets", []):
            raise PermissionError(f"Not a contracted output sheet: {sheet}")
        item = self._cells.get(sheet, {}).get(cell)
        return None if item is None else item["value"]

    def branch_status(self) -> dict[str, Any]:
        """Report readiness conservatively; unresolved/invalid choices never pass."""
        switches = self.contract.get("branch_switches", {})
        unsupported = self.contract.get("unsupported_switch_values", {})
        unresolved: list[str] = []
        unsupported_branches: list[str] = []
        switch_values: dict[str, Any] = {}
        for key, allowed in switches.items():
            sheet, cell = key.split("!", 1)
            value = self._cells.get(sheet, {}).get(cell, {}).get("value")
            unsupported_values = unsupported.get(key, [])
            switch_values[key] = {"value": value, "allowed": list(allowed), "unsupported": list(unsupported_values), "resolved": value in allowed}
            if value not in allowed:
                unresolved.append(key)
                if value in unsupported_values:
                    unsupported_branches.append(key)
        selected = "direct_input" if switch_values.get("Cost of capital worksheet!B11", {}).get("value") == "I will input" else "unsupported"
        active_specs = self.contract.get("active_outputs_by_branch", {}).get(selected, [])
        active_keys = [item["cell"] for item in active_specs]
        output_results = {}
        errors = []
        type_failures = []
        for spec in active_specs:
            key = spec["cell"]
            sheet, cell = key.split("!", 1)
            value = self._cells.get(sheet, {}).get(cell, {}).get("value")
            output_results[key] = {"value": value, "type": spec["type"], "required": spec.get("required", False)}
            if isinstance(value, str) and value in _ERROR_VALUES:
                errors.append(key)
            if spec["type"] == "numeric" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not __import__("math").isfinite(value)):
                type_failures.append(key)
        diagnostic_results = {}
        for spec in self.contract.get("conditional_diagnostics_by_branch", {}).get(selected, []):
            key = spec["cell"]
            sheet, cell = key.split("!", 1)
            value = self._cells.get(sheet, {}).get(cell, {}).get("value")
            diagnostic_results[key] = {"value": value, "type": spec["type"], "required": spec.get("required", False)}
            if isinstance(value, str) and value in _ERROR_VALUES:
                errors.append(key)
            if spec["type"] == "numeric" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not __import__("math").isfinite(value)):
                type_failures.append(key)
        missing_inputs = []
        required_inputs = list(self.contract.get("required_inputs", []))
        for condition in self.contract.get("conditional_required_inputs", []):
            switch = condition["when"]
            switch_sheet, switch_cell = switch.split("!", 1)
            if self._cells.get(switch_sheet, {}).get(switch_cell, {}).get("value") == condition.get("equals"):
                required_inputs.extend(condition.get("inputs", []))
        for key in dict.fromkeys(required_inputs):
            sheet, cell = key.split("!", 1)
            value = self._cells.get(sheet, {}).get(cell, {}).get("value")
            if value is None or value == "" or (isinstance(value, str) and value in _ERROR_VALUES):
                missing_inputs.append(key)
        calc_policy = self.contract.get("permitted_calc_settings", {})
        calc_attestation = {"calcId": self.calc_settings.get("calcId"), "calcId_change_permitted": calc_policy.get("calcId") == "any", "iteration_settings": {key: value for key, value in self.calc_settings.items() if key.startswith("iterate")}, "iteration_settings_verified": all(key not in self.calc_settings for key in ("iterate", "iterateCount", "iterateDelta"))}
        return {"ready": not unresolved and not unsupported_branches and not errors and not missing_inputs and not type_failures, "branch": selected, "switches": switch_values, "unresolved_switches": unresolved, "unsupported_branches": unsupported_branches, "active_outputs": output_results, "active_diagnostics": diagnostic_results, "type_failures": type_failures, "missing_required_inputs": missing_inputs, "cached_error_outputs": sorted(set(errors)), "calculation_attestation": calc_attestation}
