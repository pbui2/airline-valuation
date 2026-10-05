import json
from pathlib import Path

import pytest

from airline_valuation.manifest import assess_readiness, normalize_entry, validate_manifest

EXAMPLE = Path(__file__).parents[1] / "examples" / "fy2025-manifest.json"
CONTRACT = Path(__file__).parents[1] / "src" / "airline_valuation" / "contract.json"


def sample():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def b21_metadata():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))["input_metadata"]["Input sheet!B21"]


def test_example_is_reviewable_but_not_ready():
    manifest = sample()
    result = validate_manifest(manifest)
    assert result.valid, result.errors
    readiness = assess_readiness(manifest)
    assert not readiness.ready
    assert any("required workbook cell" in blocker for blocker in readiness.blockers)


def test_approved_manifest_cannot_bypass_required_contract_cells_with_empty_list():
    manifest = sample()
    manifest["required_cells"] = []
    manifest["approval"] = {"status": "approved"}
    manifest["analyst_assumptions"] = [{
        "id": "assumption", "value": "0.1", "unit": "decimal rate", "status": "approved",
        "rationale": "test", "author": "reviewer", "approval_date": "2026-02-07",
    }]
    validation = validate_manifest(manifest)
    assert not validation.valid
    assert any("required_cells must exactly match" in error for error in validation.errors)
    assert not assess_readiness(manifest).ready


def test_supported_selector_and_branch_input_are_contract_required():
    manifest = sample()
    required = set(manifest["required_cells"])
    assert "Input sheet!B11" in required
    assert "Cost of capital worksheet!B11" in required
    assert "Cost of capital worksheet!B12" in required


def test_extra_protected_or_formula_cell_mapping_rejected():
    manifest = sample()
    manifest["market_observations"][0]["workbook_cell"] = "Valuation output!B33"
    assert any("non-input or protected cell" in error for error in validate_manifest(manifest).errors)


def test_b21_shares_are_converted_to_millions_of_shares():
    value, unit = normalize_entry("Input sheet!B21", {"value": "491317990", "unit": "shares"}, b21_metadata())
    assert value == "491.31799"
    assert unit == "millions of shares"


def test_b21_millions_of_shares_are_preserved():
    value, unit = normalize_entry("Input sheet!B21", {"value": "491.31799", "unit": "millions of shares"}, b21_metadata())
    assert value == "491.31799"
    assert unit == "millions of shares"


@pytest.mark.parametrize(("value", "unit"), [
    ("491317990", ""),
    ("491317990", "shares outstanding"),
    ("491317990", "USD millions"),
    ("491317990000000", "shares"),
    ("491.31799", "shares"),
    ("491317990", "millions of shares"),
    ("0", "shares"),
    ("-1", "shares"),
    ("1.5", "shares"),
    ("1000", "millions of shares"),
])
def test_b21_rejects_ambiguous_or_inconsistent_units_and_scales(value, unit):
    with pytest.raises(ValueError):
        normalize_entry("Input sheet!B21", {"value": value, "unit": unit}, b21_metadata())


def test_b21_fixed_february_3_observation_remains_source_observation():
    manifest = sample()
    observation = manifest["market_observations"][0]
    assert observation["id"] == "shares_2026-02-03"
    assert observation["value"] == "491317990"
    assert observation["unit"] == "shares"
    assert observation["as_of"].startswith("2026-02-03")
    # It remains an unmapped source observation; workbook conversion is separate.
    normalized, target = normalize_entry("Input sheet!B21", {"value": observation["value"], "unit": observation["unit"]}, b21_metadata())
    assert (normalized, target) == ("491.31799", "millions of shares")
    assert validate_manifest(manifest).valid


def test_wrong_input_unit_and_rate_range_rejected():
    manifest = sample()
    observation = manifest["market_observations"][1]
    observation["workbook_cell"] = "Input sheet!B34"
    observation["unit"] = "percent"
    observation["value"] = "4.22"
    errors = validate_manifest(manifest).errors
    assert any("not an approved unit" in error for error in errors)


