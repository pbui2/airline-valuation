from __future__ import annotations

import json
from pathlib import Path

from airline_valuation import cli


ROOT = Path(__file__).resolve().parents[1]


def pending_manifest() -> dict:
    return {
        "schema_version": "1.0", "entity": "Southwest Airlines Co.", "fiscal_period": "FY2025",
        "cutoff": "2026-02-06T23:59:59-05:00", "filings": [], "facts": [],
        "market_observations": [], "analyst_assumptions": [], "required_cells": [],
        "approval": {"status": "pending"}, "analyst_decisions": [],
        "review_packet": {"required_sections": []},
    }


def write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_pending_unmapped_manifest_review_exits_zero(tmp_path: Path, capsys):
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    out = tmp_path / "review"
    result = cli.main(["review", "--manifest", str(manifest), "--output-dir", str(out)])
    text = capsys.readouterr().out
    packet = json.loads((out / "review-packet.json").read_text())
    assert result == 0
    assert packet["status"] == "not ready"
    assert "no mapped input cells" in text.lower()
    assert packet["warnings"]


def test_review_overwrite_is_explicit(tmp_path: Path, capsys):
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    out = tmp_path / "review"
    assert cli.main(["review", "--manifest", str(manifest), "--output-dir", str(out)]) == 0
    assert cli.main(["review", "--manifest", str(manifest), "--output-dir", str(out)]) == 2
    assert "--overwrite" in capsys.readouterr().err
    assert cli.main(["review", "--manifest", str(manifest), "--output-dir", str(out), "--overwrite"]) == 0


def test_review_malformed_manifest_exits_two(tmp_path: Path, capsys):
    malformed = write_json(tmp_path / "bad.json", {"schema_version": "bad"})
    result = cli.main(["review", "--manifest", str(malformed), "--output-dir", str(tmp_path / "review")])
    assert result == 2
    assert "REVIEW INVALID" in capsys.readouterr().err


def test_validate_malformed_is_two_missing_is_three(tmp_path: Path):
    assert cli.main(["validate", "--manifest", str(tmp_path / "no.json")]) == 3
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert cli.main(["validate", "--manifest", str(bad)]) == 2


def test_required_contract_inputs_and_mode_are_not_self_declared_by_manifest():
    contract = {"input_allowlist": ["Input sheet!B11", "Cost of capital worksheet!B11", "Cost of capital worksheet!B12"],
                "required_inputs": ["Input sheet!B11", "Cost of capital worksheet!B12"],
                "input_metadata": {"Input sheet!B11": {"kind": "numeric", "unit": "USD millions"},
                                   "Cost of capital worksheet!B11": {"kind": "switch", "allowed": ["I will input"]},
                                   "Cost of capital worksheet!B12": {"kind": "numeric", "unit": "fraction"}}}
    manifest = pending_manifest()
    manifest["required_cells"] = ["Input sheet!B11", "Cost of capital worksheet!B11", "Cost of capital worksheet!B12"]
    manifest["facts"] = [{"id": "rev", "value": 10, "status": "approved", "unit": "USD millions", "workbook_cell": "Input sheet!B11"}]
    rows, blockers = cli._mapped_for_prepare(manifest, contract)
    assert rows
    assert any("Cost of capital worksheet!B11" in e for e in blockers)
    manifest["required_cells"] = []
    _, blockers = cli._mapped_for_prepare(manifest, contract)
    assert any("required contract input is not mapped" in e for e in blockers)


def test_unit_scale_is_exact_and_approved_status_required():
    contract = {"input_allowlist": ["Input sheet!B11"], "required_inputs": [], "input_metadata": {"Input sheet!B11": {"kind": "numeric", "unit": "USD millions"}}}
    manifest = pending_manifest()
    manifest["facts"] = [{"id": "rev", "value": 10, "status": "pending", "unit": "USD", "workbook_cell": "Input sheet!B11"}]
    _, blockers = cli._mapped_for_prepare(manifest, contract)
    assert any("not an approved value" in e for e in blockers)
    assert any("unit/scale mismatch" in e for e in blockers)


def test_unmapped_optional_switch_mutation_is_rejected_b17():
    contract = {"input_allowlist": ["Input sheet!B17", "Input sheet!B18"]}
    prepared = {"Input sheet!B17": "No", "Input sheet!B18": 0.0}
    manifest = pending_manifest()
    refs, issues = cli._compare_allowlisted_inputs(contract, prepared, lambda cell, sheet: "Yes" if cell == "B17" else 0, manifest)
    assert refs["Input sheet!B17"]["changed_from_prepared"] is True
    assert any("unapproved workbook input mutation: Input sheet!B17" in issue for issue in issues)


