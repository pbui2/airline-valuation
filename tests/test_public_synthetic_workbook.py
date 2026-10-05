"""Synthetic workbook parser/writer invariants (public-export portable suite).

These tests build a small, fully self-contained XLSX package and a matching
synthetic contract from scratch — no private workbook, no network.  They
verify that the pinned-template reader/writer logic still behaves correctly on
a clean clone:

* a well-formed synthetic package matching its contract parses and verifies;
* plausible wrong-sheet / wrong-zip-path / tampered-hash faults are rejected
  with a clear ``WorkbookContractError``;
* the writers fail closed on a non-pinned template and preserve cell-level
  invariants when replacing direct input cells.

Expected fingerprints below were computed independently of the parser's own
``verify_structure`` output: they are sha256 digests of the exact hand-written
package XML shown in this module (documented algorithm: stable XML without
volatile tags for worksheet metadata; canonical formula/static records; sorted
workbook relationships).  The normal case must pass and each mutation must
fail, which is the property under test.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airline_valuation.workbook import Workbook, WorkbookContractError  # noqa: E402
from airline_valuation.ooxml_writer import _replace_cell_xml, write_prepared  # noqa: E402
from airline_valuation.provisional_writer import _replace_input  # noqa: E402

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
WS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

# Hand-written package members.  These exact bytes determine every expected
# fingerprint below.
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Types xmlns="{CT}">\n'
    '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
    '  <Default Extension="xml" ContentType="application/xml"/>\n'
    '  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
    '  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    '  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    '</Types>'
).encode("utf-8")
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{PKG}">\n'
    '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>\n'
    '</Relationships>'
).encode("utf-8")
WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<workbook xmlns="{MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
    '  <sheets>\n'
    '    <sheet name="Input sheet" sheetId="1" r:id="rId1"/>\n'
    '    <sheet name="Valuation output" sheetId="2" r:id="rId2"/>\n'
    '  </sheets>\n'
    '</workbook>'
).encode("utf-8")
WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{PKG}">\n'
    f'  <Relationship Id="rId1" Type="{WS}" Target="worksheets/sheet1.xml"/>\n'
    f'  <Relationship Id="rId2" Type="{WS}" Target="worksheets/sheet2.xml"/>\n'
    '</Relationships>'
).encode("utf-8")
SHEET1_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<worksheet xmlns="{MAIN}">\n'
    '  <sheetData>\n'
    '    <row r="1">\n'
    '      <c r="B11" t="n"><v>5</v></c>\n'
    '      <c r="B12"><f>B11*2</f><v>10</v></c>\n'
    '    </row>\n'
    '  </sheetData>\n'
    '</worksheet>'
).encode("utf-8")
SHEET2_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<worksheet xmlns="{MAIN}">\n'
    '  <sheetData>\n'
    '    <row r="1">\n'
    '      <c r="B5" t="n"><v>42</v></c>\n'
    '    </row>\n'
    '  </sheetData>\n'
    '</worksheet>'
).encode("utf-8")

PARTS = {
    "[Content_Types].xml": CONTENT_TYPES,
    "_rels/.rels": ROOT_RELS,
    "xl/workbook.xml": WORKBOOK_XML,
    "xl/_rels/workbook.xml.rels": WORKBOOK_RELS,
    "xl/worksheets/sheet1.xml": SHEET1_XML,
    "xl/worksheets/sheet2.xml": SHEET2_XML,
}

# Independently computed expected digests for the exact hand-written XML above.
# These are content-based (XML text), so they are stable across zip encodings.
EXPECTED_META_SHA256 = "0f4c2a284e6969213ee7855caf4dcbffb45bb53387539c09a28ba4cfc622ab78"
EXPECTED_FORMULA_SHA256 = "c92c7086f9777992aecf8857c6ca6262a8c3caadc3b862db7e1e830403651079"
EXPECTED_STATIC_SHEET1_SHA256 = "2171e118fc5f4d5e52fac25811c40f03b955f26889d5978cfd8608e876c449cb"
EXPECTED_STATIC_SHEET2_SHA256 = "1f25fe791282df84ca3aba847b267e17b0ba4cdbc8e07021260876b7cf11d113"
EXPECTED_REL_SHA256 = "89a7528d7653ec1dff4e22a0a1fd6d96b0b4372e1c6158a4f67e5604d9381210"
EMPTY_LIST_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def write_synthetic(tmp_path: Path, parts: dict[str, bytes] | None = None) -> Path:
    """Write a synthetic XLSX package and return its path."""
    path = tmp_path / "synthetic.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (parts if parts is not None else PARTS).items():
            archive.writestr(name, data)
    return path


def synthetic_contract(path: Path) -> dict:
    """A synthetic contract exactly matching the pristine synthetic package.

    The SHA-256 is computed from the actual package bytes so the identity and
    structure checks line up; the hand-authored fingerprint constants above are
    the independent content oracles that make the normal case pass and any
    mutation fail.
    """
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "contract_version": "synthetic-test/1",
        "source_file": "fcffsimpleginzu.xlsx",
        "sha256": file_sha,
        "sheet_order": ["Input sheet", "Valuation output"],
        "sheet_metadata": {
            "Input sheet": {"sheet_id": "1", "state": "visible", "relationship_id": "rId1", "part": "xl/worksheets/sheet1.xml"},
            "Valuation output": {"sheet_id": "2", "state": "visible", "relationship_id": "rId2", "part": "xl/worksheets/sheet2.xml"},
        },
        "defined_names": [],
        "calc_settings": {},
        "permitted_calc_settings": {},
        "worksheet_metadata_fingerprints": {
            "Input sheet": EXPECTED_META_SHA256,
            "Valuation output": EXPECTED_META_SHA256,
        },
        "formula_fingerprints": {
            "Input sheet": EXPECTED_FORMULA_SHA256,
            "Valuation output": EMPTY_LIST_SHA256,
        },
        "formula_counts": {"Input sheet": 1, "Valuation output": 0},
        "relationship_fingerprint": EXPECTED_REL_SHA256,
        "feature_policy": {
            "feature_prefixes": [],
            "relationship_optional_types": [],
            "permitted_missing_parts": [],
            "required_part_hashes": {},
        },
        "static_cell_fingerprints": {
            "Input sheet": EXPECTED_STATIC_SHEET1_SHA256,
            "Valuation output": EXPECTED_STATIC_SHEET2_SHA256,
        },
        "input_allowlist": [],
        "input_metadata": {},
        "protected_formulas": {},
        "required_parts": [],
        "forbidden_parts": [],
        "output_sheets": ["Valuation output"],
        "branch_switches": {},
        "unsupported_switch_values": {},
        "required_inputs": [],
        "conditional_required_inputs": [],
        "active_outputs_by_branch": {},
        "conditional_diagnostics_by_branch": {},
        "notes": {"synthetic": "hand-built public fixture; not the pinned template"},
    }


def contract_path(tmp_path: Path, contract: dict) -> Path:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC.5: synthetic workbook parser invariants.
# ---------------------------------------------------------------------------


def test_synthetic_workbook_valid(tmp_path):
    """A well-formed synthetic package matching its contract parses and verifies."""
    package = write_synthetic(tmp_path)
    file_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    contract = synthetic_contract(package)
    with Workbook(package, contract_path(tmp_path, contract)) as book:
        assert book.verify_identity() == {
            "matches_template": True,
            "expected_sha256": file_sha,
            "sha256": file_sha,
        }
        result = book.verify_structure("strict")
        assert result["valid"] is True
        assert result["sheet_order"] == ["Input sheet", "Valuation output"]
        # Contracted output cell is readable; unallowlisted input is rejected.
        assert book.read_output("B5") == 42.0
        with pytest.raises(PermissionError):
            book.read_input("B11")
        # Direct static cell value observed by the reader.
        assert book._cells["Input sheet"]["B11"]["value"] == 5.0


def test_synthetic_workbook_rejects_wrong_sheet(tmp_path):
    """Wrong sheet order/name is a structural contract violation."""
    parts = dict(PARTS)
    parts["xl/workbook.xml"] = WORKBOOK_XML.replace(
        b'<sheet name="Input sheet"', b'<sheet name="Renamed input"'
    )
    package = write_synthetic(tmp_path, parts)
    contract = synthetic_contract(package)
    with pytest.raises(WorkbookContractError, match="worksheet order changed"):
        with Workbook(package, contract_path(tmp_path, contract)) as book:
            book.verify_structure("strict")


def test_synthetic_workbook_rejects_wrong_zip_path(tmp_path):
    """A relationship target pointing at a missing worksheet is rejected."""
    parts = dict(PARTS)
    parts["xl/_rels/workbook.xml.rels"] = WORKBOOK_RELS.replace(
        b'Target="worksheets/sheet1.xml"', b'Target="worksheets/sheetNOPE.xml"'
    )
    package = write_synthetic(tmp_path, parts)
    contract = synthetic_contract(package)
    with pytest.raises(WorkbookContractError, match="Missing sheet relationship target"):
        with Workbook(package, contract_path(tmp_path, contract)):
            pass  # construction itself must fail


def test_synthetic_workbook_rejects_tampered_hash(tmp_path):
    """A changed static cell value breaks the static fingerprint (hash fault)."""
    parts = dict(PARTS)
    parts["xl/worksheets/sheet1.xml"] = SHEET1_XML.replace(
        b'<c r="B11" t="n"><v>5</v></c>', b'<c r="B11" t="n"><v>7</v></c>'
    )
    package = write_synthetic(tmp_path, parts)
    contract = synthetic_contract(package)
    with pytest.raises(WorkbookContractError, match="static cell inventory changed: Input sheet"):
        with Workbook(package, contract_path(tmp_path, contract)) as book:
            book.verify_structure("strict")


def test_synthetic_workbook_rejects_duplicate_zip_members(tmp_path):
    """Duplicate ZIP member names are a package-integrity violation."""
    path = tmp_path / "dup.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)  # duplicate name
    with pytest.raises(WorkbookContractError, match="duplicate ZIP member names"):
        Workbook(path, contract_path(tmp_path, synthetic_contract(path)))


# ---------------------------------------------------------------------------
# AC.5: synthetic writer invariants (fail-closed pin + cell replacement).
# ---------------------------------------------------------------------------


def test_writer_rejects_unpinned_synthetic_template(tmp_path):
    """Writers accept only the pinned pristine template SHA-256."""
    synthetic = write_synthetic(tmp_path)
    with pytest.raises(WorkbookContractError, match="pinned pristine template SHA-256"):
        write_prepared(synthetic, tmp_path / "out.xlsx", {})
    # A byte-altered copy is also rejected, not silently written.
    altered = tmp_path / "altered.xlsx"
    altered.write_bytes(synthetic.read_bytes() + b" ")
    with pytest.raises(WorkbookContractError, match="pinned"):
        write_prepared(altered, tmp_path / "out2.xlsx", {})


def test_writer_cell_replacement_invariants():
    """Direct numeric replacement preserves address/value and fails closed."""
    xml = b'<worksheet><sheetData><row r="1"><c r="B11"><v>5</v></c></row></sheetData></worksheet>'
    edited = _replace_cell_xml(xml, "B11", b"123.5")
    assert b'r="B11"' in edited and b"<v>123.5</v>" in edited
    assert b"<v>123.5</v>" in edited
    # The old value token is replaced, not appended.
    assert b"<v>5</v>" not in edited
    # A formula cell must never be overwritten.
    with pytest.raises(WorkbookContractError, match="formula cell"):
        _replace_cell_xml(
            b'<worksheet><sheetData><row r="1"><c r="B12"><f>B11*2</f><v>10</v></c></row></sheetData></worksheet>',
            "B12", b"9",
        )
    # A missing/ambiguous cell is rejected.
    with pytest.raises(WorkbookContractError, match="exactly once"):
        _replace_cell_xml(b"<worksheet/>", "B99", b"1")


def test_writer_direct_input_replacement_invariants():
    """Direct input replacement keeps the single value and refuses formulas."""
    xml = b'<worksheet><sheetData><row r="1"><c r="B11"><v>5</v></c></row></sheetData></worksheet>'
    edited = _replace_input(xml, "B11", "7", "numeric")
    assert b'<v>7</v>' in edited
    formula = b'<worksheet><sheetData><row r="1"><c r="B12"><f>B11*2</f><v>10</v></c></row></sheetData></worksheet>'
    with pytest.raises(WorkbookContractError, match="formula cell"):
        _replace_input(formula, "B12", "9", "numeric")
    # Numeric input cannot overwrite a shared-string cell.
    shared = b'<worksheet><sheetData><row r="1"><c r="C1" t="s"><v>0</v></c></row></sheetData></worksheet>'
    with pytest.raises(WorkbookContractError, match="shared-string"):
        _replace_input(shared, "C1", "9", "numeric")
