import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from airline_valuation.filing_archive import import_filings
from airline_valuation.filing_eligibility import (
    PINNED_CUTOFF,
    TRUSTED_RIGHTS_RECORDS,
    TRUSTED_TERMS_SHA256,
    TRUSTED_AVAILABILITY_SHA256,
    _TEST_RECORD,
    _TEST_SOURCE_SHA256,
    _TEST_TERMS_SHA256,
    _TEST_AVAILABILITY_SHA256,
    _evidence_rights_status,
    _pinned_rights_status,
    assess_filing_eligibility,
    accounting_eligibility_test_fixture,
)
from airline_valuation.filing_registry import FILINGS
from conftest import sec_fixture_files


def _archive(tmp_path):
    issuer = FILINGS["southwest"]
    filenames = [issuer["artifacts"][kind]["filename"] for kind in ("submissions", "index", "primary")]
    files = sec_fixture_files(*filenames)
    metadata, documents = tmp_path / "metadata", tmp_path / "documents"
    metadata.mkdir(); documents.mkdir()
    for kind in ("submissions", "index"):
        filename = issuer["artifacts"][kind]["filename"]
        shutil.copyfile(files[filename], metadata / filename)
    filename = issuer["artifacts"]["primary"]["filename"]
    shutil.copyfile(files[filename], documents / filename)
    return import_filings(metadata, documents, tmp_path / "archive")


def test_filing_metadata_cutoff_and_rights_matrix(tmp_path):
    archive = _archive(tmp_path)
    manifest = json.loads((archive / "manifest.json").read_text())
    primary = archive / manifest["filings"]["southwest"]["artifacts"]["primary"]["path"]
    captured = primary.read_bytes()
    baseline = assess_filing_eligibility(archive, "southwest")
    accepted = datetime.fromisoformat(baseline["accepted_at"].replace("Z", "+00:00"))
    assert baseline["accepted_at"] == "2026-02-05T21:07:52Z"
    assert baseline["rights_status"] == "unknown"
    assert baseline["predicates"]["rights"] is False
    assert TRUSTED_RIGHTS_RECORDS == {}
    assert not TRUSTED_TERMS_SHA256
    assert not TRUSTED_AVAILABILITY_SHA256
    for cutoff, expected_cutoff in ((accepted - timedelta(seconds=1), False), (accepted, True)):
        result = assess_filing_eligibility(archive, "southwest", cutoff=cutoff.isoformat())
        assert result["predicates"]["source"] is True
        assert result["predicates"]["cutoff"] is expected_cutoff
        assert result["predicates"]["rights"] is False
        assert result["predicates"]["unit_context"] is False
        assert result["unit_context_status"] == "unverified"
        assert result["predicates"]["policy"] is True
        assert result["eligible"] is False
        assert result["first_public_availability"] == "unknown"
        assert result["accepted_at"] == baseline["accepted_at"]
    # The archived bytes are registry-pinned; no acquisition timestamp can
    # substitute for first-public-availability evidence.
    assert hashlib.sha256(captured).hexdigest() == FILINGS["southwest"]["artifacts"]["primary"]["sha256"]
    assert manifest["filings"]["southwest"]["artifacts"]["primary"]["retrieved_at"] is None


def test_unrelated_terms_and_self_claims_fail_closed(tmp_path):
    archive = _archive(tmp_path)
    claims = {"permission": "allowed", "publisher": "SEC", "scope": "accounting_analysis",
              "version": "1", "access_date": "2026-01-01", "historical_applicability": True,
              "sha256": hashlib.sha256(b"self-asserted unrelated terms").hexdigest(),
              "bytes": b"self-asserted unrelated terms"}
    result = assess_filing_eligibility(archive, "southwest", cutoff=PINNED_CUTOFF,
               unit_context={"unit": "USD millions", "period": "fiscal_year", "issuer": "southwest"},
               rights_evidence=claims)
    assert result["eligible"] is False
    assert result["predicates"]["rights"] is False
    assert "caller-supplied rights claims cannot establish independently pinned permission" in result["reasons"]
    # Tampering with a pinned artifact invalidates the source predicate.
    (archive / "southwest-luv-20251231.htm").write_bytes(b"forged")
    blocked = assess_filing_eligibility(archive, "southwest")
    assert blocked["predicates"]["source"] is False
    assert blocked["eligible"] is False


