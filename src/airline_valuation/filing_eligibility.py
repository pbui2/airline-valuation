"""Independent, fail-closed eligibility checks for archived SEC filings.

This policy verifies archive inputs but does not grant data rights. First public
availability and rights remain unknown until independently pinned evidence is
added in code. The separate positive fixture is test-only and is not source
clearance.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from .filing_registry import FILINGS, REGISTRY_VERSION

POLICY_VERSION = "filing-eligibility/1"
PINNED_CUTOFF = "2026-02-06T23:59:59-05:00"
# No independent evidence of first public availability or rights has been
# supplied. These pins must only be populated from an authenticated channel.
TRUSTED_AVAILABILITY_SHA256: frozenset[str] = frozenset()
TRUSTED_TERMS_SHA256: frozenset[str] = frozenset()
TRUSTED_PROHIBITION_SHA256: frozenset[str] = frozenset()
# Each entry must be independently reviewed and committed here. No actual rights
# or first-public-availability evidence is currently pinned.
TRUSTED_RIGHTS_RECORDS: dict[str, dict[str, str]] = {}


def _evidence_rights_status(record: dict, source_sha256: str, purpose: str, cutoff: str,
                            terms_pins: frozenset[str], availability_pins: frozenset[str],
                            prohibition_pins: frozenset[str]) -> str:
    """Verify code-owned evidence bytes, attribution, scope, and time coverage."""
    if not isinstance(record, dict):
        return "unknown"
    terms, availability = record.get("terms_bytes"), record.get("availability_bytes")
    if not isinstance(terms, bytes) or not isinstance(availability, bytes):
        return "unknown"
    terms_digest, availability_digest = hashlib.sha256(terms).hexdigest(), hashlib.sha256(availability).hexdigest()
    if (terms_digest != record.get("terms_sha256") or terms_digest not in terms_pins or
            availability_digest != record.get("availability_sha256") or availability_digest not in availability_pins):
        return "unknown"
    if (record.get("source_sha256") != source_sha256 or record.get("publisher") != record.get("expected_publisher") or
            record.get("scope") != purpose or record.get("purpose") != purpose or
            not isinstance(record.get("version"), str) or not record["version"]):
        return "unknown"
    try:
        terms_text, availability_text = terms.decode("utf-8"), availability.decode("utf-8")
        access = _timestamp(record["access_date"] + "T00:00:00Z")
        effective_from = _timestamp(record["effective_from"])
        cutoff_at = _timestamp(cutoff)
        first_public = _timestamp(record["first_public_at"])
        public_in_bytes = re.search(r"first_public_at=([^;\s]+)", availability_text)
        source_in_bytes = re.search(r"source_sha256=([0-9a-f]{64})", availability_text)
        fields = dict(re.findall(r"([a-z_]+)=([^;\s]+)", terms_text))
        permission = fields.get("permission")
        terms_access = _timestamp(fields["access_date"] + "T00:00:00Z")
        terms_effective_from = _timestamp(fields["effective_from"])
        terms_effective_through = (_timestamp(fields["effective_through"])
                                   if fields.get("effective_through") else None)
        history = record["effective_history"]
        history_matches_terms = (isinstance(history, list) and len(history) == 1 and
                                 _timestamp(history[0]["from"]) == terms_effective_from and
                                 ((history[0].get("through") is None and terms_effective_through is None) or
                                  (history[0].get("through") is not None and terms_effective_through is not None and
                                   _timestamp(history[0]["through"]) == terms_effective_through)))
        history_applies = (terms_effective_from <= cutoff_at and
                           (terms_effective_through is None or cutoff_at <= terms_effective_through))
        if (not public_in_bytes or _timestamp(public_in_bytes.group(1)) != first_public or
                not source_in_bytes or source_in_bytes.group(1) != source_sha256 or
                first_public > cutoff_at or access > cutoff_at or effective_from > cutoff_at or
                not history_matches_terms or not history_applies or access != terms_access or effective_from != terms_effective_from or
                fields.get("publisher") != record["publisher"] or
                fields.get("publisher") != record["expected_publisher"] or
                fields.get("scope") != purpose or fields.get("scope") != record["scope"] or
                fields.get("purpose") != purpose or fields.get("purpose") != record["purpose"] or
                fields.get("version") != record["version"] or
                permission not in {"allowed", "prohibited"}):
            return "unknown"
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return "unknown"
    # An explicit prohibition in the pinned terms remains controlling even when
    # the accompanying record makes a contradictory permission claim.
    if permission == "prohibited" or terms_digest in prohibition_pins:
        return "prohibited"
    if record.get("permission") != permission:
        return "unknown"
    return permission


def _pinned_rights_status(source_sha256: str, purpose: str, cutoff: str) -> str:
    """Verify independently reviewed, code-owned terms and availability bytes."""
    return _evidence_rights_status(TRUSTED_RIGHTS_RECORDS.get(source_sha256), source_sha256, purpose,
                                   cutoff, TRUSTED_TERMS_SHA256, TRUSTED_AVAILABILITY_SHA256,
                                   TRUSTED_PROHIBITION_SHA256)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EASTERN = ZoneInfo("America/New_York")


def _safe_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("unsafe archive filename")
    root = root.absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("symlink archive path component is not allowed")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"archive entry is not a regular file: {name}")
    return path


def _parse_accepted(value: Any) -> datetime:
    """Parse SEC UTC/Z timestamps or EDGAR's explicitly Eastern local time."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("accepted timestamp missing")
    text = value.strip()
    try:
        stamp = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError("accepted timestamp invalid") from exc
    if stamp.tzinfo is None:
        # SEC submissions' legacy `accepted` field is EDGAR Eastern wall time.
        stamp = stamp.replace(tzinfo=_EASTERN)
    return stamp.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("cutoff must include timezone")
    return stamp.astimezone(timezone.utc)


