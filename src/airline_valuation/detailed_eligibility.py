"""Fail-closed source eligibility checks for detailed decision observations.

External artifacts are not authenticated merely because a caller supplies a
URL, publisher string, ``authoritative`` flag, or matching self-hash. External
eligibility therefore stays unknown until trusted publication/rights artifact
fingerprints are configured here from an independently authenticated channel.
The synthetic branch is a separate, explicitly test-only fixture predicate.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

ELIGIBILITY_VERSION = "source-eligibility/v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Deliberately empty: no independently authenticated external artifact pins
# have been supplied for this decision slice. Never populate from input data.
TRUSTED_PUBLICATION_ARTIFACT_SHA256: frozenset[str] = frozenset()
TRUSTED_TERMS_ARTIFACT_SHA256: frozenset[str] = frozenset()
TRUSTED_LOCATOR_ARTIFACT_SHA256: frozenset[str] = frozenset()
TRUSTED_ACCOUNTING_RECONCILIATION_SHA256: frozenset[str] = frozenset()
_SYNTHETIC_MARKER = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00"
_SYNTHETIC_IDENTITY = "Synthetic decision fixture"
_SYNTHETIC_PYTHON_TOKEN = object()
PINNED_CUTOFF = "2026-02-06T23:59:59-05:00"


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None


def _at_or_before_cutoff(value: str, cutoff: str) -> bool:
    try:
        limit = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if not _valid_timestamp(cutoff):
            return False
        if _valid_date(value):
            stamp_date = date.fromisoformat(value)
            stamp = datetime(stamp_date.year, stamp_date.month, stamp_date.day, 23, 59, 59, tzinfo=limit.tzinfo)
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
        return stamp <= limit
    except ValueError:
        return False


def _bytes(record: Mapping[str, Any]) -> bytes | None:
    """Read bytes from the Python API or JSON-safe base64 representation."""
    data = record.get("bytes")
    if isinstance(data, bytes):
        return data
    encoded = record.get("bytes_base64")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            return None
    return None


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _artifact(record: Mapping[str, Any], name: str, reasons: list[str]) -> tuple[bytes | None, dict[str, Any]]:
    raw = _bytes(record)
    actual = _digest(raw)
    declared = str(record.get("sha256", "")).lower()
    if raw is None or not raw:
        reasons.append(f"{name} artifact bytes missing or invalid")
    if actual is None or not _SHA256.fullmatch(declared) or declared != actual:
        reasons.append(f"{name} artifact bytes/hash mismatch")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
        reasons.append(f"{name} artifact is not valid UTF-8 JSON")
    if not isinstance(parsed, dict):
        parsed = {}
        reasons.append(f"{name} artifact payload must be an object")
    # Assertions adjacent to an artifact are not evidence; relevant fields
    # must be present inside the bytes whose digest is checked.
    for key, value in parsed.items():
        if key in record and record.get(key) != value:
            reasons.append(f"{name} manifest disagrees with hashed artifact")
    return raw, parsed


def _result(status: str, reasons: list[str], identity: Any, digest: str | None,
            cutoff: str, purpose: str, publication_date: Any = None) -> dict[str, Any]:
    return {"version": ELIGIBILITY_VERSION, "status": status,
            "eligible": status == "verified_allowed", "source_identity": identity,
            "source_sha256": digest, "publication_date": publication_date,
            "cutoff": cutoff, "purpose": purpose, "reasons": reasons}


def verify_source_eligibility(source: Mapping[str, Any], publication: Mapping[str, Any],
                              terms: Mapping[str, Any], *, cutoff: str,
                              purpose: str, allow_synthetic_test_source: bool = False) -> dict[str, Any]:
    """Verify source/artifact structure and return allowed/prohibited/unknown.

    External status is deliberately fail-closed: `verified_allowed` or
    `verified_prohibited` requires hashes in module-level trust pins populated
    only from independently authenticated artifacts. Input-supplied booleans
    and hashes cannot add trust pins. Synthetic fixtures use a separate marker
    and fixed identity, and cannot satisfy the external predicate.

    `source`, `publication`, and `terms` carry artifact bytes as bytes or
    JSON-safe base64 plus their SHA-256. Artifact metadata is decoded from
    those bytes rather than trusted from adjacent bundle assertions.
    """
    reasons: list[str] = []
    source_bytes = _bytes(source)
    digest = _digest(source_bytes)
    declared_source_hash = str(source.get("sha256", "")).lower()
    identity = source.get("identity")
    if source_bytes is None or not source_bytes:
        reasons.append("original source bytes missing or invalid")
    if digest is None or declared_source_hash != digest:
        reasons.append("original source bytes/hash mismatch")
    if not isinstance(identity, str) or not identity.strip():
        reasons.append("source identity missing")
    if not (_valid_date(cutoff) or _valid_timestamp(cutoff)):
        reasons.append("cutoff timestamp invalid")

    kind = source.get("kind")
    if kind == "synthetic_test":
        # The fixture marker and identity are required in addition to the kind
        # and caller opt-in; changing an external source's kind is insufficient.
        valid_fixture = (allow_synthetic_test_source and source_bytes is not None and
                         source_bytes.startswith(_SYNTHETIC_MARKER) and
                         identity == _SYNTHETIC_IDENTITY and
                         source.get("fixture_id") == "detailed-decisions-test-v1")
        if source.get("_fixture_token") is not _SYNTHETIC_PYTHON_TOKEN:
            valid_fixture = False
        if not valid_fixture:
            reasons.append("synthetic fixture predicate not satisfied")
        # Synthetic eligibility deliberately does not consume external claims.
        if valid_fixture:
            pub_bytes = _bytes(publication)
            terms_bytes = _bytes(terms)
            if not pub_bytes or not terms_bytes:
                reasons.append("synthetic fixture publication/terms artifacts missing")
            else:
                try:
                    pub = json.loads(pub_bytes.decode("utf-8"))
                    term = json.loads(terms_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pub, term = {}, {}
                    reasons.append("synthetic fixture evidence invalid")
                if pub.get("fixture_id") != "detailed-decisions-test-v1" or pub.get("source_sha256") != digest:
                    reasons.append("synthetic fixture publication binding invalid")
                if term.get("fixture_id") != "detailed-decisions-test-v1" or term.get("source_sha256") != digest or term.get("purpose") != purpose:
                    reasons.append("synthetic fixture terms binding invalid")
                if term.get("permission") not in ("allowed", "prohibited"):
                    reasons.append("synthetic fixture permission unknown")
                if not _valid_date(pub.get("published_at")) or not (_valid_date(cutoff) or _valid_timestamp(cutoff)) or not _at_or_before_cutoff(pub.get("published_at", ""), cutoff):
                    reasons.append("synthetic fixture publication date after cutoff or invalid")
            if reasons:
                return _result("unknown", reasons, identity, digest, cutoff, purpose)
            if term.get("permission") == "prohibited":
                return _result("verified_prohibited", [], identity, digest, cutoff, purpose, pub.get("published_at"))
            return _result("verified_allowed", [], identity, digest, cutoff, purpose, pub.get("published_at"))
        return _result("unknown", reasons, identity, digest, cutoff, purpose)

    if kind != "external":
        reasons.append("source kind unknown")
    # Explicitly reject the old inline/self-asserted form. All externally used
    # facts require original source bytes and independently trusted artifacts.
    pub_raw, pub = _artifact(publication, "publication", reasons)
    terms_raw, term = _artifact(terms, "terms", reasons)
    source_issuer = source.get("issuer")
    if pub.get("source_sha256") != digest:
        reasons.append("publication artifact not bound to original source hash")
    if pub.get("issuer") != source_issuer or not isinstance(source_issuer, str) or not source_issuer.strip():
        reasons.append("publication issuer does not match identified source issuer")
    publisher = pub.get("publisher")
    provenance = pub.get("provenance")
    locator = pub.get("locator")
    if not isinstance(publisher, str) or not publisher.strip() or provenance not in ("SEC_EDGAR", "ISSUER_ORIGINAL") or not isinstance(locator, str) or not locator.startswith("https://"):
        reasons.append("publication provenance/publisher/locator unrecognized")
    published_at = pub.get("published_at")
    if not _valid_timestamp(published_at) or not (_valid_date(cutoff) or _valid_timestamp(cutoff)) or not _at_or_before_cutoff(published_at, cutoff):
        reasons.append("publication timestamp/cutoff not attributable or is after cutoff")
    if term.get("source_sha256") != digest:
        reasons.append("terms artifact not bound to original source hash")
    if term.get("issuer") != source_issuer or not isinstance(term.get("publisher"), str) or not term.get("publisher", "").strip():
        reasons.append("terms issuer/publisher not identifiable or not bound to source issuer")
    if term.get("scope") != "airline_valuation_decision" or term.get("purpose") != purpose:
        reasons.append("terms scope/purpose unrelated or unsupported")
    if not isinstance(term.get("version"), str) or not term.get("version").strip() or not _valid_date(term.get("access_date")) or not _at_or_before_cutoff(term.get("access_date", ""), cutoff):
        reasons.append("terms version/access date missing, invalid, or after cutoff")
    permission = term.get("permission")
    if permission not in ("allowed", "prohibited"):
        reasons.append("terms permission ambiguous or unsupported")
    # No trusted external anchors are currently configured. A valid self-hash
    # is integrity only; it does not authenticate issuer, publisher, or rights.
    pub_hash = _digest(pub_raw)
    terms_hash = _digest(terms_raw)
    if pub_hash not in TRUSTED_PUBLICATION_ARTIFACT_SHA256:
        reasons.append("publication artifact has no independently trusted provenance pin")
    if terms_hash not in TRUSTED_TERMS_ARTIFACT_SHA256:
        reasons.append("terms artifact has no independently trusted rights pin")
    if reasons:
        return _result("unknown", reasons, identity, digest, cutoff, purpose, published_at)
    return _result("verified_allowed" if permission == "allowed" else "verified_prohibited",
                   [], identity, digest, cutoff, purpose, published_at)
