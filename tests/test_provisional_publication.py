"""Acceptance checks for private, uncalculated provisional publication."""
import json
import zipfile
from pathlib import Path

import pytest

from airline_valuation import cli
from airline_valuation.provisional_writer import verify_provisional_delta

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/fy2025-provisional-scenario.json"
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"


def test_provisional_detailed_populates_verified_drivers(tmp_path):
    output = tmp_path / "private"
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]) == 0
    assert {p.name for p in output.iterdir()} == {"provisional.xlsx", "NOTICE.txt", "ledger.json", "run-state.json"}
    ledger = json.loads((output / "ledger.json").read_text())
    cells = ledger["cells"]
    assert cells["Input sheet!B8"] == "Air Transport"
    assert cells["Cost of capital worksheet!B35"] == "Baa2/BBB"
    assert cells["Input sheet!B15"] == "5981" and cells["Input sheet!B17"] == "No"
    assert cells["Input sheet!B18"] == "3231"
    assert cells["Cost of capital worksheet!B32"] == "0"
    assert cells["Input sheet!B21"] == "491.31799"
    assert cells["Input sheet!B22"] == "54.26"
    assert [cells[f"Input sheet!B{x}"] for x in (26, 27, 28, 29, 30, 31, 32)] == ["0.045", "0.03", "0.03", "0.06", "5", "2", "1.25"]
    assert cells["Cost of capital worksheet!B32"] == "0"
    state = json.loads((output / "run-state.json").read_text())
    assert state["unresolved"]["forecast_assumptions"] == (
        "Input sheet!B27 is an illustrative next-year pre-tax operating margin, "
        "B28 is illustrative long-term revenue growth, and B29 is an illustrative "
        "long-term target pre-tax operating margin. No GAAP margin is inferred from adjusted EPS."
    )
    assert state["unresolved"]["debt_maturity"] == (
        "Cost of capital worksheet!B32=0 is the workbook comment fallback because Note 6 reports no single average debt maturity; "
        "the issuer's average maturity remains unresolved."
    )
    assert "error076040_02.xml" in state["unresolved"]["excel_repair"]
    assert "FORMULA RECORD" in state["unresolved"]["excel_repair"]
    assert "not removal of the entire file" in state["unresolved"]["excel_repair"]
    assert {"B19_nonoperating", "B20_noncontrolling_interest"} <= set(state["unresolved"])
    assert "not attested" in state["unresolved"]["excel_repair"]
    assert state["unresolved"]["excel_review"] == (
        "Stale cached values are not calculations; desktop Excel recalculation and human review "
        "are required, and recalculation is deferred by the operator."
    )
    notice = (output / "NOTICE.txt").read_text()
    assert "Excel repair log error076040_02.xml reports removal of a FORMULA RECORD from xl/calcChain.xml, not removal of the entire file" in notice
    assert "older Excel-saved copy is not attested" in notice
    assert "No calculated WACC is supplied" in notice
    assert "Excel recalculation" in notice
    assert ledger["unresolved"] == state["unresolved"]
    assert "Cost of capital worksheet!B12" not in cells
    assert sum(float(cells[f"Cost of capital worksheet!H{i}"]) for i in range(21, 32)) == 28063
    assert [float(cells[f"Cost of capital worksheet!H{i}"]) for i in range(21, 32)] == [0, 0, 0, 0, 0, 0, 0, 28063, 0, 0, 0]
    assert verify_provisional_delta(TEMPLATE, output / "provisional.xlsx", cells,
                                    {ref: cells[ref] for ref in ("Input sheet!B27", "Input sheet!B28", "Input sheet!B32")})["valid"]
    with zipfile.ZipFile(output / "provisional.xlsx") as archive:
        assert b'<c r="B8" s="192" t="s"><v>632</v></c>' in archive.read("xl/worksheets/sheet1.xml")
        assert b'<c r="B35" s="75" t="s"><v>436</v></c>' in archive.read("xl/worksheets/sheet10.xml")
    assert cli.PROVISIONAL_STATUS in (output / "NOTICE.txt").read_text()
    assert json.loads((output / "run-state.json").read_text())["outcome"] == cli.PROVISIONAL_STATUS
    assert "lookup_tables" in ledger["unresolved"]
    inventory = ledger["candidate_inventory"]
    assert set(inventory) == set(cells) | {"Cost of capital worksheet!B12", "Cost of capital worksheet!B49", "Cost of capital worksheet!B50", "Input sheet!B46", "Input sheet!B15 debt-only", "Cost of capital worksheet!B35 BBB", "Input sheet!B8 Airlines"}
    assert all(inventory[ref]["disposition"] == "entered" for ref in cells)
    assert inventory["Cost of capital worksheet!B12"]["disposition"] == "excluded"
    assert inventory["Cost of capital worksheet!B35 BBB"]["disposition"] == "superseded"
    assert set(ledger["formula_to_numeric_inputs"]) == {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"}
    assert set(ledger["judgments"]) == set(cells)
    assert set(ledger["replacements"]) == set(ledger["formula_to_numeric_inputs"])
    assert {"debt_maturity", "rating", "geography", "excel_review"} <= set(ledger["unresolved"])
    with zipfile.ZipFile(TEMPLATE) as archive:
        import xml.etree.ElementTree as ET
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet10.xml"))
        c53 = sheet.find(".//m:c[@r='C53']/m:f", ns)
        assert c53 is not None
        assert c53.text == "B31*(1-(1+B37)^(-B32))/B37+B30/(1+B37)^B32"
        cache = sheet.find(".//m:c[@r='C53']/m:v", ns)
        assert cache is not None  # Structural check only; the cached value may be stale or an Excel error.
    from airline_valuation.detailed_inventory import validate_input_formula_dependents
    dependency_inventory = validate_input_formula_dependents(TEMPLATE)
    assert len(dependency_inventory["direct_formula_cells"]) == 21
    assert dependency_inventory["shared_or_array_formula_cells"] == []


def test_provisional_overwrite_recovery(tmp_path, monkeypatch):
    output = tmp_path / "private"
    args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(args) == 0
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    assert cli.main(args) != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    original_replace = cli.os.replace
    def fail_stage(source, dest):
        if ".stage-" in str(source) and Path(dest) == output:
            raise OSError("injected publication failure")
        return original_replace(source, dest)
    monkeypatch.setattr(cli.os, "replace", fail_stage)
    assert cli.main(args + ["--overwrite"]) != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    assert not list(tmp_path.glob(".private.backup-*"))


def test_workflows_refuse_cross_class_overwrite(tmp_path, monkeypatch):
    from test_cli import _prepare_test_dependencies
    output = tmp_path / "run"
    scenario_args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(scenario_args) == 0
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    assert cli.main(["prepare", "--manifest", str(manifest), "--output-dir", str(output), "--overwrite"]) != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    approved = tmp_path / "approved"
    assert cli.main(["prepare", "--manifest", str(manifest), "--output-dir", str(approved)]) == 0
    approved_before = {p.name: p.read_bytes() for p in approved.iterdir()}
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(approved), "--overwrite"]) != 0
    assert {p.name: p.read_bytes() for p in approved.iterdir()} == approved_before


