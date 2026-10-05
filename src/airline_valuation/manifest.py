"""Versioned, cutoff-aware FY2025 valuation manifest validation.

This module validates evidence and workflow metadata; it does not calculate a
valuation or supply analyst forecasts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1.0"
CUTOFF = datetime(2026, 2, 6, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
_REQUIRED_TOP_LEVEL = {
    "schema_version", "entity", "fiscal_period", "cutoff", "filings",
    "facts", "market_observations", "analyst_assumptions", "approval",
    "analyst_decisions", "review_packet", "required_cells",
}
_CELL = re.compile(r"^[^!]+![A-Z]{1,3}[1-9][0-9]*$")
_REQUIRED_FILINGS = {
    "0000092380-26-000004": ("2026-02-05T16:07:52-05:00", "original"),
    "0000092380-26-000006": ("2026-02-06T16:01:14-05:00", "exhibits_only"),
}
_SUPPORTED_BRANCHES = {"Cost of capital worksheet!B11": "I will input"}
# Broad approved B21 plausibility range, anchored to the immutable 491.3m-share
# observation but allowing for normal source/date reconciliation differences.
_B21_EXPECTED_SHARES_MIN = Decimal("100000000")
_B21_EXPECTED_SHARES_MAX = Decimal("999999999")


@lru_cache(maxsize=1)
def _contract() -> dict[str, Any]:
    path = Path(__file__).with_name("contract.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _contract_required_cells() -> set[str]:
    contract = _contract()
    required = set(contract.get("required_inputs", []))
    required.update(_SUPPORTED_BRANCHES)
    for selector, value in _SUPPORTED_BRANCHES.items():
        for cell, metadata in contract.get("input_metadata", {}).items():
            if metadata.get("only_when") == f"{selector}={value}":
                required.add(cell)
    return required


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    blockers: tuple[str, ...]


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InvalidOperation
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidOperation
    return number


def _source_ok(item: Mapping[str, Any], *, accession: bool = False) -> bool:
    source = item.get("source", item)
    if not isinstance(source, Mapping):
        return False
    if not all(isinstance(source.get(key), str) and source[key].strip()
               for key in ("url", "accessed_at", "description")):
        return False
    url = source["url"].lower()
    if "example.invalid" in url or "example.test" in url:
        return False
    digest = source.get("sha256")
    pending = item.get("status") in {"pending", "unknown", "not_applicable"}
    if pending:
        if digest is not None:
            return False
    elif not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return False
    else:
        acquisition = source.get("acquisition")
        if not isinstance(acquisition, Mapping):
            return False
        if acquisition.get("sha256") != digest or acquisition.get("retrieved_at") != source.get("accessed_at"):
            return False
    if accession and not str(source.get("accession", "")).strip():
        return False
    accession_id = str(source.get("accession", "")).replace("-", "")
    if accession_id and "sec.gov/archives/edgar/data/" in url:
        if accession_id not in url.replace("-", ""):
            return False
    return True


def _cell_refs(item: Mapping[str, Any]) -> list[str]:
    value = item.get("workbook_cells", item.get("workbook_cell"))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return []


def normalize_entry(ref: str, item: Mapping[str, Any],
                    metadata: Mapping[str, Any]) -> tuple[str, str]:
    """Normalize a manifest entry to the workbook unit for an approved input.

    Returns ``(value, target_unit)``. Raises ``ValueError`` when the mapping,
    source unit, value, or scale is ambiguous or unsupported.
    """
    if ref not in _contract().get("input_allowlist", []):
        raise ValueError(f"unsupported workbook input: {ref}")
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{ref} has no approved input metadata")
    if metadata.get("kind") == "switch":
        if ref not in _SUPPORTED_BRANCHES or item.get("value") not in metadata.get("allowed", []):
            raise ValueError(f"{ref} selector choice is not approved")
        return str(item["value"]), "choice"
    if metadata.get("kind") != "numeric":
        raise ValueError(f"{ref} is not an approved input")
    value = item.get("value")
    if value is None:
        raise ValueError(f"{ref} has no numeric value to normalize")
    try:
        number = _decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{ref} value must be a finite exact numeric value") from exc
    source_unit = item.get("unit")
    if not isinstance(source_unit, str):
        raise ValueError(f"{ref} requires an explicit source unit")
    unit = source_unit.strip().lower()
    target = str(metadata.get("unit", ""))
    if ref == "Input sheet!B21":
        if unit == "shares":
            if number <= 0 or number != number.to_integral_value():
                raise ValueError("share source unit requires a positive integral share count")
            whole_shares = number
        elif unit == "millions of shares":
            if number <= 0:
                raise ValueError("share count must be positive")
            whole_shares = number * Decimal(1_000_000)
        else:
            raise ValueError("B21 source unit must be exactly 'shares' or 'millions of shares'")
        if not _B21_EXPECTED_SHARES_MIN <= whole_shares <= _B21_EXPECTED_SHARES_MAX:
            raise ValueError("B21 share count is outside the approved reconciliation range of 100 million to less than 1 billion shares")
        number = whole_shares / Decimal(1_000_000)
        return format(number, "f"), target

    accepted: dict[str, set[str]] = {}
    for cell, meta in _contract().get("input_metadata", {}).items():
        if meta.get("kind") != "numeric":
            continue
        expected = str(meta.get("unit", "")).lower()
        if "currency/share" in expected:
            accepted[cell] = {"currency/share", "usd per share", "usd per share, unadjusted close"}
        elif "fraction" in expected:
            accepted[cell] = {"fraction", "decimal fraction", "decimal rate", expected}
        elif "millions" in expected or "same scale as revenue" in expected:
            accepted[cell] = {"usd millions", "currency millions"}
        elif expected == "ratio":
            accepted[cell] = {"ratio"}
        elif expected == "years":
            accepted[cell] = {"years"}
    allowed = accepted.get(ref)
    if not allowed or unit not in allowed:
        raise ValueError(f"{ref} source unit {source_unit!r} is not an approved unit")
    if "fraction" in target.lower() and not Decimal(0) <= number <= Decimal(1):
        raise ValueError(f"{ref} must be a decimal fraction from 0 through 1")
    return format(number, "f"), target


def _validate_contract_unit(label: str, item: Mapping[str, Any], cell: str,
                            metadata: Mapping[str, Any] | None, errors: list[str]) -> None:
    if not metadata:
        errors.append(f"{label} maps to cell without input metadata: {cell}")
        return
    if cell in _SUPPORTED_BRANCHES:
        if item.get("value") not in metadata.get("allowed", []):
            errors.append(f"{label} selector {cell} must be an allowed choice")
        return
    if item.get("value") is None:
        return
    try:
        normalize_entry(cell, item, metadata)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")


def validate_manifest(manifest: Mapping[str, Any]) -> ValidationResult:
    """Check evidence provenance, units, cut-off, mapping, and known share controls."""
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        return ValidationResult(False, ("manifest must be an object",))
    missing = _REQUIRED_TOP_LEVEL - manifest.keys()
    if missing:
        errors.append("missing top-level keys: " + ", ".join(sorted(missing)))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if manifest.get("entity") != "Southwest Airlines Co.":
        errors.append("entity must be Southwest Airlines Co.")
    if manifest.get("fiscal_period") != "FY2025":
        errors.append("fiscal_period must be FY2025")
    try:
        cutoff = _parse_datetime(manifest.get("cutoff"), "cutoff")
        if cutoff.astimezone(CUTOFF.tzinfo) != CUTOFF:
            errors.append("cutoff must equal 2026-02-06T23:59:59-05:00")
    except ValueError as exc:
        errors.append(str(exc))
        cutoff = CUTOFF

    filings = manifest.get("filings", [])
    if not isinstance(filings, list):
        errors.append("filings must be a list")
        filings = []
    accessions: set[str] = set()
    for i, filing in enumerate(filings):
        label = f"filings[{i}]"
        if not isinstance(filing, Mapping):
            errors.append(f"{label} must be an object")
            continue
        accession = str(filing.get("accession", ""))
        if not accession:
            errors.append(f"{label}.accession is required")
        elif accession in accessions:
            errors.append(f"duplicate filing accession {accession}")
        accessions.add(accession)
        try:
            published = _parse_datetime(filing.get("filed_at"), f"{label}.filed_at")
            if published > cutoff:
                errors.append(f"{label}.filed_at is after cutoff")
        except ValueError as exc:
            errors.append(str(exc))
        expected = _REQUIRED_FILINGS.get(accession)
        if expected is None:
            errors.append(f"{label} accession is outside the approved filing scope")
        else:
            if filing.get("amendment_scope") != expected[1]:
                errors.append(f"{label}.amendment_scope must be {expected[1]!r}")
            try:
                accepted_at = _parse_datetime(filing.get("filed_at"), f"{label}.filed_at")
                expected_at = _parse_datetime(expected[0], "expected accepted_at")
                if accepted_at != expected_at:
                    errors.append(f"{label}.filed_at must equal {expected[0]}")
            except ValueError:
                pass
        if not _source_ok(filing):
            errors.append(f"{label} requires URL, description, SHA-256, and retrieval time")
        try:
            _parse_datetime(filing.get("accessed_at"), f"{label}.accessed_at")
        except ValueError as exc:
            errors.append(str(exc))

    if accessions != set(_REQUIRED_FILINGS):
        errors.append("filings must include exactly the two approved accessions")

    cells_seen: set[str] = set()
    cell_mappings: dict[str, list[tuple[Mapping[str, Any], str]]] = {}
    required = manifest.get("required_cells", [])
    if not isinstance(required, list) or any(not isinstance(c, str) or not _CELL.fullmatch(c) for c in required):
        errors.append("required_cells must list exact sheet!cell coordinates")
        required = []
    contract_required = _contract_required_cells()
    if set(required) != contract_required or len(required) != len(contract_required):
        errors.append("required_cells must exactly match the reviewed workbook contract")
    for collection in ("facts", "market_observations", "analyst_assumptions"):
        values = manifest.get(collection, [])
        if not isinstance(values, list):
            errors.append(f"{collection} must be a list")
            continue
        for i, item in enumerate(values):
            label = f"{collection}[{i}]"
            if not isinstance(item, Mapping):
                errors.append(f"{label} must be an object")
                continue
            unit = item.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                errors.append(f"{label}.unit must be explicit")
            value = item.get("value")
            refs = _cell_refs(item)
            mapped_metadata = [_contract().get("input_metadata", {}).get(ref) for ref in refs]
            mapped_kind = next((m.get("kind") for m in mapped_metadata if isinstance(m, Mapping)), None)
            if value is None:
                if item.get("status") not in {"unknown", "not_applicable", "pending"}:
                    errors.append(f"{label}: null value requires explicit unknown/not_applicable/pending status")
            elif mapped_kind == "switch":
                allowed = set()
                for ref, meta in zip(refs, mapped_metadata):
                    if isinstance(meta, Mapping) and meta.get("kind") == "switch":
                        allowed.update(meta.get("allowed", []))
                if value not in allowed:
                    errors.append(f"{label}.value must be an approved switch choice: {sorted(allowed)}")
            else:
                try:
                    number = _decimal(value)
                    if number == 0 and item.get("zero_semantics") != "explicit_zero":
                        errors.append(f"{label}: zero value requires zero_semantics='explicit_zero'")
                    if str(unit).strip().lower() in {"share", "shares", "whole shares"} and (number <= 0 or number != number.to_integral_value()):
                        errors.append(f"{label}: share counts must be positive whole shares")
                    if "rate" in str(item.get("id", "")).lower() or "rate" in str(unit).lower():
                        if number < 0 or number > 1:
                            errors.append(f"{label}: rates must be decimal fractions from 0 through 1")
                except (InvalidOperation, ValueError):
                    errors.append(f"{label}.value must be a finite exact numeric value or null")
            if not refs and item.get("status") not in {"pending", "unknown", "not_applicable"}:
                errors.append(f"{label} requires workbook_cell or workbook_cells when ready")
            for ref in refs:
                if not _CELL.fullmatch(ref):
                    errors.append(f"{label} has invalid exact cell coordinate {ref!r}")
                elif ref not in _contract().get("input_allowlist", []):
                    errors.append(f"{label} maps to non-input or protected cell {ref}")
                else:
                    metadata = _contract().get("input_metadata", {}).get(ref)
                    _validate_contract_unit(label, item, ref, metadata, errors)
                if ref in cells_seen:
                    errors.append(f"duplicate workbook cell mapping {ref}")
                cells_seen.add(ref)
                cell_mappings.setdefault(ref, []).append((item, collection))
            if collection == "analyst_assumptions":
                if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("rationale", "author")):
                    errors.append(f"{label} requires rationale and author")
                approval_date = item.get("approval_date")
                if item.get("status") == "approved":
                    if not isinstance(approval_date, str) or not approval_date.strip():
                        errors.append(f"{label} approved assumption requires approval_date")
                    else:
                        try:
                            datetime.fromisoformat(approval_date)
                        except ValueError:
                            errors.append(f"{label}.approval_date must be an ISO date")
                elif approval_date is not None and not isinstance(approval_date, str):
                    errors.append(f"{label}.approval_date must be null or an ISO date")
                if "source" in item:
                    errors.append(f"{label} assumption must not contain a fictitious source URL")
            else:
                if not _source_ok(item, accession=collection == "facts"):
                    errors.append(f"{label} requires source URL, SHA-256, retrieval time, and source description" + (", accession" if collection == "facts" else ""))
                if collection == "facts":
                    source = item.get("source")
                    if isinstance(source, Mapping) and source.get("accession") != "0000092380-26-000004":
                        errors.append(f"{label} must use the original 10-K accession as financial-fact authority")
                    if not isinstance(item.get("period"), str) or not item["period"].strip():
                        errors.append(f"{label}.period is required")
                    if not any(isinstance(item.get(k), str) and item[k].strip() for k in ("tag", "page", "table")) and item.get("status") not in {"pending", "unknown"}:
                        errors.append(f"{label} requires tag, page, or table location when ready")
                publication_fields = ("published_at", "available_at")
                known_publication_time = False
                for publication_field in publication_fields:
                    publication_time = item.get(publication_field)
                    if publication_time is None:
                        continue
                    known_publication_time = True
                    try:
                        publication = _parse_datetime(publication_time, f"{label}.{publication_field}")
                        if publication > cutoff:
                            errors.append(f"{label}.{publication_field} is after cutoff")
                    except ValueError as exc:
                        errors.append(str(exc))
                if not known_publication_time and item.get("status") not in {"pending", "unknown"}:
                    errors.append(f"{label} publication/availability time is unknown; evidence cannot be ready")
                if item.get("as_of") is None and item.get("status") in {"pending", "unknown"}:
                    pass
                else:
                    try:
                        observed = _parse_datetime(item.get("as_of"), f"{label}.as_of")
                        if observed > cutoff:
                            errors.append(f"{label}.as_of is after cutoff")
                    except ValueError as exc:
                        errors.append(str(exc))
                source = item.get("source")
                if isinstance(source, Mapping):
                    try:
                        _parse_datetime(source.get("accessed_at"), f"{label}.source.accessed_at")
                    except ValueError as exc:
                        errors.append(str(exc))
            if collection == "market_observations" and item.get("id") == "shares_2026-02-03":
                if (str(value) != "491317990" or item.get("unit") != "shares"
                        or not str(item.get("as_of", "")).startswith("2026-02-03")
                        or not isinstance(item.get("source"), Mapping)
                        or not str(item["source"].get("url", "")).endswith("/luv-20251231.htm")):
                    errors.append("Feb 3 share observation must remain 491317990 shares dated 2026-02-03 with its SEC source")
    extra_cells = cells_seen - contract_required
    if extra_cells:
        errors.append("mapped cells are not required by the reviewed workbook contract: " + ", ".join(sorted(extra_cells)))

    approval = manifest.get("approval")
    if not isinstance(approval, Mapping) or approval.get("status") not in {"pending", "approved", "rejected"}:
        errors.append("approval must be an object with pending, approved, or rejected status")
    decisions = manifest.get("analyst_decisions", [])
    if not isinstance(decisions, list) or any(not isinstance(d, Mapping) for d in decisions):
        errors.append("analyst_decisions must be a list of objects")
    packet = manifest.get("review_packet")
    if not isinstance(packet, Mapping) or not isinstance(packet.get("required_sections"), list):
        errors.append("review_packet.required_sections must be a list")
    return ValidationResult(not errors, tuple(errors))


def assess_readiness(manifest: Mapping[str, Any]) -> ReadinessResult:
    """Return workflow blockers independently of structural schema validity."""
    validation = validate_manifest(manifest)
    blockers = list(validation.errors)
    if isinstance(manifest, Mapping):
        filings = manifest.get("filings", [])
        if isinstance(filings, list):
            for i, filing in enumerate(filings):
                if isinstance(filing, Mapping) and (filing.get("status") != "ready" or not _source_ok(filing)):
                    blockers.append(f"filing {filing.get('accession', i)} is pending or has no verified acquisition")
    if not validation.valid:
        return ReadinessResult(False, tuple(blockers))
    contract_required = _contract_required_cells()
    if set(manifest.get("required_cells", [])) != contract_required:
        blockers.append("required_cells does not match the reviewed workbook contract")
    for cell in sorted(contract_required):
        owners: list[tuple[Mapping[str, Any], str]] = []
        for collection in ("facts", "market_observations", "analyst_assumptions"):
            for item in manifest.get(collection, []):
                if cell in _cell_refs(item):
                    owners.append((item, collection))
        if len(owners) != 1:
            blockers.append(f"required workbook cell {cell} must have exactly one mapped material input")
        elif owners[0][0].get("status") != "approved" and owners[0][0].get("status") != "ready":
            blockers.append(f"required workbook cell {cell} input is not approved/ready")
    for selector, allowed in _contract().get("branch_switches", {}).items():
        entries = [(item, collection) for collection in ("facts", "market_observations", "analyst_assumptions")
                   for item in manifest.get(collection, []) if selector in _cell_refs(item)]
        if len(entries) == 1 and entries[0][0].get("value") not in _SUPPORTED_BRANCHES.values():
            blockers.append(f"unsupported workbook branch selected for {selector}")
    for collection in ("facts", "market_observations"):
        for i, item in enumerate(manifest.get(collection, [])):
            label = f"{collection[:-1]} {item.get('id', i)}"
            if item.get("status") in {"pending", "unknown", "not_applicable"} or item.get("value") is None:
                blockers.append(f"{label} is pending or unknown")
            if not _cell_refs(item):
                blockers.append(f"{label} has no confirmed workbook mapping")
            if item.get("status") not in {"pending", "unknown", "not_applicable"} and not _source_ok(item, accession=collection == "facts"):
                blockers.append(f"{label} source acquisition is not verified")
    assumptions = manifest.get("analyst_assumptions", [])
    if not assumptions:
        blockers.append("analyst assumptions have not been entered")
    for i, assumption in enumerate(assumptions):
        if assumption.get("value") is None or assumption.get("status") != "approved":
            blockers.append(f"analyst assumption {i} is pending value or approval")
    if manifest["approval"].get("status") != "approved":
        blockers.append("review approval is pending")
    for decision in manifest.get("analyst_decisions", []):
        if decision.get("status") != "resolved":
            blockers.append(f"analyst decision {decision.get('id', 'unnamed')} is unresolved")
    for section in manifest["review_packet"].get("required_sections", []):
        if not isinstance(section, Mapping) or section.get("status") != "complete":
            name = section.get("name", "unnamed") if isinstance(section, Mapping) else "unnamed"
            blockers.append(f"review packet section {name} is incomplete")
    return ReadinessResult(not blockers, tuple(blockers))
