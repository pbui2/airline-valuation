from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from airline_valuation.provisional_writer import write_provisional, verify_provisional_delta, _replace_input
from airline_valuation.workbook import WorkbookContractError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
CONTRACT_PATH = ROOT / "src/airline_valuation/provisional_contract.json"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _entries():
    return {
        "Input sheet!B8": "Air Transport",
        "Input sheet!B11": "100",
        "Input sheet!B12": "12.5",
        "Input sheet!B13": "1.5",
        "Input sheet!B14": "20",
        "Input sheet!B15": "30",
        "Input sheet!B17": "No",
        "Input sheet!B21": "10",
        "Input sheet!B22": "5",
        "Input sheet!B24": "0.21",
        "Input sheet!B26": "0.045",
        "Input sheet!B27": "0.03",
        "Input sheet!B28": "0.03",
        "Input sheet!B29": "0.06",
        "Input sheet!B30": "5",
        "Input sheet!B31": "2",
        "Input sheet!B32": "1.25",
        "Input sheet!B34": "0.04",
        "Cost of capital worksheet!B11": "Detailed",
        "Cost of capital worksheet!B21": "Single Business(US)",
        "Cost of capital worksheet!B25": "Operating regions",
        "Cost of capital worksheet!B32": "0.03",
        "Cost of capital worksheet!B33": "Actual rating",
        "Cost of capital worksheet!B35": "Baa2/BBB",
        "Cost of capital worksheet!B48": "0",
        "Cost of capital worksheet!H21": "0.1",
        "Cost of capital worksheet!H22": "0.1",
        "Cost of capital worksheet!H23": "0.1",
        "Cost of capital worksheet!H24": "0.1",
        "Cost of capital worksheet!H25": "0.1",
        "Cost of capital worksheet!H26": "0.1",
        "Cost of capital worksheet!H27": "0.1",
        "Cost of capital worksheet!H28": "0.1",
        "Cost of capital worksheet!H29": "0.1",
        "Cost of capital worksheet!H30": "0.1",
        "Cost of capital worksheet!H31": "0.1",
    }


def _conversions():
    return {"Input sheet!B27": "0.03", "Input sheet!B28": "0.03", "Input sheet!B32": "1.25"}


def _members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rewrite_package(source, destination, updates):
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as out:
        for info in src.infolist():
            out.writestr(info, updates.get(info.filename, src.read(info.filename)))


def test_provisional_write_exact_allowed_delta_and_cached_formula(tmp_path):
    out = tmp_path / "provisional.xlsx"
    entries = _entries()
    write_provisional(TEMPLATE, out, entries, _conversions())
    result = verify_provisional_delta(TEMPLATE, out, entries, _conversions())
    assert result["valid"]
    before, after = _members(TEMPLATE), _members(out)
    assert before.keys() == after.keys()
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet10.xml"}
    assert b'<c r="D62" s="69" t="e"><f>IF(B48=0,0,B50/B49)</f><v>#DIV/0!</v></c>' in after["xl/worksheets/sheet10.xml"]
    old_cell = b'<c r="D62" s="69" t="e"><f>B50/B49</f><v>#DIV/0!</v></c>'
    assert hashlib.sha256(old_cell).hexdigest() == json.loads(CONTRACT_PATH.read_text())["d62"]["cell_xml_sha256"]
    assert after["xl/sharedStrings.xml"] == before["xl/sharedStrings.xml"]
    assert b'<c r="B8" s="192" t="s"><v>632</v></c>' in after["xl/worksheets/sheet1.xml"]
    assert b'<c r="B35" s="75" t="s"><v>436</v></c>' in after["xl/worksheets/sheet10.xml"]
    root = ET.fromstring(after["xl/worksheets/sheet10.xml"])
    assert root.find(".//m:c[@r='B11']/m:v", NS).text == "719"
    assert root.find(".//m:c[@r='B11']", NS).attrib["t"] == "s"


