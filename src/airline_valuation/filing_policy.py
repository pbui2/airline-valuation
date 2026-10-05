"""Issuer-scoped filing reconciliation for reviewed, pinned source documents.

This module produces evidence traces only; it does not authorize source use,
create valuation inputs, or promote results to a workbook.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import re

from .filing_ixbrl import parse_filing, resolve_locator
from .filing_registry import FILINGS

POLICY_VERSION = "southwest-reconciliation/1"
SW_CONTEXT = "c-4"
SW_PERIOD = {"type": "instant", "date": "2025-12-31"}
SW_ENTITY = "0000092380"
USD = "iso4217:USD"
US_GAAP_NAMESPACE = "http://fasb.org/us-gaap/2025"
ISO4217_NAMESPACE = "http://www.xbrl.org/2003/iso4217"
MILLION = Decimal("1000000")


def _row_text(raw: bytes, fact: dict) -> str:
    """Return a bounded source row containing a fact, with markup removed."""
    ident = fact.get("id")
    if not ident or len(raw) > 12 * 1024 * 1024:
        return ""
    text = raw.decode("utf-8-sig", "replace")
    tag = fact.get("locator", {}).get("tag", "")
    if tag not in {"ix:nonfraction", "ix:nonnumeric", "ix:fraction", "ix:qualifiedforex"}:
        return ""
    escaped_id = re.escape(ident)
    match = None
    for candidate in re.finditer(r"<" + re.escape(tag) + r"\b[^>]*>", text, re.I):
        if re.search(r"\bid\s*=\s*(?:\"" + escaped_id + r"\"|'" + escaped_id + r"')", candidate.group(), re.I):
            match = candidate
            break
    if not match:
        return ""
    at = match.start()
    start = text.rfind("<tr", 0, at)
    end = text.find("</tr", at)
    if start < 0 or end < 0 or end + 5 - start > 100_000:
        return ""
    row_html = text[start:end + 5]
    row = re.sub(r"<[^>]*>", " ", row_html)
    row = re.sub(r"&nbsp;|&#160;|&#xA0;", " ", row, flags=re.I)
    row = re.sub(r"&amp;", "&", row, flags=re.I)
    return " ".join(row.split())


def _fact(result: dict, concept: str, raw: bytes, *, required_label: str | None = None) -> dict | None:
    candidates = [f for f in result["facts"] if f.get("concept") == concept and
                  f.get("concept_namespace") == US_GAAP_NAMESPACE and
                  f.get("context_ref") == SW_CONTEXT and f.get("context") and
                  f["context"].get("entity") == SW_ENTITY and
                  f["context"].get("period") == SW_PERIOD and
                  not f.get("context", {}).get("dimensions") and
                  f.get("unit", {}).get("resolved") is True and
                  len(f.get("unit", {}).get("measures", [])) == 1 and
                  f["unit"]["measures"][0].get("lexical") == USD and
                  f["unit"]["measures"][0].get("namespace") == ISO4217_NAMESPACE and
                  f["unit"]["measures"][0].get("local_name") == "USD" and
                  not f.get("unresolved") and f.get("value") is not None]
    if required_label:
        candidates = [f for f in candidates if required_label.casefold() in _row_text(raw, f).casefold()]
    # Ambiguous duplicate presentations are accepted only when the reported
    # amount agrees; select a locator from the row that proves the label.
    if not candidates:
        return None
    values = {Decimal(f["value"]) for f in candidates}
    if len(values) != 1:
        return None
    return candidates[0]


def _evidence(raw: bytes, fact: dict | None, expected_label: str | None = None) -> dict | None:
    # parse_filing built this locator from these exact bytes; verify its binding
    # without reparsing the 2.8 MB source once per component.
    if fact is None or fact["locator"].get("sha256") != hashlib.sha256(raw).hexdigest():
        return None
    row = _row_text(raw, fact)
    if not row or (expected_label and expected_label.casefold() not in row.casefold()):
        return None
    return {"fact_id": fact["id"], "concept": fact["concept"],
            "value_usd": str(Decimal(fact["value"])), "context_id": fact["context_ref"],
            "unit": USD, "row_text": row, "locator": fact["locator"]}


def _debt_reconciliation(raw: bytes, result: dict) -> dict:
    """Derive source debt observations and subtotal only from row-backed facts."""
    specs = {
        "current_maturities": ("us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent", "Current maturities of long-term debt"),
        "noncurrent_debt": ("us-gaap:LongTermDebtAndCapitalLeaseObligations", "Long-term debt less current maturities"),
        "debt_table_total": ("us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", None),
        "primary_statement_long_term_debt": ("us-gaap:LongTermDebt", "Total"),
        "finance_lease_obligations": ("us-gaap:FinanceLeaseLiability", "Finance leases"),
    }
    evidence = {}
    for name, (concept, label) in specs.items():
        evidence[name] = _evidence(raw, _fact(result, concept, raw, required_label=label), label)
    missing = [name for name, item in evidence.items() if item is None]
    if missing:
        return {"status": "abstain_missing_or_ambiguous_debt_row_evidence",
                "missing_observations": missing, "evidence": {k: v for k, v in evidence.items() if v}}
    amounts = {name: Decimal(item["value_usd"]) / MILLION for name, item in evidence.items()}
    current_noncurrent = amounts["current_maturities"] + amounts["noncurrent_debt"]
    combined_with_finance = current_noncurrent + amounts["finance_lease_obligations"]
    table_to_statement = amounts["primary_statement_long_term_debt"] - amounts["debt_table_total"]
    return {
        "status": "abstain_debt_definitions_unresolved",
        "units": "USD millions",
        "derived_usd_millions": {"current_plus_noncurrent": str(current_noncurrent),
                                 "current_plus_noncurrent_plus_finance_lease": str(combined_with_finance),
                                 "statement_minus_debt_table": str(table_to_statement)},
        "equations": {"debt_components": f"{amounts['current_maturities']} + {amounts['noncurrent_debt']} = {current_noncurrent}",
                      "including_finance_lease": f"{current_noncurrent} + {amounts['finance_lease_obligations']} = {combined_with_finance}",
                      "presentation_difference": f"{amounts['primary_statement_long_term_debt']} - {amounts['debt_table_total']} = {table_to_statement}"},
        "evidence": evidence,
        "reason": "Reported presentations may reflect different definitions; finance lease inclusion and the presentation difference are unresolved, so no debt amount is selected.",
    }


def reconcile_filing(data: bytes, issuer: str = "southwest") -> dict:
    """Reconcile the reviewed Southwest cash/lease rows; isolate all other issuers."""
    if issuer not in FILINGS:
        raise ValueError("issuer is not in code-owned filing registry")
    return _reconcile_verified_parse(data, parse_filing(data, source_id=issuer), issuer)


def reconcile_parsed_filing(data: bytes, result: dict, issuer: str = "southwest") -> dict:
    """Reconcile only independently regenerated facts matching the submitted parse."""
    if issuer not in FILINGS:
        raise ValueError("issuer is not in code-owned filing registry")
    digest = hashlib.sha256(data).hexdigest()
    if not isinstance(result, dict) or result.get("sha256") != digest or result.get("source_id") != issuer:
        base = _base_result(data, issuer)
        base["status"] = "blocked_parsed_source_mismatch"
        base["exceptions"].append("parsed observations do not match the exact source bytes and issuer")
        return base
    regenerated = parse_filing(data, source_id=issuer)
    if result != regenerated:
        base = _base_result(data, issuer)
        base["status"] = "blocked_parsed_source_mismatch"
        base["exceptions"].append("submitted parsed observations differ from independently regenerated facts")
        return base
    return _reconcile_verified_parse(data, regenerated, issuer)


def _base_result(data: bytes, issuer: str) -> dict:
    spec = FILINGS[issuer]
    digest = hashlib.sha256(data).hexdigest()
    return {"policy_version": POLICY_VERSION, "issuer": issuer, "source_sha256": digest,
            "registry_sha256": spec["artifacts"]["primary"]["sha256"],
            "status": "extraction_only", "reconciliations": {}, "exceptions": []}


def _reconcile_verified_parse(data: bytes, result: dict, issuer: str) -> dict:
    """Internal path for a parse result created immediately from these exact bytes."""
    if issuer not in FILINGS:
        raise ValueError("issuer is not in code-owned filing registry")
    spec = FILINGS[issuer]
    digest = hashlib.sha256(data).hexdigest()
    base = {"policy_version": POLICY_VERSION, "issuer": issuer, "source_sha256": digest,
            "registry_sha256": spec["artifacts"]["primary"]["sha256"],
            "status": "extraction_only", "reconciliations": {}, "exceptions": []}
    if not isinstance(result, dict) or result.get("sha256") != digest or result.get("source_id") != issuer:
        base["status"] = "blocked_parsed_source_mismatch"
        base["exceptions"].append("parsed observations do not match the exact source bytes and issuer")
        return base
    if digest != spec["artifacts"]["primary"]["sha256"]:
        base["status"] = "blocked_unpinned_source"
        base["exceptions"].append("primary document SHA-256 does not match code-owned filing registry")
        return base
    if issuer != "southwest":
        base["exceptions"].append("issuer has no approved Southwest accounting reconciliation; extraction-only")
        return base

    cash = _evidence(data, _fact(result, "us-gaap:CashAndCashEquivalentsAtCarryingValue", data,
                                 required_label="Cash and cash equivalents"), "Cash and cash equivalents")
    if cash:
        cash["amount_usd_millions"] = str(Decimal(cash["value_usd"]) / MILLION)
        base["reconciliations"]["cash_and_cash_equivalents"] = {"status": "reconciled", "amount_usd_millions": cash["amount_usd_millions"], "evidence": [cash]}
    else:
        base["reconciliations"]["cash_and_cash_equivalents"] = {"status": "abstain_missing_or_ambiguous_row_evidence"}

    lease_specs = [
        ("us-gaap:OperatingLeaseLiabilityCurrent", "Current operating lease liabilities"),
        ("us-gaap:OperatingLeaseLiabilityNoncurrent", "Noncurrent operating lease liabilities"),
        ("us-gaap:FinanceLeaseLiabilityCurrent", "Current maturities of long-term debt"),
        ("us-gaap:FinanceLeaseLiabilityNoncurrent", "Long-term debt less current maturities"),
    ]
    lease_evidence = []
    for concept, label in lease_specs:
        item = _evidence(data, _fact(result, concept, data, required_label=label), label)
        if item:
            lease_evidence.append(item)
    if len(lease_evidence) == len(lease_specs):
        amounts = {x["concept"]: Decimal(x["value_usd"]) / MILLION for x in lease_evidence}
        op = amounts[lease_specs[0][0]] + amounts[lease_specs[1][0]]
        fin = amounts[lease_specs[2][0]] + amounts[lease_specs[3][0]]
        op_total = _evidence(data, _fact(result, "us-gaap:OperatingLeaseLiability", data,
                                         required_label="Total lease obligations"), "Total lease obligations")
        fin_total = _evidence(data, _fact(result, "us-gaap:FinanceLeaseLiability", data,
                                          required_label="Finance leases"), "Finance leases")
        if op_total and fin_total and op == Decimal(op_total["value_usd"]) / MILLION and fin == Decimal(fin_total["value_usd"]) / MILLION:
            op_total["amount_usd_millions"] = str(Decimal(op_total["value_usd"]) / MILLION)
            fin_total["amount_usd_millions"] = str(Decimal(fin_total["value_usd"]) / MILLION)
            base["reconciliations"]["lease_liabilities"] = {
                "status": "reconciled", "units": "USD millions",
                "equations": {"operating": f"{amounts[lease_specs[0][0]]} + {amounts[lease_specs[1][0]]} = {op}",
                              "finance": f"{amounts[lease_specs[2][0]]} + {amounts[lease_specs[3][0]]} = {fin}"},
                "operating_total": op_total, "finance_total": fin_total,
                "component_evidence": lease_evidence}
        else:
            base["reconciliations"]["lease_liabilities"] = {"status": "abstain_equation_or_total_row_evidence_missing",
                                                               "component_evidence": lease_evidence}
    else:
        base["reconciliations"]["lease_liabilities"] = {"status": "abstain_missing_or_ambiguous_component_row_evidence",
                                                           "component_evidence": lease_evidence}

    inv = next((f for f in result["facts"] if f.get("concept") == "us-gaap:ShortTermInvestments" and
                f.get("context_ref") == SW_CONTEXT and f.get("context", {}).get("entity") == SW_ENTITY and
                f.get("context", {}).get("period") == SW_PERIOD and not f.get("context", {}).get("dimensions")), None)
    base["reconciliations"]["short_term_investments"] = {"status": "abstain_unresolved_tagged_observation",
                                                            "observation": inv and {"value": inv.get("value"), "unresolved": inv.get("unresolved"),
                                                                                   "row_text": _row_text(data, inv), "locator": inv.get("locator")}}
    base["reconciliations"]["debt"] = _debt_reconciliation(data, result)
    base["exceptions"].extend(["debt classification abstained; source presentations remain definitionally unresolved",
                                "short-term investments are tagged but unresolved; absent/unresolved is not zero",
                                "policy is issuer-scoped and does not promote filing values to public reporting or a workbook"])
    return base
