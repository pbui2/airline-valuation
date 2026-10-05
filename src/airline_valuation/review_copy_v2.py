"""Review-only v2 Ginzu XLSX copy: distinct v2 review notes as threaded comments.

This module builds a REVIEW-ONLY v2 copy of the pinned fcffsimpleginzu.xlsx
workbook, layered on the v2 review scope / input policy packet
(``docs/policy-drafts/ginzu-review-scope-v2.json``,
``docs/policy-drafts/ginzu-input-policy-v2.json`` and
``docs/ginzu-input-policy-v2.md``). It never writes valuation inputs, never runs
the valuation, never recalcs, and does not touch the production writers.

Exactly seven package parts are permitted to change:

* ``xl/threadedComments/threadedComment1.xml`` — Input sheet review notes
* ``xl/threadedComments/threadedComment5.xml`` — Cost of capital worksheet notes
* ``xl/persons/person.xml`` — adds one distinctly identified v2 review author
* ``xl/comments1.xml`` — legacy comment placeholders for the new Input roots
* ``xl/comments5.xml`` — legacy comment placeholders for the new Cost roots
* ``xl/drawings/vmlDrawing1.vml`` — VML note shapes for the new Input roots
* ``xl/drawings/vmlDrawing5.vml`` — VML note shapes for the new Cost roots

Every other ZIP member (including the other legacy comment parts, the other VML
drawings, worksheet XML, shared strings, styles, calc chain and all
relationships) is preserved byte-for-byte. Archive members are copied with
their original metadata and the output is written atomically.
Unsafe/duplicate member names, malformed XML or IDs/relationships, ambiguous
threaded roots, malformed preexisting legacy comment/VML elements, and source
or v2 packet drift are all rejected.

The v2 notes are distinct from the v1 review copy: a separate v2 review person
id and a separate v2 UUID namespace for comment ids, plus v2-specific note text
(v2 banner, ``ginzu-input-policy-review/2`` schema, scope position, entry
status, the v1 entry link, and the v2 JSON/MD locators with their pinned
SHA-256). Notes for cells that already have a threaded root comment become
replies (parentId = existing root id); notes for cells without one become new
roots. A cell with more than one existing root is rejected as ambiguous.

Display repair: each new *root* also gets a legacy comment placeholder and a
VML note shape so the review comment is positioned for display in desktop Excel
([MS-XLSX] 2.3.7.3 Legacy Comment Placeholders: one placeholder per top-level
threaded comment; the placeholder author MUST contain ``tc={uid}`` with the
threaded comment id, and the placeholder ``uid``/``ref``/``xr:uid`` correspond
to the threaded comment). Replies ride the existing thread display and get no
placeholder/shape. Each new VML note shape CLONES the actual per-sheet template
note shape from the pinned source (the B7 shape ``_x0000_s1217`` on the Input
sheet, the B23 shape ``_x0000_s9241`` on the Cost sheet), changing only the
shape id (strictly greater than the maximum shape id in that drawing's idmap
block), the zero-based ClientData ``x:Row``/``x:Column`` and the translated
``x:Anchor``; every other attribute and child is preserved byte-for-byte. The
anchor translation shifts the template's eight anchor numbers by the target
cell's row/col delta, so the box starts one row above the cell and one column
to the right, exactly as the template does. Placeholders use the pinned
source's repeated ``shapeId="0"`` convention (not a unique VML id) and are bound
to their note shape through the cell's unique zero-based ClientData
Row/Column. Generation and verification recompute these identities from the
pinned source and policy, so semantic validation is independent of byte
reconstruction, and the manifest records root/placeholder/shape identities and
hashes for every note.
"""
from __future__ import annotations

import argparse
import copy
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import uuid
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

from .workbook import WorkbookContractError, _checked_zip
from . import review_scope_v2

# ElementTree keeps a process-global namespace-prefix registry
# (ET._namespace_map) that register_namespace() mutates permanently. Some
# libraries (e.g. openpyxl, imported by other test modules at collection time)
# register the Office Open XML prefixes, which changes the byte output of
# ET.tostring() and therefore the canonical digests review_scope_v2 computes.
# The v2 packet's embedded inspectors were hashed against ElementTree's pristine
# default registry, so restore exactly that default before every live
# inspection, keeping the packet-drift check deterministic regardless of earlier
# imports or tests. These seven entries are the CPython default _namespace_map.
_ET_PRISTINE_NAMESPACE_MAP = {
    "http://www.w3.org/XML/1998/namespace": "xml",
    "http://www.w3.org/1999/xhtml": "html",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf",
    "http://schemas.xmlsoap.org/wsdl/": "wsdl",
    "http://www.w3.org/2001/XMLSchema": "xs",
    "http://www.w3.org/2001/XMLSchema-instance": "xsi",
    "http://purl.org/dc/elements/1.1/": "dc",
}


@contextmanager
def _restore_et_namespace_registry() -> Iterator[None]:
    """Run a block with ElementTree's process-global namespace registry reset to
    its pristine default, then restore the caller's exact prior registrations.

    review_scope_v2 hashes cell/validation XML emitted by ET.tostring, whose
    byte output depends on the globally registered namespace prefixes. Other
    code (e.g. openpyxl, and tests calling register_namespace) mutates that
    process-global registry, so the live inspection must run under the pristine
    default the v2 packet inspectors were pinned against. The prior registry is
    restored in a finally block, so the caller's registrations survive even when
    the inspection raises.
    """
    previous = dict(ET._namespace_map)
    try:
        ET._namespace_map.clear()
        ET._namespace_map.update(_ET_PRISTINE_NAMESPACE_MAP)
        yield
    finally:
        ET._namespace_map.clear()
        ET._namespace_map.update(previous)


SOURCE_FILE = "fcffsimpleginzu.xlsx"
SOURCE_SHA256 = "c6fd6bd3205e8ef4ecd2b9a01cf81077eaf7db2651d3d02ac03939cb64adabc9"
SCOPE_JSON = Path("docs/policy-drafts/ginzu-review-scope-v2.json")
INPUT_POLICY_JSON = Path("docs/policy-drafts/ginzu-input-policy-v2.json")
INPUT_POLICY_MD = Path("docs/ginzu-input-policy-v2.md")

# Pinned SHA-256 of the three v2 packet documents. These are the final hashes
# recorded after the v2 data authoring finished; any drift fails generation
# closed before a single byte is written.
SCOPE_JSON_SHA256 = "48d1a4e7a9a130825d740a530ed952137bbb888401d28261bc60c383f9a8d755"
INPUT_POLICY_JSON_SHA256 = "1fca4f035d08e9b10bc428d0d3c78c652a79c5b01a3cf31d75919899d73be53c"
INPUT_POLICY_MD_SHA256 = "441288748ff07ac82ccef6af4f42dbea48dd2fc3b0e975489564ca72765a4c0a"

_THREADED_NS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_COMMENTS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XR_NS = "http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
_THREADED_MARKER = b"</ThreadedComments>"
_PERSON_MARKER = b"</personList>"
_COMMENT_AUTHORS_MARKER = b"</authors>"
_COMMENT_LIST_MARKER = b"</commentList>"
_VML_END_MARKER = b"</xml>"
_VML_IDMAP_RE = re.compile(r'<o:idmap v:ext="edit" data="(\d+)"/>')

# One distinctly identified v2 review author, separate from the three original
# authors in person.xml AND from the v1 review author. The id is a deterministic
# v5 UUID in a distinct v2 namespace so output stays byte-stable across runs and
# never collides with the v1 review person.
_REVIEW_PERSON_V2_ID = "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "airline-valuation/review-only-v2/person")).upper() + "}"
REVIEW_PERSON = {
    "displayName": "Airline Valuation Review v2 (review-only)",
    "id": _REVIEW_PERSON_V2_ID,
    "userId": "airline-valuation-review-only-v2",
    "providerId": "None",
}
PERMITTED_PARTS = {
    "xl/threadedComments/threadedComment1.xml",
    "xl/threadedComments/threadedComment5.xml",
    "xl/persons/person.xml",
    "xl/comments1.xml",
    "xl/comments5.xml",
    "xl/drawings/vmlDrawing1.vml",
    "xl/drawings/vmlDrawing5.vml",
}
MANIFEST_SCHEMA = "ginzu-policy-review-copy/3"
STATUS = "TEST ONLY — REVIEW ONLY — NOT APPROVED / NOT OPERATIONAL"
BANNER = "GINZU POLICY REVIEW NOTE V2 — REVIEW ONLY — NOT APPROVED / NOT OPERATIONAL"

# Per-sheet display wiring: threaded comment part -> (legacy comments part, VML
# drawing part). The pinned source wires sheet1 to threadedComment1/comments1/
# vmlDrawing1 and sheet10 to threadedComment5/comments5/vmlDrawing5.
_SHEET_PART_LINKS = {
    "xl/threadedComments/threadedComment1.xml": ("xl/comments1.xml", "xl/drawings/vmlDrawing1.vml"),
    "xl/threadedComments/threadedComment5.xml": ("xl/comments5.xml", "xl/drawings/vmlDrawing5.vml"),
}

