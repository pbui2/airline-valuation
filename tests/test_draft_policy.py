from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from airline_valuation.trial_guidance import PINNED_WORKBOOK_SHA256  # noqa: E402
from draft_policy_evaluator import (  # noqa: E402
    CONTRACT_PATH, FIXTURE_PATH, MANIFEST_PATH, POLICY_PATH, evaluate, evaluate_entry,
    read_json, rule_for, workbook_sha256,
)


def _policy():
    return read_json(POLICY_PATH)


def _fixtures():
    return read_json(FIXTURE_PATH)


def _entry_refs(policy):
    return [entry.get("reference") for entry in policy.get("entries", [])]


def _case(name):
    return next(case for case in _fixtures()["cases"] if case["name"] == name)


def _run(case, *, policy=None):
    policy = policy or _policy()
    rule = rule_for(policy, case["rule_id"])
    context = dict(_fixtures()["context"])
    context.update(case.get("context_overrides", {}))
    if case.get("mutate_fallback"):
        rule = copy.deepcopy(rule)
        rule["evaluation"]["fallback"].update(case["mutate_fallback"])
    result = evaluate(rule, case["observations"], context=context, route=case.get("route"), policy=policy)
    trace = result["structured_trace"]
    assert trace["policy_version"]["schema_version"] == policy["schema_version"]
    assert trace["reference"] in rule["references"]
    assert all(locator["source_hash"] == context["source_sha256"] for locator in trace["source_locators"]) or not trace["source_locators"]
    assert trace["classification"] == "not_assessed" or isinstance(trace["classification"], dict)
    assert isinstance(trace["source_locators"], list) and isinstance(trace["rejected_candidates"], list)
    return result


def test_draft_inventory_matches_detailed_contract():
    contract = read_json(CONTRACT_PATH)
    policy = _policy()
    expected = contract["input_allowlist"]
    refs = _entry_refs(policy)
    assert policy["schema_version"] == "ginzu-input-policy-review/1"
    assert len(expected) == 67
    assert refs == expected, "draft inventory must contain all 67 references in contract order"


def test_draft_branch_and_formula_metadata():
    contract = read_json(CONTRACT_PATH)
    policy = _policy()
    entries = {row["reference"]: row for row in policy.get("entries", [])}
    assert list(entries) == contract["input_allowlist"], "every reference needs independently inspectable metadata"
    assert set(contract["formula_to_numeric_inputs"]) == {"Input sheet!B27", "Input sheet!B28", "Input sheet!B32"}
    for ref in contract["input_allowlist"]:
        entry = entries[ref]
        evidence = entry["verified_workbook_evidence"]
        assert evidence["cell_hash"] != "not transcribed in this draft" and len(evidence["cell_hash"]) == 64
        assert evidence["label_comment_validation_formula"] not in {"not transcribed in this draft", "hash-verified inspector required"}
        assert entry["classification"].get("branch")
        if ref in contract["formula_to_numeric_inputs"]:
            formula = contract["formula_to_numeric_inputs"][ref]
            assert entry["classification"]["status"] == "formula_to_numeric_guarded"
            assert entry["proposal"]["evaluation"]["operation"] == "unresolved"
            assert evidence["cell_hash"] != formula["cell_xml_sha256"]
            assert evidence["contract_formula_cell_xml_sha256"] == formula["cell_xml_sha256"]
            assert evidence["cell_hash"] != evidence["contract_formula_cell_xml_sha256"]
            assert entry["dependencies"] == [formula["formula"]]


def test_draft_policy_schema_and_trace():
    policy, fixture = _policy(), _fixtures()
    assert policy["evaluator_vocabulary"]["operation_enum"] == ["select", "sum_components", "linear_driver_fallback", "unresolved"]
    assert fixture["schema_version"] == "ginzu-draft-scenarios/1"
    trace_schema = policy["trace_schema"]
    assert trace_schema["version"] == "ginzu-decision-trace/1"
    required_trace_fields = set(trace_schema["required_fields"])
    for case in fixture["cases"]:
        result = _run(case, policy=policy)
        expected = case["expected"]
        assert result["status"] == expected["status"], case["name"]
        assert result["value"] == expected["value"], case["name"]
        assert set(expected["trace_contains"]) <= set(result["trace"]), case["name"]
        structured = result["structured_trace"]
        assert required_trace_fields <= set(structured), case["name"]
        assert structured["typed_decision"]["decision"] in trace_schema["decision_enum"]
        assert set(structured) >= required_trace_fields
        assert structured["rights"] and structured["cutoff"] and structured["scale"] and structured["sign"]
        assert structured["uncertainty"] and structured["sensitivity"] and structured["branch"] and structured["fallback"]
        assert isinstance(structured["rejected_candidates"], list)
        assert all("locator" in rejected and "reason" in rejected for rejected in structured["rejected_candidates"])


