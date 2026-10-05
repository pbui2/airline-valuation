"""Review-only Ginzu XLSX copy: attach deterministic review notes as threaded comments.

This module builds a REVIEW-ONLY copy of the pinned fcffsimpleginzu.xlsx workbook.
It never writes valuation inputs, never runs the valuation, never recalcs, and
does not touch the production writers (``provisional_writer``/``ooxml_writer``).

Exactly three package parts are permitted to change:

* ``xl/threadedComments/threadedComment1.xml`` — Input sheet review notes
* ``xl/threadedComments/threadedComment5.xml`` — Cost of capital worksheet notes
* ``xl/persons/person.xml`` — adds one distinctly identified review author

Every other ZIP member (including legacy comment parts, VML drawings, worksheet
XML, shared strings, styles, calc chain and all relationships) is preserved
byte-for-byte. Archive members are copied with their original metadata and the
output is written atomically. Unsafe/duplicate member names, malformed XML or
IDs/relationships, and source or draft drift are all rejected.

A review note is generated deterministically from the corresponding entry in
``docs/policy-drafts/ginzu-input-policy-v1.json`` and never silently truncated.
Notes for cells that already have a threaded root comment become replies
(parentId = existing root id); notes for cells without one become new roots.
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
from .trial_guidance import inspect_trial_guidance

# ElementTree keeps a process-global namespace-prefix registry
# (ET._namespace_map) that register_namespace() mutates permanently. Some
# libraries (e.g. openpyxl, imported by other test modules at collection time)
# register the Office Open XML prefixes, which changes the byte output of
# ET.tostring() and therefore the cell/validation hashes inspect_trial_guidance
# records. The draft's embedded inspectors were hashed against ElementTree's
# pristine default registry, so restore exactly that default before every live
# inspection, keeping the draft-drift check deterministic regardless of earlier
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

    inspect_trial_guidance hashes cell/validation XML emitted by ET.tostring,
    whose byte output depends on the globally registered namespace prefixes.
    Other code (e.g. openpyxl, and tests calling register_namespace) mutates that
    process-global registry, so the live inspection must run under the pristine
    default the draft inspectors were pinned against. The prior registry is
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
DRAFT_JSON = Path("docs/policy-drafts/ginzu-input-policy-v1.json")
DRAFT_MD = Path("docs/ginzu-input-policy-v1.md")
DRAFT_JSON_SHA256 = "bb793ec27a0818a7b83a1aaa89f139627a82b729b4325b1ee8e88e9889c47622"
DRAFT_MD_SHA256 = "51c558c7428f1021e91473e549ac3e494a417a1028a717971c83b68c9c03cdb0"

_THREADED_NS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_THREADED_MARKER = b"</ThreadedComments>"
_PERSON_MARKER = b"</personList>"

# One distinctly identified review author, separate from the three original
# authors in person.xml (Aswath Damodaran x2 and Microsoft Office User). The id
# is a deterministic v5 UUID so output stays byte-stable across runs.
_REVIEW_PERSON_ID = "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "airline-valuation/review-only/person")).upper() + "}"
REVIEW_PERSON = {
    "displayName": "Airline Valuation Review (review-only)",
    "id": _REVIEW_PERSON_ID,
    "userId": "airline-valuation-review-only",
    "providerId": "None",
}
PERMITTED_PARTS = {
    "xl/threadedComments/threadedComment1.xml",
    "xl/threadedComments/threadedComment5.xml",
    "xl/persons/person.xml",
}
MANIFEST_SCHEMA = "ginzu-policy-review-copy/1"
STATUS = "TEST ONLY — REVIEW ONLY — NOT APPROVED / NOT OPERATIONAL"
BANNER = "GINZU POLICY REVIEW NOTE — REVIEW ONLY — NOT APPROVED / NOT OPERATIONAL"


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


def _load_draft(root: Path) -> tuple[dict, dict]:
    json_path = root / DRAFT_JSON
    md_path = root / DRAFT_MD
    json_raw = json_path.read_bytes()
    md_raw = md_path.read_bytes()
    json_sha = _sha(json_raw)
    md_sha = _sha(md_raw)
    if json_sha != DRAFT_JSON_SHA256:
        raise WorkbookContractError(f"draft JSON drifted: {DRAFT_JSON} sha256 {json_sha} != pinned {DRAFT_JSON_SHA256}")
    if md_sha != DRAFT_MD_SHA256:
        raise WorkbookContractError(f"draft MD drifted: {DRAFT_MD} sha256 {md_sha} != pinned {DRAFT_MD_SHA256}")
    try:
        draft = json.loads(json_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbookContractError(f"draft JSON is malformed: {DRAFT_JSON}") from exc
    hashes = {
        "json_rel": str(DRAFT_JSON),
        "json_sha": json_sha,
        "md_rel": str(DRAFT_MD),
        "md_sha": md_sha,
    }
    return draft, hashes


def _verify_draft_against_source(template: str | Path, draft: dict) -> None:
    """Reject draft drift: every embedded inspector must equal the live pinned inspection."""
    refs = draft.get("entries")
    if not isinstance(refs, list) or len(refs) != 67:
        raise WorkbookContractError("draft JSON must contain exactly 67 ordered entries")
    with _restore_et_namespace_registry():
        inspection = inspect_trial_guidance(Path(template), verify_pin=True)
    live = {entry["reference"]: entry for entry in inspection["entries"]}
    if len(live) != 67:
        raise WorkbookContractError("draft drift: live pinned inspection does not cover 67 references")
    seen: list[str] = []
    for entry in refs:
        reference = entry.get("reference")
        if not isinstance(reference, str) or reference not in live or reference in seen:
            raise WorkbookContractError(f"draft drift: entry references must match the 67 ordered contract allowlist: {reference!r}")
        seen.append(reference)
        proposal = entry.get("proposal")
        inspector = proposal.get("inspector") if isinstance(proposal, dict) else None
        if inspector != live[reference]:
            raise WorkbookContractError(f"draft drift: embedded inspector for {reference} does not match the pinned inspection")
    if seen != [entry["reference"] for entry in inspection["entries"]]:
        raise WorkbookContractError("draft drift: entry order does not match the pinned contract order")


def _comment_id(reference: str) -> str:
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"airline-valuation/review-note/{reference}")).upper() + "}"


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_note(entry: dict, draft: dict, draft_hashes: dict) -> str:
    """Deterministic review note for one draft entry, full policy fidelity.

    Every required subject is preserved verbatim as a labeled line: document
    status, entry id, reference, policy schema/version, proposal status,
    policy family, applicability, source concept, source hierarchy, composition,
    tie-break, missing rule, gate, fallback status with trigger/alternatives/
    sensitivity, and unresolved rationale. No field is truncated or condensed.
    An explicit review question is derived from the unresolved rationale
    without inventing an answer or falsifying provenance, and the full-policy
    locator (JSON/MD paths and SHA-256, plus id/reference/version/banner) keeps
    the source retrievable.
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
    lines = [
        BANNER,
        f"Document status: {_fmt(draft.get('document_status'))}",
        f"Entry: {_fmt(entry.get('id'))} · Reference: {reference} · Policy schema: {_fmt(draft.get('schema_version'))}",
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
    ])
    guard = (proposal.get("inspector") or {}).get("guard_classification")
    if guard:
        lines.append(f"Guard: {guard}")
    fallback_parts = [_fmt(fallback.get("status"))]
    if fallback.get("trigger"):
        fallback_parts.append(f"trigger {_fmt(fallback.get('trigger'))}")
    if alternatives:
        fallback_parts.append("alternatives " + "; ".join(_fmt(item) for item in alternatives))
    if fallback.get("sensitivity"):
        fallback_parts.append(f"sensitivity {_fmt(fallback.get('sensitivity'))}")
    lines.append("Fallback status: " + " · ".join(fallback_parts))
    lines.append(f"Unresolved rationale: {unresolved}")
    lines.append(f"Review question: What must the reviewer resolve before the proposed policy can apply at {reference}? — {_first_clause(unresolved)}")
    lines.append(f"Draft JSON: {draft_hashes['json_rel']} (sha256={draft_hashes['json_sha']}) · Draft MD: {draft_hashes['md_rel']} (sha256={draft_hashes['md_sha']})")
    lines.append("NOT APPROVED / NOT OPERATIONAL — review-only artifact; no source authorization, writer, valuation, forecast, or accounting approval.")
    return "\n".join(lines)