def test_writer_rejects_unsupported_or_unrepresented_cells(tmp_path):
    with pytest.raises(ValueError, match="approved choice"):
        write_provisional(TEMPLATE, tmp_path / "bad.xlsx", {"Input sheet!B8": "Single Business(US)"})
    with pytest.raises(ValueError, match="approved choice"):
        write_provisional(TEMPLATE, tmp_path / "bad.xlsx", {"Cost of capital worksheet!B35": "Aaa/AAA"})
    with pytest.raises(ValueError, match="not allowlisted"):
        write_provisional(TEMPLATE, tmp_path / "bad.xlsx", {"Input sheet!B46": "0.1"})
    with pytest.raises(ValueError, match="not allowlisted"):
        write_provisional(TEMPLATE, tmp_path / "bad.xlsx", {"Cost of capital worksheet!B12": "0.08"})
    with pytest.raises(ValueError, match="approved choice"):
        write_provisional(TEMPLATE, tmp_path / "bad.xlsx", {"Cost of capital worksheet!B11": "I will input"})


def test_contract_covers_each_direct_scenario_cell_and_pins_dependencies():
    from airline_valuation.provisional_scenario import _ALLOWED
    contract = json.loads(CONTRACT_PATH.read_text())
    assert set(contract["input_allowlist"]) == _ALLOWED - {"Input sheet!B46"}
    assert set(contract["input_allowlist"]) == set(contract["input_metadata"])
    assert {ref for ref, data in contract["input_metadata"].items() if data["kind"] == "dropdown_string"} == {"Input sheet!B8", "Cost of capital worksheet!B35"}
    with zipfile.ZipFile(TEMPLATE) as archive:
        for name, digest in {**contract["pinned_members"], **contract["relationship_pins"]}.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        for name, pin in contract["x14_validation_pins"].items():
            import re
            block, = re.findall(rb"<x14:dataValidations\b.*?</x14:dataValidations>", archive.read(name))
            assert hashlib.sha256(block).hexdigest() == pin["sha256"]
            assert len(re.findall(rb"<x14:dataValidation\b", block)) == pin["count"]


def test_preferred_guard_rejects_inconsistent_inputs(tmp_path):
    cases = [
        ({"Cost of capital worksheet!B48": "0", "Cost of capital worksheet!B49": "2"}, "absent"),
        ({"Cost of capital worksheet!B48": "1"}, "positive B49"),
        ({"Cost of capital worksheet!B48": "1", "Cost of capital worksheet!B49": "0", "Cost of capital worksheet!B50": "0"}, "positive B49"),
        ({"Cost of capital worksheet!B48": "1", "Cost of capital worksheet!B49": "2", "Cost of capital worksheet!B50": "-1"}, "nonnegative B50"),
        ({"Cost of capital worksheet!B48": "2"}, "0 or 1"),
        ({"Cost of capital worksheet!B49": "2"}, "explicit B48"),
    ]
    for entries, error in cases:
        with pytest.raises(ValueError, match=error):
            write_provisional(TEMPLATE, tmp_path / "bad.xlsx", entries)
    entries = {"Cost of capital worksheet!B48": "1", "Cost of capital worksheet!B49": "2", "Cost of capital worksheet!B50": "0",
               "Input sheet!B27": "0.03", "Input sheet!B28": "0.03", "Input sheet!B32": "1.25"}
    out = write_provisional(TEMPLATE, tmp_path / "preferred.xlsx", entries, _conversions())
    assert verify_provisional_delta(TEMPLATE, out, entries, _conversions())["valid"]


def test_cell_replacement_does_not_capture_neighboring_cells_or_rows():
    xml = b'<row r="1"><c r="A1"><v>7</v></c><c r="B1"/><c r="A2"><v>9</v></c></row>'
    changed = _replace_input(xml, "B1", "2", "numeric")
    assert changed == b'<row r="1"><c r="A1"><v>7</v></c><c r="B1"><v>2</v></c><c r="A2"><v>9</v></c></row>'
    with pytest.raises(WorkbookContractError, match="exactly once"):
        _replace_input(xml + b'<c r="B1"/>', "B1", "2", "numeric")