# Per-sheet VML note template derived from the pinned source's own note shapes:
# the B7 note shape (_x0000_s1217) on the Input sheet and the B23 note shape
# (_x0000_s9241) on the Cost of capital worksheet. Each new note shape CLONES
# the actual source template shape byte-for-byte, changing only the shape id,
# the zero-based ClientData x:Row/x:Column and the x:Anchor (per the approved
# plan); style, z-index, and every child element are preserved verbatim. The
# anchor translation shifts the template's eight anchor numbers by the target
# cell's row/col delta from the template cell (B7/B23), keeping dx/dy fixed, so
# the box starts one row above the cell (template row1 = cell zero-based row - 1)
# and one column to the right (col1 = col0 + 1), exactly as the template does.
_VML_TEMPLATE = {
    "xl/drawings/vmlDrawing1.vml": {
        "template_shape_id": 1217,  # B7
        "template_row": 6, "template_col": 1,
        "cell_row": 6, "cell_col": 1,
        "template_anchor": "2, 15, 5, 6, 7, 119, 25, 10",
        "style": "margin-left:499.5pt;margin-top:81.75pt;width:419.625pt;height:311.625pt",
        "a_col1": 2, "a_dx1": 15, "a_row1": 5, "a_dy1": 6,
        "a_col2": 7, "a_dx2": 119, "a_row2": 25, "a_dy2": 10,
    },
    "xl/drawings/vmlDrawing5.vml": {
        "template_shape_id": 9241,  # B23
        "template_row": 22, "template_col": 1,
        "cell_row": 22, "cell_col": 1,
        "template_anchor": "2, 15, 21, 5, 7, 42, 41, 14",
        "style": "margin-left:291.375pt;margin-top:314.625pt;width:357pt;height:306.75pt",
        "a_col1": 2, "a_dx1": 15, "a_row1": 21, "a_dy1": 5,
        "a_col2": 7, "a_dx2": 42, "a_row2": 41, "a_dy2": 14,
    },
}

# Legacy comment placeholder body template, verbatim from the pinned source's
# own placeholder entries ([MS-XLSX] 2.3.7.3). The placeholder carries the full
# thread text; for a new v2 root the thread is the v2 note itself.
_LEGACY_TEXT_PREFIX = (
    "[Threaded comment]\n\n"
    "Your version of Excel allows you to read this threaded comment; however, any edits to it will get removed "
    "if the file is opened in a newer version of Excel. Learn more: https://go.microsoft.com/fwlink/?linkid=870924\n\n"
    "Comment:\n    "
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fmt(value: object) -> str:
    return "none" if value is None else str(value)


def _first_clause(text: str) -> str:
    """Leading complete clause of a policy statement, delimiter kept.

    Used only to ground the review question in the policy's own words. It
    always returns a complete grammatical unit (or the whole text when there
    is no ';' or '.' boundary), so no field is ever truncated mid-sentence;
    the full rationale remains verbatim on the Unresolved rationale line.
    """
    if not text:
        return text
    match = re.search(r"[.;]", text)
    if match is None:
        return text
    return text[: match.end()].strip()


def _load_packet(root: Path) -> tuple[dict, dict, dict]:
    """Load the v2 packet under its three pinned hashes; fail closed on drift."""
    scope_path = root / SCOPE_JSON
    policy_path = root / INPUT_POLICY_JSON
    md_path = root / INPUT_POLICY_MD
    scope_raw = scope_path.read_bytes()
    policy_raw = policy_path.read_bytes()
    md_raw = md_path.read_bytes()
    scope_sha = _sha(scope_raw)
    policy_sha = _sha(policy_raw)
    md_sha = _sha(md_raw)
    if scope_sha != SCOPE_JSON_SHA256:
        raise WorkbookContractError(
            f"v2 scope JSON drifted: {SCOPE_JSON} sha256 {scope_sha} != pinned {SCOPE_JSON_SHA256}")
    if policy_sha != INPUT_POLICY_JSON_SHA256:
        raise WorkbookContractError(
            f"v2 input policy JSON drifted: {INPUT_POLICY_JSON} sha256 {policy_sha} != pinned {INPUT_POLICY_JSON_SHA256}")
    if md_sha != INPUT_POLICY_MD_SHA256:
        raise WorkbookContractError(
            f"v2 input policy MD drifted: {INPUT_POLICY_MD} sha256 {md_sha} != pinned {INPUT_POLICY_MD_SHA256}")
    try:
        scope = json.loads(scope_raw.decode("utf-8"))
        policy = json.loads(policy_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbookContractError("v2 packet JSON is malformed") from exc
    hashes = {
        "scope_rel": str(SCOPE_JSON),
        "scope_sha": scope_sha,
        "policy_rel": str(INPUT_POLICY_JSON),
        "policy_sha": policy_sha,
        "md_rel": str(INPUT_POLICY_MD),
        "md_sha": md_sha,
    }
    return scope, policy, hashes


def _verify_policy_against_source(template: str | Path, scope: dict, policy: dict) -> None:
    """Reject v2 packet drift: canonical loaders, mapping/entry consistency, and
    live new-cell inspectors against the pinned workbook."""
    try:
        review_scope_v2.load_review_scope()
        review_scope_v2.load_input_policy()
    except review_scope_v2.ReviewScopeError as exc:
        raise WorkbookContractError(f"v2 packet drift: {exc}") from exc
    mapping = scope.get("mapping")
    if not isinstance(mapping, list) or len(mapping) != 58:
        raise WorkbookContractError("v2 scope mapping must contain exactly 58 ordered entries")
    entries = policy.get("entries")
    if not isinstance(entries, list) or len(entries) != 58:
        raise WorkbookContractError("v2 input policy must contain exactly 58 ordered entries")
    mapping_refs = [item.get("reference") for item in mapping]
    entry_refs = [entry.get("reference") for entry in entries]
    if mapping_refs != entry_refs:
        raise WorkbookContractError("v2 scope mapping and input policy entry order/references differ")
    seen: set[str] = set()
    for item in mapping:
        ref = item.get("reference")
        pos = item.get("position")
        if not ref or ref in seen or pos != len(seen) + 1:
            raise WorkbookContractError(f"v2 scope mapping is malformed at position {pos!r}")
        seen.add(ref)
    for index, entry in enumerate(entries, 1):
        if entry.get("id") != f"GINZU2-{index:03d}":
            raise WorkbookContractError(f"v2 input policy entry id sequence is broken at position {index}")
        if entry.get("scope_position") != index:
            raise WorkbookContractError(f"v2 input policy scope_position is broken at position {index}")
        ref = entry.get("reference")
        if ref in review_scope_v2.NEW_CELL_REFS_SET:
            if entry.get("entry_status") != "new_cell":
                raise WorkbookContractError(f"v2 new-cell entry status is wrong for {ref}")
        else:
            if entry.get("entry_status") != "retained_from_v1" or not entry.get("v1_entry_id"):
                raise WorkbookContractError(f"v2 retained entry status/link is wrong for {ref}")
    with _restore_et_namespace_registry():
        live = review_scope_v2.inspect_new_cells(Path(template), verify_pin=True)
    live_by_ref = {entry["reference"]: entry for entry in live["entries"]}
    for entry in entries:
        ref = entry.get("reference")
        if ref not in review_scope_v2.NEW_CELL_REFS_SET:
            continue
        inspector = (entry.get("proposal") or {}).get("inspector") or {}
        live_entry = live_by_ref.get(ref)
        if live_entry is None or inspector.get("cell_xml_sha256") != live_entry.get("cell_xml_sha256"):
            raise WorkbookContractError(f"v2 new-cell inspector drifted for {ref}")


def _comment_id(reference: str) -> str:
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"airline-valuation/review-note-v2/{reference}")).upper() + "}"


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_note(entry: dict, policy: dict, packet_hashes: dict) -> str:
    """Deterministic v2 review note for one policy entry, full policy fidelity.

    Every required subject is preserved verbatim as a labeled line: document
    status, v2 entry id, scope position, entry status with the v1 entry link,
    policy schema/version, proposal status, policy family, applicability,
    source concept, source hierarchy, composition, tie-break, missing rule,
    gate, decision type, cross-boundary constraint (new cells), fallback status
    with trigger/alternatives/sensitivity, and unresolved rationale. An explicit
    issue-specific review question is derived from the unresolved rationale
    without inventing an answer or falsifying provenance, and the full-policy
    locator (scope JSON / policy JSON / MD paths with their pinned SHA-256)
    keeps the source retrievable. The note is distinct from the v1 note: v2
    banner, v2 schema, scope position, entry status/v1 link, and v2 locators.
    """
    proposal = entry.get("proposal") or {}
    reference = _fmt(entry.get("reference"))
    unresolved = _fmt(proposal.get("unresolved_rationale"))
    route_gate = proposal.get("route_gate")
    if route_gate is None:
        gate_text = "none"
    elif isinstance(route_gate, dict):
        gate_text = " · ".join(f"{key}={_fmt(value)}" for key, value in route_gate.items())
    else:
        gate_text = str(route_gate)
    fallback = proposal.get("fallback") or {}
    alternatives = fallback.get("alternatives") or []
    entry_status = _fmt(entry.get("entry_status"))
    if entry.get("v1_entry_id"):
        entry_status += f" · v1 entry: {_fmt(entry.get('v1_entry_id'))}"
    lines = [
        BANNER,
        f"Document status: {_fmt(policy.get('document_status'))}",
        f"Entry: {_fmt(entry.get('id'))} · Reference: {reference} · Scope position: {_fmt(entry.get('scope_position'))} · Policy schema: {_fmt(policy.get('schema_version'))}",
        f"Entry status: {entry_status}",
        f"Proposal status: {_fmt(proposal.get('status'))} · Policy family: {_fmt(proposal.get('policy_family'))} · Applicability: {_fmt(proposal.get('applicability_status'))}",
        f"Source concept: {_fmt(proposal.get('source_concept'))}",
        "Source hierarchy:",
    ]
    lines.extend(f"- {_fmt(item)}" for item in (proposal.get("source_hierarchy") or []))
    lines.extend([
        f"Composition: {_fmt(proposal.get('composition'))}",
        f"Tie-break: {_fmt(proposal.get('tie_break'))}",
        f"Missing rule: {_fmt(proposal.get('missing_component'))}",
        f"Gate: {gate_text}",
        f"Decision type: {_fmt(proposal.get('decision_type'))}",
    ])
    cross_boundary = proposal.get("cross_boundary")
    if cross_boundary:
        lines.append(f"Cross-boundary: {_fmt(cross_boundary)}")
    fallback_parts = [_fmt(fallback.get("status"))]
    if fallback.get("trigger"):
        fallback_parts.append(f"trigger {_fmt(fallback.get('trigger'))}")
    if alternatives:
        fallback_parts.append("alternatives " + "; ".join(_fmt(item) for item in alternatives))
    if fallback.get("sensitivity"):
        fallback_parts.append(f"sensitivity {_fmt(fallback.get('sensitivity'))}")
    lines.append("Fallback status: " + " · ".join(fallback_parts))
    lines.append(f"Unresolved rationale: {unresolved}")
    lines.append(f"Review question (v2): What must the reviewer resolve before the proposed v2 policy can apply at {reference}? — {_first_clause(unresolved)}")
    lines.append(f"Scope JSON: {packet_hashes['scope_rel']} (sha256={packet_hashes['scope_sha']})")
    lines.append(f"Policy JSON: {packet_hashes['policy_rel']} (sha256={packet_hashes['policy_sha']})")
    lines.append(f"Policy MD: {packet_hashes['md_rel']} (sha256={packet_hashes['md_sha']})")
    lines.append("NOT APPROVED / NOT OPERATIONAL — review-only artifact; no source authorization, writer, valuation, forecast, or accounting approval.")
    return "\n".join(lines)