def _comment_element_xml(reference: str, comment_id: str, person_id: str, parent_id: str | None, text: str) -> bytes:
    attrs = f'ref="{reference}" personId="{person_id}" id="{comment_id}"'
    if parent_id:
        attrs += f' parentId="{parent_id}"'
    body = _xml_escape(text)
    return f'<threadedComment {attrs}><text xml:space="preserve">{body}</text></threadedComment>'.encode("utf-8")


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
                roots.setdefault(ref, cid)
    return roots


def _sheet_threaded_part(reference: str) -> str:
    sheet_name = reference.split("!", 1)[0]
    if sheet_name == "Input sheet":
        return "xl/threadedComments/threadedComment1.xml"
    if sheet_name == "Cost of capital worksheet":
        return "xl/threadedComments/threadedComment5.xml"
    raise WorkbookContractError(f"review note reference is not on a permitted sheet: {reference}")


def _expected_members(archive: zipfile.ZipFile, draft: dict, draft_hashes: dict) -> tuple[dict[str, bytes], list[dict]]:
    expected = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    existing = {
        "xl/threadedComments/threadedComment1.xml": _existing_root_ids(archive, "xl/threadedComments/threadedComment1.xml"),
        "xl/threadedComments/threadedComment5.xml": _existing_root_ids(archive, "xl/threadedComments/threadedComment5.xml"),
    }
    insertions: dict[str, list[bytes]] = {
        "xl/threadedComments/threadedComment1.xml": [],
        "xl/threadedComments/threadedComment5.xml": [],
    }
    notes: list[dict] = []
    seen: list[str] = []
    for entry in draft["entries"]:
        reference = entry["reference"]
        if reference in seen:
            raise WorkbookContractError(f"duplicate draft entry reference: {reference}")
        seen.append(reference)
        cell = reference.rsplit("!", 1)[1]
        part = _sheet_threaded_part(reference)
        comment_id = _comment_id(reference)
        root_id = existing[part].get(cell)
        note = build_note(entry, draft, draft_hashes)
        insertions[part].append(_comment_element_xml(cell, comment_id, REVIEW_PERSON["id"], root_id, note))
        notes.append({
            "reference": reference,
            "cell": cell,
            "entry_id": entry.get("id"),
            "part": part,
            "comment_id": comment_id,
            "parent_id": root_id,
            "thread": "reply" if root_id else "root",
        })
    for part in insertions:
        original = expected[part]
        marker = _THREADED_MARKER
        if original.count(marker) != 1:
            raise WorkbookContractError(f"threaded comment part lacks exactly one closing root: {part}")
        expected[part] = original[: original.index(marker)] + b"".join(insertions[part]) + original[original.index(marker):]
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
    return expected, notes


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


