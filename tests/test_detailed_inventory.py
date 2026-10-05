from __future__ import annotations

import copy
from pathlib import Path

import pytest

from airline_valuation import cli
from airline_valuation.detailed_inventory import (
    PINNED_WORKBOOK_SHA256,
    PINNED_WORKSHEET_XML_SHA256,
    inspect_detailed_workbook,
    validate_verified_driver_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "fcffsimpleginzu.xlsx"


def test_pinned_selected_route_audits_positive_without_certifying_stale_caches():
    evidence = inspect_detailed_workbook(WORKBOOK)
    validate_verified_driver_evidence(evidence)
    audit = evidence["route_audit"]
    assert evidence["source_sha256"] == PINNED_WORKBOOK_SHA256
    assert evidence["worksheet_xml_sha256"] == PINNED_WORKSHEET_XML_SHA256
    assert evidence["worksheet_relationship_id"] == "rId10"
    assert evidence["worksheet_relationship_part"] == "xl/worksheets/sheet10.xml"
    assert evidence["formula_count"] == 217
    assert evidence["canonical_formula_inventory_sha256"] == "b47284584245e43f3e6164a0ec1bc70e33940c3cbd44555604992f855f8a8473"
    assert all(evidence["formula_hashes_match_pins"].values())
    assert all(audit["formula_hashes_match_pins"].values())
    assert len(audit["reachable_formulas"]) < evidence["formula_count"]
    assert audit["valid"] is True and audit["unknown_dependencies"] == []
    assert audit["selectors"] == {"B11": "Detailed", "B21": "Single Business(US)", "B25": "Operating regions", "B33": "Actual rating"}
    assert audit["lookup_rows"]["industry"] == {"sheet": "Industry Averages(US)", "key_cell": "A4", "value_cell": "G4", "key": "Air Transport", "key_shared_index": "632", "value": "0.75790381791186878", "matches_pin": True}
    assert audit["lookup_rows"]["rating"] == {"sheet": "Synthetic rating", "key_cell": "G52", "value_cell": "H52", "key": "Baa2/BBB", "key_shared_index": "436", "value": "1.1113E-2", "matches_pin": True}
    assert "K23" in audit["shared_followers"] and "J23" in audit["shared_followers"]
    assert "Country equity risk premiums!B209" in audit["shared_followers"]
    assert audit["dependencies"]["J23"] == ["H23", "H32"]  # $H$32 never shifts to H33.
    assert "Input sheet!B17" in audit["reachable_cells"]
    assert "Operating lease converter!F33" not in audit["reachable_cells"]  # B17=No.
    assert evidence["d62_xml"] == '<c r="D62" s="69" t="e"><f>B50/B49</f><v>#DIV/0!</v></c>'
    assert evidence["d62_xml_sha256"] == "069de4cfa70e478c83e299289b4899754fafa004238f97415afffeb2844ab57c"
    assert evidence["formulas"]["D62"]["formula"] == "B50/B49"
    assert evidence["d62_exact_xml_matches_pin"] is True
    assert evidence["blocked"] is False and evidence["blockers"] == []
    assert evidence["cached_errors"]["D62"] == "#DIV/0!"
    assert evidence["cached_errors"]["E62"] == "#N/A"
    assert "rebuild/review" in evidence["scenario_requirements"]["D62"]
    assert evidence["driver_candidates"]["B35"]["observed"] is None
    assert evidence["driver_candidates"]["B49"]["observed"] is None
    assert evidence["driver_candidates"]["B50"]["observed"] is None
    assert evidence["dependency_extraction"]["unknown_cells"]  # Unrelated whole-sheet formulas do not block selected route.


@pytest.mark.parametrize("mutation,match", [
    (lambda x: x.update(source_sha256="0" * 64), "workbook source"),
    (lambda x: x.update(worksheet_xml_sha256="0" * 64), "worksheet XML"),
    (lambda x: x.update(worksheet_relationship_part="xl/worksheets/sheet1.xml"), "relationship"),
    (lambda x: x["formulas"]["B23"].update(formula_sha256="0" * 64), "route formula B23"),
    (lambda x: x["route_audit"]["lookup_rows"]["industry"].update(value="0"), "industry lookup"),
    (lambda x: x["route_audit"]["lookup_rows"]["rating"].update(key_shared_index="0"), "rating lookup"),
    (lambda x: x["route_audit"].update(unknown_dependencies=["K23"]), "unknown dependencies"),
    (lambda x: x["route_audit"]["selectors"].update(B33="Synthetic Rating"), "selected branch"),
    (lambda x: x.update(d62_exact_xml_matches_pin=False), "D62 exact"),
    (lambda x: x.update(blocked=True), "remains blocked"),
])
def test_route_evidence_fails_closed_on_tampering(mutation, match):
    evidence = copy.deepcopy(inspect_detailed_workbook(WORKBOOK))
    mutation(evidence)
    with pytest.raises(ValueError, match=match):
        validate_verified_driver_evidence(evidence)


def test_formula_cells_cannot_be_offered_as_input_driver():
    evidence = inspect_detailed_workbook(WORKBOOK)
    evidence["driver_candidates"]["B23"] = {"offered_as_input": True}
    with pytest.raises(ValueError, match="formula cell B23 cannot be offered"):
        validate_verified_driver_evidence(evidence)


def test_nonpublishing_cli_preserves_existing_output_on_bad_scenario(tmp_path: Path, capsys):
    existing = tmp_path / "existing-output"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_bytes(b"preserve prior output")
    malformed_scenario = tmp_path / "malformed-scenario.json"
    malformed_scenario.write_text("not valid JSON", encoding="utf-8")
    result = cli.main(["provisional", "--scenario", str(malformed_scenario), "--output-dir", str(existing)])
    output = capsys.readouterr()
    assert result != 0
    assert output.out == "" and output.err
    assert sentinel.read_bytes() == b"preserve prior output"
    assert list(existing.iterdir()) == [sentinel]
