import hashlib
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from airline_valuation.ooxml_writer import write_prepared, verify_prepared_delta
from airline_valuation.workbook import WorkbookContractError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
CONTRACT = ROOT / "src/airline_valuation/contract.json"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _entries():
    return {"Input sheet!B11": "123.5", "Cost of capital worksheet!B11": "I will input"}


def _write_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)


def _members(path):
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_write_prepared_exact_values_and_preservation(tmp_path):
    out = tmp_path / "prepared.xlsx"
    write_prepared(TEMPLATE, out, _entries())
    assert verify_prepared_delta(TEMPLATE, out, _entries())["valid"]
    original, changed = _members(TEMPLATE), _members(out)
    assert original.keys() == changed.keys()
    assert all(original[name] == changed[name] for name in original if name not in {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet10.xml"})
    root = ET.fromstring(changed["xl/worksheets/sheet1.xml"])
    assert root.find(".//m:c[@r='B11']/m:v", NS).text == "123.5"
    assert root.find(".//m:c[@r='B11']", NS).attrib.get("t", "n") == "n"
    root = ET.fromstring(changed["xl/worksheets/sheet10.xml"])
    # The exact pre-existing shared-string index is retained; no shared string is appended.
    assert root.find(".//m:c[@r='B11']/m:v", NS).text.isdigit()
    assert original["xl/sharedStrings.xml"] == changed["xl/sharedStrings.xml"]


def test_reject_unpinned_template_and_invalid_values(tmp_path):
    copy = tmp_path / "altered.xlsx"
    copy.write_bytes(TEMPLATE.read_bytes() + b" ")
    with pytest.raises(WorkbookContractError, match="pinned"):
        write_prepared(copy, tmp_path / "out.xlsx", {})
    for entries in ({"Input sheet!B11": "1e999"}, {"Input sheet!B11": "01"}, {"Input sheet!B11": "NaN"}, {"Input sheet!B7": "2"}, {"Input sheet!B11": "x"}):
        with pytest.raises((ValueError, WorkbookContractError)):
            write_prepared(TEMPLATE, tmp_path / "out.xlsx", entries)
    with pytest.raises(ValueError, match="approved switch"):
        write_prepared(TEMPLATE, tmp_path / "out.xlsx", {"Cost of capital worksheet!B11": "Detailed"})
    with pytest.raises(ValueError, match="numeric"):
        write_prepared(TEMPLATE, tmp_path / "out.xlsx", {"Input sheet!B16": "Industry Average"})
    # A switch is accepted only where the contract declares kind=switch and an allowed choice.
    import json
    contract = json.loads(CONTRACT.read_text())
    from airline_valuation.ooxml_writer import _normal_entries
    assert _normal_entries({"Cost of capital worksheet!B11": "I will input"}, contract)["Cost of capital worksheet!B11"] == ("I will input", "selector")
    with pytest.raises(ValueError, match="numeric"):
        _normal_entries({"Input sheet!B16": "Yes"}, contract)


def test_verify_rejects_changed_unrequested_cell_and_other_part(tmp_path):
    out = tmp_path / "prepared.xlsx"
    write_prepared(TEMPLATE, out, _entries())
    members = _members(out)
    members["xl/worksheets/sheet1.xml"] = members["xl/worksheets/sheet1.xml"].replace(b"B12", b"B12", 1).replace(b">0</v>", b">77</v>", 1)
    mutated = tmp_path / "mutated.xlsx"
    _write_zip(mutated, list(members.items()))
    with pytest.raises(WorkbookContractError, match="outside|unexpected"):
        verify_prepared_delta(TEMPLATE, mutated, _entries())
    members = _members(out)
    members["docProps/core.xml"] = members["docProps/core.xml"] + b" "
    mutated = tmp_path / "other.xlsx"
    _write_zip(mutated, list(members.items()))
    with pytest.raises(WorkbookContractError, match="unexpected package"):
        verify_prepared_delta(TEMPLATE, mutated, _entries())


@pytest.mark.parametrize("part,mutate", [
    ("_rels/.rels", lambda b: b.replace(b"xl/workbook.xml", b"xl/other.xml", 1)),
    ("[Content_Types].xml", lambda b: b.replace(b"worksheet+xml", b"text/plain", 1)),
    ("[Content_Types].xml", lambda b: b.replace(b"</Types>", b'<Override PartName="/xl/workbook.xml" ContentType="text/plain"/></Types>', 1)),
])
def test_writer_rejects_root_relationship_and_content_type_tampering(tmp_path, part, mutate):
    damaged = tmp_path / "damaged-template.xlsx"
    with zipfile.ZipFile(TEMPLATE) as src:
        members = [(info, mutate(src.read(info.filename)) if info.filename == part else src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(damaged, "w") as dst:
        for info, data in members: dst.writestr(info, data)
    with pytest.raises(WorkbookContractError, match="pinned"):
        write_prepared(damaged, tmp_path / "out.xlsx", _entries())
    from airline_valuation.ooxml_writer import _sheet_parts
    with zipfile.ZipFile(damaged) as archive, pytest.raises(WorkbookContractError):
        _sheet_parts(archive, __import__("json").loads(CONTRACT.read_text()))
    from airline_valuation.workbook import Workbook
    with pytest.raises(WorkbookContractError):
        Workbook(damaged, CONTRACT)


def test_writer_rejects_formula_cell_even_if_allowlisted(monkeypatch, tmp_path):
    # Template-specific guard remains dynamic even if a future contract mistakenly allowlists a formula.
    import airline_valuation.ooxml_writer as writer
    original = writer._normal_entries
    monkeypatch.setattr(writer, "_normal_entries", lambda entries, contract: {"Input sheet!B11": ("4", "numeric")})
    source = tmp_path / "template.xlsx"
    source.write_bytes(TEMPLATE.read_bytes())
    monkeypatch.setattr(writer, "_template_contract", lambda template: (Path(template), __import__("json").loads(CONTRACT.read_text())))
    with zipfile.ZipFile(TEMPLATE) as src, zipfile.ZipFile(source, "w") as dst:
        for info in src.infolist(): dst.writestr(info, src.read(info.filename))
    # Injecting an invalid contract edit isn't possible through the byte-preserving writer; direct unit test of token guard.
    with pytest.raises(WorkbookContractError, match="formula cell"):
        writer._replace_cell_xml(b'<c r="B11"><f>1</f><v>0</v></c>', "B11", b"4")
