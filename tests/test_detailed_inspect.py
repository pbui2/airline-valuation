import hashlib
import json
import zipfile
from pathlib import Path

from argparse import Namespace

from airline_valuation import cli
from airline_valuation.detailed_workflow import canonical_digest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _valid_run(run_dir: Path) -> Path:
    run_dir.mkdir()
    candidate = run_dir / "candidate.xlsx"
    candidate.write_bytes(TEMPLATE.read_bytes())
    package = {"cells": {}}
    package_digest = canonical_digest(package)
    decision = {
        "decision_package": package,
        "decision_package_sha256": package_digest,
        "canonical_sha256": "",
        # A report-level `supported` flag is not used as an inspection pass gate.
        "status": "unresolved",
        "price_suitability": True,
        "treasury_suitability": True,
        "lookup_suitability": True,
    }
    decision["canonical_sha256"] = canonical_digest({key: value for key, value in decision.items() if key != "canonical_sha256"})
    package_path = run_dir / "package.json"
    _write_json(package_path, package)
    decision_path = run_dir / "decision-report.json"
    _write_json(decision_path, decision)
    ledger = {
        "schema": "airline-valuation-detailed-run-ledger/1",
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "package_sha256": package_digest,
        "cells": {},
    }
    ledger_path = run_dir / "ledger.json"
    _write_json(ledger_path, ledger)
    state = {
        "schema": cli.DETAILED_RUN_STATE_SCHEMA,
        "outcome": cli.DETAILED_RUN_OUTCOME,
        "template_path": str(TEMPLATE),
        "template_sha256": cli.sha256(TEMPLATE),
        "candidate_sha256": cli.sha256(candidate),
        "decision_report_sha256": cli.sha256(decision_path),
        "ledger_sha256": cli.sha256(ledger_path),
        "package_file_sha256": cli.sha256(package_path),
        "package_sha256": package_digest,
    }
    _write_json(run_dir / cli.RUN_STATE, state)
    return candidate


def test_detailed_inspect_hash_consistent_pinned_candidate_negative_control_is_blocked(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    # This deliberately uses the real, non-synthetic pinned workbook bytes.
    # Empty required inputs are confined to the negative-control test contract.
    contract = detailed_workflow.load_contract()
    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: contract)
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)

    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=candidate))

    output = capsys.readouterr().out
    assert result == cli.BLOCKED
    report = json.loads(output)
    assert report["valid"] is False
    assert report["status"] == "prepared_uncalculated"
    assert report["saved_sha256"] == cli.sha256(candidate)
    assert report["cache_uncertainties"] == []
    assert "Detailed decision package does not bind every required contract input" in report["issues"]
    assert "attestation.json" not in {path.name for path in run_dir.iterdir()}
    assert "Excel calculation or recalculation" in " ".join(report["limitations"])


def test_detailed_inspect_accepts_external_saved_copy_without_promoting_status(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    contract = detailed_workflow.load_contract()
    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: contract)
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    saved_copy = tmp_path / "excel-saved-copy.xlsx"
    saved_copy.write_bytes(candidate.read_bytes())
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}

    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=saved_copy))

    report = json.loads(capsys.readouterr().out)
    assert result == cli.BLOCKED  # authentic template has active cached formula-error blockers
    assert report["saved_workbook"] == str(saved_copy)
    assert report["saved_sha256"] == cli.sha256(saved_copy)
    assert report["saved_matches_candidate"] is True
    assert report["status"] == "prepared_uncalculated"
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before


def test_detailed_inspect_rejects_changed_allowlisted_numeric_input_in_saved_copy(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    contract = detailed_workflow.load_contract()
    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: contract)
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    saved_copy = tmp_path / "mutated-saved-copy.xlsx"
    with zipfile.ZipFile(candidate) as source, zipfile.ZipFile(saved_copy, "w") as output:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                original = b'<c r="B21" s="199"/>'
                assert data.count(original) == 1
                data = data.replace(original, b'<c r="B21" s="199"><v>492.31799</v></c>', 1)
            output.writestr(info, data)

    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=saved_copy))

    report = json.loads(capsys.readouterr().out)
    assert result == cli.BLOCKED
    assert "saved-copy input mismatch: Input sheet!B21" in report["issues"]
    assert not any("authorized Detailed delta invalid" in issue for issue in report["issues"])
    assert report["status"] == "prepared_uncalculated"


def test_detailed_inspect_reports_stale_optional_saved_copy_hash(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    contract = detailed_workflow.load_contract()
    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: contract)
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    saved_copy = tmp_path / "saved-copy.xlsx"
    saved_copy.write_bytes(candidate.read_bytes())
    state_path = run_dir / cli.RUN_STATE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["saved_copy_sha256"] = "0" * 64
    _write_json(state_path, state)

    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=saved_copy))

    report = json.loads(capsys.readouterr().out)
    assert result == cli.BLOCKED
    assert any("optional run-state saved_copy_sha256" in issue for issue in report["issues"])
    assert report["status"] == "prepared_uncalculated"


def test_detailed_inspect_rejects_saved_copy_symlink_and_traversal(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    contract = detailed_workflow.load_contract()
    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: contract)
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    outside = tmp_path / "outside.xlsx"
    outside.symlink_to(candidate)

    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=outside))
    report = json.loads(capsys.readouterr().out)
    assert result == cli.BLOCKED
    assert any("contains a symlink" in issue for issue in report["issues"])

    traversal = Path("..").joinpath(candidate.relative_to(tmp_path))
    result = cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=traversal))
    report = json.loads(capsys.readouterr().out)
    assert result == cli.BLOCKED
    assert any("traversal components" in issue for issue in report["issues"])


def test_detailed_inspect_rejects_extra_legacy_artifacts(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: {"sha256": cli.sha256(TEMPLATE), "required_inputs": [], "input_allowlist": []})
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    (run_dir / "attestation.json").write_text("{}", encoding="utf-8")

    assert cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=candidate)) == cli.BLOCKED
    report = json.loads(capsys.readouterr().out)
    assert any("exact candidate package" in issue for issue in report["issues"])


def test_detailed_inspect_rejects_symlink_candidate(tmp_path, monkeypatch, capsys):
    from airline_valuation import detailed_workflow

    monkeypatch.setattr(detailed_workflow, "load_contract", lambda: {"sha256": cli.sha256(TEMPLATE), "required_inputs": [], "input_allowlist": []})
    run_dir = tmp_path / "detailed-run"
    candidate = _valid_run(run_dir)
    candidate.unlink()
    candidate.symlink_to(TEMPLATE)

    assert cli.cmd_inspect(Namespace(run_dir=run_dir, workbook=candidate)) == cli.BLOCKED
    report = json.loads(capsys.readouterr().out)
    assert any("unsafe or missing Detailed run artifact" in issue for issue in report["issues"])