def _validate_output_targets(template: Path, destination: Path, manifest_path: Path,
                             draft_paths: tuple[Path, ...] = ()) -> None:
    """Fail closed before any mkdir/write: no output may alias a protected file.

    Protected files are the source template passed in, the canonical project
    operational source workbook, the pinned draft JSON/MD (both the canonical
    repo copies and any resolved against the active root), the pinned detailed
    contract JSON, every output (destination/manifest), and the internal
    ``.tmp`` siblings the atomic writers open with "wb"/write_text — a symlink
    planted at a ``.tmp`` sibling could redirect a write onto a protected file
    even when the main output path is safe. Aliases are detected by resolved
    path and by same device/inode, so symlinks and hardlinks cannot redirect a
    write onto a protected file.
    """
    project_root = Path(__file__).resolve().parents[2]
    canonical_source = project_root / SOURCE_FILE
    contract_path = Path(__file__).resolve().with_name("detailed_contract.json")
    protected = [
        template, canonical_source, contract_path,
        project_root / DRAFT_JSON, project_root / DRAFT_MD, *draft_paths,
    ]
    outputs = [destination, manifest_path]
    temp_siblings = [
        destination.with_name(destination.name + ".tmp"),
        manifest_path.with_name(manifest_path.name + ".tmp"),
    ]
    for out in outputs:
        for prot in protected:
            if _same_file(out, prot):
                raise WorkbookContractError(
                    f"review copy output path {out} aliases the protected source {prot}; refusing to overwrite")
    for i, out in enumerate(outputs):
        for other in outputs[i + 1:]:
            if _same_file(out, other):
                raise WorkbookContractError(
                    f"review copy output path {out} aliases another output {other}; refusing to overwrite")
    for temp in temp_siblings:
        for prot in protected + outputs:
            if _same_file(temp, prot):
                raise WorkbookContractError(
                    f"review copy temporary path {temp} aliases the protected path {prot}; refusing to write through it")


