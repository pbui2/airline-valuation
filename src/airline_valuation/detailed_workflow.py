"""Fail-closed evidence report for the Detailed route.

This route deliberately does not publish a workbook until every required input,
source-use decision, lookup review, and independent review is supported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

PACKAGE_SCHEMA = "airline-valuation-detailed-evidence-bundle/1"
DECISION_SCHEMA = "airline-valuation-detailed-decision-report/2"
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "fcffsimpleginzu.xlsx"
_HEX = re.compile(r"[0-9a-f]{64}\\Z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False,
                     default=lambda item: {"$bytes_sha256": hashlib.sha256(item).hexdigest(), "$bytes_length": len(item)}
                     if isinstance(item, bytes) else str(item)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing regular file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON input must be an object")
    return data


def load_contract() -> dict[str, Any]:
    """Load the full Detailed-route input contract without narrowing provisional inputs."""
    route = _json(Path(__file__).with_name("detailed_contract.json"))
    base = _json(Path(__file__).with_name("provisional_contract.json"))
    if route.get("schema_version") != "airline-valuation-detailed-route-contract/1":
        raise ValueError("unsupported detailed contract schema")
    if route.get("contract_id") == base.get("contract_id"):
        raise ValueError("detailed contract identity must be distinct")
    refs = base.get("input_allowlist")
    metadata = base.get("input_metadata")
    if not isinstance(refs, list) or set(refs) != set(metadata or {}):
        raise ValueError("provisional input contract is malformed")
    if route.get("input_allowlist") != refs or set(route.get("input_metadata", {})) != set(refs):
        raise ValueError("detailed route must snapshot the complete provisional input contract")
    if route.get("input_metadata") != metadata:
        raise ValueError("detailed route input metadata differs from its provisional writer contract")
    from .provisional_scenario import _REQUIRED
    if route.get("required_inputs") != sorted(_REQUIRED) and set(route.get("required_inputs", [])) != _REQUIRED:
        raise ValueError("detailed route required inputs differ from the provisional writer contract")
    if route.get("formula_to_numeric_inputs") != base.get("formula_to_numeric_inputs"):
        raise ValueError("detailed route formula-to-numeric pins differ from the provisional writer contract")
    if route.get("d62") != base.get("d62"):
        raise ValueError("detailed route D62 pin differs from the provisional writer contract")
    immutable = route.get("immutable_template_pins")
    expected_pins = {key: base.get(key) for key in (
        "excluded_cells", "formula_fingerprints", "lookup_fingerprints", "lookup_rows",
        "pinned_members", "x14_validation_pins", "relationship_pins")}
    if not isinstance(immutable, dict) or immutable != expected_pins:
        raise ValueError("detailed route immutable template pins differ from the provisional writer contract")
    if route.get("worksheet_parts") != base.get("worksheet_parts") or route.get("sha256") != base.get("sha256"):
        raise ValueError("detailed route template identity differs from the provisional writer contract")
    merged = dict(base)
    merged["input_allowlist"] = list(route["input_allowlist"])
    merged["input_metadata"] = dict(route["input_metadata"])
    merged["required_inputs"] = list(route["required_inputs"])
    merged["detailed_route_contract"] = route
    return merged


def _required_cells() -> set[str]:
    from .provisional_scenario import _REQUIRED
    return set(_REQUIRED)


def build_report(bundle: dict[str, Any], policy_version: str | None = None) -> dict[str, Any]:
    """Build a report from the fixed policy implementation; bundle claims confer no authority."""
    from .detailed_decisions import build_decision_package
    from . import detailed_decisions

    policy_rules = getattr(detailed_decisions, "POLICIES", None)
    supported_policy = ("detailed-policy-set/" + canonical_digest(policy_rules)
                        if isinstance(policy_rules, dict) else None)
    blockers: list[str] = []
    if not isinstance(supported_policy, str):
        blockers.append("decision policy implementation does not expose a supported policy set")
    elif policy_version is not None and policy_version != supported_policy:
        blockers.append("requested policy version is not the implemented policy version")
    if not isinstance(bundle, dict):
        bundle = {}
        blockers.append("evidence bundle metadata is malformed")
    observations = bundle.get("observations", [])
    if not isinstance(observations, list):
        observations = []
        blockers.append("observations must be a list")
    if bundle.get("schema_version") != PACKAGE_SCHEMA:
        blockers.append("unsupported evidence bundle schema")

    try:
        # The opt-in is deliberately never exposed through bundle/CLI metadata.
        decision = build_decision_package(observations)
        json.dumps(decision, allow_nan=False, default=lambda v: {"$bytes_sha256": hashlib.sha256(v).hexdigest(), "$bytes_length": len(v)} if isinstance(v, bytes) else str(v))
    except Exception as exc:
        decision = {"status": "blocked", "cells": {}, "blocking_gaps": ["decision package processing failed safely: " + type(exc).__name__]}
        blockers.append("decision package processing failed safely")
    cells = decision.get("cells") if isinstance(decision.get("cells"), dict) else {}
    required = _required_cells()
    gaps = decision.get("blocking_gaps", [])
    if decision.get("status") != "decision_report_ready":
        blockers.extend(str(item) for item in gaps if isinstance(item, (str, int, float)))
        if not gaps:
            blockers.append("decision builder reports unresolved evidence or policy gaps")
    missing = sorted(required - set(cells))
    if missing:
        blockers.append("required scenario inputs have no decision: " + ", ".join(missing))
    unsupported = sorted(ref for ref in required if ref in cells and (
        not isinstance(cells[ref], dict) or cells[ref].get("decision") in (None, "") or
        cells[ref].get("status") in {"abstain", "blocked_missing_or_ineligible_evidence", "blocked_dependency"}))
    if unsupported:
        blockers.append("required input decisions abstain or are unsupported: " + ", ".join(unsupported))
    eligibility = decision.get("source_eligibility", {})
    if not isinstance(eligibility, dict) or eligibility.get("eligible") is not True:
        blockers.append("source, publication, purpose-specific permission, and cutoff suitability are not established")
    blockers.append("independent price, Treasury, and lookup suitability artifacts are unavailable")
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": DECISION_SCHEMA,
        "status": "unresolved",
        "summary": (
            f"Analysis completed with unresolved inputs ({len(blockers)} blockers). "
            "No Detailed workbook was published. Required-source rights and cutoff suitability "
            "are not established; independent price, Treasury, and lookup suitability artifacts "
            "are unavailable."
        ),
        "policy_version": supported_policy,
        "policy_sha256": canonical_digest(getattr(detailed_decisions, "POLICIES", {})),
        "evidence_bundle_sha256": canonical_digest(bundle),
        "decision_package_sha256": decision.get("canonical_sha256"),
        "decision_package": decision,
        "blockers": blockers,
        "workbook": None,
        "workbook_sha256": None,
        "required_cell_count": len(required),
        "decision_cell_count": len(cells),
    }


def validate_unresolved_report(run_dir: Path) -> bool:
    """Validate the exact file set and hashes of a published unresolved report."""
    run_dir = Path(run_dir)
    expected = {"decision-report.json", "ledger.json", "package.json", "report.txt", "run-state.json"}
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("unresolved report directory must be a regular directory")
    entries = list(run_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries) or {path.name for path in entries} != expected:
        raise ValueError("unresolved report artifact set is unsafe or unexpected")

    state = _json(run_dir / "run-state.json")
    report = _json(run_dir / "decision-report.json")
    ledger = _json(run_dir / "ledger.json")
    package = _json(run_dir / "package.json")
    if state.get("schema") != "airline-valuation-detailed-run-state/1" or state.get("outcome") != "unresolved":
        raise ValueError("unsupported unresolved run state")
    if state.get("candidate_workbook") is not None or state.get("workbook_sha256") is not None:
        raise ValueError("unresolved report must not bind a workbook")
    if report.get("status") != "unresolved" or report.get("workbook") is not None or report.get("workbook_sha256") is not None:
        raise ValueError("decision report is not unresolved")
    if report.get("canonical_sha256") != canonical_digest(
        {key: value for key, value in report.items() if key != "canonical_sha256"}
    ):
        raise ValueError("decision report canonical hash is invalid")
    if package != report.get("decision_package") or package.get("canonical_sha256") != canonical_digest(
        {key: value for key, value in package.items() if key != "canonical_sha256"}
    ):
        raise ValueError("decision package canonical hash is invalid")

    report_hash = sha256(run_dir / "decision-report.json")
    package_hash = sha256(run_dir / "package.json")
    ledger_hash = sha256(run_dir / "ledger.json")
    text_hash = sha256(run_dir / "report.txt")
    if (state.get("decision_report") != "decision-report.json" or state.get("ledger") != "ledger.json" or
            state.get("package") != "package.json" or state.get("report_text") != "report.txt" or
            state.get("report_sha256") != report_hash or state.get("ledger_sha256") != ledger_hash or
            state.get("report_text_sha256") != text_hash or
            ledger.get("decision_report_sha256") != report_hash or
            ledger.get("package_file_sha256") != package_hash or ledger.get("workbook_sha256") is not None):
        raise ValueError("unresolved report file hash binding is invalid")
    return True


def analyze(bundle_path: Path, output_dir: Path, *, policy_version: str | None = None, template: Path = TEMPLATE) -> dict[str, Any]:
    """Emit a canonical report and publish only after every independent suitability gate passes."""
    bundle = _json(bundle_path)
    report = build_report(bundle, policy_version)
    report["evidence_bundle_file_sha256"] = sha256(bundle_path)
    report["template_sha256"] = sha256(template) if template.is_file() and not template.is_symlink() else None
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("refusing to overwrite an existing detailed output class")
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=parent))
    try:
        report["canonical_sha256"] = canonical_digest({key: value for key, value in report.items() if key != "canonical_sha256"})
        serializer = lambda value: json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False,
            default=lambda v: {"$bytes_sha256": hashlib.sha256(v).hexdigest(), "$bytes_length": len(v)} if isinstance(v, bytes) else str(v)) + "\n"
        (staging / "decision-report.json").write_text(serializer(report), encoding="utf-8")
        decision_package = report.get("decision_package", {})
        (staging / "package.json").write_text(serializer(decision_package), encoding="utf-8")
        report_hash = sha256(staging / "decision-report.json")
        package_hash = sha256(staging / "package.json")
        ledger = {
            "schema_version": "airline-valuation-detailed-ledger/1", "outcome": "unresolved",
            "template_sha256": report["template_sha256"], "source_file_sha256": report["evidence_bundle_file_sha256"],
            "policy_version": report["policy_version"], "policy_sha256": report["policy_sha256"],
            "decision_package_sha256": report["decision_package_sha256"], "decision_report_sha256": report_hash,
            "package_file_sha256": package_hash, "workbook_sha256": None, "blockers": report["blockers"],
        }
        (staging / "ledger.json").write_text(serializer(ledger), encoding="utf-8")
        ledger_hash = sha256(staging / "ledger.json")
        decision_package = report.get("decision_package", {})
        cells = decision_package.get("cells", {}) if isinstance(decision_package, dict) else {}
        evidence_rows = decision_package.get("evidence", []) if isinstance(decision_package, dict) else []
        evidence_by_fact: dict[str, list[dict[str, Any]]] = {}
        for item in evidence_rows if isinstance(evidence_rows, list) else []:
            if isinstance(item, dict) and isinstance(item.get("fact"), str):
                evidence_by_fact.setdefault(item["fact"], []).append(item)

        def shown(value: Any) -> str:
            if value is None or value == "":
                return "unknown / not provided"
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)
            return str(value)

        package_cutoff = (decision_package.get("source_eligibility", {}).get("cutoff")
                          if isinstance(decision_package, dict) and isinstance(decision_package.get("source_eligibility"), dict)
                          else None)
        lines = [
            "Detailed analysis — unresolved",
            "",
            report["summary"],
            "",
            "Valuation cutoff",
            f"- Cutoff: {shown(package_cutoff)}",
            "- Cutoff suitability is not established for all required evidence.",
            "",
            "Economic uncertainty",
            "- Quantified economic uncertainty: unknown / not quantified; unresolved inputs prevent a supported valuation.",
            "- Per-cell reasons and uncertainty notes below are policy/evidence limitations, not probability estimates.",
            "",
            "Source rights and use",
            "- Purpose-specific rights eligibility: unknown / not established for the required evidence as a whole.",
            "- Unverified or ineligible observations are context only and are not represented as verified or adopted decisions.",
            "",
            "Required Detailed cells",
        ]
        for ref in sorted(_required_cells()):
            cell = cells.get(ref) if isinstance(cells, dict) else None
            lines.extend(["", f"{ref}"])
            if not isinstance(cell, dict):
                lines.extend(["- Decision: abstention; no decision cell was produced.", "- Status: missing decision cell.",
                              "- Source identity/hash/page/period/unit/publication/rights: unknown / not provided.",
                              "- Policy version/transform/reason/uncertainty: unknown / not provided.",
                              "- Historical alternative/comparator: unknown / not provided."])
                continue
            decision_value = cell.get("decision")
            abstention = decision_value in (None, "") or cell.get("status") in {
                "abstain", "blocked_missing_or_ineligible_evidence", "blocked_dependency"}
            lines.extend([
                f"- Decision: {'abstention' if abstention else shown(decision_value)}",
                f"- Status: {shown(cell.get('status'))}",
                f"- Unit: {shown(cell.get('unit'))}",
                f"- Policy ID/version: {shown(cell.get('policy_id'))} / {shown(cell.get('policy_version'))}",
                f"- Transform(s): {shown(cell.get('transforms'))}",
                f"- Reason: {shown(cell.get('reason'))}",
                f"- Uncertainty: {shown(cell.get('uncertainty', cell.get('economic_uncertainty')))}",
            ])
            facts = cell.get("evidence_facts", [])
            traces = cell.get("eligibility_trace", {})
            for fact in facts if isinstance(facts, list) else []:
                rows = evidence_by_fact.get(fact, [])
                trace = traces.get(fact) if isinstance(traces, dict) else None
                if not rows:
                    lines.append(
                        f"- Source for {fact}: identity=unknown / not provided; hash=unknown / not provided; "
                        f"page=unknown / not provided; period=unknown / not provided; unit=unknown / not provided; "
                        f"publication=unknown / not provided; rights/use=unknown / not established; "
                        f"eligibility={shown(trace)}"
                    )
                for row in rows:
                    lines.append(
                        f"- Source for {fact}: identity={shown(row.get('source_identity'))}; "
                        f"hash={shown(row.get('source_sha256'))}; page={shown(row.get('page'))}; "
                        f"period={shown(row.get('period'))}; date={shown(row.get('date'))}; "
                        f"unit={shown(row.get('unit'))}; publication={shown(row.get('publication'))}; "
                        f"rights/use={shown(row.get('source_use'))}; locator status={shown(row.get('locator_status'))}; "
                        f"eligibility={shown(trace)}"
                    )
            alternatives = {key: cell[key] for key in (
                "old_candidate_alternative", "historical_alternative_only", "comparator_only", "prior_candidate")
                if key in cell}
            lines.append(f"- Historical alternative/comparator: {shown(alternatives) if alternatives else 'unknown / not provided'}")
        lines.extend([
            "",
            "Blockers",
            *[f"- {item}" for item in report["blockers"]],
            "",
            f"Evidence bundle SHA-256: {report['evidence_bundle_file_sha256']}",
            f"Policy SHA-256: {report['policy_sha256']}",
            f"Decision report SHA-256: {report_hash}",
            "",
        ])
        (staging / "report.txt").write_text("\n".join(lines), encoding="utf-8")
        state = {
            "schema": "airline-valuation-detailed-run-state/1", "outcome": "unresolved", "candidate_workbook": None,
            "decision_report": "decision-report.json", "ledger": "ledger.json", "package": "package.json",
            "report_text": "report.txt", "template_sha256": report["template_sha256"],
            "source_sha256": report["evidence_bundle_file_sha256"], "policy_sha256": report["policy_sha256"],
            "decision_sha256": report["decision_package_sha256"], "report_sha256": report_hash,
            "workbook_sha256": None, "ledger_sha256": ledger_hash,
            "report_text_sha256": sha256(staging / "report.txt"),
        }
        (staging / "run-state.json").write_text(serializer(state), encoding="utf-8")
        validate_unresolved_report(staging)
        if output_dir.exists() or output_dir.is_symlink():
            raise ValueError("refusing to overwrite an existing detailed output class")
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def _synthetic_decision_package() -> dict[str, Any]:
    """Private test-only hook, kept separate from JSON and command-line inputs."""
    from .detailed_decisions import build_synthetic_test_decision_package
    return build_synthetic_test_decision_package()


def _validate_synthetic_test_package(package: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    from .detailed_decisions import SYNTHETIC_POLICY_VERSION

    if package.get("schema_version") != "airline-valuation-detailed-synthetic-test-package/1":
        raise ValueError("unsupported synthetic test package schema")
    if package.get("policy_version") != SYNTHETIC_POLICY_VERSION or not package.get("policy", {}).get("fixture_only"):
        raise ValueError("synthetic decision package policy is unsupported")
    if package.get("policy", {}).get("production_eligibility_relaxed") is not False:
        raise ValueError("synthetic test package cannot relax production eligibility")
    if package.get("status") != "synthetic_test_only" or package.get("calculated_workbook") is not False:
        raise ValueError("synthetic package provenance is not test-only")
    if package.get("canonical_sha256") != canonical_digest(
        {key: value for key, value in package.items() if key != "canonical_sha256"}
    ):
        raise ValueError("synthetic decision package canonical hash is invalid")
    required = set(contract["required_inputs"])
    cells = package.get("cells")
    if not isinstance(cells, dict) or set(package.get("required_cells", [])) != required or not required <= set(cells):
        raise ValueError("synthetic package does not bind the exact required contract inputs")
    decisions: dict[str, str] = {}
    for ref in required:
        cell = cells[ref]
        if not isinstance(cell, dict) or cell.get("decision") in (None, ""):
            raise ValueError(f"synthetic package has no usable decision for {ref}")
        if cell.get("status") != "synthetic_test_decision" or cell.get("policy_version") != SYNTHETIC_POLICY_VERSION:
            raise ValueError(f"synthetic decision provenance is invalid for {ref}")
        trace = cell.get("policy_trace")
        if not isinstance(trace, dict) or not isinstance(cell.get("source_sha256"), str):
            raise ValueError(f"synthetic policy/evidence trace is missing for {ref}")
        if not re.fullmatch(r"[0-9a-f]{64}", cell["source_sha256"]):
            raise ValueError(f"synthetic evidence hash is malformed for {ref}")
        decision = str(cell["decision"])
        if trace.get("cell") != ref or trace.get("value") != decision or trace.get("version") != SYNTHETIC_POLICY_VERSION:
            raise ValueError(f"synthetic evidence does not bind the decision for {ref}")
        raw_evidence = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00" + json.dumps(
            trace, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if hashlib.sha256(raw_evidence).hexdigest() != cell["source_sha256"]:
            raise ValueError(f"synthetic source artifact is stale or mismatched for {ref}")
        decisions[ref] = decision
    route = contract["detailed_route_contract"]
    for ref, expected in route["route_selectors"].items():
        if decisions.get(ref) != expected:
            raise ValueError(f"synthetic selector mismatch for {ref}")
    conversions = contract["formula_to_numeric_inputs"]
    if set(conversions) != set(route["formula_to_numeric_inputs"]):
        raise ValueError("synthetic formula-to-numeric exception set differs from contract")
    for ref in conversions:
        try:
            float(decisions[ref])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"formula-to-numeric input {ref} is not numeric") from exc
    for ref in required:
        kind = contract["input_metadata"][ref].get("kind")
        if kind in {"numeric", "formula_to_numeric"}:
            try:
                value = float(decisions[ref])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"numeric contract input {ref} is not numeric") from exc
            if not (value == value and abs(value) != float("inf")):
                raise ValueError(f"numeric contract input {ref} is not finite")
    # Independently establish synthetic market/lookup observations from the
    # fixture values and immutable route pins; these are never real-market claims.
    from decimal import Decimal
    price_material = {"shares_millions": decisions["Input sheet!B21"], "price_usd": decisions["Input sheet!B22"]}
    price = {"status": "synthetic_test_only", "market_cap_usd_millions": str(Decimal(price_material["shares_millions"]) * Decimal(price_material["price_usd"])),
             "inputs": price_material, "sha256": canonical_digest(price_material)}
    treasury_material = {"risk_free_rate": decisions["Input sheet!B34"], "policy_version": SYNTHETIC_POLICY_VERSION,
                         "provenance": "fictional fixture premise; not Treasury data"}
    treasury = {"status": "synthetic_test_only", "sha256": canonical_digest(treasury_material), "evidence": treasury_material}
    lookup_material = {"lookup_rows": route["immutable_template_pins"]["lookup_rows"],
                       "pinned_members": route["immutable_template_pins"]["pinned_members"]}
    if not lookup_material["lookup_rows"] or not lookup_material["pinned_members"]:
        raise ValueError("independent lookup test evidence is unavailable")
    lookup = {"status": "synthetic_test_only", "sha256": canonical_digest(lookup_material), "evidence": lookup_material}
    evidence = {"price": price, "treasury": treasury, "lookup": lookup}
    return decisions, evidence


def build_synthetic_test_candidate(output_dir: Path, *, template: Path = TEMPLATE) -> dict[str, Any]:
    """Create a fictional, Python-only ``prepared_uncalculated`` test candidate.

    This test fixture is not real source eligibility, a JSON/CLI option, or an
    issuer valuation. It publishes only to a new directory and never overwrites.
    """
    from .provisional_writer import write_provisional, verify_provisional_delta

    contract = load_contract()
    if template.is_symlink() or not template.is_file() or sha256(template) != contract.get("sha256"):
        raise ValueError("synthetic test candidate requires the pinned Detailed template")
    package = _synthetic_decision_package()
    decisions, evidence = _validate_synthetic_test_package(package, contract)
    conversions = {ref: decisions[ref] for ref in contract["formula_to_numeric_inputs"]}
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("refusing to overwrite an existing Detailed output class")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        candidate = staging / "candidate.xlsx"
        write_provisional(template, candidate, decisions, conversions, contract=contract)
        verify_provisional_delta(template, candidate, decisions, conversions, contract=contract)
        package_hash = canonical_digest(package)
        report = {
            "schema_version": "airline-valuation-detailed-test-candidate-report/1",
            "status": "synthetic_test_only", "outcome": "prepared_uncalculated",
            "policy_version": package["policy_version"], "test_only_provenance": package["policy"],
            "decision_package": package, "decision_package_sha256": package_hash,
            "test_evidence": evidence, "template_sha256": sha256(template),
            "canonical_sha256": "",
        }
        report["canonical_sha256"] = canonical_digest({key: value for key, value in report.items() if key != "canonical_sha256"})
        serializer = lambda value: json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        (staging / "package.json").write_text(serializer(package), encoding="utf-8")
        (staging / "decision-report.json").write_text(serializer(report), encoding="utf-8")
        candidate_hash = sha256(candidate)
        package_file_hash = sha256(staging / "package.json")
        report_hash = sha256(staging / "decision-report.json")
        ledger = {"schema": "airline-valuation-detailed-run-ledger/1", "candidate_sha256": candidate_hash,
                  "package_sha256": package_hash, "cells": decisions}
        (staging / "ledger.json").write_text(serializer(ledger), encoding="utf-8")
        ledger_hash = sha256(staging / "ledger.json")
        state = {"schema": "airline-valuation-detailed-run-state/1", "outcome": "prepared_uncalculated",
                 "template_path": str(template.resolve()), "template_sha256": sha256(template),
                 "candidate_sha256": candidate_hash, "decision_report_sha256": report_hash,
                 "ledger_sha256": ledger_hash, "package_file_sha256": package_file_hash,
                 "package_sha256": package_hash}
        (staging / "run-state.json").write_text(serializer(state), encoding="utf-8")
        verify_provisional_delta(template, candidate, decisions, conversions, contract=contract)
        if output_dir.exists() or output_dir.is_symlink():
            raise ValueError("refusing to overwrite an existing Detailed output class")
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return state
