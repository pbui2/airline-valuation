import hashlib
import json
from decimal import Decimal

from airline_valuation.detailed_decisions import (
    SYNTHETIC_POLICY_VERSION,
    build_decision_package,
    build_synthetic_test_decision_package,
)
from airline_valuation.detailed_workflow import load_contract

_MARKER = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00"
_IDENTITY = "Synthetic decision fixture"
_FIXTURE_ID = "detailed-decisions-test-v1"


def _artifact(fact, value):
    raw = _MARKER + fact.encode() + b":" + str(value).encode()
    digest = hashlib.sha256(raw).hexdigest()
    publication = {"fixture_id": _FIXTURE_ID, "source_sha256": digest, "published_at": "2025-12-31"}
    terms = {"fixture_id": _FIXTURE_ID, "source_sha256": digest,
             "purpose": "airline_valuation_decision", "permission": "allowed"}
    encode = lambda data: json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return {
        "fact": fact, "value": str(value), "source_sha256": digest, "source_identity": _IDENTITY,
        "identity": fact.replace("_", " "), "unit": "USD millions", "date": "2025-12-31",
        "page": "independent synthetic test artifact",
        "source": {"bytes": raw, "sha256": digest, "identity": _IDENTITY,
                   "kind": "synthetic_test", "fixture_id": _FIXTURE_ID},
        "publication": {"bytes": encode(publication)}, "terms": {"bytes": encode(terms)},
    }


def test_synthetic_policy_covers_contract_and_has_independent_arithmetic():
    package = build_synthetic_test_decision_package()
    contract = load_contract()
    required = set(contract["required_inputs"])
    assert package["issuer"] == "Synthetic test issuer"
    assert package["policy_version"] == SYNTHETIC_POLICY_VERSION
    assert set(package["cells"]) == required
    assert all(cell["decision"] is not None for cell in package["cells"].values())
    debt = package["cells"]["Input sheet!B15"]
    assert debt["decision"] == str(Decimal("220") + Decimal("30"))
    assert debt["lease_reconciliation"]["debt_components_reconcile"] is True
    assert debt["lease_reconciliation"]["operating_lease_disjoint"] is True
    assert set(debt["lease_reconciliation"]["source_hashes"]) == {
        "reported_debt", "other_borrowings", "finance_lease", "operating_lease"
    }
    assert package["cells"]["Input sheet!B18"]["decision"] == str(Decimal("95") + Decimal("5"))
    for ref, expected in contract["detailed_route_contract"]["route_selectors"].items():
        assert package["cells"][ref]["decision"] == expected
    for ref in required:
        cell = package["cells"][ref]
        assert cell["unit"]
        assert cell["policy_id"]
        assert cell["policy_version"] == SYNTHETIC_POLICY_VERSION
        assert cell["policy_trace"]
        meta = contract["input_metadata"][ref]
        if meta.get("kind") in ("selector", "dropdown_string"):
            assert cell["decision"] in meta["allowed"]
    for ref in {"Input sheet!B19", "Input sheet!B20", *(f"Input sheet!B{i}" for i in range(26, 33))}:
        cell = package["cells"][ref]
        assert cell["synthetic_alternatives"]
        assert cell["sensitivity"]["issuer_forecast"] is False


def test_independent_debt_mutation_reconciles_only_with_updated_component_artifact():
    package = build_synthetic_test_decision_package(observations=[
        _artifact("synthetic_reported_debt", "230"),
        _artifact("synthetic_other_borrowings", "222"),
    ])
    assert package["cells"]["Input sheet!B15"]["decision"] == "260"

    mismatch = build_synthetic_test_decision_package(observations=[
        _artifact("synthetic_reported_debt", "230"),
    ])
    cell = mismatch["cells"]["Input sheet!B15"]
    assert cell["decision"] is None
    assert cell["lease_reconciliation"]["debt_components_reconcile"] is False


def test_cash_mutation_requires_updated_artifacts_and_changes_b18_trace_hash():
    original = _artifact("synthetic_cash", "95")
    updated = _artifact("synthetic_cash", "101")
    package = build_synthetic_test_decision_package(observations=[updated])
    cell = package["cells"]["Input sheet!B18"]

    assert package["policy"]["fixture_only"] is True
    assert cell["decision"] == str(Decimal("101") + Decimal("5")) == "106"
    assert cell["eligibility_trace"]["synthetic_cash"]["source_sha256"] == updated["source_sha256"]
    assert cell["eligibility_trace"]["synthetic_cash"]["source_sha256"] != original["source_sha256"]

    stale = dict(original, value="101")
    blocked = build_synthetic_test_decision_package(observations=[stale])
    blocked_cell = blocked["cells"]["Input sheet!B18"]
    assert blocked_cell["decision"] is None
    assert blocked_cell["status"] == "blocked_missing_or_ineligible_evidence"
    assert blocked_cell["eligibility_trace"]["synthetic_cash"]["eligible"] is False


def test_finance_and_operating_lease_mutations_require_matching_hashes_and_classification():
    stale_finance = build_synthetic_test_decision_package(observations=[
        {"fact": "synthetic_finance_lease", "value": "10"},
    ])
    assert stale_finance["cells"]["Input sheet!B15"]["decision"] is None

    component_mismatch = build_synthetic_test_decision_package(observations=[
        _artifact("synthetic_finance_lease", "10"),
    ])
    assert component_mismatch["cells"]["Input sheet!B15"]["decision"] is None
    assert component_mismatch["cells"]["Input sheet!B15"]["lease_reconciliation"]["debt_components_reconcile"] is False

    overlap = build_synthetic_test_decision_package(observations=[
        _artifact("synthetic_operating_lease", "8"),
    ])
    assert overlap["cells"]["Input sheet!B15"]["decision"] is None
    assert overlap["cells"]["Input sheet!B15"]["lease_reconciliation"]["operating_lease_disjoint"] is False

    valid_operating_change = build_synthetic_test_decision_package(observations=[
        _artifact("synthetic_operating_lease", "35"),
    ])
    assert valid_operating_change["cells"]["Input sheet!B15"]["decision"] == "255"


def test_synthetic_path_is_python_only_and_real_default_remains_blocked():
    real = build_decision_package()
    assert real["issuer"] == "Southwest Airlines"
    assert real["status"] == "blocked_pending_verification"
    assert real["cells"]["Input sheet!B15"]["decision"] is None
    assert real["cells"]["Input sheet!B18"]["decision"] is None

    synthetic = build_synthetic_test_decision_package()
    assert synthetic["issuer"] != real["issuer"]
    assert synthetic["policy"]["fixture_only"] is True
    assert synthetic["policy"]["production_eligibility_relaxed"] is False
    assert not any("Southwest" in str(cell) for cell in synthetic["cells"].values())