def _build_manifest(destination: Path, draft_hashes: dict, notes: list[dict]) -> dict:
    """The exact generated manifest document; verification requires full equality."""
    return {
        "schema_version": MANIFEST_SCHEMA,
        "status": STATUS,
        "source": {"file": SOURCE_FILE, "sha256": SOURCE_SHA256},
        "draft": {"json": draft_hashes["json_rel"], "json_sha256": draft_hashes["json_sha"],
                  "md": draft_hashes["md_rel"], "md_sha256": draft_hashes["md_sha"]},
        "review_author": REVIEW_PERSON,
        "output": {"file": str(destination), "sha256": _sha(destination.read_bytes())},
        "changed_parts": sorted(PERMITTED_PARTS),
        "reference_count": len(notes),
        "notes": notes,
        "manual_excel_review_copy_display": "pending — open in desktop Excel to confirm threaded comment rendering, reply threading and the added review author",
    }


def _normalized_manifest_doc(doc: dict) -> dict:
    """Deep copy of a manifest document with only output.file normalized to its
    resolved absolute path; every other value must match exactly."""
    normalized = copy.deepcopy(doc)
    output = normalized.get("output")
    if isinstance(output, dict) and isinstance(output.get("file"), str):
        output["file"] = str(Path(output["file"]).resolve())
    return normalized


def generate_review_copy(template: str | Path, dest: str | Path, *, manifest: str | Path | None = None,
                         root: str | Path | None = None) -> tuple[Path, Path]:
    """Build the review-only workbook copy and its manifest; writes nothing else."""
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    template_path = Path(template)
    destination = Path(dest)
    manifest_path = Path(manifest) if manifest is not None else destination.with_suffix(".xlsx-manifest.json")
    draft_paths = (root_path / DRAFT_JSON, root_path / DRAFT_MD)
    _validate_output_targets(template_path, destination, manifest_path, draft_paths)
    raw = template_path.read_bytes()
    if _sha(raw) != SOURCE_SHA256:
        raise WorkbookContractError("review copy accepts only the pinned pristine template SHA-256")
    draft, draft_hashes = _load_draft(root_path)
    with _checked_zip(template_path) as archive:
        _validate_relationships(archive)
    _verify_draft_against_source(template_path, draft)
    with _checked_zip(template_path) as archive:
        expected, notes = _expected_members(archive, draft, draft_hashes)
        _write_package(archive, destination, expected)
    manifest_doc = _build_manifest(destination, draft_hashes, notes)
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
            raise WorkbookContractError("review copy package member inventory changed")
        for name in source.namelist():
            if changed.read(name) != expected[name]:
                raise WorkbookContractError(f"review copy member differs outside the exact permitted parts: {name}")
        changed_names = {name for name in source.namelist() if source.read(name) != expected[name]}
        if changed_names != PERMITTED_PARTS:
            raise WorkbookContractError(f"review copy delta is not exactly the 3 permitted parts: {sorted(changed_names)}")
    if dest_path.read_bytes() != _render_package(source, expected):
        raise WorkbookContractError("review copy ZIP container differs outside the exact 3 permitted parts")


