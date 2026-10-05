"""Independent test-only interpreter for review-only input-policy proposals.

It never grants source rights, approval, workbook write authority, or operational
valuation permission. Special rules are available only for synthetic tests.

Trace discipline: ``source_locators`` lists only the contributing selected
source(s) actually used to produce the result. Every candidate that was not
selected contributes is recorded in ``rejected_candidates`` with a locator and
an explicit reason. Eligibility alone never implies rights or an active branch:
on any unresolved or not-applicable outcome, ``rights.status`` is ``unresolved``
and ``branch.state`` is ``unresolved`` (or ``conditional`` for a verified but
inactive route gate).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs/policy-drafts/ginzu-input-policy-v1.json"
CONTRACT_PATH = ROOT / "src/airline_valuation/detailed_contract.json"
MANIFEST_PATH = ROOT / "docs/policy-drafts/ginzu-review-manifest-v1.json"
FIXTURE_PATH = ROOT / "tests/fixtures/ginzu_draft_scenarios_v1.json"
ALLOWED_OPS = {"select", "sum_components", "linear_driver_fallback", "unresolved"}
# Numeric demonstration operations must never run for a decision/selector entry:
# a selector's decision domain is its own Yes/No (or choice) value, not a numeric source.
NUMERIC_OPS = {"select", "sum_components", "linear_driver_fallback"}
DECISION_ENTRY_TYPES = {"selector"}
WRONG_TYPE_SPECIAL_RULE_FOR_ENTRY = "wrong_type_special_rule_for_entry"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rule_for(policy: dict[str, Any], rule_id: str) -> dict[str, Any]:
    for rule in policy.get("special_rules", []):
        if rule.get("id") == rule_id:
            return rule
    raise KeyError(rule_id)


def _metadata_matches(observation: dict[str, Any], context: dict[str, Any]) -> list[str]:
    reasons = []
    for field in ("entity", "consolidation", "period", "dimensions", "unit", "concept", "scale", "sign"):
        if field not in context or field not in observation or observation[field] != context[field]:
            reasons.append(f"{field}_mismatch_or_missing")
    if observation.get("rights") != "permitted":
        reasons.append("rights_not_permitted")
    if observation.get("cutoff") != "available":
        reasons.append("cutoff_not_available")
    if observation.get("source_sha256") != context.get("source_sha256"):
        reasons.append("source_hash_mismatch")
    if not isinstance(observation.get("locator"), str) or not observation["locator"].strip():
        reasons.append("locator_missing")
    value = observation.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        reasons.append("value_not_finite_number")
    return reasons


def _result(status: str, value: Any, *, trace: list[str], policy: dict[str, Any], reference: str,
            context: dict[str, Any], classification: Any, accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> dict[str, Any]:
    decision = "observe_only" if value is not None else "abstain"
    schema = policy.get("trace_schema", {})
    policy_family = policy.get("policy_families", {}).get("version", "not_assessed")
    # Rights and branch activity are granted only for a concrete selected outcome;
    # mere eligibility, conflicts, or aborted operations never imply either.
    granted = bool(accepted) and status not in ("unresolved", "not_applicable")
    structured = {
        "policy_version": {"schema_version": policy.get("schema_version", "not_assessed"), "family_version": policy_family},
        "reference": reference,
        "typed_decision": {"decision": decision, "status": status, "value": value, "abstention_reason": None if value is not None else (trace[0] if trace else "unresolved")},
        "source_locators": [{"locator": item.get("locator"), "source_hash": item.get("source_hash", context.get("source_sha256")), "document_identity": item.get("entity", context.get("entity", "not_assessed")), "page_table_line_cell_tag_context": item.get("locator", "not_assessed")} for item in accepted],
        "rejected_candidates": rejected,
        "rights": {"status": "permitted" if granted else "unresolved", "basis": "synthetic fixture assertion only" if granted else "not_assessed"},
        "cutoff": {"first_public_timestamp": context.get("first_public_timestamp", "not_assessed"), "timezone": context.get("timezone", "not_assessed"), "cutoff_rule": context.get("cutoff_rule", "not_assessed"), "eligibility": "synthetic_fixture_only" if granted else "unresolved"},
        "scale": {"unit": context.get("unit", "not_assessed"), "scale": context.get("scale", "not_assessed")},
        "sign": {"reported": context.get("sign", "not_assessed"), "normalization": "none"},
        "uncertainty": context.get("uncertainty", "not_assessed"),
        "sensitivity": context.get("sensitivity", "not_assessed"),
        "branch": {"state": "conditional" if status == "not_applicable" else "active" if granted else "unresolved", "selector_evidence": context.get("route_evidence", "not_assessed")},
        "fallback": {"state": "synthetic_test_only" if "synthetic_test_only_fallback" in trace else "none_or_not_assessed", "authorization": "not_granted"},
        "classification": classification if classification is not None else "not_assessed",
    }
    required = schema.get("required_fields", {})
    if required:
        for key in required:
            structured.setdefault(key, "not_assessed")
    return {"status": status, "value": value, "trace": trace, "structured_trace": structured}


def _numeric_rule_for_selector(rule: dict[str, Any], classification: Any) -> bool:
    """True when a numeric demonstration operation is bound to a decision/selector
    classification. A selector's decision domain is its own Yes/No (or choice)
    value, not a numeric source, so the numeric demo must never run for it."""
    op = (rule.get("evaluation") or {}).get("operation")
    entry_type = classification.get("type") if isinstance(classification, dict) else None
    return op in NUMERIC_OPS and entry_type in DECISION_ENTRY_TYPES


def _wrong_type_fail_closed(observations: list[dict[str, Any]], *, policy: dict[str, Any],
                            reference: str, context: dict[str, Any], classification: Any) -> dict[str, Any]:
    """Typed unresolved/None fail-closed result for a numeric rule bound to a
    selector: explicit reason in the trace and on every rejected candidate, no
    selected source locators, and no rights or active branch implied."""
    rejected = [{"locator": observation.get("locator"), "reason": WRONG_TYPE_SPECIAL_RULE_FOR_ENTRY}
                for observation in observations]
    return _result("unresolved", None, trace=[WRONG_TYPE_SPECIAL_RULE_FOR_ENTRY], policy=policy,
                   reference=reference, context=context, classification=classification,
                   accepted=[], rejected=rejected)


def _evaluate_rule(rule: dict[str, Any], observations: list[dict[str, Any]], *, context: dict[str, Any],
                   route: dict[str, Any] | None, policy: dict[str, Any], reference: str,
                   classification: Any, approval_fields: dict[str, Any] | None) -> dict[str, Any]:
    ev = rule.get("evaluation", {})
    op = ev.get("operation")
    # `accepted` holds only the contributing selected source(s); `rejected`
    # holds every candidate that was not selected, each with a locator/reason.
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def done(status: str, value: Any, trace: list[str]) -> dict[str, Any]:
        return _result(status, value, trace=trace, policy=policy, reference=reference, context=context,
                       classification=classification, accepted=accepted, rejected=rejected)

    if op not in ALLOWED_OPS:
        return done("unresolved", None, ["unknown_operation_fail_closed"])
    if approval_fields:
        return done("unresolved", None, ["approval_fields_ignored_fail_closed"])
    if op == "unresolved":
        for observation in observations:
            rejected.append({"locator": observation.get("locator"), "reason": "entry_operation_unresolved"})
        return done("unresolved", None, ["operation_unresolved"])

    # Classify candidates once: metadata and declared source-kind eligibility.
    tier_rows = ev.get("source_tiers", [])
    declared_kinds = {row.get("source_kind") for row in tier_rows if isinstance(row.get("source_kind"), str)}
    eligible: list[dict[str, Any]] = []
    for observation in observations:
        reasons = _metadata_matches(observation, context)
        if observation.get("source_kind") not in declared_kinds:
            reasons.append("source_kind_not_declared_for_rule")
        if reasons:
            rejected.append({"locator": observation.get("locator"), "reason": ";".join(reasons)})
        else:
            eligible.append(observation)

    if ev.get("route_gate"):
        gate = ev["route_gate"]
        gate_ref = gate.get("reference")
        contract = read_json(CONTRACT_PATH)
        choices = contract.get("input_metadata", {}).get(gate_ref, {}).get("allowed")
        if not choices and gate_ref == "Cost of capital worksheet!B25":
            choices = ["Operating regions", "Revenues"]
        selector = route.get(gate_ref) if isinstance(route, dict) else None
        if not isinstance(selector, str) or not selector.strip():
            for observation in eligible:
                rejected.append({"locator": observation.get("locator"), "reason": "route_selector_missing"})
            return done("unresolved", None, ["route_selector_missing"])
        if selector not in choices:
            for observation in eligible:
                rejected.append({"locator": observation.get("locator"), "reason": "route_selector_invalid"})
            return done("unresolved", None, ["route_selector_invalid"])
        if selector != gate.get("when"):
            for observation in eligible:
                rejected.append({"locator": observation.get("locator"), "reason": "route_gate_inactive_verified_allowed_selector"})
            return done("not_applicable", None, ["route_gate_inactive_verified_allowed_selector"])

    if op == "select":
        ranks = {row["source_kind"]: (row.get("precedence", row.get("tier", 999)), row.get("tier", 999)) for row in tier_rows}
        ranked = sorted(eligible, key=lambda item: (ranks[item["source_kind"]], item["locator"]))
        if not ranked:
            return done("unresolved", None, ["no_eligible_observation"])
        best_rank = ranks[ranked[0]["source_kind"]]
        same = [item for item in ranked if ranks[item["source_kind"]] == best_rank]
        # Losing eligible candidates (lower precedence) are rejected, never selected.
        for item in ranked:
            if item not in same:
                rejected.append({"locator": item["locator"], "reason": "lower_precedence_not_selected"})
        facts = {(item["value"], item["concept"], item["unit"], item["scale"], item["sign"], item["period"], json.dumps(item["dimensions"], sort_keys=True)) for item in same}
        if len(facts) != 1:
            for item in same:
                rejected.append({"locator": item["locator"], "reason": "same_tier_conflict"})
            return done("unresolved", None, ["same_tier_conflict"])
        winner = same[0]
        # Agreeing same-tier candidates that are not selected are still rejected
        # with a locator/reason so every eligible candidate is accounted for once.
        for item in same[1:]:
            rejected.append({"locator": item["locator"], "reason": "same_tier_agreeing_not_selected"})
        accepted.append({"locator": winner["locator"], "source_kind": winner["source_kind"]})
        return done("synthetic_observation", winner["value"], ["selected_source_tier", winner["source_kind"], "locator", winner["locator"]])

    if op == "sum_components":
        components = ev.get("components", [])
        component_names = {component.get("name") for component in components}
        for item in eligible:
            if item.get("component") not in component_names:
                rejected.append({"locator": item["locator"], "reason": "component_not_used"})
        values, seen = [], set()
        selected: list[dict[str, Any]] = []
        trace = []
        for component in components:
            matching_kind = [item for item in eligible if item.get("component") == component.get("name")]
            if len(matching_kind) != 1:
                for item in eligible:
                    rejected.append({"locator": item["locator"], "reason": "missing_or_ambiguous_component"})
                return done("unresolved", None, ["missing_or_ambiguous_component", str(component.get("name"))])
            item = matching_kind[0]
            overlap = item.get("overlap_group") or item["locator"]
            if overlap in seen:
                for other in eligible:
                    rejected.append({"locator": other["locator"], "reason": "overlapping_components"})
                return done("unresolved", None, ["overlapping_components", str(overlap)])
            seen.add(overlap)
            if component.get("sign") not in (-1, 1) or item.get("sign") != context["sign"]:
                for other in eligible:
                    rejected.append({"locator": other["locator"], "reason": "component_sign_mismatch"})
                return done("unresolved", None, ["component_sign_mismatch"])
            values.append(component["sign"] * item["value"])
            selected.append(item)
            trace.extend(["component", component["name"], "locator", item["locator"]])
        for item in selected:
            accepted.append({"locator": item["locator"], "source_kind": item["source_kind"]})
        return done("synthetic_composition", sum(values), trace)

    if op == "linear_driver_fallback":
        fallback = ev.get("fallback")
        if not isinstance(fallback, dict) or fallback.get("status") != "synthetic_test_only":
            for observation in eligible:
                rejected.append({"locator": observation.get("locator"), "reason": "fallback_not_synthetic_test_only"})
            return done("unresolved", None, ["fallback_not_synthetic_test_only"])
        if context.get("allow_synthetic_fallback") is not True:
            for observation in eligible:
                rejected.append({"locator": observation.get("locator"), "reason": "fallback_gate_closed"})
            return done("unresolved", None, ["fallback_gate_closed"])
        drivers = [item for item in eligible if item.get("component") == "issuer_driver"]
        for observation in eligible:
            if observation.get("component") != "issuer_driver":
                rejected.append({"locator": observation.get("locator"), "reason": "not_issuer_driver"})
        if len(drivers) != 1:
            for observation in drivers:
                rejected.append({"locator": observation.get("locator"), "reason": "driver_ambiguous" if len(drivers) > 1 else "driver_missing"})
            return done("unresolved", None, ["driver_or_parameters_unresolved"])
        coeffs = fallback.get("coefficients")
        if not isinstance(coeffs, list) or len(coeffs) != 2 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in coeffs):
            for observation in drivers:
                rejected.append({"locator": observation.get("locator"), "reason": "driver_or_parameters_unresolved"})
            return done("unresolved", None, ["driver_or_parameters_unresolved"])
        driver = drivers[0]
        accepted.append({"locator": driver["locator"], "source_kind": driver["source_kind"]})
        return done("synthetic_modeled", coeffs[0] + coeffs[1] * driver["value"], ["synthetic_test_only_fallback", "driver_locator", driver["locator"], "coefficients", *coeffs])
    return done("unresolved", None, ["unreachable_fail_closed"])


def evaluate(rule: dict[str, Any], observations: list[dict[str, Any]], *, context: dict[str, Any],
             route: dict[str, Any] | None = None, approval_fields: dict[str, Any] | None = None,
             policy: dict[str, Any] | None = None, reference: str | None = None,
             classification: Any = None) -> dict[str, Any]:
    """Evaluate one rule; `reference` must be included in the rule association."""
    policy = policy or read_json(POLICY_PATH)
    reference = reference or (rule.get("references") or [None])[0]
    if not reference or reference not in rule.get("references", []):
        return _result("unresolved", None, trace=["rule_reference_mismatch"], policy=policy,
                       reference=str(reference), context=context, classification=classification,
                       accepted=[], rejected=[])
    # Reference/type compatibility holds at the public low-level evaluate too,
    # before any routing or selection: a numeric demonstration bound to a
    # decision/selector classification fails closed (typed unresolved/None with
    # an explicit reason) instead of producing a numeric value.
    if _numeric_rule_for_selector(rule, classification):
        return _wrong_type_fail_closed(observations, policy=policy, reference=reference,
                                       context=context, classification=classification)
    return _evaluate_rule(rule, observations, context=context, route=route, policy=policy,
                          reference=reference, classification=classification, approval_fields=approval_fields)


def evaluate_entry(policy: dict[str, Any], reference: str, observations: list[dict[str, Any]], *,
                   context: dict[str, Any], route: dict[str, Any] | None = None,
                   approval_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind by exact cell reference; only explicitly associated special rules run."""
    entry = next((item for item in policy.get("entries", []) if item.get("reference") == reference), None)
    classification = entry.get("classification") if entry else None
    # Explicit special rules are synthetic demonstrations, never operational policy.
    rule = next((item for item in policy.get("special_rules", []) if reference in item.get("references", [])), None)
    if rule is None:
        ev = (entry or {}).get("proposal", {}).get("evaluation", {})
        unresolved = {"id": "entry-unresolved", "references": [reference], "evaluation": {**ev, "operation": "unresolved"}}
        return _evaluate_rule(unresolved, observations, context=context, route=route, policy=policy,
                              reference=reference, classification=classification, approval_fields=approval_fields)
    # Reference/type compatibility: a numeric demonstration must never run for a
    # decision/selector entry (e.g. B17's selector cannot be evaluated by a numeric
    # sum_components rule), so a wrong-type demo cannot bypass the per-entry gate.
    if _numeric_rule_for_selector(rule, classification):
        return _wrong_type_fail_closed(observations, policy=policy, reference=reference,
                                       context=context, classification=classification)
    return _evaluate_rule(rule, observations, context=context, route=route, policy=policy,
                          reference=reference, classification=classification, approval_fields=approval_fields)


def workbook_sha256() -> str:
    return hashlib.sha256((ROOT / "fcffsimpleginzu.xlsx").read_bytes()).hexdigest()
