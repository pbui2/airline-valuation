from __future__ import annotations

import copy
import hashlib
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from airline_valuation.filing_ixbrl import parse_filing, resolve_locator
from airline_valuation.filing_policy import _debt_reconciliation, _fact, _row_text, reconcile_filing, reconcile_parsed_filing
from airline_valuation.filing_registry import FILINGS

FIXTURES = Path.home() / ".local/share/airline-valuation/sec-fixtures-v1"


def _original(issuer: str = "southwest") -> bytes:
    path = FIXTURES / f"{issuer}_primary.html"
    if not path.is_file():
        pytest.skip("private original SEC fixtures unavailable")
    return path.read_bytes()


def test_southwest_html_lease_and_cash_reconciliation(filing_originals, parsed_filing_originals):
    raw = filing_originals["southwest"]
    expected_hash = FILINGS["southwest"]["artifacts"]["primary"]["sha256"]
    assert hashlib.sha256(raw).hexdigest() == expected_hash
    extracted = parsed_filing_originals["southwest"]
    result = reconcile_parsed_filing(raw, extracted, "southwest")

    assert result["status"] == "extraction_only"
    assert result["source_sha256"] == expected_hash
    cash = result["reconciliations"]["cash_and_cash_equivalents"]
    assert cash["status"] == "reconciled"
    assert Decimal(cash["amount_usd_millions"]) == Decimal("3231")
    assert "Cash and cash equivalents" in cash["evidence"][0]["row_text"]

    leases = result["reconciliations"]["lease_liabilities"]
    assert leases["status"] == "reconciled"
    assert leases["equations"] == {"operating": "312 + 768 = 1080", "finance": "25 + 53 = 78"}
    evidence_items = [*leases["component_evidence"], leases["operating_total"], leases["finance_total"], *cash["evidence"]]
    for index, evidence in enumerate(evidence_items):
        assert evidence["unit"] == "iso4217:USD"
        if index == 0:
            assert resolve_locator(raw, evidence["locator"])
        parsed_fact = next(f for f in extracted["facts"] if f["id"] == evidence["fact_id"])
        assert parsed_fact["locator"] == evidence["locator"]
        assert parsed_fact["context"]["entity"] == "0000092380"
        assert parsed_fact["context"]["id"] == "c-4"
        assert parsed_fact["context"]["period"] == {"type": "instant", "date": "2025-12-31"}

    debt = result["reconciliations"]["debt"]
    assert debt["status"] == "abstain_debt_definitions_unresolved"
    assert debt["derived_usd_millions"] == {"current_plus_noncurrent": "4901", "current_plus_noncurrent_plus_finance_lease": "4979", "statement_minus_debt_table": "10"}
    assert debt["equations"]["debt_components"] == "324 + 4577 = 4901"
    assert debt["equations"]["including_finance_lease"] == "4901 + 78 = 4979"
    assert debt["equations"]["presentation_difference"] == "4929 - 4919 = 10"
    assert "may reflect different definitions" in debt["reason"]
    assert set(debt["evidence"]) == {"current_maturities", "noncurrent_debt", "debt_table_total", "primary_statement_long_term_debt", "finance_lease_obligations"}
    for item in debt["evidence"].values():
        assert item["unit"] == "iso4217:USD"
        assert item["locator"]["sha256"] == expected_hash
        assert item["row_text"]
    assert debt["evidence"]["current_maturities"]["row_text"].startswith("Current maturities of long-term debt")
    assert debt["evidence"]["noncurrent_debt"]["row_text"].startswith("Long-term debt less current maturities")
    assert "4,919" in debt["evidence"]["debt_table_total"]["row_text"]
    assert debt["evidence"]["primary_statement_long_term_debt"]["row_text"].startswith("Total")
    assert debt["evidence"]["finance_lease_obligations"]["row_text"].startswith("Finance leases")
    investments = result["reconciliations"]["short_term_investments"]
    assert investments["status"] == "abstain_unresolved_tagged_observation"
    assert investments["observation"]["unresolved"] is True
    assert investments["observation"]["value"] is None