def test_draft_fallbacks_are_typed_and_review_only():
    policy = _policy()
    rule = rule_for(policy, "B26_MODELED_ISSUER_DRIVER")
    fallback = rule["evaluation"]["fallback"]
    assert fallback["status"] == "synthetic_test_only"
    obs_case = _case("modeled_fallback_synthetic_only")
    closed = evaluate(rule, obs_case["observations"], context=_fixtures()["context"])
    assert closed["status"] == "unresolved" and closed["value"] is None
    assert "fallback_gate_closed" in closed["trace"]
    reviewer_manifest = read_json(MANIFEST_PATH)
    assert reviewer_manifest["status"] == "TEST ONLY — REVIEW ONLY — NOT APPROVED"
    assert reviewer_manifest["signoffs"]["policy_approval"] == "absent"


def test_draft_policy_scenarios():
    for case in _fixtures()["cases"]:
        result = _run(case)
        expected = case["expected"]
        assert (result["status"], result["value"]) == (expected["status"], expected["value"])


def test_draft_policy_mutation_sensitivity():
    # A realistic precedence regression must select the comparator instead of audited evidence.
    case = _case("select_higher_precedence")
    original = _run(case)
    assert original["value"] == 100
    # Only the contributing selected source appears; the losing eligible
    # candidate is recorded as a rejected candidate with a precedence reason.
    assert [(item["locator"], item["source_hash"]) for item in original["structured_trace"]["source_locators"]] == [
        ("fixture://audited/1", _fixtures()["context"]["source_sha256"]),
    ]
    assert [(item["locator"], item["reason"]) for item in original["structured_trace"]["rejected_candidates"]] == [
        ("fixture://derived/1", "lower_precedence_not_selected"),
    ]
    changed = copy.deepcopy(_policy())
    rule = next(row for row in changed["special_rules"] if row["id"] == case["rule_id"])
    for tier in rule["evaluation"]["source_tiers"]:
        if tier["source_kind"] == "issuer_audited_statement":
            tier["precedence"] = 9
    assert _run(case, policy=changed)["value"] == 90

    # A sign reversal changes a two-component synthetic total; expected 20 is fixture-owned.
    sum_case = _case("sum_components_exact")
    changed = copy.deepcopy(_policy())
    sum_rule = next(row for row in changed["special_rules"] if row["id"] == sum_case["rule_id"])
    sum_rule["evaluation"]["components"][0]["sign"] = -1
    assert _run(sum_case)["value"] == 20
    assert _run(sum_case, policy=changed)["value"] == -4

    # A fallback coefficient change is observable and remains a distinctly modeled result.
    fallback_case = _case("modeled_fallback_synthetic_only")
    changed = copy.deepcopy(_policy())
    fallback_rule = next(row for row in changed["special_rules"] if row["id"] == fallback_case["rule_id"])
    fallback_rule["evaluation"]["fallback"]["coefficients"] = [2, 4]
    result = _run(fallback_case, policy=changed)
    assert result["status"] == "synthetic_modeled" and result["value"] == pytest.approx(2.2)
    assert result["value"] != fallback_case["expected"]["value"]

    # A route gate mutation activates a formerly inactive route and must differ from the independent expected result.
    route_case = _case("route_inactive")
    expected = route_case["expected"]
    changed = copy.deepcopy(_policy())
    route_rule = next(row for row in changed["special_rules"] if row["id"] == route_case["rule_id"])
    route_rule["evaluation"]["route_gate"]["when"] = "Revenues"
    result = _run(route_case, policy=changed)
    assert (expected["status"], expected["value"]) == ("not_applicable", None)
    assert (result["status"], result["value"]) != (expected["status"], expected["value"])
    assert result["status"] == "synthetic_observation" and result["value"] == 7
    assert result["value"] != expected["value"]


