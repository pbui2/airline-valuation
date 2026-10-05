"""Review-only v2 scope, mapping, and independent inspector for ten new Ginzu cells.

This module is the single review-only source of truth for the bounded v2 packet:

* an ordered, unique 58-reference scope = 28 retained Input-sheet B-column
  references with row < 45 + 20 retained Cost of capital worksheet references +
  10 new cells (``Input sheet!B9``, ``C11``..``C15``, ``C19``..``C21``, ``D11``);
* an exact 19-reference deferred set (Input-sheet B-column references with
  row >= 45) that is deliberately OUT of scope;
* an explicit exclusion of ``Cost of capital worksheet!H32``;
* a read-only, independent inspector for exactly the ten new cells using the
  same canonical serialization as ``trial_guidance.py``
  (``SHA256(ET.tostring(cell, encoding="utf-8"))`` and the same relationship
  handling for threaded/legacy comments), without importing or mutating
  ``trial_guidance.py``;
* the v2 packet builders (review scope, new-cell inventory, input policy with
  verbatim v1 retention links, and the 58-row Southwest review).

Nothing here is an approval: the packet is review-only, the workbook is never
modified, and no source authorization, rights/cutoff clearance, accounting
signoff, or write authority is claimed. Importing this module has no side
effects; JSON artifacts are only written when ``write_v2_artifacts()`` or the
``__main__`` entry point is invoked explicitly.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).with_name("detailed_contract.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PINNED_WORKBOOK_SHA256 = CONTRACT["sha256"]
DEFAULT_WORKBOOK = Path(__file__).resolve().parents[2] / CONTRACT["source_file"]
V1_POLICY_PATH = Path(__file__).resolve().parents[2] / "docs" / "policy-drafts" / "ginzu-input-policy-v1.json"
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "policy-drafts"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    "tc": "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments",
}
REF_RE = re.compile(r"(?<![A-Za-z0-9_])(?:'[^']*(?:''[^']*)*'|[A-Za-z_][A-Za-z0-9_. ]*)?!?\$?[A-Z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Z]{1,3}\$?[1-9][0-9]*)?(?![A-Za-z0-9_(])")

# ---------------------------------------------------------------------------
# Scope definition
# ---------------------------------------------------------------------------

NEW_CELL_REFS: list[str] = [
    "Input sheet!B9",
    "Input sheet!C11",
    "Input sheet!C12",
    "Input sheet!C13",
    "Input sheet!C14",
    "Input sheet!C15",
    "Input sheet!C19",
    "Input sheet!C20",
    "Input sheet!C21",
    "Input sheet!D11",
]
NEW_CELL_REFS_SET = frozenset(NEW_CELL_REFS)
EXCLUDED_H32 = "Cost of capital worksheet!H32"

def _ref_row(ref: str) -> int:
    """Row number of a reference such as 'Input sheet!B11' or 'Cost of capital worksheet!H21'."""
    return int(re.search(r"[0-9]+$", ref).group())


# Deliberately out of scope: Input-sheet B-column references with row >= 45.
DEFERRED_REFS: list[str] = [
    ref for ref in CONTRACT["input_allowlist"]
    if ref.startswith("Input sheet!") and _ref_row(ref) >= 45
]

# Retained from the v1 policy: Input-sheet B-column references with row < 45
# (28) plus the Cost of capital worksheet references (20).
RETAINED_B_LT45_REFS: list[str] = [
    ref for ref in CONTRACT["input_allowlist"]
    if ref.startswith("Input sheet!") and _ref_row(ref) < 45
]
RETAINED_COST_REFS: list[str] = [
    ref for ref in CONTRACT["input_allowlist"] if ref.startswith("Cost of capital worksheet!")
]
RETAINED_V1_REFS: list[str] = list(RETAINED_B_LT45_REFS) + list(RETAINED_COST_REFS)

# Ordered, unique 58-reference scope: 28 retained Input B<45, then 20 retained
# Cost, then the ten new cells.
SCOPE_REFS: list[str] = list(RETAINED_V1_REFS) + list(NEW_CELL_REFS)
if len(SCOPE_REFS) != len(set(SCOPE_REFS)):
    raise ValueError("v2 scope is not unique")
if len(SCOPE_REFS) != 58:
    raise ValueError(f"v2 scope must contain 58 references, got {len(SCOPE_REFS)}")
if len(RETAINED_B_LT45_REFS) != 28 or len(RETAINED_COST_REFS) != 20 or len(NEW_CELL_REFS) != 10:
    raise ValueError("v2 scope grouping (28/20/10) is inconsistent")
if len(DEFERRED_REFS) != 19:
    raise ValueError(f"v2 deferred set must contain 19 references, got {len(DEFERRED_REFS)}")
if EXCLUDED_H32 in CONTRACT["input_allowlist"]:
    raise ValueError("H32 must not be in the detailed-contract input allowlist")

SCOPE_GROUP_BY_REF = {ref: "retained_input_b_lt_45" for ref in RETAINED_B_LT45_REFS}
SCOPE_GROUP_BY_REF.update({ref: "retained_cost" for ref in RETAINED_COST_REFS})
SCOPE_GROUP_BY_REF.update({ref: "new_cell" for ref in NEW_CELL_REFS})


class ReviewScopeError(ValueError):
    """The workbook, reference, or packet does not satisfy the v2 review scope."""


# ---------------------------------------------------------------------------
# Independent read-only inspector for the ten new cells
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ElementTree keeps a process-global namespace-prefix registry
# (ET._namespace_map) that register_namespace() mutates permanently; libraries
# such as openpyxl (imported by other test modules) register the Office Open XML
# prefixes, which changes the byte output of ET.tostring() and therefore the
# cell/validation hashes. The checked-in v2 digests were hashed against
# ElementTree's pristine default registry, so every canonical digest is computed
# under exactly that default regardless of earlier imports or tests. These seven
# entries are the CPython default _namespace_map (mirrors review_copy.py).
_PRISTINE_NAMESPACE_MAP = {
    "http://www.w3.org/XML/1998/namespace": "xml",
    "http://www.w3.org/1999/xhtml": "html",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf",
    "http://schemas.xmlsoap.org/wsdl/": "wsdl",
    "http://schemas.xmlsoap.org/ws/2004/08/addressing": "wsaw",
    "http://www.w3.org/2001/XMLSchema": "xs",
    "http://www.w3.org/2001/XMLSchema-instance": "xsi",
}


@contextmanager
def _restore_et_namespace_registry() -> Iterator[None]:
    """Run a block with ElementTree's process-global registry at the pristine default."""
    previous = dict(ET._namespace_map)
    try:
        ET._namespace_map.clear()
        ET._namespace_map.update(_PRISTINE_NAMESPACE_MAP)
        yield
    finally:
        ET._namespace_map.clear()
        ET._namespace_map.update(previous)


def _canonical_digest(element: ET.Element) -> str:
    """SHA256(ET.tostring(element, encoding='utf-8')) under the pristine registry."""
    with _restore_et_namespace_registry():
        return _sha(ET.tostring(element, encoding="utf-8"))


def _ref_row(ref: str) -> int:
    return int(re.search(r"[0-9]+$", ref).group())


def _ref_column(ref: str) -> int:
    value = 0
    for char in re.match(r"[A-Z]+", ref.split("!")[1]).group():
        value = value * 26 + ord(char) - 64
    return value


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


