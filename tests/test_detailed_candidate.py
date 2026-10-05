from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airline_valuation.detailed_workflow import (  # noqa: E402
    TEMPLATE,
    build_synthetic_test_candidate,
    canonical_digest,
    sha256,
)


def test_AC3_synthetic_candidate_has_exact_contract_delta_and_independent_evidence(tmp_path):
    out = tmp_path / "fixture-run"
    result = build_synthetic_test_candidate(out)

    assert result["outcome"] == "prepared_uncalculated"
    assert {path.name for path in out.iterdir()} == {
        "package.json", "decision-report.json", "ledger.json", "run-state.json", "candidate.xlsx"
    }
    state = json.loads((out / "run-state.json").read_text())
    package = json.loads((out / "package.json").read_text())
    report = json.loads((out / "decision-report.json").read_text())
    ledger = json.loads((out / "ledger.json").read_text())
    assert state["schema"] == "airline-valuation-detailed-run-state/1"
    assert state["template_path"] == str(TEMPLATE)
    assert state["candidate_sha256"] == sha256(out / "candidate.xlsx")
    assert state["decision_report_sha256"] == sha256(out / "decision-report.json")
    assert state["ledger_sha256"] == sha256(out / "ledger.json")
    assert state["package_file_sha256"] == sha256(out / "package.json")
    assert state["package_sha256"] == canonical_digest(package)
    assert report["decision_package_sha256"] == canonical_digest(package)
    assert report["decision_package"] == package
    assert ledger["schema"] == "airline-valuation-detailed-run-ledger/1"
    assert ledger["candidate_sha256"] == state["candidate_sha256"]
    assert ledger["package_sha256"] == state["package_sha256"]
    assert ledger["cells"] == {
        ref: str(cell["decision"]) for ref, cell in package["cells"].items()
        if cell.get("decision") not in (None, "")
    }
    assert report["test_evidence"]["price"]["sha256"]
    assert report["test_evidence"]["treasury"]["sha256"]
    assert report["test_evidence"]["lookup"]["sha256"]


def test_synthetic_candidate_rolls_back_on_publish_failure(tmp_path, monkeypatch):
    import airline_valuation.detailed_workflow as workflow

    out = tmp_path / "fixture-run"
    monkeypatch.setattr(workflow.os, "rename", lambda *_: (_ for _ in ()).throw(OSError("injected publish failure")))
    with pytest.raises(OSError, match="injected"):
        build_synthetic_test_candidate(out)
    assert not out.exists()
    assert not list(tmp_path.glob(".fixture-run.staging-*"))


@pytest.mark.parametrize("mutation", ["policy", "evidence", "cell", "stale_source"])
def test_synthetic_candidate_rejects_mutated_decision_package(tmp_path, monkeypatch, mutation):
    import airline_valuation.detailed_workflow as workflow
    from airline_valuation.detailed_decisions import build_synthetic_test_decision_package

    def altered():
        package = build_synthetic_test_decision_package()
        if mutation == "policy":
            package["policy_version"] = "unrecognized-policy"
        elif mutation == "evidence":
            package["cells"]["Input sheet!B22"]["policy_trace"]["value"] = "999"
        elif mutation == "stale_source":
            cell = package["cells"]["Input sheet!B22"]
            cell["decision"] = "999"
            cell["policy_trace"]["value"] = "999"
            package["canonical_sha256"] = canonical_digest(
                {key: value for key, value in package.items() if key != "canonical_sha256"}
            )
        else:
            package["cells"]["Input sheet!B22"]["decision"] = None
        if mutation != "stale_source":
            package["canonical_sha256"] = canonical_digest(
                {key: value for key, value in package.items() if key != "canonical_sha256"}
            )
        return package

    monkeypatch.setattr(workflow, "_synthetic_decision_package", altered)
    with pytest.raises(ValueError):
        build_synthetic_test_candidate(tmp_path / "mutated")
    assert not (tmp_path / "mutated").exists()


def test_synthetic_candidate_does_not_accept_json_or_real_route_switch(tmp_path):
    from airline_valuation.detailed_workflow import analyze, PACKAGE_SCHEMA

    source = tmp_path / "bundle.json"
    source.write_text(json.dumps({"schema_version": PACKAGE_SCHEMA, "synthetic": True, "observations": []}))
    out = tmp_path / "ordinary-run"
    assert analyze(source, out)["status"] == "unresolved"
    assert not (out / "candidate.xlsx").exists()