def test_policy_review_packet_coverage():
    contract = read_json(CONTRACT_PATH)
    manifest = read_json(MANIFEST_PATH)
    refs = contract["input_allowlist"]
    packet_refs = [row["ref"] for row in manifest["references"]]
    assert packet_refs == refs
    assert manifest["scope"]["reference_count"] == 67
    assert all(row["review_status"] == "outstanding" for row in manifest["references"])
    matrix = manifest["trial_gold_cross_product"]
    issuer_rows = matrix["issuer_rows"]
    expected_issuers = ["southwest", "delta", "american", "united"]
    assert [row["trial_registry_key"] for row in issuer_rows] == expected_issuers
    assert matrix["reference_count_per_issuer"] == 67 and matrix["pair_count"] == 4 * 67
    outcome_fields = matrix["outcome_fields"]
    required_outcome_keys = {"gold_status", "evidence", "reviewer", "external_factual_validation"}
    assert required_outcome_keys <= set(outcome_fields)
    all_pairs = []
    for issuer_row in issuer_rows:
        assert issuer_row["reference_count"] == 67
        assert issuer_row["outcome_fields_for_each_ref"] == outcome_fields
        row_outcomes = [(issuer_row["trial_registry_key"], ref, issuer_row["outcome_fields_for_each_ref"]) for ref in packet_refs]
        assert len(row_outcomes) == 67
        assert all(outcome[2]["gold_status"] == "outstanding" and outcome[2]["evidence"] is None and outcome[2]["reviewer"] is None for outcome in row_outcomes)
        all_pairs.extend(row_outcomes)
    assert [(issuer, ref) for issuer, ref, _ in all_pairs] == [(issuer, ref) for issuer in expected_issuers for ref in refs]
    assert len(all_pairs) == 268
    # A dropped issuer row must be caught independently of the aggregate reference inventory.
    mutated_rows = copy.deepcopy(issuer_rows)
    del mutated_rows[2]
    assert len(mutated_rows) * matrix["reference_count_per_issuer"] != matrix["pair_count"]
    assert [row["trial_registry_key"] for row in mutated_rows] != expected_issuers


def test_draft_is_not_operational():
    policy, manifest = _policy(), read_json(MANIFEST_PATH)
    assert policy["document_status"] == "PROPOSED — NOT APPROVED / NOT OPERATIONAL"
    assert "does not authorize" in manifest["purpose"].lower() and "workbook writes" in manifest["purpose"].lower()
    assert manifest["scope"]["no_provider_calls"] is True
    assert manifest["scope"]["no_source_transfer"] is True
    assert manifest["scope"]["no_workbook_writes"] is True
    assert manifest["scope"]["valuation_status"] == "not calculated, not validated, no reliance permitted"
    assert all(value == "absent" for value in manifest["signoffs"].values())


def test_draft_workbook_pin_unchanged():
    contract = read_json(CONTRACT_PATH)
    assert workbook_sha256() == PINNED_WORKBOOK_SHA256 == contract["sha256"]
    policy = _policy()
    assert policy["source_binding"]["pinned_sha256"] == PINNED_WORKBOOK_SHA256
    assert policy["source_binding"]["observed_sha256"] == PINNED_WORKBOOK_SHA256


def test_all_evidence_link_inspector_digests_match_documented_canonicalization():
    # Every one of the 67 evidence_link.inspector_sha256 digests must be re-derivable
    # from the embedded proposal.inspector under the single documented canonicalization:
    # SHA256(json.dumps(inspector, sort_keys=True, ensure_ascii=True,
    # separators=(', ', ': ')).encode('utf-8')). This is the worker's serialization
    # convention (Python json.dumps defaults: sorted keys, ASCII-escaped text, spaced
    # separators), NOT the repository's compact separators=(',',':'), ensure_ascii=False
    # convention used for other JSON digests. See docs/ginzu-input-policy-v1.md.
    policy = _policy()
    entries = policy["entries"]
    assert len(entries) == 67
    for entry in entries:
        inspector = entry["proposal"]["inspector"]
        stored = entry["proposal"]["evidence_link"]["inspector_sha256"]
        canonical = hashlib.sha256(
            json.dumps(inspector, sort_keys=True, ensure_ascii=True, separators=(", ", ": ")).encode("utf-8")
        ).hexdigest()
        assert stored == canonical, (
            f"{entry['id']} {entry['reference']}: evidence_link.inspector_sha256 "
            "does not match the documented canonical digest of proposal.inspector"
        )