def test_unchanged_optional_switch_default_is_not_analyst_decision():
    contract = {"input_allowlist": ["Input sheet!B17"]}
    prepared = {"Input sheet!B17": "No"}
    refs, issues = cli._compare_allowlisted_inputs(contract, prepared, lambda cell, sheet: "No", pending_manifest())
    assert not issues
    assert refs["Input sheet!B17"]["changed_from_prepared"] is False
    assert refs["Input sheet!B17"]["analyst_approved_mapping"] is False
    assert "not an analyst decision" in refs["Input sheet!B17"]["interpretation"]


def test_attestation_binds_hashes_and_settings():
    att = {"schema": cli.ATTESTATION_SCHEMA, "manifest_sha256": "a", "prepared_sha256": "b", "saved_sha256": "c",
           "excel_version": "Excel", "calculation_mode": "Automatic", "full_recalculation": True,
           "saved": True, "reopened": True, "iteration_enabled": False, "iteration_count": None,
           "iteration_maximum_change": None, "signed_by": "Reviewer", "signed_date": "2026-03-01", "signature": "signed"}
    expected = {"manifest_sha256": "a", "prepared_sha256": "b", "saved_sha256": "c"}
    assert cli._attestation_errors(att, expected) == []
    att["saved_sha256"] = "old"
    assert any("saved_sha256" in e for e in cli._attestation_errors(att, expected))


def test_inspect_pending_manifest_fails_closed_before_workbook_checks(tmp_path: Path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    write_json(run_dir / cli.RUN_STATE, {"manifest_path": str(manifest), "manifest_sha256": cli.sha256(manifest)})
    workbook = tmp_path / "saved.xlsx"
    workbook.write_bytes(b"not opened because manifest is pending")
    result = cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])
    assert result == 2
    captured = capsys.readouterr()
    assert "not ready" in captured.out
    report = json.loads((run_dir / "inspect-report.json").read_text())
    assert any("manifest not ready" in issue for issue in report["issues"])
    assert json.loads((run_dir / cli.RUN_STATE).read_text())["outcome"] == "unverified"


def test_inspect_requires_ready_direct_branch_status():
    branch = {"branch": "unsupported", "ready": True, "unsupported_branches": ["selector"], "type_failures": ["output"]}
    failures = []
    if branch.get("branch") != "direct_input": failures.append("branch")
    if branch.get("ready") is not True: failures.append("ready")
    if branch.get("unsupported_branches"): failures.append("unsupported")
    if branch.get("type_failures"): failures.append("type")
    assert failures == ["branch", "unsupported", "type"]


def test_summary_cell_values_exclude_prepared_comparison_fields(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = {"status": "unverified", "saved_cell_references": {
        "Input sheet!B11": {"saved_value": 9, "prepared_default": 1, "changed_from_prepared": True,
                            "analyst_approved_mapping": True},
        "Valuation output!B33": {"saved_value": 2.5, "type": "numeric", "required": True}},
        "limitations": ["Saved cache only; no calculation performed."]}
    cli._persist_inspection(run_dir, report)
    persisted_report = json.loads((run_dir / "inspect-report.json").read_text())
    summary = json.loads((run_dir / "summary.json").read_text())
    assert persisted_report["saved_cell_references"]["Input sheet!B11"]["prepared_default"] == 1
    assert summary["saved_cell_references"] == {
        "Input sheet!B11": {"saved_value": 9},
        "Valuation output!B33": {"saved_value": 2.5}}
    assert summary["limitations"] == report["limitations"]


def test_inspect_missing_state_writes_report_and_summary(tmp_path: Path, capsys):
    workbook = tmp_path / "saved.xlsx"
    workbook.write_bytes(b"synthetic-xlsx-bytes")
    result = cli.main(["inspect", "--run-dir", str(tmp_path / "run"), "--workbook", str(workbook)])
    assert result == 3
    report = json.loads((tmp_path / "run" / "inspect-report.json").read_text())
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert report["status"] == summary["status"] == "unverified"
    assert report["saved_sha256"] == cli.sha256(workbook)
    assert "report_path" in report and "summary_path" in report


def test_attestation_missing_is_recorded_in_report(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    write_json(run_dir / cli.RUN_STATE, {"manifest_path": str(manifest), "manifest_sha256": cli.sha256(manifest), "prepared_path": str(tmp_path / "prepared.xlsx")})
    (tmp_path / "prepared.xlsx").write_bytes(b"prepared")
    saved = tmp_path / "saved.xlsx"
    saved.write_bytes(b"saved")
    assert cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(saved)]) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text())
    assert report["status"] == "unverified"
    assert "missing file" in " ".join(report["issues"])
    assert json.loads((run_dir / cli.RUN_STATE).read_text())["outcome"] == "unverified"


