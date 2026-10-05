from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from airline_valuation.detailed_workflow import (  # noqa: E402
    PACKAGE_SCHEMA, analyze, build_report, load_contract, validate_unresolved_report,
)


def _save(path: Path, value: dict):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def test_detailed_contract_is_an_independent_complete_template_snapshot():
    contract = load_contract()
    route = contract["detailed_route_contract"]
    provisional = json.loads((ROOT / "src/airline_valuation/provisional_contract.json").read_text())
    assert route["contract_id"] != provisional.get("contract_id")
    assert route["input_allowlist"] == provisional["input_allowlist"]
    assert route["input_metadata"] == provisional["input_metadata"]
    assert set(route["input_allowlist"]) == set(route["input_metadata"])
    assert set(route["required_inputs"]) == set(contract["required_inputs"])
    assert {"Input sheet!B15", "Input sheet!B17", "Input sheet!B18",
            "Input sheet!B19", "Input sheet!B20", *[f"Input sheet!B{i}" for i in range(26, 33)],
            *[f"Cost of capital worksheet!H{i}" for i in range(21, 32)]} <= set(contract["input_allowlist"])
    assert route["route_selectors"] == {
        "Cost of capital worksheet!B11": "Detailed",
        "Cost of capital worksheet!B21": "Single Business(US)",
        "Cost of capital worksheet!B25": "Operating regions",
        "Cost of capital worksheet!B33": "Actual rating",
        "Cost of capital worksheet!B35": "Baa2/BBB",
    }
    assert route["formula_to_numeric_inputs"] == provisional["formula_to_numeric_inputs"]
    assert route["d62"] == provisional["d62"]
    pins = route["immutable_template_pins"]
    for key in ("relationship_pins", "formula_fingerprints", "lookup_fingerprints",
                "lookup_rows", "pinned_members", "x14_validation_pins"):
        assert pins[key] == provisional[key]
    assert {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"} == set(route["formula_to_numeric_inputs"])
    assert route["d62_cell"] == "Cost of capital worksheet!D62"


def test_detailed_contract_loader_rejects_incomplete_snapshot_and_changed_pins(monkeypatch):
    import copy
    import airline_valuation.detailed_workflow as workflow
    original = workflow._json
    route_path = Path(workflow.__file__).with_name("detailed_contract.json")
    for mutate, message in (
        (lambda route: route.pop("input_metadata"), "complete provisional input contract"),
        (lambda route: route["immutable_template_pins"]["relationship_pins"].pop("_rels/.rels"), "immutable template pins"),
        (lambda route: route["required_inputs"].pop(), "required inputs"),
    ):
        changed = False
        def altered(path):
            nonlocal changed
            value = original(path)
            if Path(path) == route_path:
                value = copy.deepcopy(value)
                mutate(value)
                changed = True
            return value
        monkeypatch.setattr(workflow, "_json", altered)
        with pytest.raises(ValueError, match=message):
            load_contract()
        assert changed
        monkeypatch.setattr(workflow, "_json", original)


def test_builder_backed_report_is_explicitly_unresolved_and_cannot_create_workbook():
    bundle = {"schema_version": PACKAGE_SCHEMA, "observations": []}
    report = build_report(bundle, "test-policy-1")
    assert report["status"] == "unresolved"
    assert "unresolved" in report["summary"].lower()
    assert report["workbook"] is None
    assert report["workbook_sha256"] is None
    assert report["blockers"]
    assert report["decision_package"]["cells"]
    assert "not the implemented policy version" in " ".join(report["blockers"])


def test_report_requires_independent_source_and_market_suitability(monkeypatch):
    import airline_valuation.detailed_decisions as decisions
    monkeypatch.setattr(decisions, "POLICY_VERSION", "decision-policy/v1", raising=False)
    monkeypatch.setattr(decisions, "build_decision_package", lambda observations: {
        "status": "decision_report_ready", "blocking_gaps": [], "cells": {},
        "canonical_sha256": "0" * 64,
        "source_eligibility": {"eligible": True},
    })
    monkeypatch.setattr("airline_valuation.detailed_workflow._required_cells", lambda: set())
    bundle = {"schema_version": PACKAGE_SCHEMA, "observations": []}
    report = build_report(bundle, "decision-policy/v1")
    assert report["status"] == "unresolved"
    assert any("price, Treasury, and lookup" in blocker for blocker in report["blockers"])
    assert report["workbook"] is None
    assert report["workbook_sha256"] is None


def test_AC2_analyze_emits_unresolved_package_and_never_candidate_for_json_bundle(tmp_path):
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    out = tmp_path / "detailed-report"
    result = analyze(source, out, policy_version="unimplemented-policy-label")
    assert result["status"] == "unresolved"
    assert (out / "decision-report.json").is_file()
    assert (out / "report.txt").is_file()
    text_report = (out / "report.txt").read_text(encoding="utf-8")
    required = set(load_contract()["required_inputs"])
    for ref in required:
        assert ref in text_report
    for ref in {"Input sheet!B15", "Input sheet!B18", "Input sheet!B19", "Input sheet!B20",
                *[f"Input sheet!B{i}" for i in range(26, 33)]}:
        assert ref in text_report
    assert "Valuation cutoff" in text_report
    header = text_report.split("Valuation cutoff", 1)[0]
    assert len(header) <= 500
    assert "blockers" in header
    assert "source" in header.lower() and "cutoff" in header.lower()
    cells_section = text_report.split("Required Detailed cells", 1)[1].split("\nBlockers\n", 1)[0]
    for ref in required:
        assert f"\n{ref}\n" in cells_section
    assert "Economic uncertainty" in text_report
    assert "Source rights and use" in text_report
    assert "Decision: abstention" in text_report
    assert "publication=unknown / not provided" in text_report
    assert "eligibility=" in text_report
    assert "Historical alternative/comparator:" in text_report
    assert "IATA global airline outlook" in text_report
    assert "historical_context_only_not_forecast" in text_report
    assert "Policy ID/version:" in text_report
    assert "Transform(s):" in text_report
    assert "Reason:" in text_report
    assert "Uncertainty:" in text_report
    assert "human sign-off" not in text_report.lower()
    assert (out / "package.json").is_file()
    assert (out / "ledger.json").is_file()
    assert not (out / "candidate.xlsx").exists()
    saved = json.loads((out / "decision-report.json").read_text())
    blocker_section = text_report.split("\nBlockers\n", 1)[1].split("\n\nEvidence bundle", 1)[0]
    assert [line[2:] for line in blocker_section.splitlines()] == saved["blockers"]
    state = json.loads((out / "run-state.json").read_text())
    assert saved["evidence_bundle_file_sha256"]
    assert saved["policy_version"].startswith("detailed-policy-set/")
    assert state["schema"] == "airline-valuation-detailed-run-state/1"
    assert state["outcome"] == "unresolved"
    assert state["candidate_workbook"] is None
    ledger = json.loads((out / "ledger.json").read_text())
    assert ledger["decision_report_sha256"] == state["report_sha256"]
    assert ledger["package_file_sha256"] == __import__("hashlib").sha256((out / "package.json").read_bytes()).hexdigest()
    assert state["ledger_sha256"] == __import__("hashlib").sha256((out / "ledger.json").read_bytes()).hexdigest()
    assert state["report_text_sha256"] == __import__("hashlib").sha256((out / "report.txt").read_bytes()).hexdigest()
    assert validate_unresolved_report(out) is True


def test_AC3_arbitrary_policy_versions_are_reported_unresolved(tmp_path):
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    report = analyze(source, tmp_path / "policy-negative", policy_version="policy-version-user-asserted")
    assert report["status"] == "unresolved"
    assert "requested policy version is not the implemented policy version" in report["blockers"]
    assert not (tmp_path / "policy-negative" / "candidate.xlsx").exists()


def test_AC4_run_state_hash_binds_published_report_and_ledger(tmp_path):
    import hashlib
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    out = tmp_path / "hash-bound"
    analyze(source, out)
    state = json.loads((out / "run-state.json").read_text())
    ledger = json.loads((out / "ledger.json").read_text())
    assert state["report_sha256"] == hashlib.sha256((out / "decision-report.json").read_bytes()).hexdigest()
    assert state["ledger_sha256"] == hashlib.sha256((out / "ledger.json").read_bytes()).hexdigest()
    assert ledger["decision_report_sha256"] == state["report_sha256"]
    assert ledger["source_file_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert state["workbook_sha256"] is None
    assert state["report_text_sha256"] == hashlib.sha256((out / "report.txt").read_bytes()).hexdigest()
    assert validate_unresolved_report(out)


def test_AC2_unresolved_report_tampering_is_detected(tmp_path):
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    out = tmp_path / "tampered"
    analyze(source, out)
    (out / "report.txt").write_text("tampered report\\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash binding"):
        validate_unresolved_report(out)


def test_user_authored_complete_reviewed_cells_are_not_accepted(tmp_path):
    source = _save(tmp_path / "fake.json", {
        "schema_version": "airline-valuation-reviewed-detailed-package/1",
        "status": "complete_reviewed", "cells": {"Input sheet!B27": {"decision": "0.03"}},
    })
    report = analyze(source, tmp_path / "blocked", policy_version="1")
    assert report["status"] == "unresolved"
    assert "unsupported evidence bundle schema" in report["blockers"]
    assert not (tmp_path / "blocked" / "candidate.xlsx").exists()


@pytest.mark.parametrize("failure_point", ["write", "publish"])
def test_analyze_cleans_staging_after_write_or_publish_failure(tmp_path, monkeypatch, failure_point):
    import airline_valuation.detailed_workflow as workflow
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    out = tmp_path / "run"
    if failure_point == "write":
        original = Path.write_text
        def fail_report(path, *args, **kwargs):
            if path.name == "decision-report.json":
                raise OSError("injected report write failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", fail_report)
    else:
        monkeypatch.setattr(workflow.os, "rename", lambda *args: (_ for _ in ()).throw(OSError("injected publish failure")))
    with pytest.raises(OSError, match="injected"):
        analyze(source, out, policy_version="1")
    assert not out.exists()
    assert not list(tmp_path.glob(".run.staging-*"))


def test_existing_output_class_is_never_overwritten(tmp_path):
    source = _save(tmp_path / "evidence.json", {"schema_version": PACKAGE_SCHEMA, "observations": []})
    out = tmp_path / "run"
    out.mkdir()
    with pytest.raises(ValueError, match="overwrite"):
        analyze(source, out, policy_version="1")