def test_entry_evaluation_is_reference_bound_and_unresolved_entries_stay_unresolved():
    policy = _policy()
    case = _case("select_higher_precedence")
    reference = "Input sheet!B11"
    result = evaluate_entry(policy, reference, case["observations"], context=_fixtures()["context"])
    assert result["structured_trace"]["reference"] == reference
    assert result["value"] == 100
    # An ordinary entry with an unresolved specification cannot inherit a different special rule.
    unresolved_ref = "Input sheet!B12"
    unresolved = evaluate_entry(policy, unresolved_ref, case["observations"], context=_fixtures()["context"])
    assert unresolved["status"] == "unresolved" and unresolved["value"] is None
    assert all(row["reason"] == "entry_operation_unresolved" for row in unresolved["structured_trace"]["rejected_candidates"])
    mismatch = evaluate(rule_for(policy, "B11_FACT_PRECEDENCE"), case["observations"], context=_fixtures()["context"], policy=policy, reference=unresolved_ref)
    assert mismatch["status"] == "unresolved" and "rule_reference_mismatch" in mismatch["trace"]


def test_draft_selector_entry_evaluation_is_not_numeric():
    """evaluate_entry for the B17 Yes/No selector must stay unresolved (selector
    decision domain) and never return a numeric value: B17 is a decision, not a
    numeric source, and B15's numeric sum_components demonstration must not run
    for it."""
    policy = _policy()
    case = _case("sum_components_exact")
    observations = case["observations"]
    result = evaluate_entry(policy, "Input sheet!B17", observations, context=_fixtures()["context"])
    assert result["structured_trace"]["reference"] == "Input sheet!B17"
    assert result["status"] == "unresolved" and result["value"] is None
    assert not isinstance(result["value"], (int, float))
    assert result["structured_trace"]["source_locators"] == []
    assert all(row["reason"] == "entry_operation_unresolved" for row in result["structured_trace"]["rejected_candidates"])


def test_draft_wrong_type_demo_cannot_bypass_selector_entry_gate():
    """Even if a numeric special rule were to reference a selector cell, the
    per-entry gate must hold: evaluate_entry fails closed (unresolved) with a
    wrong-type reason instead of running the numeric demo for the selector."""
    policy = copy.deepcopy(_policy())
    case = _case("sum_components_exact")
    context = dict(_fixtures()["context"])
    context.update(case.get("context_overrides", {}) or {})
    # Reintroduce the wrong-type association: B15's numeric rule references B17.
    for rule in policy["special_rules"]:
        if rule["id"] == "B15_LEASE_NO_OVERLAP":
            rule["references"].append("Input sheet!B17")
    result = evaluate_entry(policy, "Input sheet!B17", case["observations"], context=context)
    assert result["status"] == "unresolved" and result["value"] is None
    assert "wrong_type_special_rule_for_entry" in result["trace"]
    assert result["structured_trace"]["source_locators"] == []
    assert all(row["reason"] == "wrong_type_special_rule_for_entry" for row in result["structured_trace"]["rejected_candidates"])