def test_cached_identity_checks_include_b31_b33():
    class FakeWorkbook:
        vals = {"B29": 150.0, "B24": 200.0, "B25": 50.0, "B26": 20.0, "B27": 10.0, "B28": 10.0,
                "B31": 130.0, "B30": 20.0, "B33": 4.0, "B32": 32.5}
        def read_output(self, cell, sheet): return self.vals[cell]
    assert cli._identities(FakeWorkbook()) == []
    FakeWorkbook.vals["B31"] = 1
    assert any("B31=B29-B30" in e for e in cli._identities(FakeWorkbook()))
    FakeWorkbook.vals["B31"] = 130.0
    FakeWorkbook.vals["B33"] = 99
    assert any("B33=B31/B32" in e for e in cli._identities(FakeWorkbook()))


def test_active_diagnostic_specs_use_saved_cache_and_type_rules():
    class FakeBook:
        def read_output(self, cell, sheet):
            assert (sheet, cell) == ("Diagnostics", "B9")
            return "#N/A"
    contract = {"conditional_diagnostics_by_branch": {"direct_input": [{"cell": "Diagnostics!B9", "type": "numeric", "required": True}]}}
    specs = contract["conditional_diagnostics_by_branch"]["direct_input"]
    observed, errors = {}, []
    for spec in specs:
        ref = spec["cell"]
        sheet, cell = ref.rsplit("!", 1)
        value = FakeBook().read_output(cell, sheet)
        observed[ref] = {"saved_value": value, "type": spec["type"], "required": spec["required"]}
        if isinstance(value, str) and value.startswith("#"):
            errors.append(ref)
        if spec["required"] and spec["type"] == "numeric" and not cli._finite_number(value):
            errors.append(ref)
    assert observed["Diagnostics!B9"]["saved_value"] == "#N/A"
    assert errors == ["Diagnostics!B9", "Diagnostics!B9"]


def _prepare_test_dependencies(monkeypatch, tmp_path: Path):
    manifest = {"facts": [{"id": "revenue", "value": "1", "unit": "millions", "status": "approved", "workbook_cell": "Input sheet!B11"}]}
    raw = json.dumps(manifest).encode()
    item = manifest["facts"][0]
    rows = [("Input sheet!B11", item["value"], item, "facts")]
    contract = {"source_file": str(ROOT / "fcffsimpleginzu.xlsx"), "input_allowlist": ["Input sheet!B11"], "required_inputs": [],
                "input_metadata": {"Input sheet!B11": {"kind": "numeric", "unit": "millions", "meaning": "revenue"}}}

    class FakeWorkbook:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def verify_identity(self): return {"matches_template": True}
        def verify_structure(self, **_kwargs): return {"valid": True}

    original_load_json = cli.load_json
    monkeypatch.setattr(cli, "load_json", lambda path: (manifest, raw, []) if Path(path).name == "manifest.json" else original_load_json(path))
    monkeypatch.setattr(cli, "_manifest_state", lambda _manifest: ([], []))
    monkeypatch.setattr(cli, "_mapped_for_prepare", lambda _manifest, _contract: (rows, []))
    monkeypatch.setattr(cli, "_contract", lambda: contract)
    monkeypatch.setattr(cli, "Workbook", FakeWorkbook)
    monkeypatch.setattr(cli, "_normalize_mapped_entry", lambda _ref, _item, _meta: ("1", "millions"))
    monkeypatch.setattr(cli, "write_prepared", lambda _template, dest, _entries: (Path(dest).write_bytes(b"prepared") and Path(dest)))
    monkeypatch.setattr(cli, "verify_prepared_delta", lambda *_args: {"valid": True})
    return manifest


def test_prepare_populates_normalized_ledger_checklist_and_run_state(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    out = tmp_path / "run"
    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out)]) == 0
    state = json.loads((out / cli.RUN_STATE).read_text())
    record = json.loads((out / "ledger.jsonl").read_text())
    assert state["outcome"] == "awaiting Excel recalculation"
    assert state["prepared_sha256"] == cli.sha256(out / "prepared.xlsx")
    assert record["items"][0]["raw_value"] == "1"
    assert record["items"][0]["normalized_value"] == "1"
    assert record["items"][0]["normalized_unit"] == "millions"
    assert "1 [millions]" in (out / "checklist.txt").read_text()


def test_prepare_overwrite_publish_failure_restores_previous_run(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    out = tmp_path / "run"
    out.mkdir()
    (out / "sentinel").write_text("old run")
    original_replace = cli.os.replace

    def fail_publish(source, destination):
        if Path(destination) == out and ".stage-" in Path(source).name:
            raise OSError("injected publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(cli.os, "replace", fail_publish)
    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out), "--overwrite"]) == 2
    assert (out / "sentinel").read_text() == "old run"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json", "run"]
    assert "injected publish failure" in capsys.readouterr().err