def test_writer_rejects_formula_cell_on_altered_copy_even_if_contract_selected(monkeypatch, tmp_path):
    import airline_valuation.provisional_writer as writer

    altered = tmp_path / "formula-cell.xlsx"
    with zipfile.ZipFile(TEMPLATE) as src:
        members = {name: src.read(name) for name in src.namelist()}
    # Replace contracted direct B32 with a formula-bearing cell while preserving package validity.
    old = b'<c r="B32" s="60"/>'
    new = b'<c r="B32" s="60"><f>1+1</f><v>2</v></c>'
    assert old in members["xl/worksheets/sheet10.xml"]
    members["xl/worksheets/sheet10.xml"] = members["xl/worksheets/sheet10.xml"].replace(old, new, 1)
    _rewrite_package(TEMPLATE, altered, members)
    contract = json.loads(CONTRACT_PATH.read_text())
    monkeypatch.setattr(writer, "_pinned_template", lambda path, _contract: Path(path))
    with pytest.raises(WorkbookContractError, match="formula cell"):
        write_provisional(altered, tmp_path / "out.xlsx", {"Cost of capital worksheet!B32": "4", "Input sheet!B27": "0.03", "Input sheet!B28": "0.03", "Input sheet!B32": "1.25"}, _conversions())


def test_verify_rejects_zip_order_and_metadata_mutations(tmp_path):
    entries = _entries()
    out = write_provisional(TEMPLATE, tmp_path / "written.xlsx", entries, _conversions())
    with zipfile.ZipFile(out) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
        reversed_path = tmp_path / "reversed.xlsx"
        with zipfile.ZipFile(reversed_path, "w") as changed:
            for info in reversed(infos):
                changed.writestr(info, payloads[info.filename])
        metadata_path = tmp_path / "metadata.xlsx"
        with zipfile.ZipFile(metadata_path, "w") as changed:
            for position, info in enumerate(infos):
                if position == 0:
                    info = zipfile.ZipInfo(info.filename, (2020, 1, 1, 0, 0, 0))
                    info.compress_type = infos[position].compress_type
                changed.writestr(info, payloads[info.filename])
    with pytest.raises(WorkbookContractError, match="member inventory"):
        verify_provisional_delta(TEMPLATE, reversed_path, entries, _conversions())
    with pytest.raises(WorkbookContractError, match="ZIP container"):
        verify_provisional_delta(TEMPLATE, metadata_path, entries, _conversions())


def test_verify_rejects_zip_prefix_suffix_and_header_mutations(tmp_path):
    out = tmp_path / "provisional.xlsx"
    entries = _entries()
    write_provisional(TEMPLATE, out, entries, _conversions())
    raw = out.read_bytes()
    for label, payload in (("suffix", raw + b"UNAUTHORIZED TRAILING BYTES"),
                           ("prefix", b"UNAUTHORIZED PREFIX" + raw),
                           ("header", raw[:6] + bytes([raw[6] ^ 1]) + raw[7:])):
        altered = tmp_path / (label + ".xlsx")
        altered.write_bytes(payload)
        with pytest.raises((WorkbookContractError, zipfile.BadZipFile, ValueError)):
            verify_provisional_delta(TEMPLATE, altered, entries, _conversions())


def test_verify_rejects_unrequested_member_and_cell_mutations(tmp_path):
    out = tmp_path / "provisional.xlsx"
    entries = _entries()
    write_provisional(TEMPLATE, out, entries, _conversions())
    members = _members(out)
    members["docProps/core.xml"] += b" "
    tampered = tmp_path / "tampered.xlsx"
    _rewrite_package(out, tampered, members)
    with pytest.raises(WorkbookContractError, match="differs outside"):
        verify_provisional_delta(TEMPLATE, tampered, entries, _conversions())
    members = _members(out)
    members["xl/worksheets/sheet10.xml"] = members["xl/worksheets/sheet10.xml"].replace(b'<c r="B12" s="184"/>', b'<c r="B12" s="184"><v>0.1</v></c>', 1)
    tampered = tmp_path / "protected.xlsx"
    _rewrite_package(out, tampered, members)
    with pytest.raises(WorkbookContractError, match="differs outside"):
        verify_provisional_delta(TEMPLATE, tampered, entries, _conversions())