def test_draft_wrong_type_demo_cannot_bypass_selector_gate_on_direct_low_level_evaluate():
    """The wrong-type gate must also hold at the public low-level evaluate() before
    routing/selection, not only at evaluate_entry: a direct call on the mutated B15
    numeric rule bound to the B17 selector reference fails closed (typed unresolved/
    None with an explicit reason, no selected sources, no rights) instead of
    returning a numeric synthetic_composition. The B15 numeric demonstration for
    its own filing-composition cell must still run."""
    policy = copy.deepcopy(_policy())
    case = _case("sum_components_exact")
    context = dict(_fixtures()["context"])
    context.update(case.get("context_overrides", {}) or {})
    entries = {entry["reference"]: entry for entry in policy["entries"]}
    b17_classification = entries["Input sheet!B17"]["classification"]
    b15_classification = entries["Input sheet!B15"]["classification"]
    assert b17_classification["type"] == "selector"
    assert b15_classification["type"] != "selector"

    # Positive control: B15's own numeric demo still produces a synthetic_composition
    # via both the per-entry path and the public low-level evaluate.
    b15_observations = [
        {"source_kind": "issuer_balance_sheet_and_note", "value": 4901,
         "component": "verified_interest_bearing_debt_components", "overlap_group": "debt",
         "concept": context["concept"], "scale": 1, "sign": 1, "rights": "permitted",
         "cutoff": "available", "source_sha256": context["source_sha256"],
         "locator": "fixture://debt/debt-total", "entity": context["entity"],
         "consolidation": context["consolidation"], "period": context["period"],
         "dimensions": {}, "unit": context["unit"]},
        {"source_kind": "issuer_balance_sheet_and_note", "value": 500,
         "component": "operating_lease_liabilities_if_selected_convention_includes_them",
         "overlap_group": "lease", "concept": context["concept"], "scale": 1, "sign": 1,
         "rights": "permitted", "cutoff": "available", "source_sha256": context["source_sha256"],
         "locator": "fixture://lease/lease-liability", "entity": context["entity"],
         "consolidation": context["consolidation"], "period": context["period"],
         "dimensions": {}, "unit": context["unit"]},
    ]
    b15_rule = rule_for(policy, "B15_LEASE_NO_OVERLAP")
    via_entry = evaluate_entry(policy, "Input sheet!B15", b15_observations, context=context)
    via_direct = evaluate(b15_rule, b15_observations, context=context, policy=policy,
                          reference="Input sheet!B15", classification=b15_classification)
    assert via_entry["status"] == "synthetic_composition" and via_entry["value"] == 5401
    assert via_direct["status"] == "synthetic_composition" and via_direct["value"] == 5401
    assert [row["locator"] for row in via_entry["structured_trace"]["source_locators"]] == [
        "fixture://debt/debt-total", "fixture://lease/lease-liability",
    ]

    # Regression: a mutated B15 numeric rule that also references the B17 selector
    # cell must fail closed when called directly at the low-level evaluate with the
    # B17 selector classification, exactly as evaluate_entry fails closed. Using
    # observations that satisfy the B15 components reproduces the reviewer finding:
    # without the gate the direct call returned a numeric synthetic_composition.
    for rule in policy["special_rules"]:
        if rule["id"] == "B15_LEASE_NO_OVERLAP":
            rule["references"].append("Input sheet!B17")
    result = evaluate(b15_rule, b15_observations, context=context, policy=policy,
                      reference="Input sheet!B17", classification=b17_classification)
    assert result["status"] == "unresolved" and result["value"] is None
    assert not isinstance(result["value"], (int, float))
    assert "wrong_type_special_rule_for_entry" in result["trace"]
    assert result["structured_trace"]["source_locators"] == []
    assert result["structured_trace"]["rights"]["status"] == "unresolved"
    assert result["structured_trace"]["branch"]["state"] == "unresolved"
    assert all(row["reason"] == "wrong_type_special_rule_for_entry"
               for row in result["structured_trace"]["rejected_candidates"])
    assert all(isinstance(row.get("locator"), str) and row["locator"]
               for row in result["structured_trace"]["rejected_candidates"])


def test_draft_same_tier_agreeing_duplicates_are_rejected_not_selected():
    """When two same-tier candidates agree, only one becomes the contributing
    selected source; the nonselected agreeing candidate is rejected with a
    locator/reason so every eligible candidate is accounted for exactly once."""
    policy = _policy()
    rule = rule_for(policy, "B11_FACT_PRECEDENCE")
    context = _fixtures()["context"]
    observations = [
        {"source_kind": "issuer_audited_statement", "value": 100, "component": None,
         "concept": "revenue", "scale": 1, "sign": 1, "rights": "permitted",
         "cutoff": "available", "source_sha256": context["source_sha256"],
         "locator": "fixture://audited/agree-a", "entity": "fixture-entity",
         "consolidation": "consolidated", "period": "FY2025", "dimensions": {}, "unit": "USD millions"},
        {"source_kind": "issuer_audited_statement", "value": 100, "component": None,
         "concept": "revenue", "scale": 1, "sign": 1, "rights": "permitted",
         "cutoff": "available", "source_sha256": context["source_sha256"],
         "locator": "fixture://audited/agree-b", "entity": "fixture-entity",
         "consolidation": "consolidated", "period": "FY2025", "dimensions": {}, "unit": "USD millions"},
    ]
    result = evaluate(rule, observations, context=context, policy=policy)
    assert result["status"] == "synthetic_observation" and result["value"] == 100
    assert [row["locator"] for row in result["structured_trace"]["source_locators"]] == ["fixture://audited/agree-a"]
    assert [(row["locator"], row["reason"]) for row in result["structured_trace"]["rejected_candidates"]] == [
        ("fixture://audited/agree-b", "same_tier_agreeing_not_selected"),
    ]
    assert result["structured_trace"]["rights"]["status"] == "permitted"
    assert result["structured_trace"]["branch"]["state"] == "active"


