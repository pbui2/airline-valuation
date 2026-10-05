"""Review-only Ginzu XLSX copy: byte-bounded 3-part delta with threaded review notes."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import airline_valuation.review_copy as rc
from airline_valuation.workbook import WorkbookContractError, _checked_zip

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
DRAFT = ROOT / "docs/policy-drafts/ginzu-input-policy-v1.json"
DRAFT_MD = ROOT / "docs/ginzu-input-policy-v1.md"
CONTRACT = ROOT / "src/airline_valuation/detailed_contract.json"
NS = {"tc": "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"}


def _draft() -> dict:
    return json.loads(DRAFT.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rewrite_package(source: Path, destination: Path, updates: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as out:
        for info in src.infolist():
            out.writestr(info, updates.get(info.filename, src.read(info.filename)))


def _new_comments(part: bytes) -> list[ET.Element]:
    root = ET.fromstring(part)
    return [c for c in root.findall("tc:threadedComment", NS) if c.attrib.get("personId") == rc.REVIEW_PERSON["id"]]


def _existing_roots(part: bytes) -> dict[str, str]:
    root = ET.fromstring(part)
    return {c.attrib["ref"]: c.attrib["id"] for c in root.findall("tc:threadedComment", NS) if "parentId" not in c.attrib}


def _refs() -> list[str]:
    return [entry["reference"] for entry in _draft()["entries"]]


def test_review_copy_pins_and_note_coverage(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert _sha(TEMPLATE) == rc.SOURCE_SHA256
    review = rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)
    assert review["valid"]
    assert review["reference_count"] == 67
    assert review["changed_parts"] == sorted(rc.PERMITTED_PARTS)
    assert review["replies"] + review["new_roots"] == 67
    members = _members(dest)
    threaded1 = _new_comments(members["xl/threadedComments/threadedComment1.xml"])
    threaded5 = _new_comments(members["xl/threadedComments/threadedComment5.xml"])
    assert len(threaded1) == 47
    assert len(threaded5) == 20
    # Every one of the 67 contract references is covered by exactly one note.
    covered = {c.attrib.get("ref") for c in threaded1 + threaded5}
    assert covered == {ref.rsplit("!", 1)[1] for ref in _refs()}
    assert {c.attrib["id"] for c in threaded1 + threaded5} == {rc._comment_id(ref) for ref in _refs()}
    # Every note carries the nonapproval banner.
    assert all("REVIEW ONLY" in (c.find("tc:text", NS).text or "") for c in threaded1 + threaded5)


def test_review_copy_note_content(tmp_path: Path):
    draft = _draft()
    hashes = {"json_rel": "docs/policy-drafts/ginzu-input-policy-v1.json", "json_sha": rc.DRAFT_JSON_SHA256,
              "md_rel": "docs/ginzu-input-policy-v1.md", "md_sha": rc.DRAFT_MD_SHA256}
    by_ref = {entry["reference"]: entry for entry in draft["entries"]}
    note = rc.build_note(by_ref["Input sheet!B8"], draft, hashes)
    for required in (
        rc.BANNER,
        "Document status: PROPOSED — NOT APPROVED / NOT OPERATIONAL",
        "Entry: GINZU-001",
        "Reference: Input sheet!B8",
        "Policy schema: ginzu-input-policy-review/1",
        "Proposal status: unresolved",
        "Policy family: selector",
        "Applicability: conditional",
        "Source hierarchy:",
        "Composition:",
        "Tie-break:",
        "Missing rule:",
        "Gate: none",
        "Fallback status: outstanding",
        "Unresolved rationale:",
        f"Draft JSON: docs/policy-drafts/ginzu-input-policy-v1.json (sha256={rc.DRAFT_JSON_SHA256})",
        f"Draft MD: docs/ginzu-input-policy-v1.md (sha256={rc.DRAFT_MD_SHA256})",
        "NOT APPROVED / NOT OPERATIONAL",
    ):
        assert required in note
    # The full note, untruncated, is what lands in the workbook.
    out = tmp_path / "review-only.xlsx"
    dest, _ = rc.generate_review_copy(TEMPLATE, out)
    members = _members(dest)
    b8 = [c for c in _new_comments(members["xl/threadedComments/threadedComment1.xml"]) if c.attrib.get("ref") == "B8"]
    assert len(b8) == 1
    assert "".join(b8[0].find("tc:text", NS).itertext()) == note
    # Formula-guarded cells carry their guard in the note.
    guard_note = rc.build_note(by_ref["Input sheet!B27"], draft, hashes)
    assert "Guard: scenario_conversion_guard" in guard_note
    # Regional H21 carries its route gate in the note (string route_gate form).
    h21_note = rc.build_note(by_ref["Cost of capital worksheet!H21"], draft, hashes)
    assert "Gate: " in h21_note and "Cost of capital worksheet!B25" in h21_note


def test_review_copy_note_full_fidelity_question_and_excel_safety(tmp_path: Path):
    draft = _draft()
    hashes = {"json_rel": "docs/policy-drafts/ginzu-input-policy-v1.json", "json_sha": rc.DRAFT_JSON_SHA256,
              "md_rel": "docs/ginzu-input-policy-v1.md", "md_sha": rc.DRAFT_MD_SHA256}
    notes = {entry["reference"]: rc.build_note(entry, draft, hashes) for entry in draft["entries"]}
    # Every one of the 67 notes carries an explicit review question with a '?'.
    for ref, note in notes.items():
        assert "Review question:" in note, ref
        question = next(line for line in note.splitlines() if line.startswith("Review question:"))
        assert "?" in question, ref
    # No silent truncation: no ellipsis and every verbatim policy field value
    # from the draft survives in its note (independent representative snippets).
    for entry in draft["entries"]:
        note = notes[entry["reference"]]
        assert "…" not in note, entry["reference"]
        proposal = entry["proposal"]
        fields = [proposal.get("source_concept"), proposal.get("composition"),
                  proposal.get("tie_break"), proposal.get("missing_component"),
                  proposal.get("unresolved_rationale")]
        fields += list(proposal.get("source_hierarchy") or [])
        fallback = proposal.get("fallback") or {}
        fields += [fallback.get("trigger")] + list(fallback.get("alternatives") or []) + [fallback.get("sensitivity")]
        for field in fields:
            if field:
                assert str(field) in note, f"field dropped from note for {entry['reference']}"
    # The review question is grounded in the unresolved rationale: its leading
    # complete clause appears in the question line, verbatim from the policy.
    b8 = next(entry for entry in draft["entries"] if entry["reference"] == "Input sheet!B8")
    question = next(line for line in notes[b8["reference"]].splitlines() if line.startswith("Review question:"))
    assert b8["proposal"]["unresolved_rationale"].split(";", 1)[0] in question
    # Excel length safety: every note stays far below the threaded-comment
    # cell-text limit even after the question and full-policy locator are added.
    assert all(len(note) <= 32767 - 1000 for note in notes.values())
    # The full-policy locator carries JSON and MD paths with their pinned SHA-256.
    for note in notes.values():
        assert "Draft JSON: docs/policy-drafts/ginzu-input-policy-v1.json (sha256=%s)" % rc.DRAFT_JSON_SHA256 in note
        assert "Draft MD: docs/ginzu-input-policy-v1.md (sha256=%s)" % rc.DRAFT_MD_SHA256 in note


def test_review_copy_delta_and_thread_integrity(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, _ = rc.generate_review_copy(TEMPLATE, out)
    review = rc.verify_review_copy_delta(TEMPLATE, dest)
    assert review["valid"] and review["replies"] == 47 and review["new_roots"] == 20
    before, after = _members(TEMPLATE), _members(dest)
    assert {name for name in before if before[name] != after[name]} == rc.PERMITTED_PARTS
    source_roots1 = _existing_roots(before["xl/threadedComments/threadedComment1.xml"])
    source_roots5 = _existing_roots(before["xl/threadedComments/threadedComment5.xml"])
    for part, roots, cell in (
        ("xl/threadedComments/threadedComment1.xml", source_roots1, "B8"),
        ("xl/threadedComments/threadedComment1.xml", source_roots1, "B13"),
        ("xl/threadedComments/threadedComment5.xml", source_roots5, "B35"),
        ("xl/threadedComments/threadedComment5.xml", source_roots5, "H21"),
    ):
        new = [c for c in _new_comments(after[part]) if c.attrib.get("ref") == cell]
        assert len(new) == 1
        comment = new[0]
        if cell in roots:
            assert comment.attrib.get("parentId") == roots[cell]
        else:
            assert "parentId" not in comment.attrib
    # Reply ref always equals its parent ref and parent ids resolve in the same part.
    for part in ("xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml"):
        root = ET.fromstring(after[part])
        ids = {c.attrib["id"]: c.attrib.get("ref") for c in root.findall("tc:threadedComment", NS)}
        for c in root.findall("tc:threadedComment", NS):
            parent = c.attrib.get("parentId")
            if parent:
                assert parent in ids
                assert ids[parent] == c.attrib.get("ref")
    assert rc.REVIEW_PERSON["id"].encode() in after["xl/persons/person.xml"]


def test_review_copy_preserves_original_comments(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, _ = rc.generate_review_copy(TEMPLATE, out)
    before, after = _members(TEMPLATE), _members(dest)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == rc.PERMITTED_PARTS
    # Every original threaded comment (same id) keeps its exact text.
    for part in ("xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml"):
        source_root = ET.fromstring(before[part])
        dest_root = ET.fromstring(after[part])
        dest_by_id = {c.attrib["id"]: c for c in dest_root.findall("tc:threadedComment", NS)}
        for comment in source_root.findall("tc:threadedComment", NS):
            preserved = dest_by_id[comment.attrib["id"]]
            assert "".join(comment.find("tc:text", NS).itertext()) == "".join(preserved.find("tc:text", NS).itertext())
            assert preserved.attrib.get("personId") == comment.attrib.get("personId")
    # Legacy comment parts, VML, drawings, worksheet XML and rels are untouched.
    for part in (name for name in before if name.startswith("xl/comments") or name.endswith(".vml")
                 or name.startswith("xl/drawings/") or name.endswith("worksheet.xml.rels")
                 or "worksheets/sheet" in name or name == "xl/worksheets/sheet1.xml" or name == "xl/worksheets/sheet10.xml"):
        assert after[part] == before[part], part


def test_review_copy_rejects_unsafe_inputs(tmp_path: Path, monkeypatch):
    pinned_sha = rc.SOURCE_SHA256
    # Duplicate ZIP member names are rejected.
    duplicate = tmp_path / "duplicate.xlsx"
    with zipfile.ZipFile(TEMPLATE) as src:
        first = src.infolist()[0]
    with zipfile.ZipFile(duplicate, "w") as out:
        out.writestr(first, b"first")
        out.writestr(first, b"second")
    monkeypatch.setattr(rc, "SOURCE_SHA256", _sha(duplicate))
    with pytest.raises(WorkbookContractError, match="duplicate ZIP member names"):
        rc.generate_review_copy(duplicate, tmp_path / "out.xlsx")
    # Unsafe member paths are rejected.
    unsafe = tmp_path / "unsafe.xlsx"
    with zipfile.ZipFile(TEMPLATE) as src:
        payloads = {info.filename: src.read(info.filename) for info in src.infolist()}
        infos = src.infolist()
    with zipfile.ZipFile(unsafe, "w") as out:
        for info in infos[:-1]:
            out.writestr(info, payloads[info.filename])
        out.writestr("../escape.xlsx", b"bad")
    monkeypatch.setattr(rc, "SOURCE_SHA256", _sha(unsafe))
    with pytest.raises(WorkbookContractError, match="unsafe ZIP member path"):
        rc.generate_review_copy(unsafe, tmp_path / "out.xlsx")
    # Non-pinned source is rejected before any work.
    changed_source = tmp_path / "drifted.xlsx"
    changed_source.write_bytes(TEMPLATE.read_bytes() + b"x")
    with pytest.raises(WorkbookContractError, match="pinned pristine template"):
        rc.generate_review_copy(changed_source, tmp_path / "out.xlsx")
    # A drifted workbook persons relationship is rejected.
    drifted_rel = tmp_path / "rel.xlsx"
    with zipfile.ZipFile(TEMPLATE) as src:
        members = {info.filename: src.read(info.filename) for info in src.infolist()}
    rels = members["xl/_rels/workbook.xml.rels"]
    members["xl/_rels/workbook.xml.rels"] = rels.replace(b'Target="persons/person.xml"', b'Target="persons/other.xml"', 1)
    _rewrite_package(TEMPLATE, drifted_rel, members)
    monkeypatch.setattr(rc, "SOURCE_SHA256", _sha(drifted_rel))
    with pytest.raises(WorkbookContractError, match="persons relationship"):
        rc.generate_review_copy(drifted_rel, tmp_path / "out.xlsx")
    # A drifted draft (embedded inspector no longer matches the pinned workbook) is rejected.
    monkeypatch.setattr(rc, "SOURCE_SHA256", pinned_sha)
    draft_root = tmp_path / "docs"
    (draft_root / "policy-drafts").mkdir(parents=True, exist_ok=True)
    (draft_root / "ginzu-input-policy-v1.md").write_bytes(DRAFT_MD.read_bytes())
    drifted_draft = draft_root / "policy-drafts" / "ginzu-input-policy-v1.json"
    draft = _draft()
    draft["entries"][0]["proposal"]["inspector"] = {"drifted": True}
    drifted_draft.write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(rc, "DRAFT_JSON_SHA256", _sha(drifted_draft))
    with pytest.raises(WorkbookContractError, match="draft drift"):
        rc.generate_review_copy(TEMPLATE, tmp_path / "out.xlsx", root=tmp_path)


def test_review_copy_mutation_sensitivity(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)["valid"]
    base = _members(dest)

    def expect_failure(label: str, mutated: dict[str, bytes] | Path, match: str) -> None:
        if isinstance(mutated, dict):
            tampered = tmp_path / (label + ".xlsx")
            _rewrite_package(dest, tampered, mutated)
        else:
            tampered = mutated
        with pytest.raises(WorkbookContractError, match=match):
            rc.verify_review_copy_delta(TEMPLATE, tampered, manifest=manifest)

    # Unrelated part mutation.
    unrelated = dict(base)
    unrelated["docProps/core.xml"] += b" "
    expect_failure("unrelated", unrelated, "outside the exact permitted parts")
    # A non-permitted cell mutation inside a worksheet.
    cell = dict(base)
    cell["xl/worksheets/sheet1.xml"] = cell["xl/worksheets/sheet1.xml"].replace(b'<c r="B8" s="192"/>', b'<c r="B8" s="192"><v>9</v></c>', 1)
    expect_failure("cell", cell, "outside the exact permitted parts")
    # Reply parent mutation.
    parent_mut = dict(base)
    part = parent_mut["xl/threadedComments/threadedComment1.xml"]
    new_b8 = [c for c in _new_comments(part) if c.attrib.get("ref") == "B8"][0]
    parent_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(
        f' parentId="{new_b8.attrib["parentId"]}"'.encode(),
        b' parentId="{00000000-0000-0000-0000-000000000000}"', 1)
    expect_failure("parent", parent_mut, "parent")
    # Note ref mutation.
    ref_mut = dict(base)
    ref_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(
        f'<threadedComment ref="B8" personId="{rc.REVIEW_PERSON["id"]}"'.encode(),
        f'<threadedComment ref="Z99" personId="{rc.REVIEW_PERSON["id"]}"'.encode(), 1)
    expect_failure("ref", ref_mut, "ref differs")
    # Note text mutation.
    text_mut = dict(base)
    text_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(b"REVIEW ONLY", b"REVIEW ONLYX", 1)
    expect_failure("text", text_mut, "text drifted")
    # Person identity mutation.
    person_mut = dict(base)
    person_mut["xl/persons/person.xml"] = person_mut["xl/persons/person.xml"].replace(
        rc.REVIEW_PERSON["id"].encode(), b"{99999999-9999-9999-9999-999999999999}", 1)
    expect_failure("person", person_mut, "author")
    # ZIP container suffix / member removal / source pin drift.
    suffix = tmp_path / "suffix.xlsx"
    suffix.write_bytes(dest.read_bytes() + b"X")
    expect_failure("suffix", suffix, "ZIP container")
    removed = tmp_path / "removed.xlsx"
    with zipfile.ZipFile(dest) as src, zipfile.ZipFile(removed, "w") as out:
        for info in src.infolist()[:-1]:
            out.writestr(info, src.read(info.filename))
    with pytest.raises(WorkbookContractError, match="member inventory"):
        rc.verify_review_copy_delta(TEMPLATE, removed, manifest=manifest)
    drifted_source = tmp_path / "drifted-source.xlsx"
    drifted_source.write_bytes(TEMPLATE.read_bytes() + b"x")
    with pytest.raises(WorkbookContractError, match="source SHA-256"):
        rc.verify_review_copy_delta(drifted_source, dest, manifest=manifest)
    # Manifest hash drift.
    bad_manifest = tmp_path / "bad-manifest.json"
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_doc["output"]["sha256"] = "0" * 64
    bad_manifest.write_text(json.dumps(manifest_doc), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rc.verify_review_copy_delta(TEMPLATE, dest, manifest=bad_manifest)


def test_review_copy_nonoperational_boundary(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    review = rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)
    assert review["status"] == rc.STATUS
    assert "REVIEW ONLY" in review["status"] and "NOT APPROVED" in review["status"]
    before, after = _members(TEMPLATE), _members(dest)
    # No operational part may change: sheets, shared strings, styles, calc chain,
    # legacy comments, VML, tables, webextensions, content types, all rels.
    for name in before:
        if name in rc.PERMITTED_PARTS:
            continue
        assert after[name] == before[name], f"non-permitted part changed: {name}"
    # The workbook is a pure copy: no input cells were written and no formula changed.
    assert after["xl/worksheets/sheet1.xml"] == before["xl/worksheets/sheet1.xml"]
    assert after["xl/worksheets/sheet10.xml"] == before["xl/worksheets/sheet10.xml"]
    assert after["xl/sharedStrings.xml"] == before["xl/sharedStrings.xml"]
    assert after["xl/calcChain.xml"] == before["xl/calcChain.xml"]
    assert after["xl/styles.xml"] == before["xl/styles.xml"]
    # Notes document their non-operational, review-only nature.
    for part in ("xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml"):
        for comment in _new_comments(after[part]):
            assert "NOT APPROVED / NOT OPERATIONAL" in "".join(comment.find("tc:text", NS).itertext())


def test_review_copy_manifest_and_status(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    assert doc["schema_version"] == rc.MANIFEST_SCHEMA
    assert doc["status"] == rc.STATUS
    assert doc["source"]["sha256"] == rc.SOURCE_SHA256
    assert doc["draft"]["json_sha256"] == rc.DRAFT_JSON_SHA256
    assert doc["draft"]["md_sha256"] == rc.DRAFT_MD_SHA256
    assert doc["output"]["sha256"] == _sha(dest)
    assert doc["review_author"]["id"] == rc.REVIEW_PERSON["id"]
    assert doc["changed_parts"] == sorted(rc.PERMITTED_PARTS)
    assert doc["reference_count"] == 67
    assert len(doc["notes"]) == 67
    assert sum(1 for note in doc["notes"] if note["thread"] == "reply") == 47
    assert sum(1 for note in doc["notes"] if note["thread"] == "root") == 20
    assert all({"reference", "entry_id", "part", "comment_id", "parent_id", "thread"}.issubset(note) for note in doc["notes"])
    assert doc["manual_excel_review_copy_display"].startswith("pending")
    review = rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)
    assert review["manifest_verified"] is True
    # A manifest that lost a note or records the wrong note order is rejected.
    bad = json.loads(manifest.read_text(encoding="utf-8"))
    bad["notes"] = bad["notes"][1:] + bad["notes"][:1]
    bad_path = tmp_path / "bad-order.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rc.verify_review_copy_delta(TEMPLATE, dest, manifest=bad_path)


def test_review_copy_cli_generate_and_verify(tmp_path: Path, capsys):
    out = tmp_path / "review-only.xlsx"
    manifest = tmp_path / "manifest.json"
    assert rc.main(["generate", "--output", str(out), "--manifest", str(manifest)]) == 0
    assert out.is_file() and manifest.is_file()
    assert rc.main(["verify", "--output", str(out), "--manifest", str(manifest)]) == 0
    assert "reference_count" in capsys.readouterr().out
    assert rc.verify_review_copy_delta(TEMPLATE, out, manifest=manifest)["valid"]


def test_review_copy_rejects_output_aliasing_source(tmp_path: Path):
    before = _sha(TEMPLATE)
    # dest = source would overwrite the pinned template with the review copy.
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(TEMPLATE, TEMPLATE)
    # manifest = source would overwrite the pinned template with a JSON manifest.
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(TEMPLATE, tmp_path / "out.xlsx", manifest=TEMPLATE)
    # manifest = dest would overwrite the freshly written review copy.
    out = tmp_path / "out.xlsx"
    with pytest.raises(WorkbookContractError, match="aliases another output"):
        rc.generate_review_copy(TEMPLATE, out, manifest=out)
    assert _sha(TEMPLATE) == before


def test_review_copy_rejects_symlink_and_hardlink_aliases(tmp_path: Path):
    before = _sha(TEMPLATE)
    symlink = tmp_path / "link.xlsx"
    symlink.symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(TEMPLATE, symlink)
    # Hardlinks cannot cross filesystems, so create one next to the source.
    with tempfile.TemporaryDirectory(dir=str(TEMPLATE.parent)) as td:
        hardlink = Path(td) / "hard.xlsx"
        os.link(TEMPLATE, hardlink)
        with pytest.raises(WorkbookContractError, match="aliases the protected source"):
            rc.generate_review_copy(TEMPLATE, hardlink)
    assert _sha(TEMPLATE) == before


def test_review_copy_rejects_tmp_sibling_alias(tmp_path: Path):
    before = _sha(TEMPLATE)
    out = tmp_path / "out.xlsx"
    # A symlink planted at the internal dest.tmp sibling would redirect the
    # atomic "wb" write onto the pinned source.
    (tmp_path / "out.xlsx.tmp").symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, out)
    manifest = tmp_path / "manifest.json"
    (tmp_path / "manifest.json.tmp").symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, out, manifest=manifest)
    assert _sha(TEMPLATE) == before


def test_review_copy_protects_operational_source_path(tmp_path: Path):
    before = _sha(TEMPLATE)
    # A different template path (symlink to the pinned source) with the canonical
    # project operational source workbook as the output must still be rejected.
    alias = tmp_path / "template.xlsx"
    alias.symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(alias, TEMPLATE)
    assert _sha(TEMPLATE) == before


def test_review_copy_rejects_draft_and_contract_aliasing(tmp_path: Path):
    # No output may overwrite the pinned draft JSON/MD or the detailed contract.
    # The guard must fire before any write, so the canonical files are never
    # touched: hash them before and after every rejected attempt.
    protected = (DRAFT, DRAFT_MD, CONTRACT)
    before = {p: _sha(p) for p in protected}
    out = tmp_path / "out.xlsx"
    for pinned in protected:
        with pytest.raises(WorkbookContractError, match="aliases the protected source"):
            rc.generate_review_copy(TEMPLATE, out, manifest=pinned)
        with pytest.raises(WorkbookContractError, match="aliases the protected source"):
            rc.generate_review_copy(TEMPLATE, pinned)
    # A symlink planted at an internal .tmp sibling pointing at a draft/contract
    # must also be rejected before the atomic write.
    (tmp_path / "out.xlsx.tmp").symlink_to(DRAFT)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, out)
    (tmp_path / "out.xlsx.tmp").unlink()
    manifest = tmp_path / "manifest.json"
    (tmp_path / "manifest.json.tmp").symlink_to(DRAFT_MD)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, out, manifest=manifest)
    (tmp_path / "manifest.json.tmp").unlink()
    (tmp_path / "contract.xlsx.tmp").symlink_to(CONTRACT)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, tmp_path / "contract.xlsx")
    (tmp_path / "contract.xlsx.tmp").unlink()
    (tmp_path / "contract-manifest.json.tmp").symlink_to(CONTRACT)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rc.generate_review_copy(TEMPLATE, tmp_path / "contract.xlsx", manifest=tmp_path / "contract-manifest.json")
    (tmp_path / "contract-manifest.json.tmp").unlink()
    assert {p: _sha(p) for p in protected} == before


def test_review_copy_draft_guard_uses_resolved_root_and_preserves_drafts(tmp_path: Path):
    # With a custom root, the draft paths resolved against THAT root are the ones
    # the call would load, so they must be protected; the canonical repo drafts
    # and the contract stay protected unconditionally.
    root = tmp_path / "project"
    (root / "docs" / "policy-drafts").mkdir(parents=True)
    (root / "docs" / "ginzu-input-policy-v1.md").write_bytes(DRAFT_MD.read_bytes())
    draft_json = root / "docs" / "policy-drafts" / "ginzu-input-policy-v1.json"
    draft_json.write_bytes(DRAFT.read_bytes())
    canonical_before = {p: _sha(p) for p in (DRAFT, DRAFT_MD, CONTRACT)}
    copied_before = _sha(draft_json)
    out = tmp_path / "out.xlsx"
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(TEMPLATE, out, root=root, manifest=draft_json)
    assert _sha(draft_json) == copied_before
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rc.generate_review_copy(TEMPLATE, out, root=root, manifest=DRAFT)
    # A legitimate generation under the copied root writes only the copy and its
    # manifest; every draft and the contract stay byte-identical.
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, root=root, manifest=tmp_path / "manifest.json")
    assert rc.verify_review_copy_delta(TEMPLATE, dest, root=root, manifest=manifest)["valid"]
    assert _sha(draft_json) == copied_before
    assert {p: _sha(p) for p in (DRAFT, DRAFT_MD, CONTRACT)} == canonical_before


def test_review_copy_manifest_full_equality_rejects_tampering(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)["manifest_verified"]

    def expect_drift(label: str, mutate) -> None:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        mutate(doc)
        bad = tmp_path / (label + ".json")
        bad.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(WorkbookContractError, match="manifest drifted"):
            rc.verify_review_copy_delta(TEMPLATE, dest, manifest=bad)

    expect_drift("status", lambda d: d.__setitem__("status", "APPROVED — OPERATIONAL"))
    expect_drift("display", lambda d: d.__setitem__("manual_excel_review_copy_display", "passed — verified in desktop Excel"))
    expect_drift("count", lambda d: d.__setitem__("reference_count", 66))
    expect_drift("changed_parts", lambda d: d.__setitem__("changed_parts", sorted(rc.PERMITTED_PARTS) + ["xl/workbook.xml"]))
    expect_drift("schema", lambda d: d.__setitem__("schema_version", "other/1"))
    expect_drift("author", lambda d: d["review_author"].__setitem__("id", "{00000000-0000-0000-0000-000000000000}"))
    expect_drift("source", lambda d: d["source"].__setitem__("sha256", "0" * 64))
    expect_drift("draft", lambda d: d["draft"].__setitem__("json_sha256", "0" * 64))
    expect_drift("note_parent", lambda d: d["notes"][0].__setitem__("parent_id", "{11111111-1111-1111-1111-111111111111}"))
    expect_drift("note_thread", lambda d: d["notes"][0].__setitem__("thread", "root"))
    expect_drift("note_ref", lambda d: d["notes"][0].__setitem__("reference", "Input sheet!Z99"))
    # Only the output.file path representation may differ (same resolved path).
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["output"]["file"] = str(tmp_path / "sub" / ".." / "review-only.xlsx")
    rep = tmp_path / "repr.json"
    rep.write_text(json.dumps(doc), encoding="utf-8")
    assert rc.verify_review_copy_delta(TEMPLATE, dest, manifest=rep)["manifest_verified"]


def test_review_copy_rejects_target_thread_contract_tampering(tmp_path: Path):
    out = tmp_path / "review-only.xlsx"
    dest, _ = rc.generate_review_copy(TEMPLATE, out)
    base = _members(dest)
    part = base["xl/threadedComments/threadedComment1.xml"]

    def expect_failure(label: str, mutated: dict[str, bytes], match: str) -> None:
        tampered = tmp_path / (label + ".xlsx")
        _rewrite_package(dest, tampered, mutated)
        with pytest.raises(WorkbookContractError, match=match):
            rc.verify_review_copy_delta(TEMPLATE, tampered)

    # Duplicate root comment (new id, same cell).
    root = ET.fromstring(part)
    first_root = next(c for c in root.findall("tc:threadedComment", NS) if "parentId" not in c.attrib)
    dup = dict(base)
    clone = ET.SubElement(root, f"{{{rc._THREADED_NS}}}threadedComment")
    clone.attrib.update(first_root.attrib)
    clone.attrib["id"] = "{55555555-5555-5555-5555-555555555555}"
    dup["xl/threadedComments/threadedComment1.xml"] = ET.tostring(root, encoding="utf-8")
    expect_failure("dup_root", dup, "duplicate threaded root")

    # Unknown person on an original (non-review) comment.
    root2 = ET.fromstring(part)
    original = next(c for c in root2.findall("tc:threadedComment", NS) if c.attrib.get("personId") != rc.REVIEW_PERSON["id"])
    original.set("personId", "{00000000-0000-0000-0000-000000000000}")
    unknown = dict(base)
    unknown["xl/threadedComments/threadedComment1.xml"] = ET.tostring(root2, encoding="utf-8")
    expect_failure("unknown_person", unknown, "unknown person")

    # Unresolved parentId added to an original root comment.
    root3 = ET.fromstring(part)
    original_comment = next(c for c in root3.findall("tc:threadedComment", NS)
                            if "parentId" not in c.attrib and c.attrib.get("personId") != rc.REVIEW_PERSON["id"])
    original_comment.set("parentId", "{00000000-0000-0000-0000-000000000000}")
    badparent = dict(base)
    badparent["xl/threadedComments/threadedComment1.xml"] = ET.tostring(root3, encoding="utf-8")
    expect_failure("bad_parent", badparent, "does not resolve")

    # Relationship TargetMode drifted to External.
    rel_mut = dict(base)
    rel_mut["xl/worksheets/_rels/sheet1.xml.rels"] = rel_mut["xl/worksheets/_rels/sheet1.xml.rels"].replace(
        b'Target="../threadedComments/threadedComment1.xml"',
        b'Target="../threadedComments/threadedComment1.xml" TargetMode="External"', 1)
    expect_failure("targetmode", rel_mut, "TargetMode is not Internal")

    # Content types drop the persons part.
    ct_mut = dict(base)
    assert b"application/vnd.ms-excel.person+xml" in ct_mut["[Content_Types].xml"]
    ct_mut["[Content_Types].xml"] = ct_mut["[Content_Types].xml"].replace(
        b"application/vnd.ms-excel.person+xml", b"application/vnd.ms-excel.other+xml", 1)
    expect_failure("ct_person", ct_mut, "content types no longer declare the persons part")


def test_review_copy_draft_check_immune_to_et_namespace_registration(tmp_path: Path):
    import xml.etree.ElementTree as ET
    # Other test modules register OOXML prefixes at collection/run time (the
    # openpyxl register_namespace set). The draft-drift check must stay
    # deterministic regardless of that process-global state.
    ET.register_namespace("s", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    ET.register_namespace("x14", "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main")
    out = tmp_path / "review-only.xlsx"
    dest, manifest = rc.generate_review_copy(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert rc.verify_review_copy_delta(TEMPLATE, dest, manifest=manifest)["valid"]


def test_review_copy_et_namespace_registry_restored_after_inspection(tmp_path: Path, monkeypatch):
    import xml.etree.ElementTree as ET
    # A caller's registration must survive generation and verification, and the
    # exact prior registry must be restored even when the live inspection raises.
    ET.register_namespace("zz", "http://example.invalid/review-copy-zz")
    before = dict(ET._namespace_map)
    out = tmp_path / "review-only.xlsx"
    dest, _ = rc.generate_review_copy(TEMPLATE, out)
    assert rc.verify_review_copy_delta(TEMPLATE, dest)["valid"]
    assert ET._namespace_map == before
    assert "http://example.invalid/review-copy-zz" in ET._namespace_map

    def boom(path, verify_pin=True):
        raise RuntimeError("inspection failed")

    monkeypatch.setattr(rc, "inspect_trial_guidance", boom)
    with pytest.raises(RuntimeError, match="inspection failed"):
        rc.generate_review_copy(TEMPLATE, tmp_path / "boom.xlsx")
    assert ET._namespace_map == before