def _inspect_new_cells(path: Path, *, verify_pin: bool) -> dict[str, Any]:
    """Independently inspect exactly the ten new cells (canonical serialization)."""
    raw_package = path.read_bytes()
    package_hash = _sha(raw_package)
    if verify_pin and package_hash != PINNED_WORKBOOK_SHA256:
        raise ReviewScopeError(
            f"workbook SHA-256 {package_hash} does not match pinned {PINNED_WORKBOOK_SHA256}"
        )
    with zipfile.ZipFile(path, "r") as archive:
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in relationships.findall("p:Relationship", NS)}
        sheet_parts: dict[str, str] = {}
        for sheet in wb.findall("m:sheets/m:sheet", NS):
            rid = sheet.attrib["{" + NS["r"] + "}id"]
            sheet_parts[sheet.attrib["name"]] = _target("xl/workbook.xml", targets[rid])
        strings = _strings(archive)
        entries: list[dict[str, Any]] = []
        for ref in NEW_CELL_REFS:
            sheet_name, address = ref.split("!", 1)
            part = sheet_parts[sheet_name]
            sheet_bytes = archive.read(part)
            root = ET.fromstring(sheet_bytes)
            cellmap = {c.attrib["r"]: c for c in root.findall(".//m:sheetData/m:row/m:c", NS)}
            cell = cellmap.get(address)
            comments = []
            for kind, comment_part, comment_bytes in _parts_comments(archive, part):
                cr = ET.fromstring(comment_bytes)
                for text in _comment_records(cr, address, kind == "threaded"):
                    comments.append({"kind": kind, "text": text,
                                     "part": comment_part, "part_sha256": _sha(comment_bytes)})
            rnum = _row(address)
            target_col = _column(address)
            context = []
            for rr in (rnum - 1, rnum, rnum + 1):
                values = [{"cell": ref_cell, "value": _cell_value(c, strings)}
                          for ref_cell, c in cellmap.items()
                          if _row(ref_cell) == rr and _cell_value(c, strings) is not None
                          and (abs(_column(ref_cell) - target_col) <= 2 or _column(ref_cell) <= 3)]
                if values:
                    context.append({"row": rr, "cells": sorted(values, key=lambda x: _column(x["cell"]))})
            formula_node = cell.find("m:f", NS) if cell is not None else None
            formula = formula_node.text if formula_node is not None else None
            validations = []
            for node in root.iter():
                if not node.tag.endswith("dataValidation"):
                    continue
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
                                        "xml_sha256": _canonical_digest(node)})
            entries.append({
                "reference": ref, "worksheet_part": part,
                "worksheet_xml_sha256": _sha(sheet_bytes),
                "cell_present": cell is not None,
                "cell_type": cell.attrib.get("t") if cell is not None else None,
                "cell_style": cell.attrib.get("s") if cell is not None else None,
                "cell_value": _cell_value(cell, strings),
                "formula": formula, "formula_sha256": _sha((formula or "").encode()),
                "dependencies": _formula_dependencies(formula),
                "cell_xml_sha256": _canonical_digest(cell) if cell is not None else None,
                "comments": comments,
                "comment_status": "present" if comments else "absent in related comment parts",
                "nearby_headers_and_labels": context,
                "validations": validations,
                "guard_classification": None,
            })
    return {
        "schema_version": "ginzu-new-cell-inventory/2",
        "source_file": path.name,
        "pinned_source_sha256": PINNED_WORKBOOK_SHA256,
        "observed_source_sha256": package_hash,
        "source_matches_pin": package_hash == PINNED_WORKBOOK_SHA256,
        "reference_count": len(entries),
        "entries": entries,
    }


def inspect_new_cells(path: str | Path | None = None, *, verify_pin: bool = True) -> dict[str, Any]:
    """Return cell-level evidence for exactly the ten new cells; never modifies the workbook."""
    return _inspect_new_cells(Path(path) if path is not None else DEFAULT_WORKBOOK, verify_pin=verify_pin)


def new_cell_by_reference(reference: str) -> dict[str, Any]:
    """Return the live inspector entry for one of the ten new cells.

    Raises ReviewScopeError for any reference outside the ten new cells (used by
    tests for negative-ID coverage).
    """
    if reference not in NEW_CELL_REFS_SET:
        raise ReviewScopeError(f"{reference} is not one of the ten v2 new cells")
    by_ref = {entry["reference"]: entry for entry in inspect_new_cells()["entries"]}
    return by_ref[reference]


# ---------------------------------------------------------------------------
# Authored v2 content: ten substantive new-cell proposals
# ---------------------------------------------------------------------------

_NEW_CELL_LABELS = {
    "Input sheet!B9": "Industry (Global)",
    "Input sheet!C11": "Revenues",
    "Input sheet!C12": "Operating income or EBIT",
    "Input sheet!C13": "Interest expense",
    "Input sheet!C14": "Book value of equity",
    "Input sheet!C15": "Book value of debt",
    "Input sheet!C19": "Cross holdings and other non-operating assets",
    "Input sheet!C20": "Minority interests",
    "Input sheet!C21": "Number of shares outstanding",
    "Input sheet!D11": "Years since last 10K",
}

NEW_CELL_CLASSIFICATION = {
    "Input sheet!B9": {
        "type": "lookup_route",
        "status": "selector",
        "unit": "industry key (Global averages)",
        "period": "issuer classification period; specify",
        "branch": "global industry lookup (Cost B21 Global route)",
    },
    "Input sheet!C11": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify", "branch": "prior-10-K revenue"},
    "Input sheet!C12": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify", "branch": "prior-10-K operating income"},
    "Input sheet!C13": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify", "branch": "prior-10-K interest expense"},
    "Input sheet!C14": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify", "branch": "prior-10-K book value of equity"},
    "Input sheet!C15": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify", "branch": "prior-10-K book value of debt"},
    "Input sheet!C19": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify",
                        "branch": "prior-10-K cross holdings / non-operating assets"},
    "Input sheet!C20": {"type": "filing_fact", "status": "unresolved",
                        "unit": "USD millions; verify scale/currency",
                        "period": "fiscal year prior to LTM; specify",
                        "branch": "prior-10-K minority interests"},
    "Input sheet!C21": {"type": "filing_fact", "status": "unresolved",
                        "unit": "shares; confirm share class and scale",
                        "period": "fiscal year prior to LTM; specify",
                        "branch": "prior-10-K shares outstanding"},
    "Input sheet!D11": {"type": "lookup_route", "status": "selector",
                        "unit": "year fraction (1.00 / 0.75 / 0.50 / 0.25)",
                        "period": "LTM window vs most recent fiscal year; specify",
                        "branch": "years-since-last-10K alignment"},
}

_DEFAULT_EVALUATION = {
    "operation": "unresolved",
    "source_tiers": [],
    "components": [],
    "tie_breaker": "Only an exact, issuer-specific, period/unit/route-compatible, rights-cleared source or approved assumption may resolve this cell; conflicting or absent evidence abstains.",
    "missing_component": "abstain; do not assume zero",
    "fallback": None,
    "bounds": "unresolved; workbook validations are input constraints, not source values",
    "route_gate": None,
}

