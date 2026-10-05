from copy import deepcopy
import base64
import hashlib
import json

from airline_valuation.detailed_decisions import (
    BASE_EVIDENCE, FILING_SHA256, FORECAST_REQUIREMENTS,
    build_decision_package, canonical_digest,
)
from airline_valuation.detailed_eligibility import (
    PINNED_CUTOFF, verify_source_eligibility,
)


def _fixture_artifacts(source_hash, purpose="airline_valuation_decision", permission="allowed"):
    return (
        {"fixture_id":"detailed-decisions-test-v1", "source_sha256":source_hash, "published_at":"2025-12-31"},
        {"fixture_id":"detailed-decisions-test-v1", "source_sha256":source_hash, "purpose":purpose, "permission":permission},
    )


def _synthetic_bundle():
    bundle = deepcopy(BASE_EVIDENCE)
    for row in bundle:
        raw = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00" + row["fact"].encode() + b":" + row["value"].encode()
        digest = hashlib.sha256(raw).hexdigest()
        pub, term = _fixture_artifacts(digest)
        row.update(source_sha256=digest, source_identity="Synthetic decision fixture",
                   source={"bytes":raw, "sha256":digest, "identity":"Synthetic decision fixture", "kind":"synthetic_test", "fixture_id":"detailed-decisions-test-v1"},
                   publication={"bytes":json.dumps(pub, sort_keys=True, separators=(",", ":")).encode()},
                   terms={"bytes":json.dumps(term, sort_keys=True, separators=(",", ":")).encode()},
                   page="synthetic test locator", locator_status="verified_extracted_pdf_page")
    raw = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00shared-debt-source"
    digest = hashlib.sha256(raw).hexdigest()
    pub, term = _fixture_artifacts(digest)
    for row in bundle:
        if row["fact"] in {"reported_debt", "recognized_operating_lease_liabilities", "finance_lease_debt_in_reported_debt"}:
            row["source"] = {"bytes":raw, "sha256":digest, "identity":"Synthetic decision fixture", "kind":"synthetic_test", "fixture_id":"detailed-decisions-test-v1"}
            row["source_sha256"] = digest
            row["source_identity"] = "Synthetic decision fixture"
            row["publication"] = {"bytes":json.dumps(pub, sort_keys=True, separators=(",", ":")).encode()}
            row["terms"] = {"bytes":json.dumps(term, sort_keys=True, separators=(",", ":")).encode()}
    next(x for x in bundle if x["fact"] == "reported_debt")["lease_reconciliation"] = {
        "source_sha256":digest, "reported_debt_sha256":digest, "operating_lease_sha256":digest,
        "finance_lease_sha256":digest, "finance_lease_included":True, "operating_lease_disjoint":True}
    return bundle


def _synthetic_report(bundle=None):
    return build_decision_package(_synthetic_bundle() if bundle is None else bundle,
                                  allow_synthetic_test_source=True)


def test_AC1_real_southwest_fixture_fails_closed_and_has_no_false_verified_locators():
    report = build_decision_package()
    assert report["schema_version"].endswith("/v3")
    assert report["calculated_workbook"] is False
    assert report["status"] == "blocked_pending_verification"
    assert all(row["source_sha256"] == FILING_SHA256 for row in report["evidence"])
    assert report["cells"]["Input sheet!B15"]["decision"] is None
    assert report["cells"]["Input sheet!B18"]["decision"] is None
    assert report["source_eligibility"]["cutoff"] == PINNED_CUTOFF
    assert all(row.get("source", {}).get("kind") is None for row in report["evidence"])