def _comment_element_xml(reference: str, comment_id: str, person_id: str, parent_id: str | None, text: str) -> bytes:
    attrs = f'ref="{reference}" personId="{person_id}" id="{comment_id}"'
    if parent_id:
        attrs += f' parentId="{parent_id}"'
    body = _xml_escape(text)
    return f'<threadedComment {attrs}><text xml:space="preserve">{body}</text></threadedComment>'.encode("utf-8")


def _cell_rc(cell: str) -> tuple[int, int]:
    """Zero-based (row, column) for an A1-style cell reference."""
    match = re.match(r"([A-Z]+)(\d+)$", cell)
    if not match:
        raise WorkbookContractError(f"invalid cell reference: {cell!r}")
    col = 0
    for ch in match.group(1):
        col = col * 26 + (ord(ch) - 64)
    return int(match.group(2)) - 1, col - 1


def _translate_anchor(vml_part: str, cell: str) -> str:
    """Translated x:Anchor for a new note shape on ``cell``.

    Shifts the per-sheet template's eight anchor numbers (B7 for the Input
    sheet, B23 for the Cost sheet) by the target cell's row/col delta from the
    template cell, keeping the four dx/dy offsets fixed, so the box starts one
    row above the cell (template row1 = cell zero-based row - 1) and one column
    to the right (col1 = col0 + 1) exactly as the template does. Fails closed
    if the shift would drive any anchor coordinate negative.
    """
    row0, col0 = _cell_rc(cell)
    tmpl = _VML_TEMPLATE[vml_part]
    dcol = col0 - tmpl["cell_col"]
    drow = row0 - tmpl["cell_row"]
    col1 = tmpl["a_col1"] + dcol
    row1 = tmpl["a_row1"] + drow
    col2 = tmpl["a_col2"] + dcol
    row2 = tmpl["a_row2"] + drow
    if min(col1, row1, col2, row2) < 0:
        raise WorkbookContractError(f"VML anchor translation out of bounds for {cell} in {vml_part}")
    return f"{col1}, {tmpl['a_dx1']}, {row1}, {tmpl['a_dy1']}, {col2}, {tmpl['a_dx2']}, {row2}, {tmpl['a_dy2']}"


def _load_vml_templates(archive: zipfile.ZipFile) -> dict[str, tuple[bytes, int]]:
    """Extract the per-sheet template note shape (B7 / B23) from the pinned
    source and validate it against the pinned anchor, ClientData and style;
    returns ``{vml_part: (template bytes, template z-index)}``. Fail closed if
    the template drifted from the pinned values.
    """
    templates: dict[str, tuple[bytes, int]] = {}
    for vml_part, tmpl in _VML_TEMPLATE.items():
        try:
            text = archive.read(vml_part).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkbookContractError(f"malformed VML drawing part: {vml_part}") from exc
        match = re.search(r'<v:shape id="_x0000_s%d"(.*?)</v:shape>' % tmpl["template_shape_id"], text, re.S)
        if match is None:
            raise WorkbookContractError(f"VML template shape _x0000_s{tmpl['template_shape_id']} missing in {vml_part}")
        block = f'<v:shape id="_x0000_s{tmpl["template_shape_id"]}"{match.group(1)}</v:shape>'
        if tmpl["template_anchor"] not in block:
            raise WorkbookContractError(f"VML template anchor drifted in {vml_part}")
        if f'<x:Row>{tmpl["template_row"]}</x:Row>' not in block or f'<x:Column>{tmpl["template_col"]}</x:Column>' not in block:
            raise WorkbookContractError(f"VML template ClientData drifted in {vml_part}")
        if tmpl["style"] not in block:
            raise WorkbookContractError(f"VML template style drifted in {vml_part}")
        z_match = re.search(r"z-index:(\d+)", block)
        if z_match is None:
            raise WorkbookContractError(f"VML template shape lacks a z-index in {vml_part}")
        templates[vml_part] = (block.encode("utf-8"), int(z_match.group(1)))
    return templates


def _clone_vml_shape(template_bytes: bytes, vml_part: str, shape_id: int, cell: str) -> bytes:
    """Clone the per-sheet template note shape (B7 / B23) for a new root cell.

    Changes only the shape id, the zero-based ClientData x:Row/x:Column and the
    translated x:Anchor; every other attribute (style, z-index, fillcolor,
    insetmode) and every child element (fill, shadow, path, textbox, and the
    ClientData's MoveWithCells/SizeWithCells/AutoFill) is preserved byte-for-
    byte from the actual source template shape.
    """
    tmpl = _VML_TEMPLATE[vml_part]
    row0, col0 = _cell_rc(cell)
    out = template_bytes.replace(f"_x0000_s{tmpl['template_shape_id']}".encode(), f"_x0000_s{shape_id}".encode(), 1)
    out = out.replace(f"<x:Row>{tmpl['template_row']}</x:Row>".encode(), f"<x:Row>{row0}</x:Row>".encode(), 1)
    out = out.replace(f"<x:Column>{tmpl['template_col']}</x:Column>".encode(), f"<x:Column>{col0}</x:Column>".encode(), 1)
    out = out.replace(tmpl["template_anchor"].encode(), _translate_anchor(vml_part, cell).encode(), 1)
    return out


def _legacy_text(note_text: str) -> str:
    """Full placeholder body text for one new root thread."""
    return _LEGACY_TEXT_PREFIX + note_text


def _legacy_author_xml(comment_id: str) -> bytes:
    return f"<author>tc={comment_id}</author>".encode("utf-8")


def _legacy_comment_xml(cell: str, author_id: int, comment_id: str, note_text: str) -> bytes:
    """A legacy comment placeholder element ([MS-XLSX] 2.3.7.3): the author
    carries ``tc={uid}``, the uid/ref/xr:uid correspond to the threaded comment,
    and shapeId is the pinned source's placeholder convention ``0`` (repeated
    for every placeholder, never treated as a unique VML id). The placeholder is
    bound to its cell by the unique zero-based ClientData x:Row/x:Column of the
    matching VML note shape."""
    body = _xml_escape(_legacy_text(note_text))
    return (
        f'<comment ref="{cell}" authorId="{author_id}" shapeId="0" xr:uid="{comment_id}">'
        f'<text><t xml:space="preserve">{body}</t></text></comment>'
    ).encode("utf-8")