def test_provisional_blocked_audit_no_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "validate_verified_driver_evidence", lambda _: (_ for _ in ()).throw(ValueError("selected route unknown")))
    output = tmp_path / "unpublished"
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]) != 0
    assert not output.exists()


def test_provisional_missing_candidate_inventory_no_publication(tmp_path):
    data = json.loads(SCENARIO.read_text())
    del data["candidate_inventory"]
    scenario = tmp_path / "missing-inventory.json"
    scenario.write_text(json.dumps(data))
    output = tmp_path / "unpublished"
    assert cli.main(["provisional", "--scenario", str(scenario), "--output-dir", str(output)]) != 0
    assert not output.exists()


def test_provisional_rejects_invalid_detailed_driver(tmp_path):
    data = json.loads(SCENARIO.read_text())
    data["cells"]["Cost of capital worksheet!B35"] = "BBB"
    scenario = tmp_path / "invalid.json"
    scenario.write_text(json.dumps(data))
    output = tmp_path / "unpublished"
    assert cli.main(["provisional", "--scenario", str(scenario), "--output-dir", str(output)]) != 0
    assert not output.exists()


def test_amended_provisional_cli_publication(tmp_path):
    output = tmp_path / "provisional"
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]) == 0
    assert {item.name for item in output.iterdir()} == {"provisional.xlsx", "NOTICE.txt", "ledger.json", "run-state.json"}
    ledger = json.loads((output / "ledger.json").read_text())
    assert ledger["cells"]["Input sheet!B15"] == "5981"
    assert ledger["cells"]["Input sheet!B17"] == "No"
    assert ledger["cells"]["Input sheet!B18"] == "3231"
    assert ledger["cells"]["Cost of capital worksheet!B32"] == "0"


def test_amended_provisional_exact_delta_and_isolation(tmp_path):
    output = tmp_path / "provisional"
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]) == 0
    ledger = json.loads((output / "ledger.json").read_text())
    cells = ledger["cells"]
    conversions = {ref: cells[ref] for ref in ledger["formula_to_numeric_inputs"]}
    assert verify_provisional_delta(TEMPLATE, output / "provisional.xlsx", cells, conversions)["valid"]
    with zipfile.ZipFile(TEMPLATE) as before, zipfile.ZipFile(output / "provisional.xlsx") as after:
        old = {name: before.read(name) for name in before.namelist()}
        new = {name: after.read(name) for name in after.namelist()}
    assert old.keys() == new.keys()
    assert {name for name in old if old[name] != new[name]} == {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet10.xml"}
    assert old["xl/calcChain.xml"] == new["xl/calcChain.xml"] if "xl/calcChain.xml" in old else True


def test_amended_provisional_failure_preserves_previous_artifacts(tmp_path, monkeypatch):
    output = tmp_path / "provisional"
    args = ["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]
    assert cli.main(args) == 0
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    original_replace = cli.os.replace
    def fail_publish(source, destination):
        if ".stage-" in str(source) and Path(destination) == output:
            raise OSError("injected amended publication failure")
        return original_replace(source, destination)
    monkeypatch.setattr(cli.os, "replace", fail_publish)
    assert cli.main(args + ["--overwrite"]) == cli.BLOCKED
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_amended_provisional_notice_and_ledger_status(tmp_path):
    output = tmp_path / "provisional"
    assert cli.main(["provisional", "--scenario", str(SCENARIO), "--output-dir", str(output)]) == 0
    ledger = json.loads((output / "ledger.json").read_text())
    state = json.loads((output / "run-state.json").read_text())
    notice = (output / "NOTICE.txt").read_text()
    assert ledger["status"] == state["outcome"] == cli.PROVISIONAL_STATUS
    assert "xl/calcChain.xml" in notice
    assert "not attested" in notice
    assert "No calculated WACC is supplied" in notice
    assert "caches may be stale" in notice and "errors" in notice
