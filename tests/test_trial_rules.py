import json
from pathlib import Path

from airline_valuation.trial_rules import SourceObservation, load_standard, map_observations

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "src/airline_valuation/detailed_contract.json"
STANDARD = ROOT / "src/airline_valuation/trial_standard.json"


def test_standard_is_versioned_complete_and_covers_contract_inventory():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    standard = load_standard()
    entries = standard["entries"]
    refs = [row["ref"] for row in entries]
    assert standard["issuer_neutral"] is True
    assert standard["standard_version"]
    assert len(refs) == 67
    assert set(refs) == set(contract["input_allowlist"])
    required = {"classification", "unit", "period", "evidence", "alternatives", "uncertainty"}
    assert all(required <= row.keys() and all(row[field] for field in required) for row in entries)
    assert {row["classification"] for row in entries} <= {
        "filing_derived", "external_price_yield_rating", "analyst_assumption",
        "lookup_route", "formula_guarded", "unsupported",
    }
    assert not {row["classification"] for row in entries} & {"filing_derived", "external_price_yield_rating"}


def test_all_refs_receive_named_abstention_without_evidence():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    result = map_observations([], entity="issuer-123")
    assert set(result) == set(contract["input_allowlist"])
    assert all(row["decision"] is None and row["status"] == "abstain_source_binding_unverified" for row in result.values())
    assert result["Input sheet!B15"]["decision"] is None
    assert result["Input sheet!B17"]["decision"] is None


def _observation(ref, *, entity="issuer-123", period="FY2025", unit=None, digest="a" * 64, fact="revenue", value="100"):
    entry = next(row for row in load_standard()["entries"] if row["ref"] == ref)
    return SourceObservation(ref, entity, period, unit or entry["unit"], digest, fact, value)


def test_unique_observation_never_maps_positive_even_with_well_formed_hash():
    observation = _observation("Input sheet!B11")
    result = map_observations([observation], entity="issuer-123")[observation.ref]
    assert result["status"] == "abstain_source_binding_unverified"
    assert result["decision"] is None
    assert result["candidate_count"] == 1


def test_forged_hash_or_mismatched_fy_cannot_produce_positive_decision():
    ref = "Input sheet!B11"
    forged_hash = _observation(ref, digest="f" * 64)
    mismatched_fy = _observation(ref, period="FY2024")
    for observation in (forged_hash, mismatched_fy):
        result = map_observations([observation], entity="issuer-123")[ref]
        assert result["decision"] is None
        assert result["status"] == "abstain_source_binding_unverified"


def test_wrong_entity_or_unit_cannot_produce_positive_decision():
    ref = "Input sheet!B11"
    for observation in (_observation(ref, entity="other-issuer"), _observation(ref, unit="wrong unit")):
        result = map_observations([observation], entity="issuer-123")[ref]
        assert result["decision"] is None
        assert result["status"] == "abstain_source_binding_unverified"


def test_ambiguous_observations_abstain_without_tie_breaking():
    ref = "Input sheet!B11"
    first = _observation(ref, digest="a" * 64)
    second = _observation(ref, digest="b" * 64, value="101")
    result = map_observations([first, second], entity="issuer-123")[ref]
    assert result["status"] == "abstain_ambiguous_observations"
    assert result["decision"] is None
    assert result["candidate_count"] == 2


def test_pinned_guidance_semantics_are_reflected_conservatively():
    entries = {row["ref"]: row for row in load_standard()["entries"]}
    assert entries["Input sheet!B23"]["classification"] == "unsupported"
    assert "effective tax rate" in entries["Input sheet!B23"]["evidence"].lower()
    assert "not beta" in entries["Input sheet!B23"]["uncertainty"].lower()
    assert entries["Input sheet!B24"]["classification"] == "unsupported"
    assert "marginal tax rate" in entries["Input sheet!B24"]["evidence"].lower()
    assert "I26" in entries["Input sheet!B27"]["evidence"]
    assert "B26" in entries["Input sheet!B28"]["evidence"]
    assert "B31" in entries["Input sheet!B32"]["evidence"]
    assert "Number of Preferred Shares" in entries["Cost of capital worksheet!B48"]["evidence"]
    assert "Current Market Price per Share" in entries["Cost of capital worksheet!B49"]["evidence"]
    assert "Annual Dividend per Share" in entries["Cost of capital worksheet!B50"]["evidence"]


def test_no_scenario_defaults_or_token_usage():
    text = STANDARD.read_text(encoding="utf-8").casefold()
    assert "southwest" not in text
    assert "0.045" not in text and "0.03" not in text
    module = (ROOT / "src/airline_valuation/trial_rules.py").read_text(encoding="utf-8").casefold()
    assert "_synthetic_python_token" not in module
    assert "invoke_model" not in module and "openai" not in module
    result = map_observations([], entity="issuer-123")
    assert all(row["decision"] is None for row in result.values())