def test_southwest_observation_and_note_mutations(filing_originals, parsed_filing_originals):
    raw = filing_originals["southwest"]
    parsed = copy.deepcopy(parsed_filing_originals["southwest"])
    baseline_debt = _debt_reconciliation(raw, parsed)
    current = next(f for f in parsed["facts"] if f.get("concept") == "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent" and f.get("context_ref") == "c-4" and f.get("value") == "324000000")
    current["value"] = "325000000"
    mutated_debt = _debt_reconciliation(raw, parsed)
    assert mutated_debt["derived_usd_millions"]["current_plus_noncurrent"] == "4902"
    assert mutated_debt["derived_usd_millions"]["current_plus_noncurrent"] != baseline_debt["derived_usd_millions"]["current_plus_noncurrent"]
    blocked = reconcile_parsed_filing(raw, parsed, "southwest")
    assert blocked["status"] == "blocked_parsed_source_mismatch"
    assert blocked["reconciliations"] == {}

    # Altering a source row invalidates exact-byte locators and yields a named
    # abstention rather than carrying forward source amounts from another note.
    changed_debt_note = raw.replace(b"Finance leases", b"Finance lease", 1)
    note_block = _debt_reconciliation(changed_debt_note, parsed)
    assert note_block["status"] == "abstain_missing_or_ambiguous_debt_row_evidence"
    assert "finance_lease_obligations" in note_block["missing_observations"]
    policy_source = inspect.getsource(__import__("airline_valuation.filing_policy", fromlist=["reconcile_filing"]))
    for obsolete_literal in ('"4919"', '"4929"', '"324"', '"4577"', '"4901"', '"78"'):
        assert obsolete_literal not in policy_source

    baseline = reconcile_filing(raw)
    # A changed observed fact cannot silently retain a trusted calculation.
    changed_observation = raw.replace(b">3,231<", b">3,232<", 1)
    if changed_observation == raw:
        # ixbrl numeric text may be split/encoded; mutate a stable source byte.
        changed_observation = raw.replace(b"Cash and cash equivalents", b"Cash and cash equivalentX", 1)
    assert changed_observation != raw
    observed_result = reconcile_filing(changed_observation)
    assert observed_result["status"] == "blocked_unpinned_source"
    assert observed_result["source_sha256"] != baseline["source_sha256"]

    # Changing the note/row evidence also changes the result block, rather than
    # leaving the original reconciliation attested against altered source bytes.
    changed_note = raw.replace(b"Current operating lease liabilities", b"Current operating lease liability", 1)
    assert changed_note != raw
    note_result = reconcile_filing(changed_note)
    assert note_result["status"] == "blocked_unpinned_source"
    assert note_result["reconciliations"] == {}


def test_southwest_fact_policy_requires_expanded_concept_and_currency_namespaces():
    fact = {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
            "concept_namespace": "http://fasb.org/us-gaap/2025",
            "context_ref": "c-4", "context": {"entity": "0000092380",
            "period": {"type": "instant", "date": "2025-12-31"}, "dimensions": []},
            "unit": {"resolved": True, "measures": [{"lexical": "iso4217:USD",
            "namespace": "http://www.xbrl.org/2003/iso4217", "local_name": "USD"}]},
            "value": "1", "unresolved": False}
    concept = fact["concept"]
    result = {"facts": [fact]}
    assert _fact(result, concept, b"") is fact

    rebound_concept = {**fact, "concept_namespace": "urn:foreign"}
    assert _fact({"facts": [rebound_concept]}, concept, b"") is None
    bad_measure_ns = {**fact, "unit": {"resolved": True, "measures": [{
        "lexical": "iso4217:USD", "namespace": "urn:foreign", "local_name": "USD"}]}}
    assert _fact({"facts": [bad_measure_ns]}, concept, b"") is None
    lexical_only = {**fact, "unit": {"resolved": True, "measures": [{"lexical": "iso4217:USD"}]}}
    assert _fact({"facts": [lexical_only]}, concept, b"") is None


def test_row_lookup_does_not_match_textual_or_unrelated_id_collision():
    raw = b'<table><tr><td>id="fact1" fake text</td></tr><tr><td><ix:nonFraction id="fact1" name="x" contextRef="c" unitRef="u">23</ix:nonFraction>Right row</td></tr></table>'
    fact = {"id": "fact1", "locator": {"tag": "ix:nonfraction"}}
    assert _row_text(raw, fact) == '23 Right row'


def test_reconcile_parsed_filing_reuses_verified_parse_and_rejects_mismatch(filing_originals, parsed_filing_originals):
    raw = filing_originals["southwest"]
    parsed = parsed_filing_originals["southwest"]
    shared = reconcile_parsed_filing(raw, parsed, "southwest")
    assert shared == reconcile_filing(raw, "southwest")
    assert shared["reconciliations"]["debt"]["derived_usd_millions"]["current_plus_noncurrent"] == "4901"
    wrong_issuer = reconcile_parsed_filing(raw, parsed, "delta")
    assert wrong_issuer["status"] == "blocked_parsed_source_mismatch"
    changed = reconcile_parsed_filing(raw + b" ", parsed, "southwest")
    assert changed["status"] == "blocked_parsed_source_mismatch"


def test_second_issuer_extension_and_policy_isolation(filing_originals):
    raw = filing_originals["delta"]
    result = reconcile_filing(raw, issuer="delta")
    assert result["status"] == "extraction_only"
    assert result["reconciliations"] == {}
    assert any("no approved Southwest" in item for item in result["exceptions"])
    assert result["registry_sha256"] == FILINGS["delta"]["artifacts"]["primary"]["sha256"]
