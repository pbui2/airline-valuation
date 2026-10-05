"""Read-only source-observation packet for the pinned Southwest FY2025 filing.

Candidates are source observations only; this module does not establish rights,
cutoff eligibility, independent accounting approval, or workbook write authority.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib

from .filing_ixbrl import parse_filing, resolve_locator
from .filing_policy import _row_text
from .filing_registry import FILINGS
from .trial_rules import load_standard

PACKET_SCHEMA = "southwest-source-cell-candidates/1"
CANDIDATE_REFS = {
    "Input sheet!B11": ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "duration", "Total operating revenues"),
    "Input sheet!B12": ("us-gaap:OperatingIncomeLoss", "duration", "OPERATING INCOME"),
    "Input sheet!B14": ("us-gaap:StockholdersEquity", "instant", "Total stockholders' equity"),
}
FY_PERIOD = {"type": "duration", "start": "2025-01-01", "end": "2025-12-31"}
INSTANT_PERIOD = {"type": "instant", "date": "2025-12-31"}
USD = "iso4217:USD"
US_GAAP_NAMESPACE = "http://fasb.org/us-gaap/2025"
ISO4217_NAMESPACE = "http://www.xbrl.org/2003/iso4217"


class PacketValidationError(ValueError):
    """Source bytes or parsed facts fail independent validation."""


def _validated_facts(raw: bytes, parsed: dict | None = None) -> tuple[dict, str]:
    if not isinstance(raw, bytes):
        raise PacketValidationError("source must be exact original bytes")
    spec = FILINGS["southwest"]["artifacts"]["primary"]
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != spec["length"] or digest != spec["sha256"]:
        raise PacketValidationError("source bytes do not match pinned Southwest original")
    regenerated = parse_filing(raw, source_id="southwest")
    if parsed is not None and (not isinstance(parsed, dict) or parsed != regenerated):
        raise PacketValidationError("parsed observations differ from independent parse of pinned source")
    return regenerated, digest


def _candidate(raw: bytes, parsed: dict, digest: str, ref: str, concept: str, period_type: str) -> dict | None:
    """Defensive direct-call path: only accept facts regenerated from exact bytes."""
    spec = FILINGS["southwest"]["artifacts"]["primary"]
    if (not isinstance(raw, bytes) or len(raw) != spec["length"]
            or hashlib.sha256(raw).hexdigest() != spec["sha256"] or digest != spec["sha256"]):
        return None
    regenerated = parse_filing(raw, source_id="southwest")
    if not isinstance(parsed, dict) or parsed != regenerated:
        return None
    fact = _candidate_from_verified(raw, regenerated, digest, ref, concept, period_type,
                                    verify_locator_resolution=True)
    return fact


def _candidate_from_verified(raw: bytes, parsed: dict, digest: str, ref: str, concept: str,
                             period_type: str, *, verify_locator_resolution: bool = False) -> dict | None:
    """Extract from _validated_facts output; never treat arbitrary caller data as verified."""
    spec = FILINGS["southwest"]["artifacts"]["primary"]
    if (not isinstance(raw, bytes) or len(raw) != spec["length"]
            or hashlib.sha256(raw).hexdigest() != spec["sha256"] or digest != spec["sha256"]):
        return None
    if ref not in CANDIDATE_REFS or CANDIDATE_REFS[ref][:2] != (concept, period_type):
        return None
    expected_period = FY_PERIOD if period_type == "duration" else INSTANT_PERIOD
    expected_period = ({"type": "duration", "startdate": "2025-01-01", "enddate": "2025-12-31"}
                       if period_type == "duration" else expected_period)
    eligible = []
    required_label = CANDIDATE_REFS[ref][2]
    for fact in parsed.get("facts", []):
        context = fact.get("context") or {}
        unit = fact.get("unit") or {}
        measures = unit.get("measures", [])
        if (fact.get("concept") != concept or fact.get("concept_namespace") != US_GAAP_NAMESPACE
                or not fact.get("context_ref") or context.get("id") != fact.get("context_ref")
                or context.get("entity") != "0000092380" or context.get("period") != expected_period
                or context.get("dimensions") or fact.get("unresolved") or fact.get("value") is None
                or unit.get("resolved") is not True or len(measures) != 1
                or measures[0].get("lexical") != USD or measures[0].get("namespace") != ISO4217_NAMESPACE
                or measures[0].get("local_name") != "USD"):
            continue
        locator = fact.get("locator") or {}
        if (locator.get("format") != "html_dom" or locator.get("source_id") != "southwest"
                or locator.get("sha256") != digest or not locator.get("path")):
            continue
        # _validated_facts supplied the complete freshly parsed result on the builder
        # path; require its locator to identify this unique fact. Direct _candidate
        # calls additionally resolve the locator independently against source bytes.
        matching = [f for f in parsed["facts"] if f.get("locator", {}).get("path") == locator.get("path")]
        if len(matching) != 1 or matching[0] != fact:
            continue
        if verify_locator_resolution and not resolve_locator(raw, locator):
            continue
        row = _row_text(raw, fact)
        if not row or required_label.casefold() not in row.casefold():
            continue
        try:
            amount_usd = Decimal(fact["value"])
            scale = int(fact.get("scale", "0"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not amount_usd.is_finite():
            continue
        eligible.append({
            "fact": fact["concept"], "value_usd": str(amount_usd),
            "value_usd_millions": str(amount_usd / Decimal("1000000")),
            "unit": USD, "scale": scale, "context_id": fact["context_ref"],
            "period": expected_period, "issuer_cik": "0000092380",
            "entity_scheme": context.get("entity_scheme"),
            "source_sha256": digest, "row_text": row,
            "locator": deepcopy(locator),
        })
    # Multiple presentations are considered ambiguous even if numbers agree.
    return eligible[0] if len(eligible) == 1 else None


def build_southwest_packet(raw: bytes, parsed: dict | None = None) -> dict:
    """Build 67 source-candidate/abstention records from exact pinned bytes."""
    verified, digest = _validated_facts(raw, parsed)
    entries = load_standard()["entries"]
    if len(entries) != 67:
        raise PacketValidationError("trial standard does not enumerate exactly 67 references")
    cells = {}
    for entry in entries:
        ref = entry["ref"]
        if ref in CANDIDATE_REFS:
            concept, period, _label = CANDIDATE_REFS[ref]
            evidence = _candidate_from_verified(raw, verified, digest, ref, concept, period)
            if evidence:
                cells[ref] = {
                    "status": "source_candidate_pending_rights_cutoff_independent_review",
                    "decision": None, "write_eligible": False,
                    "reason": "source observation only; rights, cutoff, independent accounting review, and write authority are not established",
                    "candidate": evidence,
                }
            else:
                cells[ref] = {"status": "abstain_missing_or_ambiguous_source_fact", "decision": None,
                              "write_eligible": False, "reason": "no unique exact-context, exact-unit, row-backed fact"}
        elif ref == "Input sheet!B15":
            cells[ref] = {"status": "abstain_debt_presentation_unresolved", "decision": None,
                          "write_eligible": False, "reason": "debt definitions and statement/table presentation remain unresolved"}
        elif ref == "Input sheet!B17":
            cells[ref] = {"status": "abstain_lease_overlap_unresolved", "decision": None,
                          "write_eligible": False, "reason": "debt/lease overlap and approved reconciliation are unresolved"}
        else:
            cells[ref] = {"status": "abstain_out_of_bounded_trial_scope", "decision": None,
                          "write_eligible": False, "reason": "no approved Southwest source mapping or independent evidence in this bounded trial"}
    return {"schema": PACKET_SCHEMA, "issuer": "southwest", "issuer_cik": "0000092380",
            "fiscal_period": "FY2025", "source_sha256": digest,
            "registry_sha256": FILINGS["southwest"]["artifacts"]["primary"]["sha256"],
            "reference_count": len(cells), "cells": cells,
            "limitations": ["Source candidates are not approved workbook decisions.",
                            "Publication rights, cutoff, and independent review are not verified.",
                            "No model, network access, or workbook write is performed."]}