def _verify_notes_in_dest(dest_path: Path, expected_notes: list[dict], contract_order: list[str]) -> None:
    with _checked_zip(dest_path) as changed:
        comments = {
            "xl/threadedComments/threadedComment1.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment1.xml")),
            "xl/threadedComments/threadedComment5.xml": ET.fromstring(changed.read("xl/threadedComments/threadedComment5.xml")),
        }
        person_root = ET.fromstring(changed.read("xl/persons/person.xml"))
    people = [person.attrib.get("id") for person in person_root.findall(f"{{{_THREADED_NS}}}person")]
    if REVIEW_PERSON["id"] not in people:
        raise WorkbookContractError("review author person missing from xl/persons/person.xml")
    if len(people) != len(set(people)) or len(people) != 4:
        raise WorkbookContractError("person.xml must contain exactly the 3 original authors plus the review author")
    new_by_part: dict[str, list[dict]] = {"xl/threadedComments/threadedComment1.xml": [], "xl/threadedComments/threadedComment5.xml": []}
    for note in expected_notes:
        part = note["part"]
        found = None
        for comment in comments[part].findall(f"{{{_THREADED_NS}}}threadedComment"):
            if comment.attrib.get("id") == note["comment_id"]:
                found = comment
                break
        if found is None:
            raise WorkbookContractError(f"review note comment missing for {note['reference']}")
        text_node = next((child for child in found if child.tag.rsplit("}", 1)[-1] == "text"), None)
        text = "".join(text_node.itertext()) if text_node is not None else ""
        if text != note["note_text"]:
            raise WorkbookContractError(f"review note text drifted for {note['reference']}")
        if found.attrib.get("personId") != REVIEW_PERSON["id"]:
            raise WorkbookContractError(f"review note author differs for {note['reference']}")
        expected_cell = note["reference"].rsplit("!", 1)[1]
        if found.attrib.get("ref") != expected_cell:
            raise WorkbookContractError(f"review note ref differs for {note['reference']}")
        parent = found.attrib.get("parentId")
        if note["thread"] == "reply" and parent != note["parent_id"]:
            raise WorkbookContractError(f"review note reply parent differs for {note['reference']}")
        if note["thread"] == "root" and parent is not None:
            raise WorkbookContractError(f"review note root unexpectedly has a parent for {note['reference']}")
        new_by_part[part].append(note)
    # Ordered refs: within each part the new comments appear in contract order.
    for part, notes in new_by_part.items():
        expected_refs = [ref for ref in contract_order if _sheet_threaded_part(ref) == part]
        if [note["reference"] for note in notes] != expected_refs:
            raise WorkbookContractError(f"review notes are not in pinned contract order in {part}")


def _validate_target_thread_contract(dest_path: Path) -> None:
    """Fail-closed structural checks on the review copy's threaded comment roots,
    IDs, person assignments, namespaces, relationships and content types.

    The source pin already guarantees physical immutability of the input; these
    explicit checks make tampering with the copy's thread topology fail loudly.
    """
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
        # Relationships must stay Internal and content types must still declare
        # the threaded-comment and persons parts exactly as contracted.
        for rel_part in ("xl/worksheets/_rels/sheet1.xml.rels", "xl/worksheets/_rels/sheet10.xml.rels"):
            rel_root = ET.fromstring(changed.read(rel_part))
            for rel in rel_root.findall(f"{{{_PKG_NS}}}Relationship"):
                if rel.attrib.get("Type", "").endswith("/threadedComment") and rel.attrib.get("TargetMode", "Internal") != "Internal":
                    raise WorkbookContractError(f"threaded comment relationship TargetMode is not Internal: {rel_part}")
        wb_rel = ET.fromstring(changed.read("xl/_rels/workbook.xml.rels"))
        for rel in wb_rel.findall(f"{{{_PKG_NS}}}Relationship"):
            if rel.attrib.get("Type", "").endswith("/person") and rel.attrib.get("TargetMode", "Internal") != "Internal":
                raise WorkbookContractError("person relationship TargetMode is not Internal")
        content_types = changed.read("[Content_Types].xml")
        if b"application/vnd.ms-excel.threadedcomments+xml" not in content_types:
            raise WorkbookContractError("content types no longer declare the threaded comment parts")
        if b"application/vnd.ms-excel.person+xml" not in content_types:
            raise WorkbookContractError("content types no longer declare the persons part")