def test_AC1_reviewer_strings_and_bundle_assertions_never_grant_external_rights():
    raw = b"actual external document"
    digest = hashlib.sha256(raw).hexdigest()
    pub = {"source_sha256":digest,"issuer":"Southwest Airlines","publisher":"SEC","provenance":"SEC_EDGAR","locator":"https://sec.gov/example","published_at":"2026-02-01T10:00:00-05:00"}
    terms = {"source_sha256":digest,"issuer":"Southwest Airlines","publisher":"Southwest Airlines","scope":"airline_valuation_decision","purpose":"airline_valuation_decision","version":"1","access_date":"2026-02-01","permission":"allowed"}
    encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    source = {"bytes_base64":base64.b64encode(raw).decode(),"sha256":digest,"identity":"Southwest Airlines filing","issuer":"Southwest Airlines","kind":"external"}
    publication = {"bytes_base64":base64.b64encode(encode(pub)).decode(),"sha256":hashlib.sha256(encode(pub)).hexdigest(), **pub, "authoritative":True}
    rights = {"bytes_base64":base64.b64encode(encode(terms)).decode(),"sha256":hashlib.sha256(encode(terms)).hexdigest(), **terms}
    result = verify_source_eligibility(source, publication, rights, cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision")
    assert result["status"] == "unknown" and not result["eligible"]
    assert "eligible" in result and "status" in result
    assert any("trusted provenance pin" in reason for reason in result["reasons"])
    report = build_decision_package(bundle_assessments={"source_rights":{"reviewer":"A Reviewer","review_status":"independently_reviewed","conclusion":"allowed"}})
    assert report["cells"]["Input sheet!B18"]["decision"] is None


def test_AC1_synthetic_test_only_python_fixture_supports_decimal_arithmetic():
    report = _synthetic_report()
    assert report["cells"]["Input sheet!B15"]["decision"] == "5981"
    assert report["cells"]["Input sheet!B18"]["decision"] == "3231"
    assert report["cells"]["Input sheet!B15"]["transforms"][0].startswith("Decimal(")
    assert report["cells"]["Input sheet!B18"]["eligibility_trace"]["cash"]["eligible"] is True
    assert all("bytes" not in str(row.get("source", {})) for row in report["evidence"])


def test_AC1_synthetic_path_requires_fixed_marker_identity_and_test_opt_in():
    bundle = _synthetic_bundle()
    assert build_decision_package(bundle)["cells"]["Input sheet!B18"]["decision"] is None
    spoofed = _synthetic_bundle()
    row = next(x for x in spoofed if x["fact"] == "cash")
    row["source"]["kind"] = "external"
    assert _synthetic_report(spoofed)["cells"]["Input sheet!B18"]["decision"] is None
    row["source"]["kind"] = "synthetic_test"
    row["source"]["fixture_id"] = "other"
    assert _synthetic_report(spoofed)["cells"]["Input sheet!B18"]["decision"] is None


def test_AC1_external_fake_publication_and_terms_artifacts_are_unknown():
    raw = b"external"
    digest = hashlib.sha256(raw).hexdigest()
    source = {"bytes":raw,"sha256":digest,"identity":"Issuer source","issuer":"Issuer","kind":"external"}
    fake_pub = {"bytes":b'{"authoritative":true}',"sha256":hashlib.sha256(b'{"authoritative":true}').hexdigest(),"authoritative":True,"source_sha256":digest,"date":"2026-02-01"}
    fake_terms = {"bytes":b'{"permission":"allowed"}',"sha256":hashlib.sha256(b'{"permission":"allowed"}').hexdigest(),"permission":"allowed","terms_sha256":"self-hash"}
    result = verify_source_eligibility(source, fake_pub, fake_terms, cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision")
    assert result["status"] == "unknown"
    assert not result["eligible"]


def test_AC1_unrelated_terms_and_post_cutoff_publication_are_unknown():
    bundle = _synthetic_bundle()
    row = next(x for x in bundle if x["fact"] == "cash")
    pub, terms = _fixture_artifacts(row["source"]["sha256"], purpose="unrelated")
    pub["published_at"] = "2026-02-07"
    row["publication"] = {"bytes":json.dumps(pub).encode()}
    row["terms"] = {"bytes":json.dumps(terms).encode()}
    result = verify_source_eligibility(row["source"], row["publication"], row["terms"], cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision", allow_synthetic_test_source=True)
    assert result["status"] == "unknown"


def test_AC1_verified_prohibited_is_distinct_from_unknown():
    raw = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00forbidden"
    digest = hashlib.sha256(raw).hexdigest()
    pub, term = _fixture_artifacts(digest, permission="prohibited")
    source = {"bytes":raw,"sha256":digest,"identity":"Synthetic decision fixture","kind":"synthetic_test","fixture_id":"detailed-decisions-test-v1"}
    artifact = lambda value: {"bytes":json.dumps(value, sort_keys=True, separators=(",", ":")).encode()}
    result = verify_source_eligibility(source, artifact(pub), artifact(term), cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision", allow_synthetic_test_source=True)
    assert result["status"] == "unknown"  # JSON-shaped claims cannot forge the in-process fixture token.
    from airline_valuation.detailed_eligibility import _SYNTHETIC_PYTHON_TOKEN
    source["_fixture_token"] = _SYNTHETIC_PYTHON_TOKEN
    verified = verify_source_eligibility(source, artifact(pub), artifact(term), cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision", allow_synthetic_test_source=True)
    assert verified["status"] == "verified_prohibited" and not verified["eligible"]
    unknown = verify_source_eligibility(source, {}, {}, cutoff=PINNED_CUTOFF, purpose="airline_valuation_decision", allow_synthetic_test_source=True)
    assert unknown["status"] == "unknown"


def test_AC1_B19_B20_and_seven_forecast_inputs_are_separate_abstentions():
    report = build_decision_package()
    cells = report["cells"]
    expected = {"Input sheet!B19", "Input sheet!B20", *(f"Input sheet!B{i}" for i in range(26, 33))}
    assert set(FORECAST_REQUIREMENTS) == expected
    for ref in expected:
        assert cells[ref]["decision"] is None and cells[ref]["status"] == "abstain"
        assert cells[ref]["policy_id"] and cells[ref]["unit"] and cells[ref]["reason"]
        assert cells[ref]["old_candidate_alternative"]["kind"] == "prior candidate, not adopted"
    assert len({cells[ref]["reason"] for ref in expected}) == 9


def test_AC1_verified_prohibited_cash_and_debt_sources_block_cells():
    bundle = _synthetic_bundle()
    cash = next(row for row in bundle if row["fact"] == "cash")
    pub, terms = _fixture_artifacts(cash["source_sha256"], permission="prohibited")
    cash["publication"] = {"bytes": json.dumps(pub, sort_keys=True, separators=(",", ":")).encode()}
    cash["terms"] = {"bytes": json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()}
    report = _synthetic_report(bundle)
    assert report["cells"]["Input sheet!B18"]["decision"] is None
    assert report["cells"]["Input sheet!B18"]["eligibility_trace"]["cash"]["status"] == "verified_prohibited"
    assert report["cells"]["Input sheet!B18"]["eligibility_trace"]["cash"]["eligible"] is False

    bundle = _synthetic_bundle()
    debt_rows = [row for row in bundle if row["fact"] in {"reported_debt", "recognized_operating_lease_liabilities", "finance_lease_debt_in_reported_debt"}]
    for row in debt_rows:
        pub, terms = _fixture_artifacts(row["source"]["sha256"], permission="prohibited")
        row["publication"] = {"bytes": json.dumps(pub, sort_keys=True, separators=(",", ":")).encode()}
        row["terms"] = {"bytes": json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()}
    report = _synthetic_report(bundle)
    assert report["cells"]["Input sheet!B15"]["decision"] is None
    assert any(trace["status"] == "verified_prohibited" and not trace["eligible"]
               for trace in report["cells"]["Input sheet!B15"]["eligibility_trace"].values())


def test_AC1_synthetic_candidate_package_rejects_verified_prohibited_source():
    from airline_valuation.detailed_decisions import build_synthetic_test_decision_package
    from airline_valuation.detailed_eligibility import _SYNTHETIC_PYTHON_TOKEN
    fact, value = "synthetic_cash", "95"
    raw = b"AIRLINE-VALUATION-SYNTHETIC-FIXTURE-V1\x00" + fact.encode() + b":" + value.encode()
    digest = hashlib.sha256(raw).hexdigest()
    pub, terms = _fixture_artifacts(digest, permission="prohibited")
    encode = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
    row = {"fact": fact, "value": value, "source_sha256": digest, "source_identity": "Synthetic decision fixture",
           "identity": fact, "unit": "USD millions", "date": "2025-12-31", "page": "synthetic test observation",
           "source": {"bytes": raw, "sha256": digest, "identity": "Synthetic decision fixture",
                      "kind": "synthetic_test", "fixture_id": "detailed-decisions-test-v1",
                      "_fixture_token": _SYNTHETIC_PYTHON_TOKEN},
           "publication": {"bytes": encode(pub)}, "terms": {"bytes": encode(terms)}}
    package = build_synthetic_test_decision_package(observations=[row])
    cash = package["cells"]["Input sheet!B18"]
    assert cash["decision"] is None
    assert cash["eligibility_trace"]["synthetic_cash"]["status"] == "verified_prohibited"
    assert cash["eligibility_trace"]["synthetic_cash"]["eligible"] is False


def test_AC1_real_package_has_blocked_decision_for_every_contract_cell():
    from airline_valuation.detailed_workflow import load_contract
    report = build_decision_package()
    required = set(load_contract()["required_inputs"])
    assert set(report["cells"]) == required
    assert report["status"] == "blocked_pending_verification"
    assert all(report["cells"][ref]["decision"] is None for ref in required - {"Cost of capital worksheet!B32"})
    assert report["cells"]["Cost of capital worksheet!B32"]["status"] == "modeling_choice"
    assert report["cells"]["Cost of capital worksheet!B32"]["decision"] == "0"
    for ref in required - {"Cost of capital worksheet!B32"}:
        item = report["cells"][ref]
        assert item["policy_version"] and item["unit"] and item["reason"]
    assert report["cells"]["Input sheet!B22"]["policy_id"] == "market-data.current-share-price"
    assert report["cells"]["Input sheet!B34"]["policy_id"] == "market-data.risk-free-rate"
    assert report["cells"]["Cost of capital worksheet!H21"]["policy_id"] == "lookup.unverified-required-input"
    assert report["cells"]["Input sheet!B19"] is not report["cells"]["Input sheet!B20"]
    assert all(report["cells"][f"Input sheet!B{i}"] is not report["cells"][f"Input sheet!B{i+1}"] for i in range(26, 32))


def test_AC1_package_digest_and_pinned_timestamp_are_stable():
    report = build_decision_package()
    assert report["canonical_sha256"] == canonical_digest({k:v for k,v in report.items() if k != "canonical_sha256"})
    assert report["source_eligibility"]["cutoff"] == "2026-02-06T23:59:59-05:00"