def _relationship_map(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    root = ET.fromstring(archive.read(part))
    result: dict[str, tuple[str, str]] = {}
    for rel in root.findall(f"{{{_PKG_NS}}}Relationship"):
        rid, typ = rel.attrib.get("Id"), rel.attrib.get("Type")
        target = rel.attrib.get("Target")
        if not rid or rid in result or not typ or not target:
            raise WorkbookContractError(f"invalid relationship inventory in {part}")
        result[rid] = (typ, target)
    return result


def _resolve_relative(source_part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _validate_relationships(archive: zipfile.ZipFile) -> None:
    """Confirm the contracted threaded-comment and persons wiring before copying."""
    sheet1 = _relationship_map(archive, "xl/worksheets/_rels/sheet1.xml.rels")
    sheet10 = _relationship_map(archive, "xl/worksheets/_rels/sheet10.xml.rels")
    workbook = _relationship_map(archive, "xl/_rels/workbook.xml.rels")
    threaded1 = [target for typ, target in sheet1.values() if typ.endswith("/threadedComment")]
    threaded5 = [target for typ, target in sheet10.values() if typ.endswith("/threadedComment")]
    if threaded1 != ["../threadedComments/threadedComment1.xml"]:
        raise WorkbookContractError("sheet1 threaded comment relationship drifted")
    if threaded5 != ["../threadedComments/threadedComment5.xml"]:
        raise WorkbookContractError("sheet10 threaded comment relationship drifted")
    comments1 = [target for typ, target in sheet1.values() if typ.endswith("/comments")]
    comments5 = [target for typ, target in sheet10.values() if typ.endswith("/comments")]
    if comments1 != ["../comments1.xml"]:
        raise WorkbookContractError("sheet1 legacy comment relationship drifted")
    if comments5 != ["../comments5.xml"]:
        raise WorkbookContractError("sheet10 legacy comment relationship drifted")
    vml1 = [target for typ, target in sheet1.values() if typ.endswith("/vmlDrawing")]
    vml5 = [target for typ, target in sheet10.values() if typ.endswith("/vmlDrawing")]
    if vml1 != ["../drawings/vmlDrawing1.vml"]:
        raise WorkbookContractError("sheet1 VML drawing relationship drifted")
    if vml5 != ["../drawings/vmlDrawing5.vml"]:
        raise WorkbookContractError("sheet10 VML drawing relationship drifted")
    persons = [(_resolve_relative("xl/workbook.xml", target))
               for rid, (typ, target) in workbook.items() if typ.endswith("/person")]
    if persons != ["xl/persons/person.xml"] or workbook.get("rId21", ("", ""))[0].endswith("/person") is False:
        raise WorkbookContractError("workbook persons relationship drifted from rId21 -> xl/persons/person.xml")
    for rid in ("rId21",):
        if rid not in workbook or not workbook[rid][0].endswith("/person"):
            raise WorkbookContractError("workbook persons relationship rId21 missing or drifted")


def _existing_root_ids(archive: zipfile.ZipFile, threaded_part: str) -> dict[str, str]:
    root = ET.fromstring(archive.read(threaded_part))
    roots: dict[str, str] = {}
    for comment in root.findall(f"{{{_THREADED_NS}}}threadedComment"):
        if "parentId" not in comment.attrib:
            ref = comment.attrib.get("ref")
            cid = comment.attrib.get("id")
            if ref and cid:
                if ref in roots and roots[ref] != cid:
                    raise WorkbookContractError(
                        f"ambiguous threaded root comments for cell {ref} in {threaded_part}")
                roots.setdefault(ref, cid)
    return roots


def _sheet_threaded_part(reference: str) -> str:
    sheet_name = reference.split("!", 1)[0]
    if sheet_name == "Input sheet":
        return "xl/threadedComments/threadedComment1.xml"
    if sheet_name == "Cost of capital worksheet":
        return "xl/threadedComments/threadedComment5.xml"
    raise WorkbookContractError(f"v2 review note reference is not on a permitted sheet: {reference}")


def _validate_thread_roots(archive: zipfile.ZipFile) -> None:
    """Reject ambiguous threaded roots up front, before any note is derived.

    Each cell that already has a threaded root gets a reply; a cell with more
    than one root cannot be threaded deterministically and fails closed here
    (stricter than the v1 copy, which silently kept the first root).
    """
    _existing_root_ids(archive, "xl/threadedComments/threadedComment1.xml")
    _existing_root_ids(archive, "xl/threadedComments/threadedComment5.xml")


def _validate_expected_ids(root: ET.Element, part: str) -> None:
    comments = root.findall(f"{{{_THREADED_NS}}}threadedComment")
    ids: dict[str, str] = {}
    for comment in comments:
        cid = comment.attrib.get("id")
        ref = comment.attrib.get("ref")
        if not cid or cid in ids:
            raise WorkbookContractError(f"invalid or duplicate threaded comment id in {part}")
        ids[cid] = ref or ""
    for comment in comments:
        parent = comment.attrib.get("parentId")
        if parent and parent not in ids:
            raise WorkbookContractError(f"threaded comment parentId missing in {part}: {parent}")
        if parent and ids[parent] != (comment.attrib.get("ref") or ""):
            raise WorkbookContractError(f"threaded comment reply ref differs from its parent ref in {part}")


def _validate_person_ids(root: ET.Element) -> None:
    people = root.findall(f"{{{_THREADED_NS}}}person")
    ids = [person.attrib.get("id") for person in people]
    if any(not person_id for person_id in ids) or len(ids) != len(set(ids)):
        raise WorkbookContractError("invalid or duplicate person id in xl/persons/person.xml")


def _parse_comments_part(archive: zipfile.ZipFile, part: str) -> tuple[list[str], list[dict]]:
    """Parse a legacy comments part into (authors, comments).

    Comments carry ref/authorId/shapeId plus the xr namespace uid; the parsed
    dict uses the un-prefixed key ``xr_uid``. Malformed XML fails closed here.
    """
    try:
        root = ET.fromstring(archive.read(part))
    except ET.ParseError as exc:
        raise WorkbookContractError(f"malformed legacy comment part: {part}") from exc
    authors = [author.text or "" for author in root.findall(f".//{{{_COMMENTS_NS}}}author")]
    comments: list[dict] = []
    for comment in root.findall(f".//{{{_COMMENTS_NS}}}comment"):
        comments.append({
            "ref": comment.attrib.get("ref"),
            "authorId": comment.attrib.get("authorId"),
            "shapeId": comment.attrib.get("shapeId"),
            "xr_uid": comment.attrib.get(f"{{{_XR_NS}}}uid"),
        })
    return authors, comments


def _validate_comments_part(archive: zipfile.ZipFile, part: str) -> None:
    """Fail closed on malformed preexisting legacy comment elements: duplicate
    authors, duplicate comment refs/uids, out-of-range author ids, or missing
    identity attributes. This protects the normal path against a synthetic or
    drifted source; the pinned source already satisfies every check."""
    authors, comments = _parse_comments_part(archive, part)
    if any(not author for author in authors) or len(authors) != len(set(authors)):
        raise WorkbookContractError(f"invalid or duplicate legacy comment author in {part}")
    seen_refs: set[str] = set()
    seen_uids: set[str] = set()
    for comment in comments:
        ref = comment["ref"]
        uid = comment["xr_uid"]
        author_id = comment["authorId"]
        if not ref or not uid or not author_id:
            raise WorkbookContractError(f"legacy comment missing identity attributes in {part}")
        if ref in seen_refs:
            raise WorkbookContractError(f"duplicate legacy comment ref in {part}: {ref}")
        if uid in seen_uids:
            raise WorkbookContractError(f"duplicate legacy comment uid in {part}: {uid}")
        seen_refs.add(ref)
        seen_uids.add(uid)
        if not re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", uid):
            raise WorkbookContractError(f"legacy comment uid is not a GUID in {part}: {uid!r}")
        if not re.fullmatch(r"\d+", author_id) or int(author_id) >= len(authors):
            raise WorkbookContractError(f"legacy comment authorId out of range in {part}: {author_id}")


def _parse_vml_part(archive: zipfile.ZipFile, part: str) -> dict:
    """Parse a VML drawing part into shape records plus the idmap block.

    VML is not namespace-friendly to ElementTree, so this uses the same targeted
    regexes the rest of the pipeline relies on. A shape record carries its
    numeric id, zero-based Row/Column, Anchor and z-index; malformed shapes fail
    closed.
    """
    raw = archive.read(part)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbookContractError(f"malformed VML drawing part: {part}") from exc
    if text.count("</xml>") != 1:
        raise WorkbookContractError(f"VML drawing part lacks exactly one closing root: {part}")
    idmaps = _VML_IDMAP_RE.findall(text)
    if len(idmaps) != 1:
        raise WorkbookContractError(f"VML drawing part must contain exactly one idmap block: {part}")
    shapes: list[dict] = []
    for match in re.finditer(r'<v:shape\s+id="([^"]+)"([^>]*?)(/>|>(.*?)</v:shape>)', text, re.S):
        sid_text = match.group(1)
        sid_match = re.fullmatch(r"_x0000_s(\d+)", sid_text)
        if sid_match is None:
            raise WorkbookContractError(f"malformed VML shape id in {part}: {sid_text!r}")
        body = match.group(4) or ""
        row_m = re.search(r"<x:Row>(\d+)</x:Row>", body)
        col_m = re.search(r"<x:Column>(\d+)</x:Column>", body)
        anchor_m = re.search(r"<x:Anchor>\s*([\d,\s]+?)\s*</x:Anchor>", body, re.S)
        z_m = re.search(r"z-index:(\d+)", match.group(2))
        if row_m is None or col_m is None or anchor_m is None:
            raise WorkbookContractError(f"VML note shape missing ClientData in {part}: {sid_text}")
        shapes.append({
            "shape_id": int(sid_match.group(1)),
            "row": int(row_m.group(1)),
            "col": int(col_m.group(1)),
            "anchor": re.sub(r"\s+", " ", anchor_m.group(1)).strip(),
            "z": int(z_m.group(1)) if z_m else None,
        })
    ids = [shape["shape_id"] for shape in shapes]
    if len(ids) != len(set(ids)):
        raise WorkbookContractError(f"duplicate VML shape id in {part}")
    return {
        "idmap_data": idmaps[0],
        "shapes": shapes,
        "max_shape_id": max(ids) if ids else 0,
        "max_z": max((shape["z"] for shape in shapes if shape["z"] is not None), default=0),
    }


def _validate_vml_part(archive: zipfile.ZipFile, part: str) -> None:
    """Fail closed on malformed preexisting VML note shapes."""
    _parse_vml_part(archive, part)


def _validate_source_display_parts(archive: zipfile.ZipFile) -> None:
    """Fail closed on malformed preexisting legacy comment / VML elements before
    any note or shape is derived."""
    for part in ("xl/comments1.xml", "xl/comments5.xml"):
        _validate_comments_part(archive, part)
    for part in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"):
        _validate_vml_part(archive, part)


def _sheet_display_state(archive: zipfile.ZipFile) -> dict[str, dict]:
    """Per threaded-comment part: existing author count and VML shape bookkeeping
    used to assign deterministic, unique new placeholder/shape identities."""
    state: dict[str, dict] = {}
    for threaded_part, (comments_part, vml_part) in _SHEET_PART_LINKS.items():
        authors, _ = _parse_comments_part(archive, comments_part)
        vml = _parse_vml_part(archive, vml_part)
        state[threaded_part] = {
            "comments_part": comments_part,
            "vml_part": vml_part,
            "author_count": len(authors),
            "shape_count": len(vml["shapes"]),
            "max_shape_id": vml["max_shape_id"],
            "max_z": vml["max_z"],
            "idmap_data": vml["idmap_data"],
        }
    return state


def _expected_members(archive: zipfile.ZipFile, scope: dict, policy: dict, packet_hashes: dict) -> tuple[dict[str, bytes], list[dict]]:
    expected = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    existing = {
        "xl/threadedComments/threadedComment1.xml": _existing_root_ids(archive, "xl/threadedComments/threadedComment1.xml"),
        "xl/threadedComments/threadedComment5.xml": _existing_root_ids(archive, "xl/threadedComments/threadedComment5.xml"),
    }
    insertions: dict[str, list[bytes]] = {
        "xl/threadedComments/threadedComment1.xml": [],
        "xl/threadedComments/threadedComment5.xml": [],
    }
    # Per-sheet display insertions: (comments author bytes, comment bytes) and
    # (vml shape bytes), kept in policy order for deterministic identities.
    display_state = _sheet_display_state(archive)
    vml_templates = _load_vml_templates(archive)
    comment_authors: dict[str, list[bytes]] = {part: [] for part in insertions}
    comment_placeholders: dict[str, list[bytes]] = {part: [] for part in insertions}
    vml_shapes: dict[str, list[bytes]] = {part: [] for part in insertions}
    notes: list[dict] = []
    seen: list[str] = []
    for entry in policy["entries"]:
        reference = entry["reference"]
        if reference in seen:
            raise WorkbookContractError(f"duplicate v2 entry reference: {reference}")
        seen.append(reference)
        cell = reference.rsplit("!", 1)[1]
        part = _sheet_threaded_part(reference)
        comment_id = _comment_id(reference)
        root_id = existing[part].get(cell)
        note = build_note(entry, policy, packet_hashes)
        insertions[part].append(_comment_element_xml(cell, comment_id, REVIEW_PERSON["id"], root_id, note))
        note_record = {
            "reference": reference,
            "cell": cell,
            "entry_id": entry.get("id"),
            "scope_position": entry.get("scope_position"),
            "entry_status": entry.get("entry_status"),
            "v1_entry_id": entry.get("v1_entry_id"),
            "part": part,
            "comment_id": comment_id,
            "parent_id": root_id,
            "thread": "reply" if root_id else "root",
            "note_text_sha256": _sha(note.encode("utf-8")),
            "shape_id": None,
            "anchor": None,
            "z_index": None,
            "legacy_author_id": None,
            "placeholder_sha256": None,
            "shape_sha256": None,
        }
        if not root_id:
            # New root: assign the next unique VML shape id above this sheet's
            # idmap block and the next legacy author index, then clone the
            # per-sheet template note shape and emit the legacy placeholder.
            state = display_state[part]
            comments_part, vml_part = state["comments_part"], state["vml_part"]
            shape_id = state["max_shape_id"] + 1
            author_id = state["author_count"]
            state["max_shape_id"] = shape_id
            state["author_count"] = author_id + 1
            note_record["shape_id"] = shape_id
            note_record["anchor"] = _translate_anchor(vml_part, cell)
            # The cloned shape preserves the template shape's z-index verbatim
            # (no z change per the plan), so every clone of a sheet shares it.
            note_record["z_index"] = vml_templates[vml_part][1]
            note_record["legacy_author_id"] = author_id
            comment_authors[part].append(_legacy_author_xml(comment_id))
            placeholder = _legacy_comment_xml(cell, author_id, comment_id, note)
            comment_placeholders[part].append(placeholder)
            cloned = _clone_vml_shape(vml_templates[vml_part][0], vml_part, shape_id, cell)
            vml_shapes[part].append(cloned)
            note_record["placeholder_sha256"] = _sha(placeholder)
            note_record["shape_sha256"] = _sha(cloned)
        notes.append(note_record)
    for part in insertions:
        original = expected[part]
        marker = _THREADED_MARKER
        if original.count(marker) != 1:
            raise WorkbookContractError(f"threaded comment part lacks exactly one closing root: {part}")
        expected[part] = original[: original.index(marker)] + b"".join(insertions[part]) + original[original.index(marker):]
    for part in insertions:
        comments_part, vml_part = _SHEET_PART_LINKS[part]
        comments_raw = expected[comments_part]
        if comments_raw.count(_COMMENT_AUTHORS_MARKER) != 1 or comments_raw.count(_COMMENT_LIST_MARKER) != 1:
            raise WorkbookContractError(f"legacy comment part lacks exactly one authors/commentList close: {comments_part}")
        authors_bytes = b"".join(comment_authors[part])
        placeholders_bytes = b"".join(comment_placeholders[part])
        comments_raw = (comments_raw[: comments_raw.index(_COMMENT_AUTHORS_MARKER)]
                        + authors_bytes
                        + comments_raw[comments_raw.index(_COMMENT_AUTHORS_MARKER):])
        comments_raw = (comments_raw[: comments_raw.index(_COMMENT_LIST_MARKER)]
                        + placeholders_bytes
                        + comments_raw[comments_raw.index(_COMMENT_LIST_MARKER):])
        expected[comments_part] = comments_raw
        vml_raw = expected[vml_part]
        if vml_raw.count(_VML_END_MARKER) != 1:
            raise WorkbookContractError(f"VML drawing part lacks exactly one closing root: {vml_part}")
        expected[vml_part] = vml_raw[: vml_raw.index(_VML_END_MARKER)] + b"".join(vml_shapes[part]) + vml_raw[vml_raw.index(_VML_END_MARKER):]
    person = expected["xl/persons/person.xml"]
    if person.count(_PERSON_MARKER) != 1:
        raise WorkbookContractError("person part lacks exactly one closing personList root")
    person_element = (f'<person displayName="{_xml_escape(REVIEW_PERSON["displayName"])}" '
                      f'id="{REVIEW_PERSON["id"]}" userId="{_xml_escape(REVIEW_PERSON["userId"])}" '
                      f'providerId="{REVIEW_PERSON["providerId"]}"/>').encode("utf-8")
    expected["xl/persons/person.xml"] = person[: person.index(_PERSON_MARKER)] + person_element + person[person.index(_PERSON_MARKER):]
    for part in ("xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml", "xl/persons/person.xml"):
        ET.fromstring(expected[part])
    _validate_expected_ids(ET.fromstring(expected["xl/threadedComments/threadedComment1.xml"]), "xl/threadedComments/threadedComment1.xml")
    _validate_expected_ids(ET.fromstring(expected["xl/threadedComments/threadedComment5.xml"]), "xl/threadedComments/threadedComment5.xml")
    _validate_person_ids(ET.fromstring(expected["xl/persons/person.xml"]))
    for part in ("xl/comments1.xml", "xl/comments5.xml"):
        _validate_comments_part_bytes(expected[part], part)
    for part in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"):
        _validate_vml_part_bytes(expected[part], part)
    return expected, notes


def _validate_comments_part_bytes(raw: bytes, part: str) -> None:
    """Fail closed on the built legacy comment part (authors unique, comments
    well-formed, uids/refs unique, author ids in range)."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WorkbookContractError(f"malformed legacy comment part: {part}") from exc
    authors = [author.text or "" for author in root.findall(f".//{{{_COMMENTS_NS}}}author")]
    if any(not author for author in authors) or len(authors) != len(set(authors)):
        raise WorkbookContractError(f"invalid or duplicate legacy comment author in {part}")
    seen_refs: set[str] = set()
    seen_uids: set[str] = set()
    for comment in root.findall(f".//{{{_COMMENTS_NS}}}comment"):
        ref = comment.attrib.get("ref")
        uid = comment.attrib.get(f"{{{_XR_NS}}}uid")
        author_id = comment.attrib.get("authorId")
        shape_id = comment.attrib.get("shapeId")
        if not ref or not uid or not author_id or not shape_id:
            raise WorkbookContractError(f"legacy comment missing identity attributes in {part}")
        if ref in seen_refs or uid in seen_uids:
            raise WorkbookContractError(f"duplicate legacy comment ref/uid in {part}: {ref}")
        seen_refs.add(ref)
        seen_uids.add(uid)
        if not re.fullmatch(r"\d+", author_id) or int(author_id) >= len(authors):
            raise WorkbookContractError(f"legacy comment authorId out of range in {part}: {author_id}")
        if not re.fullmatch(r"\d+", shape_id):
            raise WorkbookContractError(f"legacy comment shapeId is not numeric in {part}: {shape_id}")


def _validate_vml_part_bytes(raw: bytes, part: str) -> None:
    """Fail closed on the built VML drawing part (exactly one idmap and closing
    root, unique well-formed note shapes)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbookContractError(f"malformed VML drawing part: {part}") from exc
    if text.count("</xml>") != 1:
        raise WorkbookContractError(f"VML drawing part lacks exactly one closing root: {part}")
    if len(_VML_IDMAP_RE.findall(text)) != 1:
        raise WorkbookContractError(f"VML drawing part must contain exactly one idmap block: {part}")
    ids: list[int] = []
    for match in re.finditer(r'<v:shape\s+id="_x0000_s(\d+)"([^>]*?)(/>|>(.*?)</v:shape>)', text, re.S):
        sid = int(match.group(1))
        body = match.group(4) or ""
        if re.search(r"<x:Row>(\d+)</x:Row>", body) is None or re.search(r"<x:Column>(\d+)</x:Column>", body) is None:
            raise WorkbookContractError(f"VML note shape missing ClientData in {part}: _x0000_s{sid}")
        ids.append(sid)
    if len(ids) != len(set(ids)):
        raise WorkbookContractError(f"duplicate VML shape id in {part}")


def _render_package(archive: zipfile.ZipFile, expected: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for info in archive.infolist():
            # writestr mutates the shared ZipInfo (header_offset), so copy it to
            # keep the source archive's member table usable for later reads.
            output.writestr(copy.copy(info), expected[info.filename])
    return buffer.getvalue()


def _write_package(archive: zipfile.ZipFile, dest: Path, expected: dict[str, bytes]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as output:
            for info in archive.infolist():
                output.writestr(copy.copy(info), expected[info.filename])
        with _checked_zip(temporary):
            pass
        os.replace(temporary, dest)
    finally:
        if temporary.exists():
            temporary.unlink()


def _same_file(a: Path, b: Path) -> bool:
    """True when two paths denote the same physical file: equal resolved paths
    or the same device/inode (a symlink or hardlink alias)."""
    try:
        if a.resolve() == b.resolve():
            return True
    except OSError:
        pass
    try:
        if a.exists() and b.exists() and a.stat().st_dev == b.stat().st_dev and a.stat().st_ino == b.stat().st_ino:
            return True
    except OSError:
        pass
    return False


def _protected_paths(template: Path, root: Path) -> list[Path]:
    """Canonical protected files that no v2 output/tmp may alias: the pinned
    source, the operational source workbook, the detailed contract, the v1
    review-copy artifacts, the v1 draft JSON/MD, and the v2 packet docs."""
    project_root = Path(__file__).resolve().parents[2]
    return [
        template,
        project_root / SOURCE_FILE,
        Path(__file__).resolve().with_name("detailed_contract.json"),
        project_root / "review-artifacts/ginzu-policy-proposed-review-only.xlsx",
        project_root / "review-artifacts/ginzu-policy-proposed-review-only-manifest.json",
        project_root / "docs/policy-drafts/ginzu-input-policy-v1.json",
        project_root / "docs/ginzu-input-policy-v1.md",
        root / SCOPE_JSON,
        root / INPUT_POLICY_JSON,
        root / INPUT_POLICY_MD,
    ]


def _validate_output_targets(template: Path, destination: Path, manifest_path: Path,
                             root: Path) -> None:
    """Fail closed before any mkdir/write: no output may alias a protected file.

    Protected files are the source template passed in, the canonical project
    operational source workbook, the detailed contract, the v1 review-copy
    artifacts, the v1 draft JSON/MD, the pinned v2 packet docs (both the
    canonical repo copies and any resolved against the active root), every
    output (destination/manifest), and the internal ``.tmp`` siblings the
    atomic writers open with "wb"/write_text — a symlink planted at a ``.tmp``
    sibling could redirect a write onto a protected file even when the main
    output path is safe. Aliases are detected by resolved path and by same
    device/inode, so symlinks and hardlinks cannot redirect a write onto a
    protected file.
    """
    protected = _protected_paths(template, root)
    outputs = [destination, manifest_path]
    temp_siblings = [
        destination.with_name(destination.name + ".tmp"),
        manifest_path.with_name(manifest_path.name + ".tmp"),
    ]
    for out in outputs:
        for prot in protected:
            if _same_file(out, prot):
                raise WorkbookContractError(
                    f"v2 review copy output path {out} aliases the protected source {prot}; refusing to overwrite")
    for i, out in enumerate(outputs):
        for other in outputs[i + 1:]:
            if _same_file(out, other):
                raise WorkbookContractError(
                    f"v2 review copy output path {out} aliases another output {other}; refusing to overwrite")
    for temp in temp_siblings:
        for prot in protected + outputs:
            if _same_file(temp, prot):
                raise WorkbookContractError(
                    f"v2 review copy temporary path {temp} aliases the protected source {prot}; refusing to write through it")


def _build_manifest(destination: Path, packet_hashes: dict, notes: list[dict]) -> dict:
    """The exact generated v2 manifest document; verification requires full equality."""
    return {
        "schema_version": MANIFEST_SCHEMA,
        "status": STATUS,
        "source": {"file": SOURCE_FILE, "sha256": SOURCE_SHA256},
        "scope": {"json": packet_hashes["scope_rel"], "json_sha256": packet_hashes["scope_sha"]},
        "draft": {"json": packet_hashes["policy_rel"], "json_sha256": packet_hashes["policy_sha"],
                  "md": packet_hashes["md_rel"], "md_sha256": packet_hashes["md_sha"]},
        "review_author": REVIEW_PERSON,
        "output": {"file": str(destination), "sha256": _sha(destination.read_bytes())},
        "changed_parts": sorted(PERMITTED_PARTS),
        "reference_count": len(notes),
        "replies": sum(1 for note in notes if note["thread"] == "reply"),
        "new_roots": sum(1 for note in notes if note["thread"] == "root"),
        "new_legacy_placeholders": sum(1 for note in notes if note["placeholder_sha256"] is not None),
        "new_vml_shapes": sum(1 for note in notes if note["shape_sha256"] is not None),
        "notes": notes,
        "manual_excel_review_copy_display": "pending — open in desktop Excel to confirm threaded comment rendering, reply threading, the added review author, and the new VML comment boxes",
    }


def _normalized_manifest_doc(doc: dict) -> dict:
    """Deep copy of a manifest document with only output.file normalized to its
    resolved absolute path; every other value must match exactly."""
    normalized = copy.deepcopy(doc)
    output = normalized.get("output")
    if isinstance(output, dict) and isinstance(output.get("file"), str):
        output["file"] = str(Path(output["file"]).resolve())
    return normalized


def generate_review_copy_v2(template: str | Path, dest: str | Path, *, manifest: str | Path | None = None,
                            root: str | Path | None = None) -> tuple[Path, Path]:
    """Build the v2 review-only workbook copy and its manifest; writes nothing else."""
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    template_path = Path(template)
    destination = Path(dest)
    manifest_path = Path(manifest) if manifest is not None else destination.with_suffix(".xlsx-manifest.json")
    _validate_output_targets(template_path, destination, manifest_path, root_path)
    raw = template_path.read_bytes()
    if _sha(raw) != SOURCE_SHA256:
        raise WorkbookContractError("v2 review copy accepts only the pinned pristine template SHA-256")
    scope, policy, packet_hashes = _load_packet(root_path)
    with _checked_zip(template_path) as archive:
        _validate_relationships(archive)
        _validate_thread_roots(archive)
        _validate_source_display_parts(archive)
    _verify_policy_against_source(template_path, scope, policy)
    with _checked_zip(template_path) as archive:
        expected, notes = _expected_members(archive, scope, policy, packet_hashes)
        _write_package(archive, destination, expected)
    manifest_doc = _build_manifest(destination, packet_hashes, notes)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(manifest_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination, manifest_path


def _verify_parts_and_container(source: zipfile.ZipFile, dest_path: Path, expected: dict[str, bytes]) -> None:
    with _checked_zip(dest_path) as changed:
        if changed.namelist() != source.namelist():
            raise WorkbookContractError("v2 review copy package member inventory changed")
        for name in source.namelist():
            if changed.read(name) != expected[name]:
                raise WorkbookContractError(f"v2 review copy member differs outside the exact permitted parts: {name}")
        changed_names = {name for name in source.namelist() if source.read(name) != expected[name]}
        if changed_names != PERMITTED_PARTS:
            raise WorkbookContractError(f"v2 review copy delta is not exactly the 7 permitted parts: {sorted(changed_names)}")
    if dest_path.read_bytes() != _render_package(source, expected):
        raise WorkbookContractError("v2 review copy ZIP container differs outside the exact 7 permitted parts")


def _verify_notes_in_dest(dest_path: Path, expected_notes: list[dict], contract_order: list[str]) -> None:
    with _checked_zip(dest_path) as changed:
        comments = {
            "xl/threadedComments/threadedComment1.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment1.xml")),
            "xl/threadedComments/threadedComment5.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment5.xml")),
        }
        person_root = ET.fromstring(changed.read("xl/persons/person.xml"))
    people = [person.attrib.get("id") for person in person_root.findall(f"{{{_THREADED_NS}}}person")]
    if REVIEW_PERSON["id"] not in people:
        raise WorkbookContractError("v2 review author person missing from xl/persons/person.xml")
    if len(people) != len(set(people)) or len(people) != 4:
        raise WorkbookContractError("person.xml must contain exactly the 3 original authors plus the v2 review author")
    new_by_part: dict[str, list[dict]] = {"xl/threadedComments/threadedComment1.xml": [], "xl/threadedComments/threadedComment5.xml": []}
    for note in expected_notes:
        part = note["part"]
        found = None
        for comment in comments[part].findall(f"{{{_THREADED_NS}}}threadedComment"):
            if comment.attrib.get("id") == note["comment_id"]:
                found = comment
                break
        if found is None:
            raise WorkbookContractError(f"v2 review note comment missing for {note['reference']}")
        text_node = next((child for child in found if child.tag.rsplit("}", 1)[-1] == "text"), None)
        text = "".join(text_node.itertext()) if text_node is not None else ""
        if text != note["note_text"]:
            raise WorkbookContractError(f"v2 review note text drifted for {note['reference']}")
        if _sha(text.encode("utf-8")) != note["note_text_sha256"]:
            raise WorkbookContractError(f"v2 review note hash drifted for {note['reference']}")
        if found.attrib.get("personId") != REVIEW_PERSON["id"]:
            raise WorkbookContractError(f"v2 review note author differs for {note['reference']}")
        expected_cell = note["reference"].rsplit("!", 1)[1]
        if found.attrib.get("ref") != expected_cell:
            raise WorkbookContractError(f"v2 review note ref differs for {note['reference']}")
        parent = found.attrib.get("parentId")
        if note["thread"] == "reply" and parent != note["parent_id"]:
            raise WorkbookContractError(f"v2 review note reply parent differs for {note['reference']}")
        if note["thread"] == "root" and parent is not None:
            raise WorkbookContractError(f"v2 review note root unexpectedly has a parent for {note['reference']}")
        new_by_part[part].append(note)
    # Ordered refs: within each part the new comments appear in contract order.
    for part, notes in new_by_part.items():
        expected_refs = [ref for ref in contract_order if _sheet_threaded_part(ref) == part]
        if [note["reference"] for note in notes] != expected_refs:
            raise WorkbookContractError(f"v2 review notes are not in pinned contract order in {part}")


def _verify_display_in_dest(dest_path: Path, expected_notes: list[dict], source_state: dict[str, dict]) -> None:
    """Semantic validation of the new legacy placeholders and VML shapes in the
    destination, independent of byte reconstruction: every new root must have a
    placeholder whose author carries ``tc={uid}`` and whose ref/xr:uid/shapeId
    match the threaded comment and the new VML shape, and every new VML shape
    must use the per-sheet template with a translated anchor and a zero-based
    ClientData at the note's cell. The destination idmap block must equal the
    source's, and the new shape ids must be unique and sit strictly above the
    source's maximum shape id.
    """
    with _checked_zip(dest_path) as changed:
        parts = {
            threaded: _parse_comments_part(changed, comments_part)
            for threaded, (comments_part, _vml) in _SHEET_PART_LINKS.items()
        }
        vml = {
            threaded: _parse_vml_part(changed, vml_part)
            for threaded, (_comments, vml_part) in _SHEET_PART_LINKS.items()
        }
    by_uid = {part: {c["xr_uid"]: c for c in comments} for part, (_authors, comments) in parts.items()}
    shapes_by_id = {part: {s["shape_id"]: s for s in vml_data["shapes"]} for part, vml_data in vml.items()}
    roots_per_part: dict[str, list[dict]] = {"xl/threadedComments/threadedComment1.xml": [], "xl/threadedComments/threadedComment5.xml": []}
    for note in expected_notes:
        if note["thread"] != "root":
            continue
        part = note["part"]
        roots_per_part[part].append(note)
        comments_part, vml_part = _SHEET_PART_LINKS[part]
        authors, comments = parts[part]
        placeholder = by_uid[part].get(note["comment_id"])
        if placeholder is None:
            raise WorkbookContractError(f"v2 review placeholder uid drifted for {note['reference']}")
        if placeholder["ref"] != note["cell"]:
            raise WorkbookContractError(f"v2 review placeholder ref differs for {note['reference']}")
        if placeholder["authorId"] != str(note["legacy_author_id"]):
            raise WorkbookContractError(f"v2 review placeholder authorId differs for {note['reference']}")
        # The pinned source's placeholders all repeat shapeId="0"; the new
        # placeholders follow that convention and are bound to their VML note
        # shape through the cell's unique zero-based ClientData x:Row/x:Column.
        if placeholder["shapeId"] != "0":
            raise WorkbookContractError(f"v2 review placeholder shapeId differs for {note['reference']}")
        if note["legacy_author_id"] is None or note["legacy_author_id"] >= len(authors):
            raise WorkbookContractError(f"v2 review placeholder author out of range for {note['reference']}")
        if authors[note["legacy_author_id"]] != f"tc={note['comment_id']}":
            raise WorkbookContractError(f"v2 review placeholder author is not tc={{uid}} for {note['reference']}")
        shape = shapes_by_id[part].get(note["shape_id"])
        if shape is None:
            raise WorkbookContractError(f"v2 review VML shape missing for {note['reference']}")
        row0, col0 = _cell_rc(note["cell"])
        if shape["row"] != row0 or shape["col"] != col0:
            raise WorkbookContractError(f"v2 review VML ClientData cell differs for {note['reference']}")
        if shape["anchor"] != _translate_anchor(vml_part, note["cell"]):
            raise WorkbookContractError(f"v2 review VML anchor differs for {note['reference']}")
    for part, root_notes in roots_per_part.items():
        _comments_part, vml_part = _SHEET_PART_LINKS[part]
        authors, _comments = parts[part]
        vml_data = vml[part]
        source = source_state[part]
        if vml_data["idmap_data"] != source["idmap_data"]:
            raise WorkbookContractError(f"v2 review VML idmap drifted in {vml_part}")
        if len(authors) != source["author_count"] + len(root_notes):
            raise WorkbookContractError(f"v2 review legacy author count drifted in {_comments_part}")
        if len(vml_data["shapes"]) != source["shape_count"] + len(root_notes):
            raise WorkbookContractError(f"v2 review VML shape count drifted in {vml_part}")
        new_ids = sorted(note["shape_id"] for note in root_notes)
        if len(new_ids) != len(set(new_ids)):
            raise WorkbookContractError(f"v2 review VML shape ids are not unique in {vml_part}")
        if new_ids and new_ids[0] <= source["max_shape_id"]:
            raise WorkbookContractError(f"v2 review VML shape id not above the source idmap block in {vml_part}")


def _validate_target_display_contract(dest_path: Path) -> None:
    """Fail-closed structural checks on the v2 copy's legacy comment parts and
    VML drawings: namespaces, unique identities, idmap preservation, and the
    threaded comment / person contract carried over from the threaded parts."""
    with _checked_zip(dest_path) as changed:
        parts = {
            "xl/threadedComments/threadedComment1.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment1.xml")),
            "xl/threadedComments/threadedComment5.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment5.xml")),
        }
        person_root = ET.fromstring(changed.read("xl/persons/person.xml"))
        for part, root in parts.items():
            if root.tag != f"{{{_THREADED_NS}}}ThreadedComments":
                raise WorkbookContractError(f"threaded comment part has the wrong root element: {part}")
            ids: dict[str, str] = {}
            roots_by_ref: dict[str, str] = {}
            for comment in root:
                if comment.tag != f"{{{_THREADED_NS}}}threadedComment":
                    raise WorkbookContractError(f"threaded comment child has the wrong namespace/element: {part}")
                cid = comment.attrib.get("id")
                ref = comment.attrib.get("ref")
                if not cid or not ref or cid in ids:
                    raise WorkbookContractError(f"malformed or duplicate threaded comment id in {part}")
                ids[cid] = ref
                if "parentId" not in comment.attrib:
                    if ref in roots_by_ref:
                        raise WorkbookContractError(f"duplicate threaded root comment for cell {ref} in {part}")
                    roots_by_ref[ref] = cid
            for comment in root:
                parent = comment.attrib.get("parentId")
                if parent and parent not in ids:
                    raise WorkbookContractError(f"threaded comment parentId does not resolve in {part}: {parent}")
                if parent and ids[parent] != comment.attrib.get("ref"):
                    raise WorkbookContractError(f"threaded comment reply ref differs from its parent ref in {part}")
        people = [p.attrib.get("id") for p in person_root.findall(f"{{{_THREADED_NS}}}person")]
        if any(not pid for pid in people) or len(people) != len(set(people)):
            raise WorkbookContractError("duplicate or missing person id in xl/persons/person.xml")
        known_people = set(people)
        for part, root in parts.items():
            for comment in root.findall(f"{{{_THREADED_NS}}}threadedComment"):
                person = comment.attrib.get("personId")
                if not person or person not in known_people:
                    raise WorkbookContractError(f"threaded comment references an unknown person in {part}: {person!r}")
        for rel_part in ("xl/worksheets/_rels/sheet1.xml.rels", "xl/worksheets/_rels/sheet10.xml.rels"):
            rel_root = ET.fromstring(changed.read(rel_part))
            for rel in rel_root.findall(f"{{{_PKG_NS}}}Relationship"):
                if rel.attrib.get("Type", "").endswith(("/threadedComment", "/comments", "/vmlDrawing")) \
                        and rel.attrib.get("TargetMode", "Internal") != "Internal":
                    raise WorkbookContractError(f"comment display relationship TargetMode is not Internal: {rel_part}")
        wb_rel = ET.fromstring(changed.read("xl/_rels/workbook.xml.rels"))
        for rel in wb_rel.findall(f"{{{_PKG_NS}}}Relationship"):
            if rel.attrib.get("Type", "").endswith("/person") and rel.attrib.get("TargetMode", "Internal") != "Internal":
                raise WorkbookContractError("person relationship TargetMode is not Internal")
        content_types = changed.read("[Content_Types].xml")
        if b"application/vnd.ms-excel.threadedcomments+xml" not in content_types:
            raise WorkbookContractError("content types no longer declare the threaded comment parts")
        if b"application/vnd.ms-excel.person+xml" not in content_types:
            raise WorkbookContractError("content types no longer declare the persons part")
        if b"application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml" not in content_types:
            raise WorkbookContractError("content types no longer declare the legacy comment parts")
        if b"application/vnd.openxmlformats-officedocument.vmlDrawing" not in content_types:
            raise WorkbookContractError("content types no longer declare the VML drawing parts")
        # Legacy comment parts and VML drawings: unique identities, idmap intact.
        for comments_part in ("xl/comments1.xml", "xl/comments5.xml"):
            _validate_comments_part_bytes(changed.read(comments_part), comments_part)
        for vml_part in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"):
            _validate_vml_part_bytes(changed.read(vml_part), vml_part)