def test_prepare_refuses_orphan_backup_and_preserves_recoverable_run(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    out = tmp_path / "run"
    out.mkdir()
    (out / "important-artifact.txt").write_text("prior generation")
    backup = tmp_path / ".run.backup-previous-pid-18427"
    out.rename(backup)

    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out), "--overwrite"]) == 2
    error = capsys.readouterr().err
    assert "orphan run backup" in error
    assert "manually restore" in error
    assert not out.exists()
    assert (backup / "important-artifact.txt").read_text() == "prior generation"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".run.backup-previous-pid-18427", "manifest.json"]


def test_prepare_rejects_symlink_output_directory(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    target = tmp_path / "target"
    target.mkdir()
    out = tmp_path / "run-link"
    out.symlink_to(target, target_is_directory=True)
    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out)]) == 2
    assert "symlink" in capsys.readouterr().err
    assert list(target.iterdir()) == []


def test_prepare_stage_validation_failure_preserves_old_run(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    out = tmp_path / "run"
    out.mkdir()
    (out / "sentinel").write_text("old")
    monkeypatch.setattr(cli, "_validate_staged_run", lambda *_args: (_ for _ in ()).throw(ValueError("injected stage validation failure")))
    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out), "--overwrite"]) == 2
    assert (out / "sentinel").read_text() == "old"
    assert "injected stage validation failure" in capsys.readouterr().err


def test_prepare_backup_cleanup_failure_keeps_published_generation_successful(tmp_path: Path, monkeypatch, capsys):
    _prepare_test_dependencies(monkeypatch, tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    out = tmp_path / "run"
    out.mkdir()
    (out / "sentinel").write_text("old")
    original_rmtree = cli.shutil.rmtree
    monkeypatch.setattr(cli.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("injected backup cleanup failure")) if ".backup-" in Path(path).name else original_rmtree(path))
    assert cli.main(["prepare", "--manifest", str(manifest_path), "--output-dir", str(out), "--overwrite"]) == 0
    assert (out / "prepared.xlsx").read_bytes() == b"prepared"
    assert "published successfully" in capsys.readouterr().err


def _inspect_with_real_template(run_dir: Path, workbook: Path) -> int:
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    template = ROOT / "fcffsimpleginzu.xlsx"
    write_json(run_dir / cli.RUN_STATE, {"manifest_path": str(manifest), "manifest_sha256": cli.sha256(manifest), "prepared_path": str(template), "template_path": str(template), "template_sha256": cli.sha256(template), "prepared_sha256": cli.sha256(template)})
    return cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(workbook)])


def test_inspect_rejects_prepared_path_outside_run_directory(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    template = ROOT / "fcffsimpleginzu.xlsx"
    manifest = ROOT / "examples" / "fy2025-manifest.json"
    write_json(run_dir / cli.RUN_STATE, {"manifest_path": str(manifest), "manifest_sha256": cli.sha256(manifest),
        "prepared_path": str(template), "template_path": str(template), "template_sha256": cli.sha256(template),
        "prepared_sha256": cli.sha256(template)})
    saved = tmp_path / "saved.xlsx"
    saved.write_bytes(b"unopened")
    assert cli.main(["inspect", "--run-dir", str(run_dir), "--workbook", str(saved)]) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text())
    assert any("prepared workbook path escapes" in issue for issue in report["issues"])
    assert json.loads((run_dir / cli.RUN_STATE).read_text())["outcome"] == "unverified"


def test_inspect_malformed_saved_zip_writes_unverified_report(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    malformed = tmp_path / "malformed.xlsx"
    malformed.write_bytes(b"not a ZIP workbook")
    assert _inspect_with_real_template(run_dir, malformed) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text())
    assert report["status"] == "unverified"
    assert any("workbook structure/read error" in item for item in report["issues"])
    assert json.loads((run_dir / cli.RUN_STATE).read_text())["outcome"] == "unverified"


def test_inspect_real_template_missing_attestation_is_unverified(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    template = ROOT / "fcffsimpleginzu.xlsx"
    assert _inspect_with_real_template(run_dir, template) == 2
    report = json.loads((run_dir / "inspect-report.json").read_text())
    assert report["status"] == "unverified"
    assert any("missing file" in item and "attestation" in item for item in report["issues"])
    assert json.loads((run_dir / cli.RUN_STATE).read_text())["outcome"] == "unverified"


def test_inspect_absent_workbook_is_three(tmp_path: Path):
    result = cli.main(["inspect", "--run-dir", str(tmp_path / "run"), "--workbook", str(tmp_path / "missing.xlsx")])
    assert result == 3
    report = json.loads((tmp_path / "run" / "inspect-report.json").read_text())
    assert report["status"] == "unverified"
    assert "missing workbook" in " ".join(report["issues"])