def test_pinned_evidence_bytes_attribution_history_and_prohibition_matrix():
    terms_pin = frozenset({_TEST_TERMS_SHA256})
    availability_pin = frozenset({_TEST_AVAILABILITY_SHA256})
    assert hashlib.sha256(_TEST_RECORD["terms_bytes"]).hexdigest() == _TEST_TERMS_SHA256
    assert hashlib.sha256(_TEST_RECORD["availability_bytes"]).hexdigest() == _TEST_AVAILABILITY_SHA256
    assert _evidence_rights_status(_TEST_RECORD, _TEST_SOURCE_SHA256, "accounting_analysis",
                                  PINNED_CUTOFF, terms_pin, availability_pin, frozenset()) == "allowed"
    # Wrong source and purpose, mismatched record metadata/bytes, and permission
    # claims that disagree with pinned terms all fail closed.
    assert _evidence_rights_status(_TEST_RECORD, "0" * 64, "accounting_analysis",
                                  PINNED_CUTOFF, terms_pin, availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status(_TEST_RECORD, _TEST_SOURCE_SHA256, "other_purpose",
                                  PINNED_CUTOFF, terms_pin, availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status({**_TEST_RECORD, "publisher": "OTHER"}, _TEST_SOURCE_SHA256,
                                  "accounting_analysis", PINNED_CUTOFF, terms_pin,
                                  availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status({**_TEST_RECORD, "scope": "other_purpose"}, _TEST_SOURCE_SHA256,
                                  "accounting_analysis", PINNED_CUTOFF, terms_pin,
                                  availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status({**_TEST_RECORD, "terms_bytes": b"tampered"}, _TEST_SOURCE_SHA256,
                                  "accounting_analysis", PINNED_CUTOFF, terms_pin,
                                  availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status({**_TEST_RECORD, "permission": "prohibited"}, _TEST_SOURCE_SHA256,
                                  "accounting_analysis", PINNED_CUTOFF, terms_pin,
                                  availability_pin, frozenset()) == "unknown"
    mismatched_terms = _TEST_RECORD["terms_bytes"].replace(b"synthetic-1", b"synthetic-2")
    mismatched_digest = hashlib.sha256(mismatched_terms).hexdigest()
    assert _evidence_rights_status({**_TEST_RECORD, "terms_bytes": mismatched_terms,
                                   "terms_sha256": mismatched_digest}, _TEST_SOURCE_SHA256,
                                  "accounting_analysis", PINNED_CUTOFF, frozenset({mismatched_digest}),
                                  availability_pin, frozenset()) == "unknown"
    assert _evidence_rights_status(_TEST_RECORD, _TEST_SOURCE_SHA256, "accounting_analysis",
                                  "2024-12-31T23:59:59Z", terms_pin,
                                  availability_pin, frozenset()) == "unknown"
    prohibited_terms = b"publisher=TEST_FIXTURE; scope=accounting_analysis; purpose=accounting_analysis; " \
                       b"version=synthetic-1; access_date=2026-01-01; effective_from=2025-01-01T00:00:00Z; " \
                       b"permission=prohibited"
    prohibited_digest = hashlib.sha256(prohibited_terms).hexdigest()
    assert prohibited_digest != _TEST_TERMS_SHA256
    prohibited_record = {**_TEST_RECORD, "terms_bytes": prohibited_terms,
                         "terms_sha256": prohibited_digest, "permission": "prohibited"}
    assert _evidence_rights_status(prohibited_record, _TEST_SOURCE_SHA256, "accounting_analysis",
                                  PINNED_CUTOFF, frozenset({prohibited_digest}), availability_pin,
                                  frozenset()) == "prohibited"
    # Terms prohibition controls even when the record falsely claims permission.
    assert _evidence_rights_status({**prohibited_record, "permission": "allowed"},
                                  _TEST_SOURCE_SHA256, "accounting_analysis", PINNED_CUTOFF,
                                  frozenset({prohibited_digest}), availability_pin, frozenset()) == "prohibited"
    # Conversely, a record prohibition cannot turn allowed terms into a prohibition.
    assert _evidence_rights_status({**_TEST_RECORD, "permission": "prohibited"},
                                  _TEST_SOURCE_SHA256, "accounting_analysis", PINNED_CUTOFF,
                                  terms_pin, availability_pin, frozenset()) == "unknown"
    assert _pinned_rights_status(_TEST_SOURCE_SHA256, "accounting_analysis", PINNED_CUTOFF) == "unknown"


def test_filing_report_test_only_permission_isolation():
    allowed = accounting_eligibility_test_fixture()
    prohibited = accounting_eligibility_test_fixture("synthetic-accounting-prohibited")
    arbitrary = accounting_eligibility_test_fixture("caller-claims-allowed")
    assert allowed["eligible"] is True
    assert allowed["status"] == "test_only_allowed"
    assert allowed["fixture_only"] is True
    assert allowed["production_source_clearance"] is False
    assert all(allowed["predicates"].values())
    assert prohibited["eligible"] is False
    assert prohibited["status"] == "test_only_prohibited"
    assert prohibited["predicates"]["rights"] is False
    assert prohibited["terms_sha256"] != allowed["terms_sha256"]
    assert prohibited["availability_sha256"] == allowed["availability_sha256"]
    assert arbitrary["eligible"] is False
    assert arbitrary["status"] == "unknown"