def test_numeric_currency_requires_explicit_usd_millions_unit():
    manifest = sample()
    fact = manifest["facts"][0]
    fact.update(value="100", unit="USD", workbook_cell="Input sheet!B11")
    assert any("not an approved unit" in error for error in validate_manifest(manifest).errors)


def test_financial_facts_must_use_original_not_amendment_authority():
    manifest = sample()
    fact = manifest["facts"][0]
    fact["source"]["accession"] = "0000092380-26-000006"
    fact["status"] = "ready"
    fact["value"] = "123"
    fact["as_of"] = "2026-02-05T16:07:52-05:00"
    fact["tag"] = "ExampleFact"
    assert any("original 10-K accession" in error for error in validate_manifest(manifest).errors)


def test_delayed_publication_after_cutoff_is_rejected_and_unknown_blocks_ready():
    manifest = sample()
    fact = manifest["facts"][0]
    fact.update(status="ready", value="100", as_of="2026-02-05T16:00:00-05:00",
                published_at="2026-02-07T09:00:00-05:00", tag="ExampleFact")
    assert any("published_at is after cutoff" in error for error in validate_manifest(manifest).errors)
    fact["published_at"] = None
    fact["available_at"] = None
    assert any("publication/availability time is unknown" in error for error in validate_manifest(manifest).errors)


def test_both_publication_and_availability_times_are_checked():
    manifest = sample()
    fact = manifest["facts"][0]
    fact.update(status="ready", value="100", as_of="2026-02-05T16:00:00-05:00",
                published_at="2026-02-07T09:00:00-05:00", available_at="2026-02-06T20:00:00-05:00",
                tag="ExampleFact")
    errors = validate_manifest(manifest).errors
    assert any("published_at is after cutoff" in error for error in errors)


def test_source_url_must_match_accession():
    manifest = sample()
    fact = manifest["facts"][0]
    fact["source"]["url"] = "https://www.sec.gov/Archives/edgar/data/92380/000009238026000006/"
    assert any("source URL" in error for error in validate_manifest(manifest).errors)


def test_pending_filing_blocks_readiness_even_if_inputs_approved():
    manifest = sample()
    manifest["approval"] = {"status": "approved"}
    manifest["analyst_assumptions"] = [{"id": "test", "value": "0.1", "unit": "decimal rate",
        "status": "approved", "rationale": "test", "author": "reviewer", "approval_date": "2026-02-07"}]
    result = assess_readiness(manifest)
    assert not result.ready
    assert any("filing 0000092380-26-000004 is pending" in blocker for blocker in result.blockers)


def test_treasury_observation_time_unknown_stays_pending():
    manifest = sample()
    treasury = manifest["market_observations"][1]
    assert treasury["observation_date"] == "2026-02-06"
    assert treasury["as_of"] is None
    assert treasury["published_at"] is None
    assert treasury["availability_status"] == "daily-only-time-unknown"
    assert any("pending or unknown" in blocker for blocker in assess_readiness(manifest).blockers)


def test_zero_numeric_value_requires_explicit_semantics():
    manifest = sample()
    shares = manifest["market_observations"][0]
    shares["id"] = "synthetic_share_count"
    shares["value"] = "0"
    shares["zero_semantics"] = "explicit_zero"
    errors = validate_manifest(manifest).errors
    assert any("share counts must be positive whole shares" in error for error in errors)
    fact = manifest["facts"][0]
    fact.update(value="0", status="pending", zero_semantics=None)
    assert any("zero value requires zero_semantics" in error for error in validate_manifest(manifest).errors)


def test_duplicate_mapping_rejected():
    manifest = sample()
    manifest["facts"][0]["workbook_cell"] = "Input sheet!B11"
    manifest["market_observations"][0]["workbook_cell"] = "Input sheet!B11"
    assert any("duplicate workbook cell mapping" in error for error in validate_manifest(manifest).errors)