def verify_review_copy_delta_v2(template: str | Path, dest: str | Path, *, manifest: str | Path | None = None,
                                root: str | Path | None = None) -> dict:
    """Require the exact v2 review-only delta: 7 permitted parts and nothing else."""
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    template_path = Path(template)
    dest_path = Path(dest)
    raw = template_path.read_bytes()
    if _sha(raw) != SOURCE_SHA256:
        raise WorkbookContractError("v2 review copy source SHA-256 does not match the pinned template")
    scope, policy, packet_hashes = _load_packet(root_path)
    _verify_policy_against_source(template_path, scope, policy)
    contract_order = [entry["reference"] for entry in policy["entries"]]
    with _checked_zip(template_path) as source:
        _validate_relationships(source)
        _validate_thread_roots(source)
        _validate_source_display_parts(source)
        source_state = _sheet_display_state(source)
        expected, notes = _expected_members(source, scope, policy, packet_hashes)
        note_texts = {entry["reference"]: build_note(entry, policy, packet_hashes) for entry in policy["entries"]}
        notes_with_text = [{**note, "note_text": note_texts[note["reference"]]} for note in notes]
        with _checked_zip(dest_path) as changed:
            if changed.namelist() != source.namelist():
                raise WorkbookContractError("v2 review copy package member inventory changed")
        _verify_notes_in_dest(dest_path, notes_with_text, contract_order)
        _verify_display_in_dest(dest_path, notes, source_state)
        _validate_target_display_contract(dest_path)
        _verify_parts_and_container(source, dest_path, expected)
        output_sha = _sha(dest_path.read_bytes())
    review = {
        "valid": True,
        "schema_version": MANIFEST_SCHEMA,
        "status": STATUS,
        "source_sha256": SOURCE_SHA256,
        "scope_json_sha256": packet_hashes["scope_sha"],
        "policy_json_sha256": packet_hashes["policy_sha"],
        "policy_md_sha256": packet_hashes["md_sha"],
        "output_sha256": output_sha,
        "reference_count": len(notes),
        "changed_parts": sorted(PERMITTED_PARTS),
        "replies": sum(1 for note in notes if note["thread"] == "reply"),
        "new_roots": sum(1 for note in notes if note["thread"] == "root"),
        "new_legacy_placeholders": sum(1 for note in notes if note["placeholder_sha256"] is not None),
        "new_vml_shapes": sum(1 for note in notes if note["shape_sha256"] is not None),
    }
    if manifest is not None:
        manifest_path = Path(manifest)
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_manifest = _build_manifest(dest_path, packet_hashes, notes)
        if _normalized_manifest_doc(manifest_doc) != _normalized_manifest_doc(expected_manifest):
            raise WorkbookContractError("v2 manifest drifted from the exact generated document")
        review["manifest_verified"] = True
    return review


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m airline_valuation.review_copy_v2",
                                     description="Review-only v2 Ginzu XLSX copy: generate or verify the exact 7-part delta "
                                                 "(threaded comments, review author, legacy placeholders, VML shapes).")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write the v2 review-only workbook copy and manifest")
    gen.add_argument("--template", type=Path, default=Path(__file__).resolve().parents[2] / SOURCE_FILE)
    gen.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "review-artifacts/ginzu-policy-proposed-review-only-v2.xlsx")
    gen.add_argument("--manifest", type=Path, default=None)
    gen.set_defaults(func=_cmd_generate)
    ver = sub.add_parser("verify", help="verify an existing v2 review-only workbook copy")
    ver.add_argument("--template", type=Path, default=Path(__file__).resolve().parents[2] / SOURCE_FILE)
    ver.add_argument("--output", type=Path, required=True)
    ver.add_argument("--manifest", type=Path, default=None)
    ver.set_defaults(func=_cmd_verify)
    return parser


def _cmd_generate(args: argparse.Namespace) -> int:
    dest, manifest_path = generate_review_copy_v2(args.template, args.output, manifest=args.manifest)
    review = verify_review_copy_delta_v2(args.template, dest, manifest=manifest_path)
    print(json.dumps({**review, "workbook": str(dest), "manifest": str(manifest_path)}, indent=2, sort_keys=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    review = verify_review_copy_delta_v2(args.template, args.output, manifest=args.manifest)
    print(json.dumps(review, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(f"REVIEW COPY V2 BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
