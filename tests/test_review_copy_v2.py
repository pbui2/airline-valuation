"""Review-only v2 Ginzu XLSX copy: exact 7-part delta with display repair.

These tests verify the v2 review-only copy built by
``src/airline_valuation/review_copy_v2.py`` against the pinned
fcffsimpleginzu.xlsx source and the v2 scope/input-policy packet.

Acceptance checks (AC.1-AC.3) of the approved display-repair plan:

* AC.1 — the copy is an exact 7-part delta (two threaded-comment parts, the
  persons part, two legacy comment parts and two VML drawings); it adds 21 new
  threaded roots (5 Input + 16 Cost) and 37 replies, and for each new root one
  unique legacy placeholder (author ``tc={uid}``, matching ``xr:uid``/``ref``)
  and one unique VML note shape (per-sheet note template, shape id above the
  source idmap block's maximum, translated anchor, zero-based ClientData).
* AC.2 — every other ZIP member, and every original threaded comment, legacy
  placeholder/author and VML shape, is preserved byte-for-byte.
* AC.3 — the copy fails closed on every mutation: uid, authorId, shapeId,
  anchor, idmap, cell/ref, duplicate placeholder/shape, malformed preexisting
  source elements, threaded/text/person drift, container/source/manifest/packet
  drift and output aliasing. Semantic validation runs independently of byte
  reconstruction and also without a manifest.

The v1 review copy (``review_copy.py``) tests remain untouched and still pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import airline_valuation.review_copy as rc
import airline_valuation.review_copy_v2 as rcv2
from airline_valuation.workbook import WorkbookContractError, _checked_zip

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
SCOPE_JSON = ROOT / "docs/policy-drafts/ginzu-review-scope-v2.json"
POLICY_JSON = ROOT / "docs/policy-drafts/ginzu-input-policy-v2.json"
POLICY_MD = ROOT / "docs/ginzu-input-policy-v2.md"
CONTRACT = ROOT / "src/airline_valuation/detailed_contract.json"
V1_ARTIFACT = ROOT / "review-artifacts/ginzu-policy-proposed-review-only.xlsx"
V1_MANIFEST = ROOT / "review-artifacts/ginzu-policy-proposed-review-only-manifest.json"
NS = {"tc": "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"}

# Per-sheet display wiring, mirroring the pinned source.
SHEET_VML = {
    "xl/threadedComments/threadedComment1.xml": "xl/drawings/vmlDrawing1.vml",
    "xl/threadedComments/threadedComment5.xml": "xl/drawings/vmlDrawing5.vml",
}
SHEET_COMMENTS = {
    "xl/threadedComments/threadedComment1.xml": "xl/comments1.xml",
    "xl/threadedComments/threadedComment5.xml": "xl/comments5.xml",
}

# AC.1 independent fixed expectations, hand-derived from the pinned source —
# never read from the generator or the manifest. The 21 new root references and
# their 5/16 Input/Cost distribution, plus — for the representative cells B13,
# C13 and H21..H31 — the deterministic VML shape id and translated x:Anchor
# computed by shifting the pinned B7 template (Input, anchor `2,15,5,6,7,119,25,10`
# at zero-based cell 6,1) or the B23 template (Cost, anchor `2,15,21,5,7,42,41,14`
# at zero-based cell 22,1) by the target cell's row/col delta (dx/dy fixed).
AC1_ROOT_REFS = {
    "Input sheet!B13", "Input sheet!C13", "Input sheet!C19", "Input sheet!C20", "Input sheet!C21",
    "Cost of capital worksheet!B11", "Cost of capital worksheet!B21",
    "Cost of capital worksheet!B48", "Cost of capital worksheet!B49", "Cost of capital worksheet!B50",
    "Cost of capital worksheet!H21", "Cost of capital worksheet!H22", "Cost of capital worksheet!H23",
    "Cost of capital worksheet!H24", "Cost of capital worksheet!H25", "Cost of capital worksheet!H26",
    "Cost of capital worksheet!H27", "Cost of capital worksheet!H28", "Cost of capital worksheet!H29",
    "Cost of capital worksheet!H30", "Cost of capital worksheet!H31",
}
AC1_INPUT_ROOT_CELLS = {"B13", "C13", "C19", "C20", "C21"}  # exactly five
AC1_COST_ROOT_CELLS = {"B11", "B21", "B48", "B49", "B50",
                       "H21", "H22", "H23", "H24", "H25", "H26", "H27", "H28", "H29", "H30", "H31"}  # exactly sixteen
# Fixed root reference -> (VML shape id, translated x:Anchor).
AC1_FIXED_BINDINGS = {
    "Input sheet!B13": (1234, "2, 15, 11, 6, 7, 119, 31, 10"),
    "Input sheet!C13": (1235, "3, 15, 11, 6, 8, 119, 31, 10"),
    "Cost of capital worksheet!H21": (9256, "8, 15, 19, 5, 13, 42, 39, 14"),
    "Cost of capital worksheet!H22": (9257, "8, 15, 20, 5, 13, 42, 40, 14"),
    "Cost of capital worksheet!H23": (9258, "8, 15, 21, 5, 13, 42, 41, 14"),
    "Cost of capital worksheet!H24": (9259, "8, 15, 22, 5, 13, 42, 42, 14"),
    "Cost of capital worksheet!H25": (9260, "8, 15, 23, 5, 13, 42, 43, 14"),
    "Cost of capital worksheet!H26": (9261, "8, 15, 24, 5, 13, 42, 44, 14"),
    "Cost of capital worksheet!H27": (9262, "8, 15, 25, 5, 13, 42, 45, 14"),
    "Cost of capital worksheet!H28": (9263, "8, 15, 26, 5, 13, 42, 46, 14"),
    "Cost of capital worksheet!H29": (9264, "8, 15, 27, 5, 13, 42, 47, 14"),
    "Cost of capital worksheet!H30": (9265, "8, 15, 28, 5, 13, 42, 48, 14"),
    "Cost of capital worksheet!H31": (9266, "8, 15, 29, 5, 13, 42, 49, 14"),
}

# AC.2 / AC.3 hard-coded baseline hashes (source pinned constants).
V1_ARTIFACT_SHA256 = "11e302d19041d8d33d9dce71a4a5312576d979426c235c738308ddee6c22bef9"
V1_MANIFEST_SHA256 = "fd5000675ba90bb7a57559580404ce70a1706889e8c464568fae38fc70d756cc"
CONTRACT_SHA256 = "733aa532dffb89a5e4142bdc6851ec3bd255c0684bc5197a937d864d2c95fc8e"
V2_ARTIFACT_SHA256 = "85a780584de8ece3350cc12b418129c70533c85300ce7c35eafe587cd3db22b1"
OLD_V2_ARTIFACT_SHA256 = "3b0926ab253c985e0450126298119eaa1d63d71ddb3f783ffc78723f9c3ff43c"
V2_ARTIFACT = ROOT / "review-artifacts/ginzu-policy-proposed-review-only-v2.xlsx"
V2_MANIFEST = ROOT / "review-artifacts/ginzu-policy-proposed-review-only-v2-manifest.json"
CURRENT_DOCS = ("RUNBOOK.md", "PROGRESS.md", "BACKLOG.md", "CHANGES.md", "MAINTENANCE_CHECKLIST.md")

# The exact generated pending value of the manifest's human Excel display field
# ([MS-XLSX] display confirmation still pending; never an accepted/complete claim).
EXPECTED_PENDING_DISPLAY = ("pending — open in desktop Excel to confirm threaded comment rendering, "
                            "reply threading, the added review author, and the new VML comment boxes")

# Contradictory accepted/passed display claims to reject in the current v2
# display-repair passage: an explicit manifest-field accepted value, or a
# positive assertion that the v2 Excel display/rendering was accepted, confirmed,
# verified or passed. Negative contexts ("display is NOT accepted", "do not call
# ... display accepted", "never ... accepted") must NOT be rejected, so each
# match is skipped when a negation token appears just before it. Historical v1
# statements are out of scope because only the current-v2-hash passage is scanned.
_ACCEPTED_DISPLAY_CLAIM_RE = re.compile(
    r"(manual_excel_review_copy_display\s*[:=]\s*(accepted|complete|confirmed|passed|verified)\b)"
    r"|(\b(?:excel\s+)?(?:display|rendering)\s+(?:was|is|has been|were)\s+(?:accepted|confirmed|verified|passed)\b)"
    r"|(\b(?:display|rendering)\s+accepted\b)"
    r"|(\bdisplay\s+passed\b)"
    r"|(\brendering\s+(?:confirmed|verified|passed)\b)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(not|never|no|without|deny|denied|refuse|refused|unverified)\b", re.IGNORECASE)


def _v2_display_passages(text: str) -> list[str]:
    """The current v2 display-repair passage(s) in a doc: the region around each
    occurrence of the current v2 artifact hash (the hash only appears in current
    display-repair content, so historical v1 text is never scanned)."""
    return [text[max(0, m.start() - 200): m.start() + 1200]
            for m in re.finditer(re.escape(V2_ARTIFACT_SHA256), text)]


def _contains_contradictory_accepted(text: str) -> bool:
    """True when ``text`` asserts the v2 Excel display was accepted/confirmed/
    verified/passed (positive claim), skipping matches that are negated within
    the preceding clause (e.g. "display is NOT accepted", "do not call the
    copy's Excel display accepted")."""
    for match in _ACCEPTED_DISPLAY_CLAIM_RE.finditer(text):
        before = text[max(0, match.start() - 60): match.start()]
        if _NEGATION_RE.search(before):
            continue
        return True
    return False


def _assert_current_docs_display_pending(name: str, text: str) -> None:
    """Fail unless the current v2 display-repair passage in ``name`` states a
    pending display status and carries no contradictory accepted/passed claim."""
    passages = _v2_display_passages(text)
    assert passages, f"{name}: current v2 artifact hash missing"
    for passage in passages:
        assert re.search(r"\bpending\b", passage, re.IGNORECASE), \
            f"{name}: current v2 display passage does not state pending display"
        assert not _contains_contradictory_accepted(passage), \
            f"{name}: current v2 display passage claims the Excel display accepted/passed"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_b(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy() -> dict:
    return json.loads(POLICY_JSON.read_text(encoding="utf-8"))


def _packet_hashes() -> dict:
    return {
        "scope_rel": str(rcv2.SCOPE_JSON),
        "scope_sha": rcv2.SCOPE_JSON_SHA256,
        "policy_rel": str(rcv2.INPUT_POLICY_JSON),
        "policy_sha": rcv2.INPUT_POLICY_JSON_SHA256,
        "md_rel": str(rcv2.INPUT_POLICY_MD),
        "md_sha": rcv2.INPUT_POLICY_MD_SHA256,
    }


def _refs() -> list[str]:
    return [entry["reference"] for entry in _policy()["entries"]]


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rewrite_package(source: Path, destination: Path, updates: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as out:
        for info in src.infolist():
            out.writestr(info, updates.get(info.filename, src.read(info.filename)))


def _new_comments(part: bytes) -> list[ET.Element]:
    root = ET.fromstring(part)
    return [c for c in root.findall("tc:threadedComment", NS) if c.attrib.get("personId") == rcv2.REVIEW_PERSON["id"]]


def _v1_new_comments(part: bytes) -> list[ET.Element]:
    root = ET.fromstring(part)
    return [c for c in root.findall("tc:threadedComment", NS) if c.attrib.get("personId") == rc.REVIEW_PERSON["id"]]


def _existing_roots(part: bytes) -> dict[str, str]:
    root = ET.fromstring(part)
    return {c.attrib["ref"]: c.attrib["id"] for c in root.findall("tc:threadedComment", NS) if "parentId" not in c.attrib}


def _note_text(comment: ET.Element) -> str:
    text_node = next((child for child in comment if child.tag.rsplit("}", 1)[-1] == "text"), None)
    return "".join(text_node.itertext()) if text_node is not None else ""


def _comment_uids(part: bytes) -> list[str]:
    """All xr:uid values (the threaded comment linkage) in a legacy comments part."""
    root = ET.fromstring(part)
    xr = "http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return [c.attrib.get(f"{{{xr}}}uid") for c in root.findall(f".//{{{main}}}comment")]


def _authors_of(part: bytes) -> list[str]:
    root = ET.fromstring(part)
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return [a.text or "" for a in root.findall(f".//{{{main}}}author")]


def _vml_shape_blocks(part: bytes) -> dict[int, bytes]:
    text = part.decode("utf-8")
    blocks = {}
    for m in re_iter(r'<v:shape\s+id="_x0000_s(\d+)".*?</v:shape>', text):
        blocks[int(m.group(1))] = m.group(0).encode("utf-8")
    return blocks


def re_iter(pattern: str, text: str):
    import re
    for m in re.finditer(pattern, text, re.S):
        yield m


def _shape_summary(block: bytes) -> dict:
    import re
    text = block.decode("utf-8")
    return {
        "row": int(re.search(r"<x:Row>(\d+)</x:Row>", text).group(1)),
        "col": int(re.search(r"<x:Column>(\d+)</x:Column>", text).group(1)),
        "anchor": re.sub(r"\s+", " ", re.search(r"<x:Anchor>\s*([\d,\s]+?)\s*</x:Anchor>", text, re.S).group(1)).strip(),
        "z": int(re.search(r"z-index:(\d+)", text).group(1)),
    }


def _manifest_notes(manifest: Path) -> list[dict]:
    return json.loads(manifest.read_text(encoding="utf-8"))["notes"]


def test_v2_review_copy_exact_delta_and_notes(tmp_path: Path):
    out = tmp_path / "review-only-v2.xlsx"
    dest, manifest = rcv2.generate_review_copy_v2(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert _sha(TEMPLATE) == rcv2.SOURCE_SHA256
    review = rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=manifest)
    assert review["valid"]
    assert review["reference_count"] == 58
    assert review["changed_parts"] == sorted(rcv2.PERMITTED_PARTS) == sorted({
        "xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml",
        "xl/persons/person.xml", "xl/comments1.xml", "xl/comments5.xml",
        "xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"})
    assert review["replies"] == 37 and review["new_roots"] == 21
    assert review["new_legacy_placeholders"] == 21 and review["new_vml_shapes"] == 21
    assert review["replies"] + review["new_roots"] == 58
    before, after = _members(TEMPLATE), _members(dest)
    # AC.1 exact delta: only the seven permitted parts changed.
    assert {name for name in before if before[name] != after[name]} == rcv2.PERMITTED_PARTS
    # Threaded note distribution: 38 Input-sheet notes (33 replies + 5 new roots)
    # in threadedComment1, 20 Cost notes (4 replies + 16 new roots) in threadedComment5.
    threaded1 = _new_comments(after["xl/threadedComments/threadedComment1.xml"])
    threaded5 = _new_comments(after["xl/threadedComments/threadedComment5.xml"])
    assert len(threaded1) == 38
    assert len(threaded5) == 20
    covered = {c.attrib.get("ref") for c in threaded1 + threaded5}
    assert covered == {ref.rsplit("!", 1)[1] for ref in _refs()}
    # Every comment id uses the distinct v2 UUID namespace (never the v1 one),
    # and the v2 person is distinct from the v1 review person.
    assert {c.attrib["id"] for c in threaded1 + threaded5} == {rcv2._comment_id(ref) for ref in _refs()}
    assert rcv2._comment_id("Input sheet!B8") != rc._comment_id("Input sheet!B8")
    assert rcv2.REVIEW_PERSON["id"] != rc.REVIEW_PERSON["id"]
    assert all(c.attrib.get("personId") == rcv2.REVIEW_PERSON["id"] for c in threaded1 + threaded5)
    # Every note carries the v2 nonapproval banner and is not a silent v1 note.
    for comment in threaded1 + threaded5:
        text = _note_text(comment)
        assert rcv2.BANNER in text
        assert "REVIEW ONLY" in text and "NOT APPROVED / NOT OPERATIONAL" in text
    # Reply/root topology matches the pinned source threads: existing roots get
    # replies, cells without a root become new roots.
    source_roots1 = _existing_roots(before["xl/threadedComments/threadedComment1.xml"])
    source_roots5 = _existing_roots(before["xl/threadedComments/threadedComment5.xml"])
    for comment in threaded1 + threaded5:
        cell = comment.attrib["ref"]
        part_roots = source_roots1 if comment in threaded1 else source_roots5
        if cell in part_roots:
            assert comment.attrib.get("parentId") == part_roots[cell]
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
    # AC.1 display repair: exactly one legacy placeholder and one VML shape per
    # new root (5 on the Input sheet, 16 on the Cost sheet), none for replies.
    notes = _manifest_notes(manifest)
    root_notes = [n for n in notes if n["thread"] == "root"]
    reply_notes = [n for n in notes if n["thread"] == "reply"]
    assert len(root_notes) == 21 and len(reply_notes) == 37
    assert sum(1 for n in root_notes if n["part"] == "xl/threadedComments/threadedComment1.xml") == 5
    assert sum(1 for n in root_notes if n["part"] == "xl/threadedComments/threadedComment5.xml") == 16
    assert all(n["shape_id"] is None and n["anchor"] is None and n["placeholder_sha256"] is None
               and n["shape_sha256"] is None and n["legacy_author_id"] is None for n in reply_notes)
    assert all(n["shape_id"] is not None and n["anchor"] is not None and n["placeholder_sha256"] is not None
               and n["shape_sha256"] is not None and n["legacy_author_id"] is not None for n in root_notes)
    # Legacy comment parts: source 58/9 authors+comments plus the new roots only.
    for part, base, vml_part in (("xl/comments1.xml", 58, "xl/drawings/vmlDrawing1.vml"),
                                 ("xl/comments5.xml", 9, "xl/drawings/vmlDrawing5.vml")):
        n_roots = len([n for n in root_notes if SHEET_COMMENTS[n["part"]] == part])
        assert len(_authors_of(after[part])) == base + n_roots
        assert len(_comment_uids(after[part])) == base + n_roots
        assert len(_vml_shape_blocks(after[vml_part])) == base + n_roots
    # VML shape counts and idmap preservation.
    assert len(_vml_shape_blocks(after["xl/drawings/vmlDrawing1.vml"])) == 63
    assert len(_vml_shape_blocks(after["xl/drawings/vmlDrawing5.vml"])) == 25
    for vml_part in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"):
        assert b'<o:idmap v:ext="edit" data="' in after[vml_part]
    # Every new root placeholder: author tc={uid}, matching xr:uid/ref/shapeId;
    # every new VML shape: id above the source block, translated anchor and
    # zero-based ClientData at the note's cell.
    import re as _re
    for note in root_notes:
        comments_part = SHEET_COMMENTS[note["part"]]
        vml_part = SHEET_VML[note["part"]]
        c_bytes = after[comments_part]
        comment_m = _re.search(r'<comment ref="%s"[^>]*xr:uid="%s"[^>]*>.*?</comment>' % (note["cell"], note["comment_id"]), c_bytes.decode("utf-8"), _re.S)
        assert comment_m is not None, note["reference"]
        elem = comment_m.group(0)
        assert 'authorId="%d"' % note["legacy_author_id"] in elem
        # The pinned source repeats shapeId="0" on every placeholder; the new
        # placeholders follow that convention (never a unique VML id).
        assert 'shapeId="0"' in elem
        assert "tc=%s" % note["comment_id"] in _authors_of(c_bytes)
        block = _vml_shape_blocks(after[vml_part])[note["shape_id"]]
        summary = _shape_summary(block)
        row0, col0 = rcv2._cell_rc(note["cell"])
        assert summary["row"] == row0 and summary["col"] == col0
        assert summary["anchor"] == note["anchor"] == rcv2._translate_anchor(vml_part, note["cell"])
        assert summary["z"] == note["z_index"]
    # The exact full note text (v2 banner, issue-specific question, v2 JSON/MD
    # locators and hashes) is what lands in the workbook, untruncated.
    policy = _policy()
    hashes = _packet_hashes()
    by_ref = {entry["reference"]: entry for entry in policy["entries"]}
    note = rcv2.build_note(by_ref["Input sheet!B8"], policy, hashes)
    b8 = [c for c in _new_comments(after["xl/threadedComments/threadedComment1.xml"]) if c.attrib.get("ref") == "B8"]
    assert len(b8) == 1
    assert _note_text(b8[0]) == note
    # The v2 note is distinct from the v1 note for the same cell.
    v1_draft = json.loads((ROOT / "docs/policy-drafts/ginzu-input-policy-v1.json").read_text(encoding="utf-8"))
    v1_by_ref = {entry["reference"]: entry for entry in v1_draft["entries"]}
    v1_hashes = {"json_rel": "docs/policy-drafts/ginzu-input-policy-v1.json", "json_sha": rc.DRAFT_JSON_SHA256,
                 "md_rel": "docs/ginzu-input-policy-v1.md", "md_sha": rc.DRAFT_MD_SHA256}
    assert note != rc.build_note(v1_by_ref["Input sheet!B8"], v1_draft, v1_hashes)
    # Every note carries its v2 locators and pinned hashes.
    for ref, built in {entry["reference"]: rcv2.build_note(entry, policy, hashes) for entry in policy["entries"]}.items():
        assert "Scope JSON: docs/policy-drafts/ginzu-review-scope-v2.json (sha256=%s)" % rcv2.SCOPE_JSON_SHA256 in built, ref
        assert "Policy JSON: docs/policy-drafts/ginzu-input-policy-v2.json (sha256=%s)" % rcv2.INPUT_POLICY_JSON_SHA256 in built, ref
        assert "Policy MD: docs/ginzu-input-policy-v2.md (sha256=%s)" % rcv2.INPUT_POLICY_MD_SHA256 in built, ref
        assert "Review question (v2):" in built and "?" in built, ref
        assert "…" not in built, ref
        assert len(built) <= 32767 - 1000, ref


def test_v2_review_copy_fixed_root_bindings_and_anchors(tmp_path: Path):
    """AC.1: the 21 new roots and their exact 5/16 Input/Cost distribution, plus
    the fixed root/placeholder/shape binding and anchor coordinates for the
    representative cells B13, C13 and H21..H31, asserted against hand-derived
    constants from the pinned B7/B23 templates — never read from the generator
    or the manifest for the expected values."""
    out = tmp_path / "review-only-v2.xlsx"
    dest, _manifest = rcv2.generate_review_copy_v2(TEMPLATE, out)
    rcv2.verify_review_copy_delta_v2(TEMPLATE, dest)
    after = _members(dest)
    # Fixed 21-root reference set, parsed from the generated workbook.
    threaded1 = _new_comments(after["xl/threadedComments/threadedComment1.xml"])
    threaded5 = _new_comments(after["xl/threadedComments/threadedComment5.xml"])
    root_cells = {c.attrib["ref"] for c in threaded1 + threaded5 if "parentId" not in c.attrib}
    input_root_cells = {c.attrib["ref"] for c in threaded1 if "parentId" not in c.attrib}
    cost_root_cells = {c.attrib["ref"] for c in threaded5 if "parentId" not in c.attrib}
    assert len(input_root_cells) == 5 and input_root_cells == AC1_INPUT_ROOT_CELLS
    assert len(cost_root_cells) == 16 and cost_root_cells == AC1_COST_ROOT_CELLS
    assert len(root_cells) == 21 and root_cells == {ref.rsplit("!", 1)[1] for ref in AC1_ROOT_REFS}
    for ref, (expected_shape_id, expected_anchor) in AC1_FIXED_BINDINGS.items():
        cell = ref.rsplit("!", 1)[1]
        sheet = ref.split("!", 1)[0]
        part = "xl/threadedComments/threadedComment1.xml" if sheet == "Input sheet" else "xl/threadedComments/threadedComment5.xml"
        comments_part, vml_part = SHEET_COMMENTS[part], SHEET_VML[part]
        # Root binding: the cell is a new root with the deterministic v2 comment id.
        roots = [c for c in _new_comments(after[part]) if c.attrib.get("ref") == cell and "parentId" not in c.attrib]
        assert len(roots) == 1, ref
        assert roots[0].attrib["id"] == rcv2._comment_id(ref), ref
        # Placeholder binding: ref/xr:uid/shapeId/author tc= match the fixed root.
        import re as _re
        c_bytes = after[comments_part].decode("utf-8")
        ph = _re.search(r'<comment ref="%s"[^>]*xr:uid="%s"[^>]*>.*?</comment>' % (cell, rcv2._comment_id(ref)), c_bytes, _re.S)
        assert ph is not None, ref
        elem = ph.group(0)
        assert 'shapeId="0"' in elem, ref
        assert "tc=%s" % rcv2._comment_id(ref) in _authors_of(after[comments_part]), ref
        # Shape binding: the shape exists with the fixed shape id, the cell's
        # zero-based ClientData and the fixed translated anchor.
        block = _vml_shape_blocks(after[vml_part])[expected_shape_id]
        summary = _shape_summary(block)
        row0, col0 = rcv2._cell_rc(cell)
        assert summary["row"] == row0 and summary["col"] == col0, ref
        assert summary["anchor"] == expected_anchor, ref


def test_v2_review_copy_pins_baseline_hashes():
    """AC.2: hard-coded baseline hashes — the v1 review-only artifact, the v1
    manifest and the detailed contract (source pinned constant) — must match the
    committed files byte-for-byte. These are independent oracles, not derived
    from the v2 generator or manifest."""
    assert _sha(V1_ARTIFACT) == V1_ARTIFACT_SHA256
    assert _sha(V1_MANIFEST) == V1_MANIFEST_SHA256
    assert _sha(CONTRACT) == CONTRACT_SHA256
    assert _sha(TEMPLATE) == rcv2.SOURCE_SHA256


def test_v2_committed_artifact_matches_generated_and_docs_current():
    """AC.3: the committed v2 artifact/manifest in review-artifacts/ are the
    current seven-part display-repaired output with the fixed SHA; the committed
    manifest matches the generated expected document; and the current docs
    describe the seven-part / NOT APPROVED / pending state (not an old 3-part or
    accepted claim). Targeted only at the five current docs."""
    # Fixed output SHA-256 and rejection of the old 3-part-era artifact.
    assert _sha(V2_ARTIFACT) == V2_ARTIFACT_SHA256
    assert _sha(V2_ARTIFACT) != OLD_V2_ARTIFACT_SHA256
    assert len(rcv2.PERMITTED_PARTS) == 7
    # The committed manifest verifies the committed artifact exactly.
    review = rcv2.verify_review_copy_delta_v2(TEMPLATE, V2_ARTIFACT, manifest=V2_MANIFEST)
    assert review["valid"] and review["manifest_verified"] is True
    assert review["changed_parts"] == sorted(rcv2.PERMITTED_PARTS)
    assert len(review["changed_parts"]) == 7
    assert review["output_sha256"] == V2_ARTIFACT_SHA256
    # The committed manifest document is the seven-part / NOT APPROVED / pending
    # schema-v3 document, never an old 3-part or accepted claim. The human Excel
    # display field must equal the generated expected pending value exactly.
    doc = json.loads(V2_MANIFEST.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "ginzu-policy-review-copy/3"
    assert len(doc["changed_parts"]) == 7
    assert "NOT APPROVED" in doc["status"]
    assert doc["output"]["sha256"] == V2_ARTIFACT_SHA256
    assert doc["manual_excel_review_copy_display"] == EXPECTED_PENDING_DISPLAY
    # The exact generated value is also what the generator's manifest builder
    # emits for the committed artifact (verified end-to-end by manifest_verified
    # above); the literal is pinned here as an independent oracle.
    assert doc["manual_excel_review_copy_display"].startswith("pending")
    # Targeted current-docs status: the pending statement is bound to the current
    # v2 display-repair passage (the region around the current v2 artifact hash),
    # which must state pending and carry no contradictory accepted/passed claim.
    # Historical v1 text and the negative phrase "not accepted" are not rejected.
    for name in CURRENT_DOCS:
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        _assert_current_docs_display_pending(name, text)


def test_v2_current_docs_reject_false_accepted_status():
    """AC.3 isolated fixture: the strengthened current-docs assertion rejects a
    contradictory accepted/passed display claim bound to the current v2 passage,
    accepts the valid pending statement, and does not reject the negative phrase
    'not accepted' or a historical v1 statement outside the current passage."""
    valid = (
        f"The v2 copy changes exactly seven package parts; output XLSX `{V2_ARTIFACT_SHA256}`, "
        "manifest `02aabbf664ea08d696e2184a027d6aad3cbe7eb455bf52cbbd16f99ea95ec9f9`. "
        "Desktop Excel rendering remains **pending manual confirmation** "
        "(`manual_excel_review_copy_display: pending`); do not call the copy's Excel display accepted, "
        "and do not treat rendering as any approval. NOT APPROVED."
    )
    # Valid current passage (pending + negative phrasing) must pass.
    _assert_current_docs_display_pending("valid", valid)
    # A falsely accepted manifest-style value must fail.
    false_accepted = valid.replace("manual_excel_review_copy_display: pending",
                                   "manual_excel_review_copy_display: accepted")
    with pytest.raises(AssertionError, match="accepted/passed"):
        _assert_current_docs_display_pending("false-accepted-field", false_accepted)
    # A positive 'the v2 Excel display was accepted and verified' claim must fail.
    false_was = valid.replace(
        "do not call the copy's Excel display accepted, and do not treat rendering as any approval.",
        "the v2 Excel display was accepted and verified in desktop Excel.")
    with pytest.raises(AssertionError, match="accepted/passed"):
        _assert_current_docs_display_pending("false-was-accepted", false_was)
    # A positive 'rendering confirmed' claim must fail.
    false_rendering = valid.replace(
        "Desktop Excel rendering remains **pending manual confirmation**",
        "Desktop Excel rendering was confirmed in a manual review")
    with pytest.raises(AssertionError, match="accepted/passed"):
        _assert_current_docs_display_pending("false-rendering-confirmed", false_rendering)
    # A doc without the current v2 passage (no current hash) must fail.
    with pytest.raises(AssertionError, match="hash missing"):
        _assert_current_docs_display_pending("no-current-passage", "Historical v1 text only, no current hash.")
    # Negative phrasing ("display is NOT accepted") must not be rejected.
    negative_ok = valid.replace(
        "do not call the copy's Excel display accepted, and do not treat rendering as any approval.",
        "the copy's Excel display is NOT accepted and no approval is claimed.")
    _assert_current_docs_display_pending("negative-ok", negative_ok)
    # A historical v1 accepted statement far from the current passage (outside
    # the scanned window around the current hash) must not be rejected.
    historical_prefix = ("Previously the v1 review copy's Excel display was confirmed accepted in desktop Excel. "
                         + " " * 300)  # padding moves the historical text out of the current-passage window
    _assert_current_docs_display_pending("historical-ok", historical_prefix + valid)


def test_v2_review_copy_preserves_originals(tmp_path: Path):
    out = tmp_path / "review-only-v2.xlsx"
    dest, _ = rcv2.generate_review_copy_v2(TEMPLATE, out)
    before, after = _members(TEMPLATE), _members(dest)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == rcv2.PERMITTED_PARTS
    # Every original threaded comment (same id) keeps its exact text and author.
    for part in ("xl/threadedComments/threadedComment1.xml", "xl/threadedComments/threadedComment5.xml"):
        source_root = ET.fromstring(before[part])
        dest_root = ET.fromstring(after[part])
        dest_by_id = {c.attrib["id"]: c for c in dest_root.findall("tc:threadedComment", NS)}
        for comment in source_root.findall("tc:threadedComment", NS):
            preserved = dest_by_id[comment.attrib["id"]]
            assert _note_text(comment) == _note_text(preserved)
            assert preserved.attrib.get("personId") == comment.attrib.get("personId")
    # AC.2: original legacy authors and placeholders are preserved verbatim.
    for part in ("xl/comments1.xml", "xl/comments5.xml"):
        src_authors = _authors_of(before[part])
        dst_authors = _authors_of(after[part])
        assert dst_authors[: len(src_authors)] == src_authors
        src_uids = _comment_uids(before[part])
        dst_uids = _comment_uids(after[part])
        assert dst_uids[: len(src_uids)] == src_uids
    # AC.2: original VML shapes are preserved verbatim (same shape id, same
    # anchor / ClientData), and new shapes are appended after them.
    for vml_part in ("xl/drawings/vmlDrawing1.vml", "xl/drawings/vmlDrawing5.vml"):
        src_blocks = _vml_shape_blocks(before[vml_part])
        dst_blocks = _vml_shape_blocks(after[vml_part])
        assert set(src_blocks) <= set(dst_blocks)
        for sid, block in src_blocks.items():
            assert dst_blocks[sid] == block
    # Only the two VML drawings, the two legacy comment parts, the two threaded
    # parts and the persons part changed; all other members are byte-identical.
    untouched = [name for name in before if name not in rcv2.PERMITTED_PARTS]
    assert all(after[name] == before[name] for name in untouched)
    for name in ("xl/comments2.xml", "xl/comments3.xml", "xl/comments4.xml", "xl/comments6.xml",
                 "xl/drawings/vmlDrawing2.vml", "xl/drawings/vmlDrawing3.vml", "xl/drawings/vmlDrawing4.vml",
                 "xl/drawings/vmlDrawing6.vml"):
        assert after[name] == before[name], name
    # The workbook is a pure copy: no input cells written, no formula changed.
    assert after["xl/worksheets/sheet1.xml"] == before["xl/worksheets/sheet1.xml"]
    assert after["xl/worksheets/sheet10.xml"] == before["xl/worksheets/sheet10.xml"]
    assert after["xl/sharedStrings.xml"] == before["xl/sharedStrings.xml"]
    assert after["xl/calcChain.xml"] == before["xl/calcChain.xml"]
    assert after["xl/styles.xml"] == before["xl/styles.xml"]
    # person.xml contains exactly the 3 original authors plus the distinct v2
    # review author — never the v1 review author, proving the source is the
    # pristine pinned workbook, not the v1 review copy.
    people = [p.attrib.get("id") for p in ET.fromstring(after["xl/persons/person.xml"]).findall("tc:person", NS)]
    assert len(people) == 4 and len(people) == len(set(people))
    assert rcv2.REVIEW_PERSON["id"] in people
    assert rc.REVIEW_PERSON["id"] not in people
    assert rc.REVIEW_PERSON["id"] not in [p.attrib.get("id") for p in ET.fromstring(before["xl/persons/person.xml"]).findall("tc:person", NS)]
    # None of the v1 review comments appear in the v2 output.
    assert not _v1_new_comments(after["xl/threadedComments/threadedComment1.xml"])
    assert not _v1_new_comments(after["xl/threadedComments/threadedComment5.xml"])


def test_v2_review_copy_semantic_verify_without_manifest(tmp_path: Path):
    """AC.3 semantic validation runs independently of byte reconstruction and
    also when the manifest is omitted: the verifier re-derives every expected
    note/placeholder/shape identity from the pinned source and policy."""
    out = tmp_path / "review-only-v2.xlsx"
    dest, manifest = rcv2.generate_review_copy_v2(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    review = rcv2.verify_review_copy_delta_v2(TEMPLATE, dest)
    assert review["valid"] and review["replies"] == 37 and review["new_roots"] == 21
    assert review["new_legacy_placeholders"] == 21 and review["new_vml_shapes"] == 21
    # The generated workbook already satisfies every semantic display check.
    rcv2._verify_display_in_dest(dest, _manifest_notes(manifest), rcv2._sheet_display_state(
        __import__("zipfile").ZipFile(TEMPLATE)))
    rcv2._validate_target_display_contract(dest)


def test_v2_review_copy_rejects_mutations_and_aliases(tmp_path: Path, monkeypatch):
    out = tmp_path / "review-only-v2.xlsx"
    dest, manifest = rcv2.generate_review_copy_v2(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    assert rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=manifest)["valid"]
    base = _members(dest)
    notes = _manifest_notes(manifest)
    root13 = next(n for n in notes if n["reference"] == "Input sheet!B13")
    root_b = next(n for n in notes if n["reference"] == "Input sheet!B8")  # reply
    c1 = base["xl/comments1.xml"]
    v1 = base["xl/drawings/vmlDrawing1.vml"]

    def expect_failure(label: str, mutated: dict[str, bytes] | Path, match: str) -> None:
        if isinstance(mutated, dict):
            tampered = tmp_path / (label + ".xlsx")
            _rewrite_package(dest, tampered, mutated)
        else:
            tampered = mutated
        with pytest.raises(WorkbookContractError, match=match):
            rcv2.verify_review_copy_delta_v2(TEMPLATE, tampered, manifest=manifest)

    # AC.3 negative: forbidden ZIP member (unrelated part / worksheet cell).
    unrelated = dict(base)
    unrelated["docProps/core.xml"] += b" "
    expect_failure("unrelated", unrelated, "outside the exact permitted parts")
    cell = dict(base)
    cell["xl/worksheets/sheet1.xml"] = cell["xl/worksheets/sheet1.xml"].replace(b'<c r="B8" s="192"/>', b'<c r="B8" s="192"><v>9</v></c>', 1)
    expect_failure("cell", cell, "outside the exact permitted parts")
    # AC.3 negative: threaded comment mutations (ref, parent, text, person).
    part = base["xl/threadedComments/threadedComment1.xml"]
    ref_mut = dict(base)
    ref_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(
        f'<threadedComment ref="B8" personId="{rcv2.REVIEW_PERSON["id"]}"'.encode(),
        f'<threadedComment ref="Z99" personId="{rcv2.REVIEW_PERSON["id"]}"'.encode(), 1)
    expect_failure("ref", ref_mut, "ref differs")
    parent_mut = dict(base)
    new_b8 = [c for c in _new_comments(part) if c.attrib.get("ref") == "B8"][0]
    parent_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(
        f' parentId="{new_b8.attrib["parentId"]}"'.encode(),
        b' parentId="{00000000-0000-0000-0000-000000000000}"', 1)
    expect_failure("parent", parent_mut, "parent")
    text_mut = dict(base)
    text_mut["xl/threadedComments/threadedComment1.xml"] = part.replace(b"REVIEW ONLY", b"REVIEW ONLYX", 1)
    expect_failure("text", text_mut, "text drifted")
    person_mut = dict(base)
    person_mut["xl/persons/person.xml"] = person_mut["xl/persons/person.xml"].replace(
        rcv2.REVIEW_PERSON["id"].encode(), b"{99999999-9999-9999-9999-999999999999}", 1)
    expect_failure("person", person_mut, "author")
    # AC.3 negative: placeholder identity mutations (uid, ref/cell, authorId,
    # shapeId, author tc=) on a new root placeholder.
    uid_mut = dict(base)
    uid_mut["xl/comments1.xml"] = c1.replace(
        ('xr:uid="%s"' % root13["comment_id"]).encode(), b'xr:uid="{00000000-0000-0000-0000-000000000000}"', 1)
    expect_failure("uid", uid_mut, "uid drifted")
    author_mut = dict(base)
    author_mut["xl/comments1.xml"] = c1.replace(
        ("tc=%s" % root13["comment_id"]).encode(), b"tc={00000000-0000-0000-0000-000000000000}", 1)
    expect_failure("author", author_mut, "placeholder author is not tc")
    cellref_mut = dict(base)
    cellref_mut["xl/comments1.xml"] = c1.replace(
        ('<comment ref="%s"' % root13["cell"]).encode(), ('<comment ref="Z99"').encode(), 1)
    expect_failure("cellref", cellref_mut, "ref differs")
    authorid_mut = dict(base)
    authorid_mut["xl/comments1.xml"] = c1.replace(
        ('authorId="%d"' % root13["legacy_author_id"]).encode(), b'authorId="999"', 1)
    expect_failure("authorid", authorid_mut, "authorId differs")
    shapeid_mut = dict(base)
    # The source placeholder convention repeats shapeId="0"; mutating the new
    # placeholder's shapeId away from "0" must be rejected.
    shapeid_mut["xl/comments1.xml"] = c1.replace(
        ('<comment ref="%s" authorId="%d" shapeId="0"' % (root13["cell"], root13["legacy_author_id"])).encode(),
        ('<comment ref="%s" authorId="%d" shapeId="1"' % (root13["cell"], root13["legacy_author_id"])).encode(), 1)
    expect_failure("shapeid", shapeid_mut, "shapeId differs")
    # AC.3 negative: VML mutations (anchor, ClientData cell, idmap, shape id
    # outside block, duplicate shape id).
    anchor_mut = dict(base)
    block = _vml_shape_blocks(v1)[root13["shape_id"]].decode("utf-8")
    mutated_block = block.replace(root13["anchor"] + "</x:Anchor>", "9, 9, 9, 9, 9, 9, 9, 9</x:Anchor>", 1)
    anchor_mut["xl/drawings/vmlDrawing1.vml"] = v1.replace(block.encode(), mutated_block.encode(), 1)
    expect_failure("anchor", anchor_mut, "anchor differs")
    # AC.1/AC.3 negative: a removed new-root anchor (shape deleted) is rejected.
    removed_shape = dict(base)
    b13_block = _vml_shape_blocks(v1)[root13["shape_id"]]
    removed_shape["xl/drawings/vmlDrawing1.vml"] = v1.replace(b13_block, b"", 1)
    expect_failure("removed-anchor", removed_shape, "missing|count drifted")
    # AC.1/AC.3 negative: swapped new-root anchors are rejected per cell.
    c13 = next(n for n in notes if n["reference"] == "Input sheet!C13")
    b13_anchor = root13["anchor"]
    c13_anchor = c13["anchor"]
    swapped = dict(base)
    b13_shape = _vml_shape_blocks(v1)[root13["shape_id"]]
    c13_shape = _vml_shape_blocks(v1)[c13["shape_id"]]
    swapped1 = b13_shape.replace(b13_anchor.encode(), c13_anchor.encode(), 1)
    swapped2 = c13_shape.replace(c13_anchor.encode(), b13_anchor.encode(), 1)
    swapped_vml = v1.replace(b13_shape, swapped1, 1).replace(c13_shape, swapped2, 1)
    swapped["xl/drawings/vmlDrawing1.vml"] = swapped_vml
    expect_failure("swapped-anchor", swapped, "anchor differs")
    row_mut = dict(base)
    row0, _col0 = rcv2._cell_rc(root13["cell"])
    mutated_block = block.replace("<x:Row>%d</x:Row>" % row0, "<x:Row>0</x:Row>", 1)
    row_mut["xl/drawings/vmlDrawing1.vml"] = v1.replace(block.encode(), mutated_block.encode(), 1)
    expect_failure("row", row_mut, "ClientData cell differs")
    idmap_mut = dict(base)
    idmap_mut["xl/drawings/vmlDrawing1.vml"] = v1.replace(
        b'<o:idmap v:ext="edit" data="1"/>', b'<o:idmap v:ext="edit" data="2"/>', 1)
    expect_failure("idmap", idmap_mut, "idmap drifted")
    lowid_mut = dict(base)
    lowid_mut["xl/drawings/vmlDrawing1.vml"] = v1.replace(
        ("_x0000_s%d" % root13["shape_id"]).encode(), b"_x0000_s1175", 1)
    expect_failure("lowid", lowid_mut, "shape id not above|duplicate VML shape id|missing")
    dup_shape = dict(base)
    dup_shape["xl/drawings/vmlDrawing1.vml"] = v1.replace(
        block.encode(), block.encode() + block.encode(), 1)
    expect_failure("dupshape", dup_shape, "duplicate VML shape id")
    dup_ph = dict(base)
    # Duplicate placeholder: append a second identical placeholder element.
    import re as _re
    m = _re.search(r'<comment ref="%s".*?</comment>' % root13["cell"], c1.decode("utf-8"), _re.S)
    assert m is not None
    ph_block = m.group(0).encode("utf-8")
    dup_ph["xl/comments1.xml"] = c1.replace(ph_block, ph_block + ph_block, 1)
    expect_failure("dupph", dup_ph, "duplicate legacy comment ref/uid")
    # AC.3 negative: malformed preexisting source element must fail closed even
    # before any note/shape is derived.
    src_members = _members(TEMPLATE)
    comments1 = src_members["xl/comments1.xml"]
    src_members["xl/comments1.xml"] = comments1.replace(
        b'<comment ref="B7" authorId="0"', b'<comment authorId="0"', 1)
    malformed_src = tmp_path / "malformed-src.xlsx"
    _rewrite_package(TEMPLATE, malformed_src, src_members)
    monkeypatch.setattr(rcv2, "SOURCE_SHA256", _sha(malformed_src))
    with pytest.raises(WorkbookContractError, match="legacy comment missing identity"):
        rcv2.generate_review_copy_v2(malformed_src, tmp_path / "mal-out.xlsx")
    monkeypatch.undo()
    # AC.3 negative: ambiguous threaded root source stays rejected.
    src_members = _members(TEMPLATE)
    tc1 = src_members["xl/threadedComments/threadedComment1.xml"]
    extra_root = (b'<threadedComment ref="B8" personId="{11111111-1111-1111-1111-111111111111}" '
                  b'id="{22222222-2222-2222-2222-222222222222}"><text xml:space="preserve">dup root</text></threadedComment>')
    src_members["xl/threadedComments/threadedComment1.xml"] = tc1.replace(b"</ThreadedComments>", extra_root + b"</ThreadedComments>", 1)
    ambiguous = tmp_path / "ambiguous.xlsx"
    _rewrite_package(TEMPLATE, ambiguous, src_members)
    monkeypatch.setattr(rcv2, "SOURCE_SHA256", _sha(ambiguous))
    with pytest.raises(WorkbookContractError, match="ambiguous"):
        rcv2.generate_review_copy_v2(ambiguous, tmp_path / "amb-out.xlsx")
    monkeypatch.undo()
    # AC.3 negative: container suffix and member removal.
    suffix = tmp_path / "suffix.xlsx"
    suffix.write_bytes(dest.read_bytes() + b"X")
    expect_failure("suffix", suffix, "ZIP container")
    removed = tmp_path / "removed.xlsx"
    with zipfile.ZipFile(dest) as src, zipfile.ZipFile(removed, "w") as out:
        for info in src.infolist()[:-1]:
            out.writestr(info, src.read(info.filename))
    with pytest.raises(WorkbookContractError, match="member inventory"):
        rcv2.verify_review_copy_delta_v2(TEMPLATE, removed, manifest=manifest)
    # AC.3 negative: source hash drift.
    drifted_source = tmp_path / "drifted-source.xlsx"
    drifted_source.write_bytes(TEMPLATE.read_bytes() + b"x")
    with pytest.raises(WorkbookContractError, match="source SHA-256"):
        rcv2.verify_review_copy_delta_v2(drifted_source, dest, manifest=manifest)
    # AC.3 negative: manifest drift.
    bad_manifest = tmp_path / "bad-manifest.json"
    manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_doc["output"]["sha256"] = "0" * 64
    bad_manifest.write_text(json.dumps(manifest_doc), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=bad_manifest)
    # AC.3 negative: v2 packet hash drift.
    tampered_scope = json.loads(SCOPE_JSON.read_text(encoding="utf-8"))
    tampered_scope["mapping"] = tampered_scope["mapping"][1:] + tampered_scope["mapping"][:1]
    alt_root = tmp_path / "root"
    (alt_root / "docs/policy-drafts").mkdir(parents=True, exist_ok=True)
    (alt_root / "docs").mkdir(parents=True, exist_ok=True)
    (alt_root / rcv2.SCOPE_JSON).write_text(json.dumps(tampered_scope), encoding="utf-8")
    (alt_root / rcv2.INPUT_POLICY_JSON).write_bytes(POLICY_JSON.read_bytes())
    (alt_root / rcv2.INPUT_POLICY_MD).write_bytes(POLICY_MD.read_bytes())
    with pytest.raises(WorkbookContractError, match="scope JSON drifted"):
        rcv2.generate_review_copy_v2(TEMPLATE, tmp_path / "alt.xlsx", root=alt_root)
    # AC.3 negative: alias protection (output/manifest aliasing protected files).
    before_protected = {p: _sha(p) for p in (TEMPLATE, SCOPE_JSON, POLICY_JSON, POLICY_MD, CONTRACT, V1_ARTIFACT, V1_MANIFEST)}
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rcv2.generate_review_copy_v2(TEMPLATE, TEMPLATE)
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rcv2.generate_review_copy_v2(TEMPLATE, tmp_path / "o2.xlsx", manifest=SCOPE_JSON)
    with pytest.raises(WorkbookContractError, match="aliases another output"):
        rcv2.generate_review_copy_v2(TEMPLATE, tmp_path / "o3.xlsx", manifest=tmp_path / "o3.xlsx")
    for pinned in (CONTRACT, POLICY_JSON, POLICY_MD, V1_ARTIFACT, V1_MANIFEST):
        with pytest.raises(WorkbookContractError, match="aliases the protected source"):
            rcv2.generate_review_copy_v2(TEMPLATE, pinned)
    symlink = tmp_path / "link-v2.xlsx"
    symlink.symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="aliases the protected source"):
        rcv2.generate_review_copy_v2(TEMPLATE, symlink)
    with tempfile.TemporaryDirectory(dir=str(TEMPLATE.parent)) as td:
        hardlink = Path(td) / "hard-v2.xlsx"
        os.link(TEMPLATE, hardlink)
        with pytest.raises(WorkbookContractError, match="aliases the protected source"):
            rcv2.generate_review_copy_v2(TEMPLATE, hardlink)
    (tmp_path / "alias-tmp.xlsx.tmp").symlink_to(TEMPLATE)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rcv2.generate_review_copy_v2(TEMPLATE, tmp_path / "alias-tmp.xlsx")
    (tmp_path / "alias-tmp.xlsx.tmp").unlink()
    (tmp_path / "manifest-tmp.json.tmp").symlink_to(SCOPE_JSON)
    with pytest.raises(WorkbookContractError, match="temporary path"):
        rcv2.generate_review_copy_v2(TEMPLATE, tmp_path / "alias-out.xlsx", manifest=tmp_path / "manifest-tmp.json")
    (tmp_path / "manifest-tmp.json.tmp").unlink()
    assert all(_sha(p) == before_protected[p] for p in before_protected)


def test_v2_manifest_and_document_status(tmp_path: Path):
    out = tmp_path / "review-only-v2.xlsx"
    dest, manifest = rcv2.generate_review_copy_v2(TEMPLATE, out, manifest=tmp_path / "manifest.json")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    assert doc["schema_version"] == rcv2.MANIFEST_SCHEMA == "ginzu-policy-review-copy/3"
    assert doc["status"] == rcv2.STATUS
    assert "REVIEW ONLY" in doc["status"] and "NOT APPROVED" in doc["status"]
    assert doc["source"] == {"file": rcv2.SOURCE_FILE, "sha256": rcv2.SOURCE_SHA256}
    assert doc["scope"] == {"json": str(rcv2.SCOPE_JSON), "json_sha256": rcv2.SCOPE_JSON_SHA256}
    assert doc["draft"] == {"json": str(rcv2.INPUT_POLICY_JSON), "json_sha256": rcv2.INPUT_POLICY_JSON_SHA256,
                            "md": str(rcv2.INPUT_POLICY_MD), "md_sha256": rcv2.INPUT_POLICY_MD_SHA256}
    assert doc["output"]["sha256"] == _sha(dest)
    assert doc["review_author"]["id"] == rcv2.REVIEW_PERSON["id"]
    assert doc["review_author"]["id"] != rc.REVIEW_PERSON["id"]
    assert doc["changed_parts"] == sorted(rcv2.PERMITTED_PARTS)
    assert doc["reference_count"] == 58
    assert doc["replies"] == 37 and doc["new_roots"] == 21
    assert doc["new_legacy_placeholders"] == 21 and doc["new_vml_shapes"] == 21
    assert len(doc["notes"]) == 58
    assert sum(1 for note in doc["notes"] if note["thread"] == "reply") == 37
    assert sum(1 for note in doc["notes"] if note["thread"] == "root") == 21
    assert all({"reference", "cell", "entry_id", "scope_position", "entry_status", "v1_entry_id", "part",
                "comment_id", "parent_id", "thread", "note_text_sha256", "shape_id", "anchor", "z_index",
                "legacy_author_id", "placeholder_sha256", "shape_sha256"}.issubset(note) for note in doc["notes"])
    assert doc["manual_excel_review_copy_display"].startswith("pending")
    # Every per-note hash is the exact hash of the note text built from the policy.
    policy = _policy()
    hashes = _packet_hashes()
    for note in doc["notes"]:
        entry = next(e for e in policy["entries"] if e["reference"] == note["reference"])
        built = rcv2.build_note(entry, policy, hashes)
        assert note["note_text_sha256"] == hashlib.sha256(built.encode("utf-8")).hexdigest()
        if note["thread"] == "root":
            # Root/placeholder/shape identities + hashes are fully recomputable
            # from the policy and the pinned source, independent of the bytes.
            vml_part = SHEET_VML[note["part"]]
            comments_part = SHEET_COMMENTS[note["part"]]
            expected_placeholder = rcv2._legacy_comment_xml(note["cell"], note["legacy_author_id"],
                                                            note["comment_id"], built)
            assert note["placeholder_sha256"] == hashlib.sha256(expected_placeholder).hexdigest()
            assert 'shapeId="0"' in expected_placeholder.decode("utf-8")
            templates = rcv2._load_vml_templates(zipfile.ZipFile(TEMPLATE))
            template_bytes = templates[vml_part][0]
            expected_shape = rcv2._clone_vml_shape(template_bytes, vml_part, note["shape_id"], note["cell"])
            assert note["shape_sha256"] == hashlib.sha256(expected_shape).hexdigest()
            assert note["z_index"] == templates[vml_part][1]
            assert note["anchor"] == rcv2._translate_anchor(vml_part, note["cell"])
        else:
            assert note["shape_id"] is None and note["placeholder_sha256"] is None
    review = rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=manifest)
    assert review["manifest_verified"] is True
    # A manifest that lost a note or records the wrong note order is rejected.
    bad = json.loads(manifest.read_text(encoding="utf-8"))
    bad["notes"] = bad["notes"][1:] + bad["notes"][:1]
    bad_path = tmp_path / "bad-order.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=bad_path)
    fewer = json.loads(manifest.read_text(encoding="utf-8"))
    fewer["notes"] = fewer["notes"][:-1]
    fewer_path = tmp_path / "fewer-notes.json"
    fewer_path.write_text(json.dumps(fewer), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=fewer_path)
    # A manifest with drifted root/placeholder/shape identities is rejected.
    drifted_ids = json.loads(manifest.read_text(encoding="utf-8"))
    first_root = next(n for n in drifted_ids["notes"] if n["thread"] == "root")
    first_root["shape_id"] = None
    drifted_path = tmp_path / "drifted-ids.json"
    drifted_path.write_text(json.dumps(drifted_ids), encoding="utf-8")
    with pytest.raises(WorkbookContractError, match="manifest drifted"):
        rcv2.verify_review_copy_delta_v2(TEMPLATE, dest, manifest=drifted_path)