def test_draft_evaluator_fails_closed_on_evidence_rights_and_approval_gaps():
    case = _case("select_higher_precedence")
    rule = rule_for(_policy(), case["rule_id"])
    context = _fixtures()["context"]
    for key, bad in (("rights", "unresolved"), ("cutoff", "unknown"), ("source_sha256", "0" * 64),
                     ("entity", "other"), ("period", "FY2024"), ("unit", "shares"),
                     ("concept", "unrelated concept"), ("scale", 1000), ("sign", -1)):
        observations = copy.deepcopy(case["observations"])
        observations[1][key] = bad
        observations[0][key] = bad
        result = evaluate(rule, observations, context=context, policy=_policy())
        assert result["status"] == "unresolved" and result["value"] is None
        assert all(row["reason"] for row in result["structured_trace"]["rejected_candidates"])
    wrong_kind = copy.deepcopy(case["observations"])
    wrong_kind[1]["source_kind"] = "synthetic_test_fixture"
    wrong_kind[0]["source_kind"] = "synthetic_test_fixture"
    result = evaluate(rule, wrong_kind, context=context, policy=_policy())
    assert result["status"] == "unresolved" and result["value"] is None
    assert all("source_kind_not_declared_for_rule" in row["reason"] for row in result["structured_trace"]["rejected_candidates"])
    observations = copy.deepcopy(case["observations"])
    observations[0]["value"] = float("inf")
    observations[1]["value"] = float("inf")
    result = evaluate(rule, observations, context=context)
    assert result["status"] == "unresolved" and result["value"] is None
    result = evaluate(rule, case["observations"], context=context, approval_fields={"approved": True, "rights": "granted"})
    assert result["status"] == "unresolved" and "approval_fields_ignored_fail_closed" in result["trace"]
    unknown = copy.deepcopy(rule)
    unknown["evaluation"]["operation"] = "arbitrary_execute"
    result = evaluate(unknown, case["observations"], context=context)
    assert result["status"] == "unresolved" and "unknown_operation_fail_closed" in result["trace"]


def test_draft_selected_sources_distinguished_from_rejected():
    """The trace separates contributing selected sources from rejected candidates
    and never implies rights or an active branch from mere eligibility."""
    policy, fixture = _policy(), _fixtures()
    for case in fixture["cases"]:
        result = _run(case, policy=policy)
        expected = case["expected"]
        structured = result["structured_trace"]
        source_locators = [item["locator"] for item in structured["source_locators"]]
        rejected = structured["rejected_candidates"]
        rejected_locators = [item["locator"] for item in rejected]

        if "expected_source_locators" in expected:
            assert source_locators == expected["expected_source_locators"], case["name"]
        if "expected_rejected" in expected:
            assert rejected == expected["expected_rejected"], case["name"]
        if "expected_rights" in expected:
            assert structured["rights"]["status"] == expected["expected_rights"], case["name"]
        if "expected_branch" in expected:
            assert structured["branch"]["state"] == expected["expected_branch"], case["name"]

        # Rights/branch activity are tied to a concrete selected outcome, never
        # to an unresolved candidate (e.g. a same-tier conflict).
        if result["status"] == "unresolved":
            assert structured["rights"]["status"] == "unresolved", case["name"]
            assert structured["branch"]["state"] == "unresolved", case["name"]
            assert not source_locators, case["name"]
        elif result["status"] == "not_applicable":
            assert structured["rights"]["status"] == "unresolved", case["name"]
            assert structured["branch"]["state"] == "conditional", case["name"]
        else:
            assert structured["rights"]["status"] == "permitted", case["name"]
            assert structured["branch"]["state"] == "active", case["name"]
            assert source_locators, case["name"]

        # Every observation is accounted for exactly once: contributing or rejected.
        for observation in case["observations"]:
            locator = observation.get("locator")
            if locator is None:
                continue
            assert (locator in source_locators) != (locator in rejected_locators), (case["name"], locator)


def test_draft_conflict_and_precedence_trace_semantics():
    policy = _policy()

    # Same-tier conflict: unresolved with no selected source; the conflicting
    # candidates are rejected with a locator/reason and no rights are implied.
    conflict = _case("same_tier_conflict")
    result = _run(conflict, policy=policy)
    assert result["status"] == "unresolved"
    assert result["structured_trace"]["source_locators"] == []
    assert [(row["locator"], row["reason"]) for row in result["structured_trace"]["rejected_candidates"]] == [
        ("fixture://audited/a", "same_tier_conflict"),
        ("fixture://audited/b", "same_tier_conflict"),
    ]
    assert result["structured_trace"]["rights"]["status"] == "unresolved"
    assert result["structured_trace"]["branch"]["state"] == "unresolved"

    # Precedence: only the highest-precedence contributor is selected; the loser
    # is rejected with a precedence reason and never appears as a source.
    precedence = _case("select_higher_precedence")
    result = _run(precedence, policy=policy)
    assert result["status"] == "synthetic_observation" and result["value"] == 100
    assert [row["locator"] for row in result["structured_trace"]["source_locators"]] == ["fixture://audited/1"]
    assert [(row["locator"], row["reason"]) for row in result["structured_trace"]["rejected_candidates"]] == [
        ("fixture://derived/1", "lower_precedence_not_selected"),
    ]
    assert result["structured_trace"]["rights"]["status"] == "permitted"
    assert result["structured_trace"]["branch"]["state"] == "active"