# Ten substantive distinct proposals. Each is review-only; none is an approval.
NEW_CELL_PROPOSALS: dict[str, dict[str, Any]] = {
    "Input sheet!B9": {
        "status": "unresolved",
        "evaluation": {
            "operation": "select",
            "source_tiers": [
                {"tier": 1, "source_kind": "issuer_primary_filing",
                 "precedence": 1, "eligibility": "issuer business mix and geographic scope for the intended global group"},
                {"tier": 2, "source_kind": "pinned_workbook_validation_domain",
                 "precedence": 2, "eligibility": "permitted key values from the authenticated B8:B9 list domain only"},
            ],
            "components": [],
            "tie_breaker": "Exclusion-first: B9 is not in the detailed-contract input_allowlist, so it is not a direct-contract input target; it becomes applicable only if the Cost B21 selector activates a Global route and the issuer's global business mix justifies one group. Two materially plausible groups => abstain.",
            "missing_component": "abstain; a dropdown domain value alone is not issuer evidence",
            "fallback": None,
            "bounds": "unresolved",
            "route_gate": {"reference": "Cost of capital worksheet!B21",
                           "when": "Single Business(Global) or Multibusiness(Global)",
                           "otherwise": "not_applicable; omitted, never substituted"},
        },
        "policy_family": "selector",
        "applicability_status": "conditional",
        "source_concept": "Input sheet B9, labeled Industry (Global): the global industry-averages lookup key, distinct from B8 (Industry (US)). B9 is absent from the detailed-contract input_allowlist (exclusion-first). Two distinct workbook facts are independently verified: (1) the Input-sheet global-industry VLOOKUPs read B9 UNCONDITIONALLY (K26/K27/K29 = VLOOKUP(B9,'Industry Averages (Global)'!A2:N95,...), no IF gate); (2) the Cost of capital worksheet B23 branch uses B9 ONLY under the Global route (B21='Single Business(Global)'). Unconditional workbook evaluation of B9 must not be mistaken for an active Global cost-of-capital route.",
        "source_hierarchy": [
            "issuer_primary_filing for issuer business mix and geographic scope",
            "issuer_audited_statement_or_note for segment revenue composition",
            "issuer_filed_tagged_fact for reconciled segment dimensions",
            "pinned workbook validation domain ('Industry Averages(US)'!$A$2:$A$95 via B8:B9) for permitted key values only",
        ],
        "composition": "Exclusion-first: first record that B9 is outside the direct detailed-contract allowlist; then note that the Input-sheet VLOOKUPs K26/K27/K29 evaluate B9 unconditionally (workbook evaluation), while the Cost of capital worksheet B23 cost-of-capital branch consumes B9 only when the B21 selector is 'Single Business(Global)' (conditional route). Select one global industry key only if the Cost B21 Global route is active and issuer business mix/geographic scope justify it. Document why the global group (rather than the US B8 key) best represents the intended lookup, and keep any later cost-of-capital regional detail separate.",
        "tie_break": "Use issuer-documented business mix and purpose-specific classification; if two candidate global groups remain materially plausible, abstain rather than resolve by workbook order or an unsupported majority assumption.",
        "missing_component": "Abstain if issuer activity mix, classification period, or the active route cannot be established; a dropdown value alone is insufficient.",
        "fallback": {
            "status": "outstanding",
            "trigger": "Issuer global classification or active-route evidence is missing, stale, conflicting, or cannot justify one lookup group.",
            "formula": None,
            "parameters": None,
            "alternatives": [
                "Request current issuer segment/activity evidence and reviewer-approved classification",
                "Leave the selector decision unresolved; do not infer a lookup choice",
            ],
            "sensitivity": "Global industry group changes lookup comparators and downstream cost-of-capital assumptions; effect cannot be quantified without a justified selection and active route.",
        },
        "route_gate": {"reference": "Cost of capital worksheet!B21",
                       "when": "Single Business(Global) or Multibusiness(Global)",
                       "otherwise": "not_applicable; omitted, never substituted"},
        "unresolved_rationale": "B9 is excluded from the direct detailed-contract input allowlist (exclusion-first). The Input-sheet VLOOKUPs K26/K27/K29 evaluate B9 unconditionally (workbook evaluation), while the Cost B23 cost-of-capital branch uses B9 only under the B21 Global route; resolve whether that Global route is active and whether issuer business mix/geographic scope justify one global industry group. The B8:B9 list validation provides choices, not issuer suitability.",
        "decision_type": "selector",
        "cross_boundary": "Crosses the direct-contract scope boundary (B9 outside the allowlist) and the US/Global route boundary; unconditional workbook evaluation (Input K26/K27/K29) must not be mistaken for an active Global cost-of-capital route (Cost B23 under B21). Never substitute the B8 US key or any other issuer's group.",
    },
    "Input sheet!C11": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C11 under header C10 'Last 10K before LTM': issuer revenue for the fiscal year immediately prior to the LTM window. This is a PRIOR fiscal-year fact, never the FY2025 B11 amount; for an issuer whose LTM equals the most recent fiscal year, C11 is the previous fiscal year's revenue. C11 is consumed by the Input-sheet I25 growth conversion (IF(C11>0,(B11/C11)^(1/D11)-1,'NA')) and by the Input-sheet I28 historical growth/ROE bridge (numerator B11-C11 and prior-period denominator C14+C15-C18).",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year revenue and period bridge",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single reported prior-fiscal-year revenue amount. Prior 10-K C facts are never copies of FY2025 B facts: C11 must be sourced to the fiscal year before the LTM window even when B11 and C11 are numerically close.",
        "tie_break": "Exact issuer/concept/period/unit/scale; same-tier conflicting facts => unresolved; do not select by magnitude or recency alone.",
        "missing_component": "abstain; missing prior-period evidence is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, revenue concept, scope, unit/scale and sign for the C-column period; never use the FY2025 B11 fact or a workbook-suggested base for C11.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM (B-column) / prior-10-K (C-column) period boundary; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!C12": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C12 under C10 'Last 10K before LTM': issuer operating income or EBIT for the fiscal year prior to the LTM window, per the workbook comment 'from the annual report or 10K in the fiscal year prior to your most recent 12 month data'. Never the FY2025 B12 amount.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year operating income and period bridge",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single reported prior-fiscal-year operating income/EBIT amount; confirm the statement line maps to operating income/EBIT, not another similarly named measure, and confirm scale, sign and period.",
        "tie_break": "Exact issuer/concept/period/unit/scale; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; missing prior-period evidence is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, operating-income concept, scope, unit/scale and sign for the C-column period; never use the FY2025 B12 fact for C12.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!C13": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C13 under C10 'Last 10K before LTM': issuer interest expense for the fiscal year prior to the LTM window (no workbook comment present on this cell; the column header defines the period). Never the FY2025 B13 amount.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year interest expense and period bridge",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single reported prior-fiscal-year interest-expense amount; reconcile face/notes and confirm gross-versus-net interest convention and period.",
        "tie_break": "Exact issuer/concept/period/unit/scale; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; missing prior-period evidence is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, interest-expense concept, scope, unit/scale and sign for the C-column period; never use the FY2025 B13 fact for C13.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!C14": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C14 under C10 'Last 10K before LTM': issuer book value of equity (total) at the end of the annual report for the fiscal year prior to the LTM window, per the workbook comment. Never the FY2025 B14 amount.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year equity and attribution",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single reported prior-fiscal-year total book equity amount. Determine whether the reported total already includes separately listed minority/noncontrolling interests before adding a separate component; an absence of a search hit is not proof of zero. Never double count an amount already included in the total.",
        "tie_break": "Exact issuer/concept/period/unit/scale; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; no zero assumption for a missing prior-period NCI item",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, equity definition, NCI inclusion, unit/scale and sign for the C-column period; never use the FY2025 B14 fact for C14.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary and the equity-attribution (NCI) boundary carried from B14; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!C15": {
        "status": "unresolved",
        "evaluation": {
            "operation": "sum_components",
            "source_tiers": [
                {"tier": 1, "source_kind": "issuer_balance_sheet_and_note",
                 "precedence": 1, "eligibility": "reconcile debt definition, current/noncurrent components, lease inclusion and date for the prior fiscal year"},
            ],
            "components": [
                {"name": "verified_interest_bearing_debt_components_prior_fiscal_year", "sign": 1,
                 "source_ref": "issuer filing locators to be reconciled", "required": True,
                 "evidence_status": "unresolved"},
                {"name": "lease_liabilities_if_selected_convention_includes_them_prior_fiscal_year", "sign": 1,
                 "source_ref": "issuer filing locators to be reconciled", "required": True,
                 "evidence_status": "unresolved"},
            ],
            "tie_breaker": "Balance-sheet-first for the prior fiscal year; reconcile face lines, notes and tags; exclude finance-lease amounts already embedded in the selected debt subtotal; never add the same obligation twice. The prior-period convention must be internally consistent with the LTM-period B15/B17 treatment.",
            "missing_component": "unresolved; prior-period B17-style Yes/No must be consistent with the C15 convention",
            "fallback": None,
            "bounds": "unresolved; prior-period debt amount and lease population require accounting review",
            "route_gate": None,
        },
        "policy_family": "debt",
        "applicability_status": "active",
        "source_concept": "Input sheet C15 under C10 'Last 10K before LTM': issuer book value of interest-bearing debt (short and long term) at the end of the balance sheet of the fiscal year prior to the LTM window, per the workbook comment. Never the FY2025 B15 amount. C15 is consumed by Input-sheet I28 (the historical growth/ROE bridge denominator (B14+B15-B18)-(C14+C15-C18)).",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year debt and lease disclosures",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Reconcile prior-fiscal-year current and noncurrent interest-bearing debt to note totals and tagged observations; resolve finance- and operating-lease inclusion before concluding any obligation is or is not included. The prior-period debt/lease convention crosses the same lease-converter boundary as LTM B15/B17 and must be internally consistent with it. C15 feeds Input-sheet I28 (denominator (B14+B15-B18)-(C14+C15-C18)).",
        "tie_break": "Balance-sheet-first; reconcile face lines, notes and tags; never add the same obligation twice. Unreconciled candidate amounts abstain.",
        "missing_component": "unresolved; a missing prior-period lease or debt line is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, debt definition, current/noncurrent coverage, finance-lease inclusion, and operating-lease population for the C-column period; never use the FY2025 B15 fact for C15. C15's lease-inclusive debt convention is reviewable in scope via the B17 lease-converter path (Cost B45/C55/C60 and Valuation output B5/B25/B39); it is not blocked by deferral.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary and the debt/lease (B15/B17) boundary; a prior-10-K C fact is never a FY2025 B copy, and the lease-inclusion convention must not be changed inconsistently between periods. C15 is consumed by Input-sheet I28; the lease-converter path it belongs to is in scope (not blocked by deferral).",
    },
    "Input sheet!C19": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C19 under C10 'Last 10K before LTM': issuer cross holdings and other non-operating assets for the fiscal year prior to the LTM window (no workbook comment present; column header defines the period). Never the FY2025 B19 amount.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year non-operating assets and cross holdings",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single prior-fiscal-year non-operating/cross-holding amount; identify the valuation basis (market/fair value vs carrying value) and issuer definition. Cross holdings valuation is a separate market/financial item, not a regional allocation.",
        "tie_break": "Exact issuer/concept/period/unit/scale/valuation-basis; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; missing prior-period evidence is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, non-operating/cross-holding definition, valuation basis, unit/scale and sign for the C-column period; never use the FY2025 B19 fact for C19.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary and the non-operating/regional-route boundary; a prior-10-K C fact is never a FY2025 B copy, and cross holdings are not regional revenue to be allocated by the H21-H31 route.",
    },
    "Input sheet!C20": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C20 under C10 'Last 10K before LTM': issuer minority interests for the fiscal year prior to the LTM window (no workbook comment present; column header defines the period). Never the FY2025 B20 amount.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year minority/noncontrolling interests",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single prior-fiscal-year minority-interest amount. Determine whether reported equity totals already include minority interests before adding a separate amount; an absence of a located item is not proof of zero; never double count.",
        "tie_break": "Exact issuer/concept/period/unit/scale/attribution; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; absence of a search hit is not evidence of zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, minority-interest definition and attribution, unit/scale and sign for the C-column period; never use the FY2025 B20 fact for C20.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary and the equity-attribution (NCI) boundary carried from B14/B20; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!C21": {
        "status": "unresolved",
        "evaluation": deepcopy(_DEFAULT_EVALUATION),
        "policy_family": "financial",
        "applicability_status": "active",
        "source_concept": "Input sheet C21 under C10 'Last 10K before LTM': issuer number of shares outstanding for the fiscal year prior to the LTM window (no workbook comment present; column header defines the period). Never the FY2025 B21 amount. Unit is shares (a count), not USD.",
        "source_hierarchy": [
            "issuer_audited_statement_or_note for prior fiscal year share counts and capital structure",
            "issuer_primary_filing for exact statement and filing context",
            "issuer_filed_tagged_fact for reconciled concept/context/unit/scale",
        ],
        "composition": "Single prior-fiscal-year share count; confirm share class scope (common shares and any dilutive instruments), count basis (weighted-average vs period-end) and scale. Keep option/equity-route treatment separate.",
        "tie_break": "Exact issuer/concept/period/unit/share-class; same-tier conflicting facts => unresolved.",
        "missing_component": "abstain; missing prior-period evidence is not zero",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the exact prior fiscal year, share-class scope, count basis, unit and scale for the C-column period; never use the FY2025 B21 fact for C21.",
        "decision_type": "filing_fact",
        "cross_boundary": "Crosses the LTM / prior-10-K period boundary and the share/option-route boundary; a prior-10-K C fact is never a FY2025 B copy.",
    },
    "Input sheet!D11": {
        "status": "unresolved",
        "evaluation": {
            "operation": "select",
            "source_tiers": [
                {"tier": 1, "source_kind": "issuer_filing_calendar_evidence",
                 "precedence": 1, "eligibility": "LTM window and most-recent-fiscal-year end dates map exactly to one described case"},
                {"tier": 2, "source_kind": "issuer_guidance_or_documented_driver",
                 "precedence": 2, "eligibility": "dated issuer-specific fiscal-calendar bridge with reviewed method"},
            ],
            "components": [],
            "tie_breaker": "Select the exact fraction (1.00 / 0.75 / 0.50 / 0.25) only when the LTM window and fiscal-year-end dates map to the workbook comment's described cases; otherwise abstain. No fraction is assumed.",
            "missing_component": "abstain; do not assume 1.00 or any other year fraction",
            "fallback": None,
            "bounds": "unresolved",
            "route_gate": None,
        },
        "policy_family": "selector",
        "applicability_status": "conditional",
        "source_concept": "Input sheet D11 under header D10 'Years since last 10K': the fraction of the most recent fiscal year represented in the LTM window (1.00 if LTM equals the most recent fiscal year and the last 10-K is the previous fiscal year; 0.75 / 0.50 / 0.25 per the workbook comment). It is a user-supplied time-alignment input, not a reported financial fact and not a conversion formula.",
        "source_hierarchy": [
            "issuer_filing_calendar_evidence for fiscal-year-end and LTM end dates",
            "pinned workbook comment for the described fraction cases only",
        ],
        "composition": "Single year fraction from the comment's described set, reflecting the issuer's fiscal calendar and LTM window; document the LTM end date and most-recent fiscal year end. The workbook uses D11 in the Input-sheet I25 growth conversion IF(C11>0,(B11/C11)^(1/D11)-1,\"NA\"); any such conversion of prior-10-K (C-column) amounts to annual/LTM-comparable terms remains unapproved without separate evidence and rationale.",
        "tie_break": "Exact calendar mapping; if the LTM window does not match one of the described cases, abstain.",
        "missing_component": "abstain; no assumed year fraction",
        "fallback": None,
        "route_gate": None,
        "unresolved_rationale": "Resolve the LTM window and most-recent-fiscal-year end dates, then select the exact fraction from the comment's described values. D11 permits no unapproved conversion: it must not be used to convert prior-10-K amounts without separate authorization and documented rationale.",
        "decision_type": "selector",
        "cross_boundary": "Crosses the LTM / prior-10-K time-alignment boundary; no unapproved conversion of C-column amounts to annual/LTM-comparable terms via D11.",
    },
}


