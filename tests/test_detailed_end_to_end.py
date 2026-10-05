from __future__ import annotations

import contextlib
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airline_valuation import cli  # noqa: E402
from airline_valuation.detailed_workflow import build_synthetic_test_candidate  # noqa: E402

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"main": MAIN_NS}


def _run_inspect(run_dir: Path, workbook: Path, capsys) -> tuple[int, dict]:
    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=workbook))
    report = json.loads(capsys.readouterr().out)
    return result, report


def _new_candidate(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "candidate-run"
    build_synthetic_test_candidate(run_dir)
    saved_copy = tmp_path / "saved-copy.xlsx"
    saved_copy.write_bytes((run_dir / "candidate.xlsx").read_bytes())
    return run_dir, saved_copy


def _rewrite_xlsx_part(path: Path, part: str, edit) -> None:
    temporary = path.with_suffix(".rewritten.xlsx")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == part:
                root = ET.fromstring(data)
                edit(root)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(info, data)
    temporary.replace(path)


def _edit_cell(path: Path, reference: str, edit, part: str = "xl/worksheets/sheet10.xml") -> None:
    _rewrite_xlsx_part(
        path,
        part,
        lambda root: edit(next(cell for cell in root.findall(".//main:c", NS) if cell.attrib.get("r") == reference)),
    )


def _report_bytes(run_dir: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in run_dir.iterdir()}


def test_self_consistent_relabelled_synthetic_package_fails_provenance(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)
    package_path = run_dir / "package.json"
    decision_path = run_dir / "decision-report.json"
    ledger_path = run_dir / "ledger.json"
    state_path = run_dir / cli.RUN_STATE
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["schema_version"] = "southwest-fy2025-detailed-decisions/v3"
    package["issuer"] = "Southwest"
    package["status"] = "real_issuer_verified"
    package["source_eligibility"] = {
        "status": "verified_allowed", "eligible": True,
        "cutoff": package["source_eligibility"]["cutoff"],
    }
    package["policy"]["fixture_only"] = False
    from airline_valuation.detailed_workflow import canonical_digest
    package["canonical_sha256"] = canonical_digest(
        {key: value for key, value in package.items() if key != "canonical_sha256"}
    )
    package_path.write_text(json.dumps(package, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["decision_package"] = package
    decision["decision_package_sha256"] = canonical_digest(package)
    decision["canonical_sha256"] = canonical_digest(
        {key: value for key, value in decision.items() if key != "canonical_sha256"}
    )
    decision_path.write_text(json.dumps(decision, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["package_sha256"] = canonical_digest(package)
    ledger_path.write_text(json.dumps(ledger, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["decision_report_sha256"] = cli.sha256(decision_path)
    state["ledger_sha256"] = cli.sha256(ledger_path)
    state["package_file_sha256"] = cli.sha256(package_path)
    state["package_sha256"] = canonical_digest(package)
    state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["valid"] is False
    assert any("Detailed package schema is not supported for prepared_uncalculated runs" in issue for issue in report["issues"])
    assert any("Detailed synthetic package provenance validation failed" in issue for issue in report["issues"])


def test_synthetic_candidate_reaches_real_auditor_and_saved_copy_stays_uncalculated(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)
    before = _report_bytes(run_dir)

    result, report = _run_inspect(run_dir, saved_copy, capsys)

    # Exercise the actual exact-delta verifier and candidate-mode inventory.
    assert result == 0
    assert report["valid"] is True
    assert report["status"] == "prepared_uncalculated"
    assert report["saved_workbook"] == str(saved_copy)
    assert report["saved_matches_candidate"] is True
    assert report["saved_sha256"] == cli.sha256(saved_copy)
    assert report["workbook_inventory"] is not None
    assert report["workbook_inventory"]["route_audit"]["lookup_rows"]["industry"]["matches_pin"] is True
    assert report["workbook_inventory"]["route_audit"]["lookup_rows"]["rating"]["matches_pin"] is True
    assert report["cache_uncertainties"]
    assert any("D62 retains cached error #DIV/0!" in item for item in report["cache_uncertainties"])
    assert not any("Excel calculation" in item for item in report["cache_uncertainties"])
    assert not report["issues"]
    assert "Excel calculation or recalculation" in " ".join(report["limitations"])
    assert _report_bytes(run_dir) == before


def test_real_formula_fault_reports_pinned_formula_diagnostic(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)

    def alter_formula(cell):
        formula = cell.find(f"{{{MAIN_NS}}}f")
        assert formula is not None
        formula.text = "B50/B48"

    _edit_cell(saved_copy, "D62", alter_formula)
    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["status"] == "prepared_uncalculated"
    assert report["saved_matches_candidate"] is False
    assert any("D62 candidate formula differs from Detailed contract" in issue for issue in report["issues"])


def test_real_lookup_fault_reports_pinned_lookup_diagnostic(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)

    def alter_lookup_value(cell):
        value = cell.find(f"{{{MAIN_NS}}}v")
        assert value is not None
        value.text = "0.123456"

    _edit_cell(saved_copy, "G4", alter_lookup_value, part="xl/worksheets/sheet13.xml")
    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["status"] == "prepared_uncalculated"
    lookup = report["workbook_inventory"]["route_audit"]["lookup_rows"]["industry"]
    assert lookup["matches_pin"] is False
    assert any("industry selected lookup row differs from pin" in issue for issue in report["issues"])


def test_real_cached_error_fault_is_reported_without_recalculation(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)

    def alter_cached_value(cell):
        value = cell.find(f"{{{MAIN_NS}}}v")
        assert value is not None
        value.text = "#VALUE!"
        cell.attrib["t"] = "e"

    _edit_cell(saved_copy, "D62", alter_cached_value)
    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["status"] == "prepared_uncalculated"
    assert report["workbook_inventory"]["cached_errors"]["D62"] == "#VALUE!"
    assert any("D62 cache differs from the named uncalculated candidate condition" in issue for issue in report["issues"])


def test_real_run_state_hash_fault_reports_binding_diagnostic(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)
    state_path = run_dir / cli.RUN_STATE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["candidate_sha256"] = "0" * 64
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["status"] == "prepared_uncalculated"
    assert any("candidate_sha256 differs from Detailed run-state binding" in issue for issue in report["issues"])


def test_malformed_saved_workbook_reports_structural_repair_diagnostic(tmp_path, capsys):
    run_dir, saved_copy = _new_candidate(tmp_path)
    saved_copy.write_bytes(b"not an xlsx package; repair or resave required")

    result, report = _run_inspect(run_dir, saved_copy, capsys)

    assert result == cli.BLOCKED
    assert report["status"] == "prepared_uncalculated"
    assert any("Detailed saved-copy input comparison failed" in issue for issue in report["issues"])
    assert any("Detailed saved-copy structural inspection failed" in issue for issue in report["issues"])