def test_draft_route_gate_never_grants_rights_when_unresolved_or_inactive():
    policy = _policy()

    # Verified but inactive route: not_applicable, no source, no rights, branch conditional.
    inactive = _case("route_inactive")
    result = _run(inactive, policy=policy)
    assert result["status"] == "not_applicable"
    assert result["structured_trace"]["source_locators"] == []
    assert [(row["locator"], row["reason"]) for row in result["structured_trace"]["rejected_candidates"]] == [
        ("fixture://region/allowed-but-inactive", "route_gate_inactive_verified_allowed_selector"),
    ]
    assert result["structured_trace"]["rights"]["status"] == "unresolved"
    assert result["structured_trace"]["branch"]["state"] == "conditional"

    # Active route with no evidence: unresolved, no rights, no active branch.
    no_evidence = _case("route_active_missing_evidence")
    result = _run(no_evidence, policy=policy)
    assert result["status"] == "unresolved"
    assert result["structured_trace"]["source_locators"] == []
    assert result["structured_trace"]["rights"]["status"] == "unresolved"
    assert result["structured_trace"]["branch"]["state"] == "unresolved"

    # Active route with eligible evidence: the selected source is listed and
    # rights/branch are granted only for that concrete selection.
    active = _case("route_active_selected")
    result = _run(active, policy=policy)
    assert result["status"] == "synthetic_observation" and result["value"] == 7
    assert [row["locator"] for row in result["structured_trace"]["source_locators"]] == ["fixture://region/active"]
    assert result["structured_trace"]["rejected_candidates"] == []
    assert result["structured_trace"]["rights"]["status"] == "permitted"
    assert result["structured_trace"]["branch"]["state"] == "active"


def test_draft_route_gate_references_resolve_to_contract_selector_domains():
    """Every dict route gate must reference a single real selector cell whose
    allowed domain matches the contract; no combined pseudo-references, no
    invented selector values, and no unsupported route choices. Selector
    entries never carry a self-gate on their own cell: the selector's value is
    the decision itself, so 'No' is a decided choice, not an inactive route.
    Dependent entries retain their gate on the selector."""
    contract = read_json(CONTRACT_PATH)
    meta = contract["input_metadata"]
    policy = _policy()
    entries = {entry["reference"]: entry for entry in policy["entries"]}

    # B15 is active debt, not gated on the B17 converter selector.
    b15 = entries["Input sheet!B15"]
    assert b15["proposal"]["applicability_status"] == "active"
    assert b15["proposal"]["route_gate"] is None
    assert "Input sheet!B17" not in json.dumps(b15["proposal"].get("route_gate"))

    # B17 is a Yes/No converter selector whose own value is the decision; it has
    # no self-gate, and the pinned Yes/No domain is the only allowed choice.
    b17 = entries["Input sheet!B17"]["proposal"]["route_gate"]
    assert b17 is None
    assert meta["Input sheet!B17"]["allowed"] == ["Yes", "No"]

    # The other selector entries likewise carry no self-gate on their own cell.
    for ref in ("Input sheet!B37", "Input sheet!B45", "Input sheet!B48",
                "Input sheet!B64", "Input sheet!B67", "Input sheet!B70",
                "Cost of capital worksheet!B11", "Cost of capital worksheet!B21",
                "Cost of capital worksheet!B25"):
        assert entries[ref]["proposal"]["route_gate"] is None, ref

    # Dependent entries retain their gate on the real selector, with `when`
    # equal to the exact pinned domain value (not prose).
    b38 = entries["Input sheet!B38"]["proposal"]["route_gate"]
    assert b38["reference"] == "Input sheet!B37" and b38["when"] == "Yes"
    assert meta["Input sheet!B37"]["allowed"] == ["Yes", "No"]
    for ref in ("Input sheet!B65", "Input sheet!B68", "Input sheet!B71"):
        gate = entries[ref]["proposal"]["route_gate"]
        assert isinstance(gate, dict) and gate["when"] == "Yes", ref

    # B72 gates on the real B70 selector, never a combined pseudo-reference.
    b72 = entries["Input sheet!B72"]["proposal"]["route_gate"]
    assert b72["reference"] == "Input sheet!B70"
    assert b72["choices"] == ["Yes", "No"]
    assert b72["when"] == "Yes"
    assert meta["Input sheet!B70"]["allowed"] == ["Yes", "No"]
    assert " and " not in b72["reference"] and " or " not in b72["reference"]

    # Regional rows gate on the B25 selector with the exact Operating regions value.
    h25 = entries["Cost of capital worksheet!H25"]["proposal"]["route_gate"]
    assert h25["reference"] == "Cost of capital worksheet!B25"
    assert h25["when"] == "Operating regions"
    assert meta["Cost of capital worksheet!B25"]["allowed"] == ["Operating regions", "Revenues"]