# ---------------------------------------------------------------------------
# Authored v2 content: Southwest FY2025 status/locator rows
# ---------------------------------------------------------------------------

_SOUTHWEST_ESTABLISHED = {
    "Input sheet!B11": {
        "status": "source_candidate_pending_rights_cutoff_independent_review",
        "reason": "source observation only; rights, cutoff, independent accounting review, and write authority are not established",
        "locators": ["registered primary HTML candidate path [0,1,1457,0,7,5,0,0]",
                     "preexisting PDF citation p. 88 / printed p. 85 (income statement)"],
        "candidate_value_millions_usd": "28063",
    },
    "Input sheet!B12": {
        "status": "source_candidate_pending_rights_cutoff_independent_review",
        "reason": "source observation only; rights, cutoff, independent accounting review, and write authority are not established",
        "locators": ["registered primary HTML candidate path [0,1,1457,0,19,5,0,0]",
                     "preexisting PDF citation p. 88 / printed p. 85 (income statement)"],
        "candidate_value_millions_usd": "428",
    },
    "Input sheet!B14": {
        "status": "source_candidate_pending_rights_cutoff_independent_review",
        "reason": "source observation only; rights, cutoff, independent accounting review (including the workbook comment's noncontrolling-interest inclusion question), and write authority are not established",
        "locators": ["registered primary HTML candidate path [0,1,1446,0,45,1,0,0]",
                     "preexisting PDF citation p. 87 / printed p. 84 (balance sheet)"],
        "candidate_value_millions_usd": "7981",
    },
    "Input sheet!B15": {
        "status": "abstain_debt_presentation_unresolved",
        "reason": "debt definitions and statement/table presentation remain unresolved (component sum 4,901 vs note total 4,919 vs tagged statement 4,929; finance-lease 78 inclusion unestablished)",
        "locators": ["current maturities 324 path [0,1,1446,0,30,1,0,0]",
                     "long-term debt less current maturities 4,577 path [0,1,1446,0,33,1,0,0]",
                     "note debt total 4,919 path [0,1,1619,0,0,0,22,4,0,0]",
                     "tagged statement debt total 4,929 path [0,1,1629,8,1,0,8,2,0,0]",
                     "finance lease 78 combined path [0,1,1619,0,0,0,21,3,0,0]",
                     "operating lease 1,080 combined path [0,1,1648,0,0,0,0,1,0,10,2,0,0]"],
        "candidate_value_millions_usd": None,
    },
    "Input sheet!B17": {
        "status": "abstain_lease_overlap_unresolved",
        "reason": "debt/lease overlap and approved reconciliation are unresolved; the lease-inclusive 5,981/No convention is provisional only and requires reviewer reconciliation before use",
        "locators": ["finance lease 78 combined path [0,1,1619,0,0,0,21,3,0,0]",
                     "operating lease 1,080 combined path [0,1,1648,0,0,0,0,1,0,10,2,0,0]"],
        "candidate_value_millions_usd": None,
    },
}

