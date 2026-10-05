from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from airline_valuation.provisional_scenario import load_provisional_scenario
from airline_valuation.provisional_writer import write_provisional, verify_provisional_delta
from airline_valuation.workbook import WorkbookContractError

TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
SCENARIO = ROOT / "examples/fy2025-provisional-scenario.json"


def test_formula_to_numeric_cells_require_separate_declarations_and_are_exact(tmp_path):
    scenario = load_provisional_scenario(SCENARIO)
    cells = scenario["cells"]
    conversions = {ref: cells[ref] for ref in scenario["formula_to_numeric_inputs"]}
    destination = tmp_path / "provisional.xlsx"
    write_provisional(TEMPLATE, destination, cells, conversions)
    assert verify_provisional_delta(TEMPLATE, destination, cells, conversions)["valid"]
    with zipfile.ZipFile(destination) as archive:
        xml = archive.read("xl/worksheets/sheet1.xml")
    expected_cells = {"B27": b'<c r="B27" s="205"><v>0.03</v></c>',
                      "B28": b'<c r="B28" s="206"><v>0.03</v></c>',
                      "B32": b'<c r="B32" s="207"><v>1.25</v></c>'}
    for address, expected in expected_cells.items():
        cell = next(b"<c " + part.split(b"</c>", 1)[0] + b"</c>" for part in xml.split(b"<c ") if b'r="' + address.encode() + b'"' in part)
        assert cell == expected
    from airline_valuation.detailed_inventory import validate_input_formula_dependents
    values = {ref: cells[ref] for ref in ("Input sheet!B27", "Input sheet!B28", "Input sheet!B32")}
    assert validate_input_formula_dependents(TEMPLATE)["mode"] == "pristine"
    assert validate_input_formula_dependents(destination, staged_values=values)["mode"] == "staged"
    for invalid in ("NaN", "inf", "1,25", "1.2.3", "", "-0.3"):
        bad = dict(values, **{"Input sheet!B32": invalid})
        with pytest.raises(ValueError, match="numeric value/style pin"):
            validate_input_formula_dependents(destination, staged_values=bad)
    with pytest.raises(ValueError, match="separate declaration"):
        write_provisional(TEMPLATE, tmp_path / "missing.xlsx", cells, {"Input sheet!B27": "0.03"})


def _alter_package(source: Path, destination: Path, transforms: dict[str, object]) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as output:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename in transforms:
                data = transforms[info.filename](data)
            output.writestr(info, data)


def _insert_formula_cell(xml: bytes, cell_xml: bytes) -> bytes:
    import re

    return re.sub(rb'(<row\b[^>]*\br="26"[^>]*>)(.*?)(</row>)',
                  lambda match: match.group(1) + match.group(2) + cell_xml + match.group(3), xml, count=1)


def test_mixed_column_dependent_range_is_detected(tmp_path):
    from airline_valuation import detailed_inventory

    altered = tmp_path / "mixed-range.xlsx"
    _alter_package(TEMPLATE, altered, {"xl/worksheets/sheet1.xml": lambda xml: _insert_formula_cell(
        xml, b'<c r="Z26"><f>SUM(A26:C32)</f></c>')})
    with pytest.raises(ValueError, match="direct formula dependents"):
        detailed_inventory.validate_input_formula_dependents(altered)


@pytest.mark.parametrize("mode", ["pristine", "staged"])
@pytest.mark.parametrize("formula_type", ["shared", "array"])
def test_shared_or_array_range_intersecting_targets_fails_closed(tmp_path, mode, formula_type):
    from airline_valuation import detailed_inventory

    altered = tmp_path / f"{mode}-{formula_type}.xlsx"
    transforms = {"xl/worksheets/sheet1.xml": lambda xml: _insert_formula_cell(
        xml, f'<c r="Z26"><f t="{formula_type}" si="42" ref="A26:C32">1</f></c>'.encode())}
    kwargs = {}
    if mode == "staged":
        # Let the writer produce the exact staged target-cell XML, then combine the altered range.
        scenario = load_provisional_scenario(SCENARIO)
        cells = scenario["cells"]
        conversions = {ref: cells[ref] for ref in scenario["formula_to_numeric_inputs"]}
        staged = tmp_path / "staged.xlsx"
        write_provisional(TEMPLATE, staged, cells, conversions)
        with zipfile.ZipFile(staged) as archive:
            staged_xml = archive.read("xl/worksheets/sheet1.xml")
        range_transform = transforms["xl/worksheets/sheet1.xml"]
        transforms["xl/worksheets/sheet1.xml"] = lambda _xml: range_transform(staged_xml)
        kwargs["staged_values"] = {"Input sheet!B27": "0.03", "Input sheet!B28": "0.03", "Input sheet!B32": "1.25"}
    _alter_package(TEMPLATE, altered, transforms)
    with pytest.raises(ValueError, match="shared/array formula range intersects"):
        detailed_inventory.validate_input_formula_dependents(altered, **kwargs)



def test_changed_dependent_formula_is_rejected_by_pin(tmp_path):
    from airline_valuation import detailed_inventory

    altered = tmp_path / "changed-dependent.xlsx"
    # Existing direct dependent at C10 is changed while retaining its dependency.
    import re
    from airline_valuation import detailed_inventory
    with zipfile.ZipFile(TEMPLATE) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {item.attrib.get("Id"): item.attrib.get("Target") for item in relationships}
        sheet = next(node for node in workbook.findall(".//main:sheet", detailed_inventory._NS)
                     if node.attrib["name"] == "Stories to Numbers")
        part = "xl/" + rels[sheet.attrib["{" + detailed_inventory._NS["rel"] + "}id"]]
        xml = archive.read(part)
    match = re.search(rb'(<c r="C10"[^>]*>.*?<f[^>]*>)(.*?)(</f>.*?</c>)', xml)
    assert match is not None
    updated = xml[:match.start(2)] + match.group(2) + b"+1" + xml[match.end(2):]
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(altered, "w") as output:
        for info in source.infolist():
            data = updated if info.filename == part else source.read(info.filename)
            output.writestr(info, data)
    with pytest.raises(ValueError, match="formula hash differs"):
        detailed_inventory.validate_input_formula_dependents(altered)


def test_formula_conversion_contract_source_xml_must_match_exact_pin(tmp_path, monkeypatch):
    import airline_valuation.provisional_writer as writer

    altered = tmp_path / "altered.xlsx"
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(altered, "w") as output:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b'<c r="B27" s="205" t="e"><f>I26</f><v>#DIV/0!</v></c>',
                                    b'<c r="B27" s="205" t="e"><f>I27</f><v>#DIV/0!</v></c>', 1)
            output.writestr(info, data)
    monkeypatch.setattr(writer, "_pinned_template", lambda path, _contract: Path(path))
    scenario = load_provisional_scenario(SCENARIO)
    cells = scenario["cells"]
    conversions = {ref: cells[ref] for ref in scenario["formula_to_numeric_inputs"]}
    with pytest.raises(WorkbookContractError, match="exact pristine XML pin"):
        write_provisional(altered, tmp_path / "out.xlsx", cells, conversions)