def _artifact_bytes(root: Path, item: dict, spec: dict) -> bytes:
    filename = item.get("path")
    if filename != spec["archive_filename"]:
        raise ValueError("archive filename does not match code-owned filing registry")
    data = _safe_file(root, filename).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != spec["length"] or digest != spec["sha256"]:
        raise ValueError("archive artifact does not match code-owned length and SHA-256 pin")
    if item.get("sha256") != digest or item.get("byte_length") != len(data):
        raise ValueError("manifest artifact hash/length mismatch")
    return data


def _verified_filing(archive_dir: Path, filing_name: str) -> tuple[dict, dict]:
    if filing_name not in FILINGS:
        raise ValueError("filing is not present in code-owned SEC registry")
    root = Path(archive_dir)
    manifest_path = _safe_file(root, "manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != REGISTRY_VERSION:
        raise ValueError("archive manifest schema is not trusted")
    filing = manifest.get("filings", {}).get(filing_name)
    issuer = FILINGS[filing_name]
    if not isinstance(filing, dict) or any(filing.get(k) != issuer[k] for k in ("cik", "accession", "form", "report_date", "primary_document")):
        raise ValueError("manifest filing identity does not match code-owned registry")
    artifacts = filing.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("manifest artifacts missing")
    raw: dict[str, bytes] = {}
    expected_names = {"submissions": f"{filing_name}-submissions.json", "index": f"{filing_name}-index.json", "primary": f"{filing_name}-{issuer['primary_document']}"}
    for kind in ("submissions", "index", "primary"):
        item = artifacts.get(kind)
        registry_spec = issuer["artifacts"][kind]
        spec = dict(registry_spec, archive_filename=expected_names[kind])
        if not isinstance(item, dict) or item.get("artifact_url") != registry_spec["url"]:
            raise ValueError(f"manifest {kind} artifact URL/record invalid")
        raw[kind] = _artifact_bytes(root, item, spec)
    submissions = json.loads(raw["submissions"].decode("utf-8"))
    if str(submissions.get("cik", "")).zfill(10) != issuer["cik"]:
        raise ValueError("submissions CIK does not match filing registry")
    recent = submissions.get("filings", {}).get("recent", {})
    try:
        idx = recent.get("accessionNumber", []).index(issuer["accession"])
        if any(recent.get(key, [])[idx] != expected for key, expected in (("form", issuer["form"]), ("reportDate", issuer["report_date"]), ("primaryDocument", issuer["primary_document"]))):
            raise ValueError
        accepted_raw = recent.get("acceptanceDateTime", recent.get("accepted", []))[idx]
    except (ValueError, IndexError, KeyError, TypeError):
        raise ValueError("registered accession metadata is incomplete or inconsistent") from None
    accepted = _parse_accepted(accepted_raw)
    return {"accepted_at": accepted.isoformat().replace("+00:00", "Z"), "source_sha256": hashlib.sha256(raw["primary"]).hexdigest()}, manifest


def assess_filing_eligibility(archive_dir: Path, filing_name: str, *, cutoff: str = PINNED_CUTOFF,
                              purpose: str = "accounting_analysis", unit_context: dict | None = None,
                              rights_evidence: dict | None = None) -> dict:
    """Assess five independent predicates; caller claims cannot establish trust."""
    result: dict[str, Any] = {
        "version": POLICY_VERSION, "filing": filing_name, "purpose": purpose,
        "eligible": False, "status": "unknown", "predicates": {},
        "unit_context_status": "unverified", "accepted_at": None, "first_public_availability": "unknown",
        "first_public_availability_evidence": None, "reasons": [],
    }
    try:
        verified, _ = _verified_filing(Path(archive_dir), filing_name)
        result["accepted_at"] = verified["accepted_at"]
        source_ok = True
        try:
            cutoff_ok = _parse_accepted(verified["accepted_at"]) <= _timestamp(cutoff)
        except (TypeError, ValueError):
            cutoff_ok = False
        result["reasons"].append("accepted time is not first public availability; availability evidence is not separately pinned")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        source_ok, cutoff_ok = False, False
        result["reasons"].append(f"trusted archive/source verification failed: {exc}")
    # Caller claims never enter the trust decision; only code-owned source-bound pins do.
    rights_status = _pinned_rights_status(verified["source_sha256"], purpose, cutoff) if source_ok else "unknown"
    rights_ok = rights_status == "allowed"
    result["rights_status"] = rights_status
    if rights_status == "unknown":
        if rights_evidence is not None:
            result["reasons"].append("caller-supplied rights claims cannot establish independently pinned permission")
        else:
            result["reasons"].append("no independently pinned applicable terms bytes; rights unknown")
    elif rights_status == "prohibited":
        result["reasons"].append("independently pinned applicable terms explicitly prohibit this purpose")
    # A caller-supplied context dictionary is an assertion, not independent
    # evidence. Keep this predicate explicitly unverified until archive-bound
    # accounting observations are independently pinned.
    unit_ok = False
    result["reasons"].append("unit/context is unverified; caller assertions are not independent archive observations")
    policy_ok = purpose == "accounting_analysis" and POLICY_VERSION == "filing-eligibility/1"
    if not policy_ok:
        result["reasons"].append("purpose is outside the versioned policy")
    result["predicates"] = {"source": source_ok, "cutoff": cutoff_ok, "rights": rights_ok,
                             "unit_context": unit_ok, "policy": policy_ok}
    # Even all parseable fields are insufficient: availability and rights remain unknown.
    result["status"] = "unknown" if source_ok else "blocked"
    return result


_TEST_POLICY_VERSION = "filing-eligibility-test-only/1"
_TEST_SOURCE_SHA256 = FILINGS["southwest"]["artifacts"]["primary"]["sha256"]
_TEST_TERMS_BYTES = (b"publisher=TEST_FIXTURE; scope=accounting_analysis; purpose=accounting_analysis; "
                     b"version=synthetic-1; access_date=2026-01-01; effective_from=2025-01-01T00:00:00Z; "
                     b"permission=allowed")
_TEST_PROHIBITED_TERMS_BYTES = (b"publisher=TEST_FIXTURE; scope=accounting_analysis; purpose=accounting_analysis; "
                                b"version=synthetic-1; access_date=2026-01-01; effective_from=2025-01-01T00:00:00Z; "
                                b"permission=prohibited")
_TEST_AVAILABILITY_BYTES = (f"TEST-ONLY synthetic historical provenance; first_public_at=2025-01-01T00:00:00Z; "
                            f"source_sha256={_TEST_SOURCE_SHA256}".encode("utf-8"))
_TEST_TERMS_SHA256 = hashlib.sha256(_TEST_TERMS_BYTES).hexdigest()
_TEST_PROHIBITED_TERMS_SHA256 = hashlib.sha256(_TEST_PROHIBITED_TERMS_BYTES).hexdigest()
_TEST_AVAILABILITY_SHA256 = hashlib.sha256(_TEST_AVAILABILITY_BYTES).hexdigest()
_TEST_RECORD = {
    "label": "TEST-ONLY synthetic historical provenance; not real rights or availability evidence",
    "source_sha256": _TEST_SOURCE_SHA256, "publisher": "TEST_FIXTURE",
    "expected_publisher": "TEST_FIXTURE", "scope": "accounting_analysis",
    "purpose": "accounting_analysis", "version": "synthetic-1",
    "access_date": "2026-01-01", "effective_from": "2025-01-01T00:00:00Z",
    "first_public_at": "2025-01-01T00:00:00Z",
    "effective_history": [{"from": "2025-01-01T00:00:00Z", "through": None}],
    "terms_bytes": _TEST_TERMS_BYTES, "terms_sha256": _TEST_TERMS_SHA256,
    "availability_bytes": _TEST_AVAILABILITY_BYTES,
    "availability_sha256": _TEST_AVAILABILITY_SHA256, "permission": "allowed",
}
_TEST_TOKEN = object()
_TEST_PERMISSION_REGISTRY = {
    "synthetic-accounting-allowed": {**_TEST_RECORD, "permission": "allowed"},
    "synthetic-accounting-prohibited": {
        **_TEST_RECORD, "label": "TEST-ONLY synthetic explicit prohibition",
        "terms_bytes": _TEST_PROHIBITED_TERMS_BYTES,
        "terms_sha256": _TEST_PROHIBITED_TERMS_SHA256, "permission": "prohibited",
    },
}


def accounting_eligibility_test_fixture(record_id: str = "synthetic-accounting-allowed") -> dict:
    """Return a labeled test trace; it never represents real SEC clearance."""
    record = _TEST_PERMISSION_REGISTRY.get(record_id)
    if record is None:
        return {"version": _TEST_POLICY_VERSION, "label": "TEST-ONLY unknown fixture",
                "eligible": False, "status": "unknown", "fixture_only": True,
                "production_source_clearance": False}
    status = _evidence_rights_status(record, _TEST_SOURCE_SHA256, "accounting_analysis",
                                    PINNED_CUTOFF, frozenset({_TEST_TERMS_SHA256, _TEST_PROHIBITED_TERMS_SHA256}),
                                    frozenset({_TEST_AVAILABILITY_SHA256}), frozenset())
    terms_digest = hashlib.sha256(record["terms_bytes"]).hexdigest()
    availability_digest = hashlib.sha256(record["availability_bytes"]).hexdigest()
    prohibited = status == "prohibited"
    trusted_test_record = status in {"allowed", "prohibited"}
    allowed = status == "allowed"
    return {
        "version": _TEST_POLICY_VERSION, "label": record["label"],
        "eligible": allowed, "status": "test_only_prohibited" if prohibited else ("test_only_allowed" if allowed else "unknown"),
        "fixture_only": True, "production_source_clearance": False,
        "source_sha256": record.get("source_sha256"), "terms_sha256": terms_digest,
        "availability_sha256": availability_digest, "permission": record["permission"],
        "evidence_provenance": "synthetic test-only historical data; not real-world evidence",
        "predicates": {"source": trusted_test_record, "cutoff": trusted_test_record,
                       "rights": trusted_test_record and not prohibited,
                       # This fixed context is part of the code-owned test record,
                       # not a caller-provided context dictionary.
                       "unit_context": trusted_test_record, "policy": trusted_test_record},
        "trace_token": _TEST_TOKEN,
    }