_SOUTHWEST_NEW_CELL = {
    "Input sheet!B9": {
        "status": "abstain_no_established_locator",
        "reason": "no Southwest global-industry selector mapping is established; the active US/Global route is not determined and the direct detailed-contract allowlist excludes B9; no value invented",
    },
    "Input sheet!C11": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C11 is the prior-10-K ('Last 10K before LTM') revenue column; the bounded v1 trial established only FY2025 B-column locators, so no prior-fiscal-year C-column locator exists; a prior-10-K C fact is never a FY2025 B11 copy and no value is invented",
    },
    "Input sheet!C12": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C12 is the prior-10-K operating-income column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B12 copy and no value is invented",
    },
    "Input sheet!C13": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C13 is the prior-10-K interest-expense column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B13 copy and no value is invented",
    },
    "Input sheet!C14": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C14 is the prior-10-K book-equity column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B14 copy and no value is invented",
    },
    "Input sheet!C15": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C15 is the prior-10-K book-debt column; no prior-fiscal-year locator is established in the bounded trial and the LTM debt/lease reconciliation (4,901/4,919/4,929) is itself unresolved; a prior-10-K C fact is never a FY2025 B15 copy and no value is invented",
    },
    "Input sheet!C19": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C19 is the prior-10-K cross-holdings column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B19 copy and no value is invented",
    },
    "Input sheet!C20": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C20 is the prior-10-K minority-interests column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B20 copy and no value is invented",
    },
    "Input sheet!C21": {
        "status": "abstain_no_established_locator_prior_period_column",
        "reason": "C21 is the prior-10-K shares-outstanding column; no prior-fiscal-year locator is established in the bounded trial; a prior-10-K C fact is never a FY2025 B21 copy and no value is invented",
    },
    "Input sheet!D11": {
        "status": "abstain_no_established_locator",
        "reason": "D11 (years since last 10K) fraction is unresolved for Southwest; the LTM window and fiscal-year-end mapping are not established, and no unapproved conversion of prior-10-K amounts is performed; no value invented",
    },
}

_SOUTHWEST_OUT_OF_SCOPE_REASON = (
    "no approved Southwest source mapping or independent evidence in the bounded v1 trial "
    "(trial_southwest.py inspected 67 references and established locators only for B11/B12/B14)"
)

_SOUTHWEST_ALLOWED_STATUS = {
    "source_candidate_pending_rights_cutoff_independent_review",
    "abstain_debt_presentation_unresolved",
    "abstain_lease_overlap_unresolved",
    "abstain_out_of_bounded_trial_scope",
    "abstain_no_established_locator",
    "abstain_no_established_locator_prior_period_column",
}

# Machine-readable cross-boundary facts for the v2 scope (independently checked
# against the pinned workbook):
# * Lease-converter path is IN SCOPE: 'Cost of capital worksheet'!B45 is the
#   formula IF('Input sheet'!B17="Yes",'Operating lease converter'!F33,0),
#   C55=B45, C60=C53+C54+C55, and 'Valuation output'!B5/B25/B39 depend on Input
#   B17. B17/B15/C15 therefore have a visible in-scope lease-converter path and
#   are NOT blocked by deferral; their cell-level review is reviewable and their
#   accounting uncertainty remains an abstention, not a deferral block.
# * Terminal/stable-growth policy is DEFERRED: Input sheet!B45..B72 (stable-growth
#   cost-of-capital override B45/B46 through trapped cash B70-B72) is outside the
#   58-reference scope. There is NO demonstrated direct dependency from B17/B15/C15
#   to any deferred cell; the full valuation route cannot be reviewed without the
#   deferred terminal policy. That is a COVERAGE GAP of the deferred terminal
#   policy, not a direct lease dependency.
CROSS_BOUNDARY_ROUTES = [
    {
        "id": "LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45",
        "scope_trigger_refs": ["Input sheet!B15", "Input sheet!B17", "Input sheet!C15"],
        "in_scope_feed_path": [
            {"reference": "Cost of capital worksheet!B45",
             "formula": "IF('Input sheet'!B17=\"Yes\",'Operating lease converter'!F33,0)"},
            {"reference": "Cost of capital worksheet!C55", "formula": "B45"},
            {"reference": "Cost of capital worksheet!C60", "formula": "C53+C54+C55"},
            {"reference": "Valuation output!B5",
             "formula": "IF('Input sheet'!B17=\"Yes\",IF('Input sheet'!B16=\"Yes\",'Input sheet'!B12+'Operating lease converter'!F32+'R& D converter'!D39,'Input sheet'!B12+'Operating lease converter'!F32),IF('Input sheet'!B16=\"Yes\",'Input sheet'!B12+'R& D converter'!D39,'Input sheet'!B12))"},
            {"reference": "Valuation output!B25",
             "formula": "IF('Input sheet'!B17=\"Yes\",'Input sheet'!B15+'Operating lease converter'!C28,'Input sheet'!B15)"},
            {"reference": "Valuation output!B39",
             "formula": "IF('Input sheet'!B17=\"Yes\",IF('Input sheet'!B16=\"Yes\",'Input sheet'!B14+'Input sheet'!B15-'Input sheet'!B18+'Operating lease converter'!F33+'R& D converter'!D35,'Input sheet'!B14+'Input sheet'!B15-'Input sheet'!B18+'Operating lease converter'!F33),IF('Input sheet'!B16=\"Yes\",'Input sheet'!B14+'Input sheet'!B15-'Input sheet'!B18+'R& D converter'!D35,'Input sheet'!B14+'Input sheet'!B15-'Input sheet'!B18))"},
        ],
        "conclusion_status": "reviewable_in_scope",
        "blocked_by_deferral": False,
        "reason": (
            "The operating-lease converter selector (Input sheet!B17) feeds the in-scope Cost of capital "
            "worksheet formula cells (Cost B45 = IF(Input B17=\"Yes\",'Operating lease converter'!F33,0), "
            "C55 = B45, C60 = C53+C54+C55) and the Valuation output cells B5/B25/B39. The lease-converter "
            "dependency is visible entirely within the pinned workbook and is not gated by any deferred cell; "
            "B15/B17/C15 cell-level review is therefore NOT blocked by deferral. Their accounting uncertainty "
            "remains an abstention/unresolved state, not a deferral block."
        ),
    },
    {
        "id": "TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS",
        "deferred_refs": list(DEFERRED_REFS),
        "conclusion_status": "coverage_gap",
        "blocked_by_deferral": False,
        "reason": (
            "The full valuation route requires the deferred stable-growth/terminal override policy "
            "(Input sheet!B45..B72, beginning with the stable-growth cost-of-capital override B45/B46 consumed by "
            "'Valuation output'!M12 = IF('Input sheet'!B45=\"Yes\",'Input sheet'!B46, ...)). Those deferred cells are "
            "outside the 58-reference scope, so the full valuation output cannot be reviewed without the deferred "
            "terminal policy. This is a COVERAGE GAP of the deferred terminal policy; no direct dependency from "
            "B17/B15/C15 to any deferred cell is demonstrated."
        ),
    },
]


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------


