"""Evidence-led decisions for real issuer evidence and a separate test fixture.

The default report concerns Southwest evidence and fails closed. The optional
synthetic builder is a Python-only test fixture; it is not issuer evidence,
valuation advice, or a production eligibility route.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .detailed_eligibility import (PINNED_CUTOFF, TRUSTED_ACCOUNTING_RECONCILIATION_SHA256,
                                   TRUSTED_LOCATOR_ARTIFACT_SHA256, _SYNTHETIC_PYTHON_TOKEN,
                                   verify_source_eligibility)

SCHEMA_VERSION = "southwest-fy2025-detailed-decisions/v3"
FILING_SHA256 = "dd8569563afc978e1b61a41396ba07246ac7a85a4545bfda40c58380d4018306"
WORKBOOK_SHA256 = "c6fd6bd3205e8ef4ecd2b9a01cf81077eaf7db2651d3d02ac03939cb64adabc9"
PERIOD = "FY2025"


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=lambda v: v.hex() if isinstance(v, bytes) else str(v)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence(fact: str, value: str, page: str, unit: str, identity: str,
              *, period: str = PERIOD, date: str = "2025-12-31", status: str = "reported") -> dict[str, str]:
    return {"fact": fact, "value": value, "source_sha256": FILING_SHA256,
            "source_identity": "Southwest Airlines FY2025 Form 10-K PDF", "page": page,
            "period": period, "date": date, "unit": unit, "identity": identity,
            "status": status, "locator_status": "unverified" if page == "unverified locator" else "verified_extracted_pdf_page",
            "source_use": "unknown", "cutoff_status": "unknown", "fixture_status": "provisional_unreviewed_fixture"}


BASE_EVIDENCE = [
    _evidence("reported_debt", "4901", "unverified locator", "USD millions", "consolidated reported debt"),
    _evidence("recognized_operating_lease_liabilities", "1080", "PDF p. 114", "USD millions", "current and noncurrent operating lease liabilities"),
    _evidence("finance_lease_debt_in_reported_debt", "78", "unverified locator", "USD millions", "finance lease debt inclusion guard"),
    _evidence("cash", "3231", "PDF p. 87", "USD millions", "cash and cash equivalents"),
    _evidence("short_term_investments", "0", "unverified locator", "USD millions", "short-term investments", status="reported_nil"),
    _evidence("equity_securities_deferred_compensation", "457", "PDF pp. 64, 73, 126, 134", "USD millions", "equity securities primarily held for deferred compensation"),
    _evidence("issuer_rating", "Moody's Baa2; S&P BBB", "PDF p. 79", "rating category", "issuer credit ratings"),
    _evidence("operating_segments", "1", "unverified locator", "segments", "reportable operating segments"),
    _evidence("geographic_revenue_allocation", "not disclosed", "unverified locator", "USD millions", "geographic revenue allocation", status="not_disclosed"),
    _evidence("average_debt_maturity", "not reported as a single average", "unverified locator", "years", "average debt maturity", status="not_reported"),
    _evidence("operating_revenue", "28063", "unverified locator", "USD millions", "FY2025 operating revenues"),
    _evidence("operating_income", "428", "unverified locator", "USD millions", "FY2025 operating income"),
]

POLICIES = {
    "Input sheet!B19": ("classification.nonoperating-assets", "1.0", "USD millions", "The 457 equity-securities balance is associated primarily with deferred compensation; instrument-level ownership and operating/nonoperating classification are not established. Abstain rather than use prior candidate 0."),
    "Input sheet!B20": ("classification.noncontrolling-interest", "1.0", "USD millions", "No supported consolidated noncontrolling-interest balance and ownership attribution are present. A missing separate line is not proof of zero; abstain rather than use prior candidate 0."),
    "Input sheet!B26": ("forecast.near-term-revenue-growth", "1.0", "fraction", "IATA's 4.5% global airline growth outlook is an industry comparator, not an issuer forecast. No company-specific forecast reconciles fares, capacity, labor, fuel, or identified one-off effects; abstain."),
    "Input sheet!B27": ("forecast.near-term-operating-margin", "1.0", "fraction", "No issuer forecast bridges fares and capacity to labor and fuel costs, or separates recurring performance from one-offs; 428 / 28,063 is historical context only, not a forward margin."),
    "Input sheet!B28": ("forecast.long-run-revenue-growth", "1.0", "fraction", "No long-run inflation and capacity-growth rationale establishes a sustainable terminal revenue rate consistent with the macroeconomic ceiling; abstain rather than extend a comparator or historical growth mechanically."),
    "Input sheet!B29": ("forecast.target-operating-margin", "1.0", "fraction", "No supported cost/revenue bridge establishes a target operating margin: fares and capacity, labor, fuel, and one-off normalization are not reconciled to revenue; historical margin is not a target."),
    "Input sheet!B30": ("forecast.margin-convergence-horizon", "1.0", "years", "No dated issuer fleet plan or evidence-backed convergence schedule supports a margin convergence horizon; abstain."),
    "Input sheet!B31": ("forecast.sales-to-capital", "1.0", "ratio", "No independently reconciled investment, capacity, and capital-base bridge supports the years 1-5 sales-to-capital ratio; abstain."),
    "Input sheet!B32": ("forecast.years-6-10-sales-to-capital", "1.0", "ratio", "No independently reconciled investment, capacity, and capital-base bridge supports the years 6-10 sales-to-capital ratio; abstain."),
}
FORECAST_REQUIREMENTS = {ref: data[3] for ref, data in POLICIES.items()}
FACTS_BY_CELL = {
    "Input sheet!B15": ("reported_debt", "recognized_operating_lease_liabilities", "finance_lease_debt_in_reported_debt"),
    "Input sheet!B17": ("recognized_operating_lease_liabilities", "finance_lease_debt_in_reported_debt"),
    "Input sheet!B18": ("cash", "short_term_investments"),
    "Cost of capital worksheet!B32": ("average_debt_maturity",),
    "Cost of capital worksheet!B33": ("issuer_rating",), "Cost of capital worksheet!B35": ("issuer_rating",),
    **{ref: (fact,) for ref, fact in {"Input sheet!B19":"equity_securities_deferred_compensation", "Input sheet!B20":"noncontrolling_interest", "Input sheet!B26":"company_forward_revenue_growth", "Input sheet!B27":"company_forward_operating_margin", "Input sheet!B28":"long_run_revenue_growth_policy", "Input sheet!B29":"target_operating_margin", "Input sheet!B30":"margin_convergence_horizon", "Input sheet!B31":"forecast_sales_to_capital", "Input sheet!B32":"years_6_10_sales_to_capital"}.items()},
}


def _eligibility(row: Mapping[str, Any], *, cutoff: str, allow_synthetic_test_source: bool) -> dict[str, Any]:
    source = row.get("source", {})
    if allow_synthetic_test_source and source.get("kind") == "synthetic_test":
        source = dict(source)
        source["_fixture_token"] = _SYNTHETIC_PYTHON_TOKEN
    result = verify_source_eligibility(source, row.get("publication", {}), row.get("terms", {}),
                                       cutoff=cutoff, purpose="airline_valuation_decision",
                                       allow_synthetic_test_source=allow_synthetic_test_source)
    if row.get("source_sha256") != result.get("source_sha256"):
        result["reasons"].append("observation hash does not match eligible source")
    if row.get("source_identity") != source.get("identity"):
        result["reasons"].append("observation identity does not match eligible source")
    result["eligible"] = (result.get("status") == "verified_allowed" and
                          result.get("eligible") is True and not result["reasons"])
    return result


def build_decision_package(observations: Iterable[Mapping[str, Any]] | None = None,
                           *, bundle_assessments: Mapping[str, Any] | None = None,
                           cutoff: str = PINNED_CUTOFF,
                           allow_synthetic_test_source: bool = False) -> dict[str, Any]:
    """Build deterministic decisions; legacy bundle assessments are ignored."""
    evidence = [dict(row) for row in (BASE_EVIDENCE if observations is None else observations)]
    trace = {id(row): _eligibility(row, cutoff=cutoff, allow_synthetic_test_source=allow_synthetic_test_source) for row in evidence}
    by_fact: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for i, row in enumerate(evidence):
        if not all(row.get(k) not in (None, "") for k in ("fact", "value", "identity", "unit", "date", "page")):
            errors.append({"type": "incomplete_observation", "index": i})
        else:
            by_fact.setdefault(str(row["fact"]), []).append(row)
    conflicts = [{"type":"conflicting_observations", "fact":fact} for fact, rows in by_fact.items() if len({(str(r["value"]), str(r.get("source_sha256")), str(r.get("identity"))) for r in rows}) > 1]
    conflicting = {x["fact"] for x in conflicts}

    def fact_value(fact: str) -> str | None:
        rows = by_fact.get(fact, [])
        if len(rows) != 1 or fact in conflicting or not trace[id(rows[0])]["eligible"]:
            return None
        row = rows[0]
        source = row.get("source", {})
        synthetic_fact = source.get("kind") == "synthetic_test" and trace[id(row)]["eligible"] and bool(row.get("page"))
        locator = row.get("locator_artifact", {})
        locator_bytes = locator.get("bytes") if isinstance(locator, Mapping) else None
        locator_hash = hashlib.sha256(locator_bytes).hexdigest() if isinstance(locator_bytes, bytes) else None
        trusted_locator = locator_hash in TRUSTED_LOCATOR_ARTIFACT_SHA256 and locator.get("source_sha256") == trace[id(row)].get("source_sha256") and locator.get("fact") == fact
        if not (synthetic_fact or trusted_locator):
            return None
        return str(row["value"])

    def cell(ref: str, unit: str, facts: tuple[str, ...], decision: Any, reason: str, policy: str) -> dict[str, Any]:
        unavailable = [fact for fact in facts if fact_value(fact) is None]
        return {"decision": None if unavailable else (None if decision is None else str(decision)), "unit":unit,
                "policy_id":policy, "policy_version":"1.0", "evidence_facts":list(facts),
                "observations":{f: next((r.get("identity") for r in by_fact.get(f, [])), None) for f in facts},
                "observation_values":{f:fact_value(f) for f in facts}, "transforms":[], "reason":reason,
                "status":"blocked_missing_or_ineligible_evidence" if unavailable else "conditional_decision",
                "eligibility_trace":{f: next((trace[id(r)] for r in by_fact.get(f, [])), None) for f in facts}}

    cells: dict[str, dict[str, Any]] = {}
    lease = next((r.get("lease_reconciliation_artifact") for r in evidence if r.get("lease_reconciliation_artifact")), None)
    lease_bytes = lease.get("bytes") if isinstance(lease, Mapping) else None
    lease_hash = hashlib.sha256(lease_bytes).hexdigest() if isinstance(lease_bytes, bytes) else None
    lease_payload = {}
    if isinstance(lease_bytes, bytes):
        try:
            lease_payload = json.loads(lease_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            lease_payload = {}
    synthetic_lease_fixture = (allow_synthetic_test_source and all(
        len(by_fact.get(fact, [])) == 1 and by_fact[fact][0].get("source", {}).get("kind") == "synthetic_test" and
        trace[id(by_fact[fact][0])]["eligible"] for fact in
        ("reported_debt", "recognized_operating_lease_liabilities", "finance_lease_debt_in_reported_debt")))
    lease_ok = synthetic_lease_fixture or (lease_hash in TRUSTED_ACCOUNTING_RECONCILIATION_SHA256 and
                lease_payload.get("source_hashes") == {key: trace[id(by_fact[fact][0])].get("source_sha256") for key, fact in (
                    ("reported_debt", "reported_debt"), ("operating_lease", "recognized_operating_lease_liabilities"),
                    ("finance_lease", "finance_lease_debt_in_reported_debt"))} and
                lease_payload.get("finance_lease_included") is True and lease_payload.get("operating_lease_disjoint") is True)
    try:
        debt = Decimal(fact_value("reported_debt")) + Decimal(fact_value("recognized_operating_lease_liabilities"))
        guard = Decimal(fact_value("finance_lease_debt_in_reported_debt"))
        if not lease_ok or guard <= 0 or guard > Decimal(fact_value("reported_debt")):
            raise ValueError("lease reconciliation missing or invalid")
        cells["Input sheet!B15"] = cell("Input sheet!B15", "USD millions", FACTS_BY_CELL["Input sheet!B15"], debt, "Reported debt plus recognized operating lease liabilities; hash-bound lease reconciliation confirms disjointness and finance-lease inclusion.", "accounting.lease-inclusive-debt")
        cells["Input sheet!B15"]["transforms"] = ["Decimal(reported_debt) + Decimal(recognized_operating_lease_liabilities)", "finance lease amount is an inclusion guard, not an added amount"]
    except (InvalidOperation, TypeError, ValueError):
        cells["Input sheet!B15"] = cell("Input sheet!B15", "USD millions", FACTS_BY_CELL["Input sheet!B15"], None, "Debt arithmetic or hash-bound lease reconciliation is unavailable or invalid.", "accounting.lease-inclusive-debt")
        cells["Input sheet!B15"].update(decision=None, status="blocked_missing_or_ineligible_evidence")
    cells["Input sheet!B17"] = cell("Input sheet!B17", "Yes/No", FACTS_BY_CELL["Input sheet!B17"], "No" if cells["Input sheet!B15"]["decision"] is not None else None, "No second lease conversion is appropriate only after B15 succeeds.", "accounting.lease-double-counting")
    if cells["Input sheet!B15"]["decision"] is None:
        cells["Input sheet!B17"].update(status="blocked_dependency", reason="B17 depends on a successful B15 debt decision and reconciliation.")
    try:
        cash = Decimal(fact_value("cash")) + Decimal(fact_value("short_term_investments"))
        cells["Input sheet!B18"] = cell("Input sheet!B18", "USD millions", FACTS_BY_CELL["Input sheet!B18"], cash, "Cash plus reported short-term investments.", "accounting.cash-and-investments")
        cells["Input sheet!B18"]["transforms"] = ["Decimal(cash) + Decimal(short_term_investments)"]
    except (InvalidOperation, TypeError, ValueError):
        cells["Input sheet!B18"] = cell("Input sheet!B18", "USD millions", FACTS_BY_CELL["Input sheet!B18"], None, "Cash arithmetic or eligible source evidence is unavailable.", "accounting.cash-and-investments")

    maturity = cell("Cost of capital worksheet!B32", "years", FACTS_BY_CELL["Cost of capital worksheet!B32"], "0", "Workbook-comment fallback is a modeling choice, not issuer-reported maturity.", "cost-of-debt.average-maturity")
    maturity.update(status="modeling_choice", decision="0", modeling_choice=True, issuer_fact=False, prior_candidate={"value":"0", "unit":"years", "kind":"workbook-comment fallback"})
    cells["Cost of capital worksheet!B32"] = maturity
    for ref, value, unit, facts, reason, policy in (("Cost of capital worksheet!B33", "Actual rating", "route selector", FACTS_BY_CELL["Cost of capital worksheet!B33"], "Use issuer-rating route; no cost of debt or WACC is calculated.", "cost-of-debt.rating-route"), ("Cost of capital worksheet!B35", "Baa2/BBB", "rating category key", FACTS_BY_CELL["Cost of capital worksheet!B35"], "Map observed issuer ratings to workbook category.", "cost-of-debt.rating-mapping")):
        cells[ref] = cell(ref, unit, facts, value, reason, policy)
    old = {"Input sheet!B19":"0", "Input sheet!B20":"0", "Input sheet!B26":"0.045", "Input sheet!B27":"0.03", "Input sheet!B28":"0.03", "Input sheet!B29":"0.06", "Input sheet!B30":"5", "Input sheet!B31":"2.0", "Input sheet!B32":"1.25"}
    for ref, (policy, version, unit, reason) in POLICIES.items():
        row = cell(ref, unit, FACTS_BY_CELL[ref], None, reason, policy)
        row.update(status="abstain", decision=None, policy_version=version, reason=reason, old_candidate_alternative={"value":old[ref], "unit":unit, "kind":"prior candidate, not adopted"}, transforms=[])
        if ref == "Input sheet!B26": row["comparator_only"] = {"value":"0.045", "unit":"fraction", "source":"IATA global airline outlook", "issuer_forecast":False, "reason":"Industry comparator only; not issuer guidance."}
        if ref == "Input sheet!B27": row["historical_alternative_only"] = {"observations":{"operating_income":fact_value("operating_income"), "operating_revenue":fact_value("operating_revenue")}, "transform":"Decimal(operating_income) / Decimal(operating_revenue)", "value":str(Decimal(fact_value("operating_income"))/Decimal(fact_value("operating_revenue"))) if fact_value("operating_income") and fact_value("operating_revenue") else None, "status":"historical_context_only_not_forecast", "reason":"Historical margin context only."}
        cells[ref] = row
    # Keep the real package auditable against every contract input without
    # adopting unsupported values or importing candidate values from elsewhere.
    from .detailed_workflow import load_contract
    contract = load_contract()
    metadata = contract.get("input_metadata", {})
    for ref in contract["required_inputs"]:
        if ref in cells:
            continue
        details = metadata.get(ref, {})
        unit = {"dropdown_string": "selector", "selector": "selector", "numeric": "contract numeric input"}.get(details.get("kind"), "contract input")
        meaning = details.get("meaning", "explicit contract input")
        if ref == "Input sheet!B22":
            reason = "No verified market price with a suitable timestamp and source rights is available; abstain."
            policy = "market-data.current-share-price"
        elif ref == "Input sheet!B34":
            reason = "No verified Treasury/risk-free rate matching the valuation date, tenor, and source rights is available; abstain."
            policy = "market-data.risk-free-rate"
        elif ref in {"Input sheet!B8", "Cost of capital worksheet!B11", "Cost of capital worksheet!B21", "Cost of capital worksheet!B25", "Cost of capital worksheet!B33", "Cost of capital worksheet!B35", "Cost of capital worksheet!B48"} or ref.startswith("Cost of capital worksheet!H"):
            reason = f"Required lookup or issuer allocation evidence for {meaning} is not verified; no lookup result or allocation is adopted."
            policy = "lookup.unverified-required-input"
        elif details.get("kind") == "selector":
            reason = f"No supported evidence or approved modeling choice establishes {meaning}; abstain rather than infer a selector."
            policy = "policy.unsupported-selector"
        else:
            reason = f"No eligible, verified evidence supports {meaning}; abstain rather than copy a prior candidate or assume zero."
            policy = "evidence.missing-required-input"
        cells[ref] = {"decision": None, "status": "abstain", "unit": unit,
                      "policy_id": policy, "policy_version": "1.0", "evidence_facts": [],
                      "observations": {}, "observation_values": {}, "transforms": [],
                      "reason": reason, "uncertainty": "unresolved; no value adopted",
                      "eligibility_trace": {}, "trace": {"contract_meaning": meaning, "contract_kind": details.get("kind")}}
    gaps = ["source bytes/hash, authoritative publication/cutoff, and purpose-specific permission must be eligible for every used source", "verified locators and hash-bound finance/operating lease reconciliation are required for Southwest debt"]
    public_evidence = []
    for row in evidence:
        public_row = dict(row)
        if isinstance(public_row.get("source"), Mapping):
            public_row["source"] = {key:value for key, value in public_row["source"].items() if key != "bytes"}
        public_evidence.append(public_row)
    package = {"schema_version":SCHEMA_VERSION, "issuer":"Southwest Airlines", "period":PERIOD, "status":"blocked_pending_verification", "calculated_workbook":False,
        "evidence":sorted(public_evidence, key=lambda x:(str(x.get("fact")), str(x.get("source_sha256")), str(x.get("page")))), "cells":cells,
        "conflicts":conflicts + errors, "blocking_gaps":gaps, "source_eligibility":{"version":"source-eligibility/v2", "cutoff":cutoff, "reviewer_authority":False, "legacy_bundle_assessments_used":False, "eligible":bool(evidence) and all(trace[id(row)]["status"] == "verified_allowed" for row in evidence), "status":"verified_allowed" if evidence and all(trace[id(row)]["status"] == "verified_allowed" for row in evidence) else ("verified_prohibited" if any(trace[id(row)]["status"] == "verified_prohibited" for row in evidence) else "unknown"), "trusted_locator_artifacts_configured":bool(TRUSTED_LOCATOR_ARTIFACT_SHA256), "trusted_accounting_reconciliation_artifacts_configured":bool(TRUSTED_ACCOUNTING_RECONCILIATION_SHA256)},
        "policy":{"historical_values_are_forecast_policy":False, "numeric_forecast_policy":"unsupported; abstain", "missing_or_conflicting_evidence":"block corresponding decision"}}
    package["canonical_sha256"] = canonical_digest(package)
    return package


# This fixture is intentionally separate from the real-evidence path above.
# Values are fictional policy premises (not copied issuer/historical values) and
# can only be requested by Python code; there is no bundle switch for it.
SYNTHETIC_POLICY_VERSION = "detailed-synthetic-decision-policy/1"
_SYNTHETIC_MARKER = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00"
_SYNTHETIC_IDENTITY = "Synthetic decision fixture"
_SYNTHETIC_FIXTURE_ID = "detailed-decisions-test-v1"
_SYNTHETIC_CELLS: dict[str, tuple[str, str, str, str]] = {
    # ref: value, unit, policy id, concise premise
    "Input sheet!B8": ("Air Transport", "industry key", "route.industry", "Select the fictional air-transport workbook classification."),
    "Input sheet!B11": ("1200", "USD millions", "observed.revenue", "Fictional period revenue observation."),
    "Input sheet!B12": ("96", "USD millions", "observed.ebit", "Fictional period operating income observation."),
    "Input sheet!B13": ("24", "USD millions", "observed.interest", "Fictional period interest expense observation."),
    "Input sheet!B14": ("410", "USD millions", "observed.equity", "Fictional period book equity observation."),
    "Input sheet!B15": ("", "USD millions", "accounting.lease-inclusive-debt", "Derived from synthetic debt and lease observations after reconciliation."),
    "Input sheet!B16": ("No", "Yes/No", "policy.preferred-stock-switch", "No fictional preferred stock adjustment is modeled."),
    "Input sheet!B17": ("No", "Yes/No", "accounting.lease-double-counting", "Lease liabilities included in B15; do not add them a second time."),
    "Input sheet!B18": ("", "USD millions", "accounting.cash-and-investments", "Derived from synthetic cash and investment observations."),
    "Input sheet!B19": ("12", "USD millions", "classification.nonoperating-assets", "Fictional analyst policy classifies the identified securities as nonoperating; sensitivity 8–16."),
    "Input sheet!B20": ("3", "USD millions", "classification.noncontrolling-interest", "Fictional separate ownership observation; sensitivity 2–4."),
    "Input sheet!B21": ("80", "millions of shares", "observed.shares", "Fictional share-count observation."),
    "Input sheet!B22": ("25", "USD/share", "observed.share-price", "Fictional quoted price observation."),
    "Input sheet!B23": ("0.42", "fraction", "policy.beta", "Fictional workbook beta input assumption."),
    "Input sheet!B24": ("0.24", "fraction", "policy.tax-rate", "Fictional marginal tax policy assumption."),
    "Input sheet!B26": ("0.04", "fraction", "forecast.near-term-revenue-growth", "Fictional forecast premise with 0.02/0.04/0.06 alternatives; sensitivity bounds 0.02–0.06."),
    "Input sheet!B27": ("0.09", "fraction", "forecast.near-term-operating-margin", "Fictional forecast premise with 0.07/0.09/0.11 alternatives; sensitivity bounds 0.07–0.11."),
    "Input sheet!B28": ("0.025", "fraction", "forecast.long-run-revenue-growth", "Fictional long-run policy premise; 0.015/0.025/0.035 alternatives; sensitivity bounds 0.015–0.035."),
    "Input sheet!B29": ("0.10", "fraction", "forecast.target-operating-margin", "Fictional target premise; 0.08/0.10/0.12 alternatives; sensitivity bounds 0.08–0.12."),
    "Input sheet!B30": ("4", "years", "forecast.margin-convergence-horizon", "Fictional dated model schedule premise; alternatives 3/4/5 years."),
    "Input sheet!B31": ("1.4", "ratio", "forecast.sales-to-capital", "Fictional investment-efficiency premise; alternatives 1.2/1.4/1.6."),
    "Input sheet!B32": ("1.3", "ratio", "forecast.years-6-10-sales-to-capital", "Fictional later-period investment premise; alternatives 1.1/1.3/1.5."),
    "Input sheet!B34": ("0.035", "fraction", "policy.risk-free-rate", "Fictional model assumption; not market data."),
    "Input sheet!B37": ("No", "Yes/No", "policy.cost-of-capital-override", "Use model branch, not direct override."),
    "Input sheet!B45": ("No", "Yes/No", "policy.lease-converter-switch", "Lease amount already included in synthetic debt."),
    "Input sheet!B48": ("No", "Yes/No", "policy.regional-override", "Use synthetic region observations."),
    "Input sheet!B51": ("No", "Yes/No", "policy.tax-override", "Use synthetic model tax assumption."),
    "Input sheet!B53": ("V", "selector", "policy.valuation-currency", "Fictional fixture selects workbook currency mode V."),
    "Input sheet!B56": ("No", "Yes/No", "policy.beta-override", "Use fictional workbook beta policy."),
    "Input sheet!B59": ("No", "Yes/No", "policy.preferred-override", "No preferred adjustment selected."),
    "Input sheet!B61": ("No", "Yes/No", "policy.other-override-1", "No override selected in the fictional route."),
    "Input sheet!B64": ("No", "Yes/No", "policy.other-override-2", "No override selected in the fictional route."),
    "Input sheet!B67": ("No", "Yes/No", "policy.other-override-3", "No override selected in the fictional route."),
    "Input sheet!B70": ("No", "Yes/No", "policy.other-override-4", "No override selected in the fictional route."),
    "Cost of capital worksheet!B11": ("Detailed", "route selector", "route.detailed", "Select the Detailed method."),
    "Cost of capital worksheet!B21": ("Single Business(US)", "route selector", "route.single-business", "Fictional issuer is modeled as a single US business."),
    "Cost of capital worksheet!B25": ("Operating regions", "route selector", "route.regions", "Fictional regional allocation is explicit below."),
    "Cost of capital worksheet!B32": ("6", "years", "policy.average-debt-maturity", "Fictional assumed average maturity; not a reported issuer fact."),
    "Cost of capital worksheet!B33": ("Actual rating", "route selector", "route.actual-rating", "Select the actual-rating model branch for the fixture."),
    "Cost of capital worksheet!B35": ("Baa2/BBB", "rating category key", "policy.rating-category", "Fictional fixture model category; not an issuer rating."),
    "Cost of capital worksheet!B48": ("0", "numeric switch (0/1)", "policy.preferred-switch", "No preferred shares in fictional premises; omit price and dividend inputs."),
}
_SYNTHETIC_OBSERVATIONS = {
    "synthetic_reported_debt": "220", "synthetic_other_borrowings": "212", "synthetic_finance_lease": "8",
    "synthetic_operating_lease": "30", "synthetic_cash": "95", "synthetic_short_term_investments": "5",
}


def build_synthetic_test_decision_package(*, observations: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build a complete fictional test package. Never enabled by JSON/bundle input.

    Optional observations may replace named observations only when the caller
    supplies a complete source/publication/terms artifact set. The value must
    remain bound to those original artifact bytes; stale bindings block use.
    """
    from .detailed_workflow import load_contract

    supplied = {str(row.get("fact")): dict(row) for row in (observations or [])}
    facts: dict[str, str] = dict(_SYNTHETIC_OBSERVATIONS)
    records: dict[str, dict[str, Any]] = {}
    eligibility: dict[str, Any] = {}
    for fact, default_value in _SYNTHETIC_OBSERVATIONS.items():
        raw = _SYNTHETIC_MARKER + fact.encode() + b":" + default_value.encode()
        digest = hashlib.sha256(raw).hexdigest()
        publication = {"fixture_id": _SYNTHETIC_FIXTURE_ID, "source_sha256": digest, "published_at": "2025-12-31"}
        terms = {"fixture_id": _SYNTHETIC_FIXTURE_ID, "source_sha256": digest, "purpose": "airline_valuation_decision", "permission": "allowed"}
        row = {"fact": fact, "value": default_value, "source_sha256": digest, "source_identity": _SYNTHETIC_IDENTITY,
               "identity": fact.replace("_", " "), "unit": "USD millions", "date": "2025-12-31", "page": "synthetic test observation",
               "source": {"bytes": raw, "sha256": digest, "identity": _SYNTHETIC_IDENTITY, "kind": "synthetic_test", "fixture_id": _SYNTHETIC_FIXTURE_ID},
               "publication": {"bytes": json.dumps(publication, sort_keys=True, separators=(",", ":")).encode()},
               "terms": {"bytes": json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()}}
        if fact in supplied:
            # A mutation must carry its own independently prepared artifacts;
            # do not silently replace bytes/hashes based on a changed value.
            row.update(supplied[fact])
        facts[fact] = str(row.get("value", default_value))
        source_bytes = row.get("source", {}).get("bytes") if isinstance(row.get("source"), Mapping) else None
        source_binds_value = isinstance(source_bytes, bytes) and source_bytes == (_SYNTHETIC_MARKER + fact.encode() + b":" + facts[fact].encode())
        records[fact] = row
        eligibility[fact] = _eligibility(row, cutoff=PINNED_CUTOFF, allow_synthetic_test_source=True)
        if not source_binds_value:
            eligibility[fact]["eligible"] = False
            eligibility[fact]["reasons"].append("synthetic source artifact does not bind the observation fact/value")
    required = set(load_contract()["required_inputs"])
    values = {ref: data[0] for ref, data in _SYNTHETIC_CELLS.items()}
    values["Input sheet!B15"] = str(Decimal(facts["synthetic_reported_debt"]) + Decimal(facts["synthetic_operating_lease"]))
    values["Input sheet!B18"] = str(Decimal(facts["synthetic_cash"]) + Decimal(facts["synthetic_short_term_investments"]))
    values.update({f"Cost of capital worksheet!H{i}": ("20" if i == 24 else "80" if i == 28 else "0") for i in range(21, 32)})
    units = {ref: data[1] for ref, data in _SYNTHETIC_CELLS.items()}
    units.update({f"Cost of capital worksheet!H{i}": "USD millions" for i in range(21, 32)})
    # H28/H24 are fictional revenue allocations; total reconciles to B11.
    cells: dict[str, dict[str, Any]] = {}
    for ref in sorted(required):
        if ref not in values:
            cells[ref] = {"decision": None, "status": "abstain", "reason": "No explicit synthetic policy premise is defined for this contract cell.", "unit": None,
                          "policy_id": "synthetic.unsupported", "policy_version": SYNTHETIC_POLICY_VERSION,
                          "evidence_facts": [], "eligibility_trace": {}, "transforms": []}
            continue
        cell_facts = ["synthetic_reported_debt", "synthetic_other_borrowings", "synthetic_finance_lease", "synthetic_operating_lease"] if ref == "Input sheet!B15" else (["synthetic_cash", "synthetic_short_term_investments"] if ref == "Input sheet!B18" else [])
        if ref.startswith("Cost of capital worksheet!H"):
            cell_facts = ["synthetic_revenue_allocation"]
        # A trace is attached even for explicit policy-only premises by binding
        # the policy digest and immutable fixture marker as synthetic evidence.
        if not cell_facts:
            cell_facts = ["synthetic_policy:" + ref]
        evidence_payload = {"cell": ref, "value": values[ref], "policy": _SYNTHETIC_CELLS.get(ref, ("regional.allocation", "USD millions", "regional.allocation", "Fictional regional revenue allocation premise."))[2], "version": SYNTHETIC_POLICY_VERSION}
        raw = _SYNTHETIC_MARKER + json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        pub = {"fixture_id": _SYNTHETIC_FIXTURE_ID, "source_sha256": digest, "published_at": "2025-12-31"}
        term = {"fixture_id": _SYNTHETIC_FIXTURE_ID, "source_sha256": digest, "purpose": "airline_valuation_decision", "permission": "allowed"}
        row = {"fact": cell_facts[0], "value": values[ref], "source_sha256": digest, "source_identity": _SYNTHETIC_IDENTITY,
               "identity": ref + " synthetic premise", "unit": units[ref], "date": "2025-12-31", "page": "synthetic policy premise",
               "source": {"bytes": raw, "sha256": digest, "identity": _SYNTHETIC_IDENTITY, "kind": "synthetic_test", "fixture_id": _SYNTHETIC_FIXTURE_ID},
               "publication": {"bytes": json.dumps(pub, sort_keys=True, separators=(",", ":")).encode()},
               "terms": {"bytes": json.dumps(term, sort_keys=True, separators=(",", ":")).encode()}}
        trace = _eligibility(row, cutoff=PINNED_CUTOFF, allow_synthetic_test_source=True)
        decision = values[ref] if trace["eligible"] else None
        policy_data = _SYNTHETIC_CELLS.get(ref, ("", units[ref], "regional.allocation", "Fictional regional revenue allocation premise."))
        cell = {"decision": decision, "status": "synthetic_test_decision" if decision is not None else "blocked_missing_or_ineligible_evidence",
                "unit": units[ref], "policy_id": policy_data[2], "policy_version": SYNTHETIC_POLICY_VERSION,
                "reason": policy_data[3], "evidence_facts": cell_facts, "observation_values": {fact: facts.get(fact, values[ref]) for fact in cell_facts},
                "eligibility_trace": {fact: eligibility[fact] if fact in eligibility else trace for fact in cell_facts},
                "source_sha256": digest, "policy_trace": evidence_payload, "transforms": []}
        if cell_facts and any(fact in eligibility and not eligibility[fact]["eligible"] for fact in cell_facts):
            cell.update(decision=None, status="blocked_missing_or_ineligible_evidence")
        if ref == "Input sheet!B15":
            reported_debt = Decimal(facts["synthetic_reported_debt"])
            other_borrowings = Decimal(facts["synthetic_other_borrowings"])
            finance_lease = Decimal(facts["synthetic_finance_lease"])
            operating_lease = Decimal(facts["synthetic_operating_lease"])
            components_reconcile = finance_lease + other_borrowings == reported_debt
            operating_lease_disjoint = operating_lease > 0 and operating_lease not in (finance_lease, other_borrowings)
            cell["transforms"] = ["Decimal(synthetic_finance_lease) + Decimal(synthetic_other_borrowings) == Decimal(synthetic_reported_debt)", "Decimal(synthetic_reported_debt) + Decimal(synthetic_operating_lease); operating lease classified separately and disjoint"]
            cell["lease_reconciliation"] = {"reported_debt": str(reported_debt), "other_borrowings": str(other_borrowings), "finance_lease": str(finance_lease), "operating_lease": str(operating_lease), "debt_components_reconcile": components_reconcile, "operating_lease_disjoint": operating_lease_disjoint, "source_hashes": {key: records[fact].get("source_sha256") for key, fact in (("reported_debt", "synthetic_reported_debt"), ("other_borrowings", "synthetic_other_borrowings"), ("finance_lease", "synthetic_finance_lease"), ("operating_lease", "synthetic_operating_lease"))}}
            relevant_facts = ("synthetic_reported_debt", "synthetic_other_borrowings", "synthetic_finance_lease", "synthetic_operating_lease")
            if not components_reconcile or not operating_lease_disjoint or any(not eligibility[f]["eligible"] for f in relevant_facts):
                cell.update(decision=None, status="blocked_missing_or_ineligible_evidence")
        alternatives = {
            "Input sheet!B19": ["8", "12", "16"], "Input sheet!B20": ["2", "3", "4"],
            "Input sheet!B26": ["0.02", "0.04", "0.06"], "Input sheet!B27": ["0.07", "0.09", "0.11"],
            "Input sheet!B28": ["0.015", "0.025", "0.035"], "Input sheet!B29": ["0.08", "0.10", "0.12"],
            "Input sheet!B30": ["3", "4", "5"], "Input sheet!B31": ["1.2", "1.4", "1.6"],
            "Input sheet!B32": ["1.1", "1.3", "1.5"],
        }
        if ref in alternatives:
            cell["synthetic_alternatives"] = alternatives[ref]
            cell["sensitivity"] = {"basis": "versioned fictional scenario policy", "values": alternatives[ref], "issuer_forecast": False}
        cells[ref] = cell
    package = {"schema_version": "airline-valuation-detailed-synthetic-test-package/1", "policy_version": SYNTHETIC_POLICY_VERSION,
               "issuer": "Synthetic test issuer", "period": "fictional", "status": "synthetic_test_only", "calculated_workbook": False,
               "cells": cells, "required_cells": sorted(required), "source_eligibility": {"status": "synthetic_test_only", "eligible": all(x["eligible"] for x in eligibility.values()), "cutoff": PINNED_CUTOFF},
               "policy": {"fixture_only": True, "production_eligibility_relaxed": False, "premises": "Versioned fictional inputs and explicit model policies; never Southwest facts."}}
    package["canonical_sha256"] = canonical_digest(package)
    return package