def test_draft_b25_policy_never_asserts_operating_countries_route():
    """The B25 selector and its dependent regional rows must not invent an
    unsupported 'Operating countries' route; only the pinned allowed choices
    (Operating regions / Revenues) may appear as selector semantics."""
    policy = _policy()
    entries = {entry["reference"]: entry for entry in policy["entries"]}
    b25_proposal = entries["Cost of capital worksheet!B25"]["proposal"]
    for key in ("source_concept", "composition", "route_gate", "unresolved_rationale"):
        assert "Operating countries" not in json.dumps(b25_proposal.get(key)), key
        assert "Operating regions/countries" not in json.dumps(b25_proposal.get(key)), key
    fallback_blob = json.dumps(b25_proposal.get("fallback"))
    assert "Operating countries" not in fallback_blob
    assert "Operating regions/countries" not in fallback_blob
    # B25 is the selector itself; its decision is the choice, not a self-gate.
    assert b25_proposal["route_gate"] is None
    # Dependent regional rows gate on the B25 selector with the exact allowed value.
    for ref in (f"Cost of capital worksheet!H{i}" for i in range(25, 32)):
        assert ref in entries
        dep_gate = entries[ref]["proposal"]["route_gate"]
        assert isinstance(dep_gate, dict)
        assert dep_gate["reference"] == "Cost of capital worksheet!B25"
        assert dep_gate["when"] == "Operating regions"
        assert set(dep_gate.get("choices") or []) <= {"Operating regions", "Revenues"}
    for ref in (f"Cost of capital worksheet!H{i}" for i in range(21, 32)):
        assert ref in entries
        gate_blob = json.dumps(entries[ref]["proposal"].get("route_gate"))
        assert "Operating countries" not in gate_blob


def test_draft_formula_guarded_cells_have_typed_formula_gates():
    """B27, B28 and B32 all carry a typed formula_guarded route gate naming their
    real formula reference (I26, B26, B31); the formula guard is not a null gate."""
    policy = _policy()
    entries = {entry["reference"]: entry for entry in policy["entries"]}
    expected = {
        "Input sheet!B27": "I26",
        "Input sheet!B28": "B26",
        "Input sheet!B32": "B31",
    }
    for ref, formula in expected.items():
        entry = entries[ref]
        gate = entry["proposal"]["route_gate"]
        assert isinstance(gate, dict)
        assert gate["type"] == "formula_guarded"
        assert gate["reference"] == ref
        assert gate["formula"] == formula
        assert entry["proposal"]["applicability_status"] == "formula_guarded"


def test_draft_route_gate_evaluates_contract_allowed_b25_domain():
    """Representative route semantics: the B25 selector 'Operating regions'
    activates the H21 regional route; 'Revenues' is verified-but-inactive; an
    unsupported 'Operating countries' value is rejected as invalid, never zero."""
    policy = _policy()
    contract = read_json(CONTRACT_PATH)
    case = _case("route_active_selected")
    context = dict(_fixtures()["context"])
    context.update(case.get("context_overrides", {}) or {})
    rule = rule_for(policy, "H21_REGIONAL_ROUTE")
    observations = case["observations"]

    active = evaluate(rule, observations, context=context, policy=policy,
                      route={"Cost of capital worksheet!B25": "Operating regions"})
    assert active["status"] == "synthetic_observation" and active["value"] == 7

    inactive = evaluate(rule, observations, context=context, policy=policy,
                        route={"Cost of capital worksheet!B25": "Revenues"})
    assert inactive["status"] == "not_applicable" and inactive["value"] is None

    invalid = evaluate(rule, observations, context=context, policy=policy,
                       route={"Cost of capital worksheet!B25": "Operating countries"})
    assert invalid["status"] == "unresolved" and "route_selector_invalid" in invalid["trace"]
    assert invalid["value"] is None

    assert contract["input_metadata"]["Cost of capital worksheet!B25"]["allowed"] == [
        "Operating regions", "Revenues",
    ]