def _json_sha(obj: Any) -> str:
    """Repository compact digest convention: sorted keys, compact separators, no escaping."""
    return _sha(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _v1_entry_id_by_ref() -> dict[str, str]:
    if not V1_POLICY_PATH.exists():
        raise ReviewScopeError(f"v1 policy file not found: {V1_POLICY_PATH}")
    data = json.loads(V1_POLICY_PATH.read_text(encoding="utf-8"))
    return {entry["reference"]: entry["id"] for entry in data["entries"]}


def scope_summary() -> dict[str, Any]:
    return {
        "ordered_unique_scope": len(SCOPE_REFS),
        "retained_from_v1": len(RETAINED_V1_REFS),
        "retained_input_b_lt_45": len(RETAINED_B_LT45_REFS),
        "retained_cost": len(RETAINED_COST_REFS),
        "new_cells": len(NEW_CELL_REFS),
        "deferred": len(DEFERRED_REFS),
        "excluded": [EXCLUDED_H32],
        "allowlist_total": len(CONTRACT["input_allowlist"]),
    }


def build_review_scope() -> dict[str, Any]:
    v1_id_by_ref = _v1_entry_id_by_ref()
    mapping = []
    for pos, ref in enumerate(SCOPE_REFS, 1):
        is_new = ref in NEW_CELL_REFS_SET
        mapping.append({
            "position": pos,
            "reference": ref,
            "scope_group": SCOPE_GROUP_BY_REF[ref],
            "entry_status": "new_cell" if is_new else "retained_from_v1",
            "v1_entry_id": None if is_new else v1_id_by_ref.get(ref),
        })
    return {
        "schema_version": "ginzu-review-scope/2",
        "document_status": "PROPOSED — NOT APPROVED / NOT OPERATIONAL",
        "source_binding": {
            "contract": "src/airline_valuation/detailed_contract.json",
            "workbook": CONTRACT["source_file"],
            "pinned_sha256": PINNED_WORKBOOK_SHA256,
            "scope_rule": "Only references explicitly present in the detailed-contract input_allowlist are direct-contract input targets; the ten new cells are outside the allowlist and are reviewed only under their documented new-cell status.",
        },
        "scope_summary": scope_summary(),
        "scope_groups": {
            "retained_input_b_lt_45": list(RETAINED_B_LT45_REFS),
            "retained_cost": list(RETAINED_COST_REFS),
            "new_cells": list(NEW_CELL_REFS),
            "deferred_input_b_ge_45": list(DEFERRED_REFS),
            "excluded": [EXCLUDED_H32],
        },
        "mapping": mapping,
        "scope_rules": {
            "direct_contract_scope_only": "A reference is a direct-contract input target only when it appears in the detailed-contract input_allowlist; workbook location does not imply eligibility.",
            "deferred_rule": "The 19 Input-sheet B-column references with row >= 45 are deferred and intentionally excluded from the v2 scope; they remain in the allowlist and v1 policy.",
            "h32_exclusion": "Cost of capital worksheet!H32 is excluded: it is not in the input_allowlist and must not be treated as a guidance input.",
            "new_cell_rule": "The ten new cells (B9, C11:C15, C19:C21, D11) are outside the v1 67-reference allowlist; they are inspected independently and carry ten substantive distinct proposed entries, with B9 exclusion-first, prior-10-K C facts never FY2025 B copies, D11 no unapproved conversion, and region/lease cross-boundary constraints.",
        },
        "cross_boundary_routes": CROSS_BOUNDARY_ROUTES,
        "limitations": [
            "Review-only scope index; it is not an approval, source authorization, or write instruction.",
            "Retained v1 proposals are preserved verbatim in the v1 policy file and linked here; no v1 file is modified.",
            "The ten new-cell proposals are review-only; no workbook value, comment suggestion, or conversion is approved.",
            "The lease-converter path (Input B17 -> Cost B45 -> C55 -> C60 and Valuation output B5/B25/B39) is in scope and reviewable; the deferred Input B45+ terminal/stable-growth policy is a coverage gap, not a demonstrated B17 dependency.",
        ],
    }


def build_new_cell_inventory() -> dict[str, Any]:
    result = inspect_new_cells()
    result = deepcopy(result)
    result["schema_version"] = "ginzu-new-cell-inventory/2"
    result["document_status"] = "PROPOSED — NOT APPROVED / NOT OPERATIONAL"
    result["serialization_notes"] = (
        "cell_xml_sha256 is SHA256(ET.tostring(cell, encoding='utf-8')) over the exact worksheet cell "
        "element; worksheet_xml_sha256 is SHA256 of the worksheet part bytes; comment part hashes are "
        "SHA256 of the threaded/legacy comment part bytes. Same canonical serialization and relationship "
        "handling as trial_guidance.inspect_trial_guidance, implemented independently without mutating it."
    )
    return result


def _compact_evidence(inspector: dict[str, Any], index: int) -> dict[str, Any]:
    label = _NEW_CELL_LABELS[inspector["reference"]]
    comment_count = len(inspector["comments"])
    validations = inspector["validations"]
    validation_summary = "; ".join(
        f"{v['type']}({v['sqref']})={v['formula1']}" for v in validations
    ) or "no validation"
    return {
        "inspector_entry_index": index,
        "cell_hash": inspector["cell_xml_sha256"],
        "label_comment_validation_formula": (
            f"{label}; {comment_count} comment(s); {validation_summary}"
        ),
        "comments_transcribed": True,
    }


def build_input_policy() -> dict[str, Any]:
    if not V1_POLICY_PATH.exists():
        raise ReviewScopeError(f"v1 policy file not found: {V1_POLICY_PATH}")
    v1 = json.loads(V1_POLICY_PATH.read_text(encoding="utf-8"))
    v1_by_ref = {entry["reference"]: entry for entry in v1["entries"]}
    inventory = build_new_cell_inventory()
    inventory_by_ref = {entry["reference"]: entry for entry in inventory["entries"]}
    entries: list[dict[str, Any]] = []
    for pos, ref in enumerate(SCOPE_REFS, 1):
        entry_id = f"GINZU2-{pos:03d}"
        if ref in NEW_CELL_REFS_SET:
            proposal = deepcopy(NEW_CELL_PROPOSALS[ref])
            inspector = deepcopy(inventory_by_ref[ref])
            proposal["inspector"] = inspector
            proposal["evidence_link"] = {
                "inventory_file": "docs/policy-drafts/ginzu-new-cell-inventory-v2.json",
                "entry_index": NEW_CELL_REFS.index(ref),
                "workbook_sha256": PINNED_WORKBOOK_SHA256,
                "cell_xml_sha256": inspector["cell_xml_sha256"],
            }
            entries.append({
                "id": entry_id,
                "reference": ref,
                "scope_position": pos,
                "entry_status": "new_cell",
                "v1_entry_id": None,
                "applicability_status": proposal["applicability_status"],
                "unresolved_rationale": proposal["unresolved_rationale"],
                "verified_workbook_evidence": _compact_evidence(inspector, NEW_CELL_REFS.index(ref)),
                "classification": deepcopy(NEW_CELL_CLASSIFICATION[ref]),
                "dependencies": [],
                "proposal": proposal,
            })
        else:
            v1_entry = v1_by_ref[ref]
            proposal = deepcopy(v1_entry["proposal"])
            entries.append({
                "id": entry_id,
                "reference": ref,
                "scope_position": pos,
                "entry_status": "retained_from_v1",
                "v1_entry_id": v1_entry["id"],
                "applicability_status": v1_entry["applicability_status"],
                "unresolved_rationale": v1_entry["unresolved_rationale"],
                "verified_workbook_evidence": deepcopy(v1_entry["verified_workbook_evidence"]),
                "classification": deepcopy(v1_entry["classification"]),
                "dependencies": deepcopy(v1_entry["dependencies"]),
                "retention": {
                    "retained_verbatim": True,
                    "v1_proposal_sha256": _json_sha(v1_entry["proposal"]),
                    "note": "proposal copied byte-for-byte from the v1 entry and hash-pinned; do not edit this v2 copy",
                },
                "proposal": proposal,
            })
    return {
        "schema_version": "ginzu-input-policy-review/2",
        "document_status": "PROPOSED — NOT APPROVED / NOT OPERATIONAL",
        "source_binding": {
            "contract": "src/airline_valuation/detailed_contract.json",
            "workbook": CONTRACT["source_file"],
            "pinned_sha256": PINNED_WORKBOOK_SHA256,
            "v1_policy": "docs/policy-drafts/ginzu-input-policy-v1.json",
            "scope_ref": "docs/policy-drafts/ginzu-review-scope-v2.json",
        },
        "retention_policy": (
            "48 retained entries preserve the v1 per-entry proposals verbatim (v1 file untouched) and "
            "carry a v1_entry_id link plus a v1_proposal_sha256 pin; the ten new cells carry ten "
            "substantive distinct proposed entries as the approved plan: B9 exclusion-first, prior-10-K "
            "C facts never FY2025 B copies, D11 no unapproved conversion, region/lease cross-boundary."
        ),
        "entries": entries,
        "limitations": [
            "Review-only; no value, selector, conversion, source rights, cutoff, or approval is established.",
            "Retained proposals are authoritative in the v1 file; this file links and hash-pins them.",
        ],
    }


def build_southwest_review() -> dict[str, Any]:
    rows = []
    for pos, ref in enumerate(SCOPE_REFS, 1):
        if ref in _SOUTHWEST_ESTABLISHED:
            spec = _SOUTHWEST_ESTABLISHED[ref]
            # Accounting abstentions (B15 debt, B17 lease) are UNRESOLVED, not
            # blocked-by-deferral: the lease-converter path is in scope.
            conclusion_status = ("unresolved" if ref in ("Input sheet!B15", "Input sheet!B17")
                                 else "source_candidate_pending_review")
            row = {
                "position": pos,
                "reference": ref,
                "status": spec["status"],
                "reason": spec["reason"],
                "conclusion_status": conclusion_status,
                "established_locators": list(spec["locators"]),
                "candidate_value_millions_usd": spec["candidate_value_millions_usd"],
                "decision": None,
                "write_eligible": False,
            }
        elif ref in NEW_CELL_REFS_SET:
            spec = _SOUTHWEST_NEW_CELL[ref]
            row = {
                "position": pos,
                "reference": ref,
                "status": spec["status"],
                "reason": spec["reason"],
                "conclusion_status": "unresolved",
                "established_locators": [],
                "candidate_value_millions_usd": None,
                "decision": None,
                "write_eligible": False,
            }
        else:
            row = {
                "position": pos,
                "reference": ref,
                "status": "abstain_out_of_bounded_trial_scope",
                "reason": _SOUTHWEST_OUT_OF_SCOPE_REASON,
                "conclusion_status": "unresolved",
                "established_locators": [],
                "candidate_value_millions_usd": None,
                "decision": None,
                "write_eligible": False,
            }
        rows.append(row)
    coverage_gaps = [route for route in CROSS_BOUNDARY_ROUTES if route["conclusion_status"] == "coverage_gap"]
    in_scope_lease = [route for route in CROSS_BOUNDARY_ROUTES if route["id"] == "LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45"]
    return {
        "schema_version": "ginzu-southwest-review/2",
        "document_status": "REVIEW ONLY — NOT APPROVED / NOT OPERATIONAL",
        "issuer": "southwest",
        "issuer_cik": "0000092380",
        "fiscal_period": "FY2025",
        "source_registry_ref": "src/airline_valuation/filing_registry.py FILINGS[southwest].artifacts.primary",
        "scope_ref": "docs/policy-drafts/ginzu-review-scope-v2.json",
        "row_count": len(rows),
        "rows": rows,
        "in_scope_lease_path": in_scope_lease,
        "coverage_gaps": coverage_gaps,
        "conclusion_gate": (
            "B15/B17 cell-level review is UNRESOLVED on accounting grounds (debt presentation, lease overlap); it "
            "is NOT blocked by deferral because the lease-converter path (Input B17 -> Cost B45 -> C55 -> C60 and "
            "Valuation output B5/B25/B39) is in scope and reviewable. The full valuation route additionally requires "
            "the deferred Input B45+ terminal/stable-growth policy; that is a COVERAGE GAP (see coverage_gaps), not a "
            "demonstrated direct dependency from B15/B17/C15 to any deferred cell."
        ),
        "limitations": [
            "Source candidates and locators are review prompts, not adopted inputs.",
            "Publication rights, cutoff, independent accounting review, and write authority are not established.",
            "No private filing bytes are reproduced; only the established locators and candidate magnitudes already recorded in docs/southwest-input-review.md and trial_southwest.py are cited.",
            "No value is invented for any row without an established locator.",
            "Cell-level review is never marked blocked by deferral; only the full valuation route (deferred terminal policy) is a coverage gap.",
        ],
    }


# ---------------------------------------------------------------------------
# Loaders / validators for the checked-in JSON artifacts
# ---------------------------------------------------------------------------

_V2_FILENAMES = {
    "scope": "ginzu-review-scope-v2.json",
    "inventory": "ginzu-new-cell-inventory-v2.json",
    "input_policy": "ginzu-input-policy-v2.json",
    "southwest": "ginzu-southwest-review-v2.json",
}


def _load_artifact(name: str) -> dict[str, Any]:
    path = DOCS_DIR / _V2_FILENAMES[name]
    if not path.exists():
        raise ReviewScopeError(f"v2 artifact not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_review_scope() -> dict[str, Any]:
    data = _load_artifact("scope")
    if data.get("schema_version") != "ginzu-review-scope/2":
        raise ReviewScopeError("unsupported review-scope schema")
    mapping_refs = [item["reference"] for item in data["mapping"]]
    if mapping_refs != SCOPE_REFS:
        raise ReviewScopeError("checked-in review-scope mapping does not match module scope")
    return data


def load_new_cell_inventory() -> dict[str, Any]:
    data = _load_artifact("inventory")
    if data.get("schema_version") != "ginzu-new-cell-inventory/2":
        raise ReviewScopeError("unsupported new-cell-inventory schema")
    refs = [entry["reference"] for entry in data["entries"]]
    if refs != NEW_CELL_REFS:
        raise ReviewScopeError("checked-in new-cell inventory does not match the ten new cells")
    return data


def load_input_policy() -> dict[str, Any]:
    data = _load_artifact("input_policy")
    if data.get("schema_version") != "ginzu-input-policy-review/2":
        raise ReviewScopeError("unsupported input-policy schema")
    refs = [entry["reference"] for entry in data["entries"]]
    if refs != SCOPE_REFS:
        raise ReviewScopeError("checked-in input-policy entries do not match module scope")
    return data


def load_southwest_review() -> dict[str, Any]:
    data = _load_artifact("southwest")
    if data.get("schema_version") != "ginzu-southwest-review/2":
        raise ReviewScopeError("unsupported southwest-review schema")
    refs = [row["reference"] for row in data["rows"]]
    if refs != SCOPE_REFS:
        raise ReviewScopeError("checked-in southwest rows do not match module scope")
    return data


# ---------------------------------------------------------------------------
# Independently specified validators (advisory AC.1/AC.2 remediation)
# ---------------------------------------------------------------------------

_NEW_CELL_EXPECTED_PERIOD = {
    "Input sheet!B9": "selector",
    "Input sheet!C11": "prior-10-K",
    "Input sheet!C12": "prior-10-K",
    "Input sheet!C13": "prior-10-K",
    "Input sheet!C14": "prior-10-K",
    "Input sheet!C15": "prior-10-K",
    "Input sheet!C19": "prior-10-K",
    "Input sheet!C20": "prior-10-K",
    "Input sheet!C21": "prior-10-K",
    "Input sheet!D11": "year-fraction",
}


def validate_approved_baselines(baselines: dict[str, str]) -> dict[str, str]:
    """Reject any drift of the approved source files from fixed recorded SHA-256.

    ``baselines`` maps a repo-relative path to its fixed SHA-256 (the observed
    digest of the approved existing file, recorded independently; the approved
    plan does not supply these hashes). Each live file must hash to the
    baseline or the review is invalid. Returns the observed digests.
    """
    root = Path(__file__).resolve().parents[2]
    observed: dict[str, str] = {}
    for rel, expected in baselines.items():
        candidate = Path(rel)
        path = candidate if candidate.is_absolute() else root / candidate
        actual = _sha(path.read_bytes())
        observed[rel] = actual
        if actual != expected:
            raise ReviewScopeError(
                f"approved baseline drifted for {rel}: expected {expected}, observed {actual}"
            )
    return observed


def validate_fixed_allowlist(fixed_refs: list[str]) -> None:
    """Reject if the operational contract allowlist no longer matches the fixed enumeration."""
    if CONTRACT["input_allowlist"] != list(fixed_refs):
        raise ReviewScopeError("detailed-contract input_allowlist does not match the fixed expected references")


def validate_new_cell_entries(entries: list[dict[str, Any]]) -> None:
    """Independently specified rules for the ten new-cell policy entries.

    Expectations are authored literally here, not derived from the builder:
    * exactly the ten new cells, one entry each;
    * C-column cells (C11..C15, C19..C21) must claim a PRIOR-period (never the
      LTM/FY2025 period) classification and must not present FY2025 as the
      C-column value (swapped prior period => reject);
    * missing_component must abstain, never assume zero from absence
      (zero-from-absence => reject);
    * the proposal inspector and evidence_link must agree with the entry
      reference and cell digest (mismatched IDs => reject).
    """
    by_ref = {entry.get("reference"): entry for entry in entries}
    if set(by_ref) != NEW_CELL_REFS_SET:
        raise ReviewScopeError("new-cell entries do not cover exactly the ten new cells")
    if len(entries) != len(NEW_CELL_REFS):
        raise ReviewScopeError("new-cell entries are not unique")
    for ref in NEW_CELL_REFS:
        entry = by_ref[ref]
        classification = entry.get("classification") or {}
        proposal = entry.get("proposal") or {}
        if ref.startswith("Input sheet!C"):
            period = str(classification.get("period") or "").casefold()
            if "prior" not in period:
                raise ReviewScopeError(f"{ref} classification period does not state a prior period: {period!r}")
            concept = str(proposal.get("source_concept") or "").casefold()
            if "fy2025" in concept and "never" not in concept:
                raise ReviewScopeError(f"{ref} source_concept presents FY2025 as the C-column value")
            prior_markers = ("prior-10-k", "prior 10-k", "last 10k before ltm",
                             "fiscal year prior", "previous fiscal year", "prior fiscal-year")
            if not any(marker in concept for marker in prior_markers):
                raise ReviewScopeError(f"{ref} source_concept does not identify the prior-10-K period")
        missing = str(proposal.get("missing_component") or "").casefold()
        if missing == "zero" or missing in {"0", "0.0", "assume zero"} or "assume zero" in missing:
            raise ReviewScopeError(f"{ref} missing_component assumes zero from absence")
        inspector = proposal.get("inspector") or {}
        link = proposal.get("evidence_link") or {}
        if inspector and link:
            if inspector.get("cell_xml_sha256") != link.get("cell_xml_sha256"):
                raise ReviewScopeError(f"{ref} inspector/evidence_link cell_xml mismatch")
        if link.get("inventory_file") != "docs/policy-drafts/ginzu-new-cell-inventory-v2.json":
            raise ReviewScopeError(f"{ref} evidence_link does not point to the v2 inventory")
        if inspector and inspector.get("reference") != ref:
            raise ReviewScopeError(f"{ref} inspector reference mismatch")


def validate_input_policy_entries(policy_entries: list[dict[str, Any]]) -> None:
    """Validate the 58 v2 input-policy entries (48 retained + 10 new)."""
    refs = [entry.get("reference") for entry in policy_entries]
    if refs != SCOPE_REFS:
        raise ReviewScopeError("input-policy entries do not match the 58-reference scope order")
    new_entries = [entry for entry in policy_entries if entry.get("entry_status") == "new_cell"]
    validate_new_cell_entries(new_entries)
    retained = [entry for entry in policy_entries if entry.get("entry_status") == "retained_from_v1"]
    if len(retained) != len(RETAINED_V1_REFS):
        raise ReviewScopeError(f"expected {len(RETAINED_V1_REFS)} retained entries, got {len(retained)}")
    v1 = json.loads(V1_POLICY_PATH.read_text(encoding="utf-8"))
    v1_by_ref = {entry["reference"]: entry for entry in v1["entries"]}
    for entry in retained:
        ref = entry["reference"]
        v1_entry = v1_by_ref.get(ref)
        if v1_entry is None:
            raise ReviewScopeError(f"{ref} has no v1 entry to retain")
        if entry.get("v1_entry_id") != v1_entry["id"]:
            raise ReviewScopeError(f"{ref} v1_entry_id mismatch")
        if entry.get("proposal") != v1_entry["proposal"]:
            raise ReviewScopeError(f"{ref} v1 proposal not preserved verbatim")
        if entry.get("retention", {}).get("v1_proposal_sha256") != _json_sha(v1_entry["proposal"]):
            raise ReviewScopeError(f"{ref} retention hash does not pin the v1 proposal")


def validate_cross_boundary_routes(routes: list[dict[str, Any]]) -> None:
    """Validate the two distinct machine-readable cross-boundary facts.

    * LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45 must be reviewable in scope
      (conclusion_status=reviewable_in_scope, blocked_by_deferral=False) with the
      in-scope feed path (Cost B45/C55/C60 and Valuation output B5/B25/B39).
    * TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS must be a coverage gap
      (conclusion_status=coverage_gap, blocked_by_deferral=False) naming the
      exact 19 deferred refs and stating no proven direct dependency.
    Any route claiming blocked-by-deferral or an all-19-crosses edge is rejected.
    """
    by_id = {route.get("id"): route for route in routes}
    lease = by_id.get("LEASE_CONVERTER_FEEDS_IN_SCOPE_COST_B45")
    if lease is None:
        raise ReviewScopeError("lease-converter in-scope route is missing")
    if lease.get("conclusion_status") != "reviewable_in_scope":
        raise ReviewScopeError("lease-converter route must be reviewable_in_scope, not blocked")
    if lease.get("blocked_by_deferral") is not False:
        raise ReviewScopeError("lease-converter route must not be blocked by deferral")
    feed = {item.get("reference") for item in lease.get("in_scope_feed_path", [])}
    for ref in ("Cost of capital worksheet!B45", "Cost of capital worksheet!C55",
                "Cost of capital worksheet!C60", "Valuation output!B5",
                "Valuation output!B25", "Valuation output!B39"):
        if ref not in feed:
            raise ReviewScopeError(f"lease-converter in-scope feed path missing {ref}")
    coverage = by_id.get("TERMINAL_REVIEW_COVERAGE_DEFERRED_INPUT_B45_PLUS")
    if coverage is None:
        raise ReviewScopeError("terminal coverage-gap route is missing")
    if coverage.get("conclusion_status") != "coverage_gap":
        raise ReviewScopeError("terminal route must be a coverage_gap, not a dependency")
    if coverage.get("deferred_refs") != DEFERRED_REFS:
        raise ReviewScopeError("terminal coverage gap must name the exact 19 deferred refs")
    for route in routes:
        if route.get("blocked_by_deferral") is True or "blocked" in str(route.get("conclusion_status")):
            raise ReviewScopeError(f"route {route.get('id')} must not claim blocked-by-deferral")


def validate_southwest_conclusions(rows: list[dict[str, Any]]) -> None:
    """Validate per-row conclusion statuses on the Southwest rows.

    Cell-level review is never blocked by deferral: the lease-converter path is
    in scope. B15/B17 keep their accounting abstention (unresolved); only the
    full valuation route (deferred terminal policy) is a coverage gap. Any row
    marked 'blocked' is rejected as the false first-deferred linkage.
    """
    by_ref = {row["reference"]: row for row in rows}
    for ref in ("Input sheet!B15", "Input sheet!B17"):
        if ref not in by_ref:
            raise ReviewScopeError(f"{ref} missing from Southwest rows; B15 must remain unresolved")
        if by_ref[ref]["conclusion_status"] != "unresolved":
            raise ReviewScopeError(f"{ref} must remain unresolved (accounting abstention), not blocked by deferral")
    for row in rows:
        status = row.get("conclusion_status")
        if status not in {"unresolved", "source_candidate_pending_review", "coverage_gap"}:
            raise ReviewScopeError(f"{row['reference']} has unknown conclusion_status {status!r}")
        if status == "blocked":
            raise ReviewScopeError(f"{row['reference']} must not be marked blocked by deferral")


# ---------------------------------------------------------------------------
# Artifact regeneration (explicit only; importing never writes)
# ---------------------------------------------------------------------------

_V2_ARTIFACT_BUILDERS = {
    "scope": build_review_scope,
    "inventory": build_new_cell_inventory,
    "input_policy": build_input_policy,
    "southwest": build_southwest_review,
}


def write_v2_artifacts() -> dict[str, str]:
    """Regenerate the four checked-in v2 JSON artifacts. Explicit call only."""
    written: dict[str, str] = {}
    for name, builder in _V2_ARTIFACT_BUILDERS.items():
        path = DOCS_DIR / _V2_FILENAMES[name]
        payload = json.dumps(builder(), indent=1, ensure_ascii=False) + "\n"
        path.write_text(payload, encoding="utf-8")
        written[name] = str(path)
    return written


if __name__ == "__main__":
    import sys
    if "--write" in sys.argv[1:]:
        paths = write_v2_artifacts()
        for name, path in paths.items():
            print(f"wrote {name}: {path}")
    else:
        summary = scope_summary()
        print(json.dumps(summary, indent=1))
        print("inventory refs:", json.dumps(NEW_CELL_REFS))