def verify_review_copy_delta(template: str | Path, dest: str | Path, *, manifest: str | Path | None = None,
                             root: str | Path | None = None) -> dict:
    """Require the exact review-only delta: 3 permitted parts and nothing else."""
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    template_path = Path(template)
    dest_path = Path(dest)
    raw = template_path.read_bytes()
    if _sha(raw) != SOURCE_SHA256:
        raise WorkbookContractError("review copy source SHA-256 does not match the pinned template")
    draft, draft_hashes = _load_draft(root_path)
    _verify_draft_against_source(template_path, draft)
    contract_order = [entry["reference"] for entry in draft["entries"]]
    with _checked_zip(template_path) as source:
        _validate_relationships(source)
        expected, notes = _expected_members(source, draft, draft_hashes)
        note_texts = {entry["reference"]: build_note(entry, draft, draft_hashes) for entry in draft["entries"]}
        notes_with_text = [{**note, "note_text": note_texts[note["reference"]]} for note in notes]
        # Semantic checks first (inventory, identities, topology, note texts),
        # then the target structural contract, then the byte-level catch-all.
        with _checked_zip(dest_path) as changed:
            if changed.namelist() != source.namelist():
                raise WorkbookContractError("review copy package member inventory changed")
        _verify_notes_in_dest(dest_path, notes_with_text, contract_order)
        _validate_target_thread_contract(dest_path)
        _verify_parts_and_container(source, dest_path, expected)
        output_sha = _sha(dest_path.read_bytes())
    review = {
        "valid": True,
        "schema_version": MANIFEST_SCHEMA,
        "status": STATUS,
        "source_sha256": SOURCE_SHA256,
        "draft_json_sha256": draft_hashes["json_sha"],
        "draft_md_sha256": draft_hashes["md_sha"],
        "output_sha256": output_sha,
        "reference_count": len(notes),
        "changed_parts": sorted(PERMITTED_PARTS),
        "replies": sum(1 for note in notes if note["thread"] == "reply"),
        "new_roots": sum(1 for note in notes if note["thread"] == "root"),
    }
    if manifest is not None:
        manifest_path = Path(manifest)
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_manifest = _build_manifest(dest_path, draft_hashes, notes)
        if _normalized_manifest_doc(manifest_doc) != _normalized_manifest_doc(expected_manifest):
            raise WorkbookContractError("manifest drifted from the exact generated document")
        review["manifest_verified"] = True
    return review


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m airline_valuation.review_copy",
                                     description="Review-only Ginzu XLSX copy: generate or verify the exact 3-part delta.")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write the review-only workbook copy and manifest")
    gen.add_argument("--template", type=Path, default=Path(__file__).resolve().parents[2] / SOURCE_FILE)
    gen.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "review-artifacts/ginzu-policy-proposed-review-only.xlsx")
    gen.add_argument("--manifest", type=Path, default=None)
    gen.set_defaults(func=_cmd_generate)
    ver = sub.add_parser("verify", help="verify an existing review-only workbook copy")
    ver.add_argument("--template", type=Path, default=Path(__file__).resolve().parents[2] / SOURCE_FILE)
    ver.add_argument("--output", type=Path, required=True)
    ver.add_argument("--manifest", type=Path, default=None)
    ver.set_defaults(func=_cmd_verify)
    return parser


def _cmd_generate(args: argparse.Namespace) -> int:
    dest, manifest_path = generate_review_copy(args.template, args.output, manifest=args.manifest)
    review = verify_review_copy_delta(args.template, dest, manifest=manifest_path)
    print(json.dumps({**review, "workbook": str(dest), "manifest": str(manifest_path)}, indent=2, sort_keys=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    review = verify_review_copy_delta(args.template, args.output, manifest=args.manifest)
    print(json.dumps(review, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(f"REVIEW COPY BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
