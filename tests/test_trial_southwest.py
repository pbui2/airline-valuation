from copy import deepcopy

import pytest

from airline_valuation.filing_ixbrl import parse_filing, resolve_locator
import airline_valuation.trial_southwest as southwest_trial
from airline_valuation.trial_southwest import (
    CANDIDATE_REFS,
    PacketValidationError,
    _candidate,
    build_southwest_packet,
)
from airline_valuation.trial_rules import load_standard


def test_southwest_cell_decision_packet_source_candidates(filing_originals, monkeypatch):
    raw = filing_originals["southwest"]
    parsed = parse_filing(raw, source_id="southwest")
    parse_calls = 0
    original_parse = southwest_trial.parse_filing

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(southwest_trial, "parse_filing", counted_parse)
    packet = build_southwest_packet(raw, parsed)
    assert parse_calls == 1
    assert packet["reference_count"] == 67
    expected = {"Input sheet!B11": "28063", "Input sheet!B12": "428", "Input sheet!B14": "7981"}
    assert sum(cell["status"].startswith("source_candidate") for cell in packet["cells"].values()) == 3
    for ref, amount in expected.items():
        cell = packet["cells"][ref]
        assert cell["status"] == "source_candidate_pending_rights_cutoff_independent_review"
        assert cell["decision"] is None and not cell["write_eligible"]
        evidence = cell["candidate"]
        assert evidence["issuer_cik"] == "0000092380"
        assert evidence["entity_scheme"] == "http://www.sec.gov/CIK"
        assert evidence["source_sha256"] == packet["source_sha256"]
        assert evidence["unit"] == "iso4217:USD"
        assert evidence["value_usd_millions"] == amount
        assert evidence["value_usd"] == str(int(amount) * 1_000_000)
        assert evidence["period"] == ({"type": "duration", "startdate": "2025-01-01", "enddate": "2025-12-31"}
                                       if ref != "Input sheet!B14" else {"type": "instant", "date": "2025-12-31"})
        assert evidence["context_id"] == ("c-4" if ref == "Input sheet!B14" else "c-1")
        assert evidence["row_text"] and evidence["locator"]["path"]
        assert evidence["locator"]["source_id"] == "southwest"
        assert evidence["locator"]["sha256"] == packet["source_sha256"]
        assert resolve_locator(raw, evidence["locator"]) is True
        assert cell["decision"] is None and cell["write_eligible"] is False
    assert "rights" in " ".join(packet["limitations"])


def test_southwest_b15_b17_abstention_without_approved_reconciliation(filing_originals):
    packet = build_southwest_packet(filing_originals["southwest"])
    assert packet["cells"]["Input sheet!B15"]["status"] == "abstain_debt_presentation_unresolved"
    assert packet["cells"]["Input sheet!B17"]["status"] == "abstain_lease_overlap_unresolved"
    assert all(packet["cells"][ref]["decision"] is None for ref in ("Input sheet!B15", "Input sheet!B17"))
    assert "5981" not in repr(packet)
    assert packet["cells"]["Input sheet!B17"]["decision"] is None


def test_southwest_packet_invalid_source_context_unit_rejected(filing_originals):
    raw = filing_originals["southwest"]
    parsed = parse_filing(raw, source_id="southwest")
    forged = deepcopy(parsed)
    forged["facts"][0]["value"] = "123"
    with pytest.raises(PacketValidationError, match="independent parse"):
        build_southwest_packet(raw, forged)
    with pytest.raises(PacketValidationError, match="pinned Southwest"):
        build_southwest_packet(raw + b"changed")

    ref, (concept, period, _label) = next(iter(CANDIDATE_REFS.items()))
    original = next(f for f in parsed["facts"] if f.get("concept") == concept and f.get("value") is not None
                    and not f.get("unresolved") and not f.get("context", {}).get("dimensions")
                    and f.get("context", {}).get("period", {}).get("type") == period)
    # The helper itself must reject a forged value even if its valid locator is unchanged.
    b11_fact = next(f for f in parsed["facts"] if f.get("concept") == CANDIDATE_REFS["Input sheet!B11"][0]
                    and f.get("context", {}).get("period", {}).get("startdate") == "2025-01-01"
                    and f.get("context", {}).get("period", {}).get("enddate") == "2025-12-31"
                    and not f.get("context", {}).get("dimensions") and f.get("value") is not None)
    forged_b11 = deepcopy(parsed)
    changed_b11 = next(f for f in forged_b11["facts"] if f.get("id") == b11_fact["id"])
    unchanged_locator = deepcopy(changed_b11["locator"])
    changed_b11["value"] = "999999999999"
    assert changed_b11["locator"] == unchanged_locator
    b11_concept, b11_period, _ = CANDIDATE_REFS["Input sheet!B11"]
    assert _candidate(raw, forged_b11, parsed["sha256"], "Input sheet!B11", b11_concept, b11_period) is None

    # Candidate validation itself must fail closed for each incompatible dimension.
    for mutation in ("issuer", "period", "context", "unit", "dimensions"):
        changed = deepcopy(parsed)
        fact = next(f for f in changed["facts"] if f.get("id") == original["id"])
        if mutation == "issuer":
            fact["context"]["entity"] = "0000006201"
        elif mutation == "period":
            fact["context"]["period"] = {"type": "duration", "start": "2024-01-01", "end": "2024-12-31"}
        elif mutation == "context":
            fact["context_ref"] = "wrong-context"
        elif mutation == "unit":
            fact["unit"]["measures"][0]["lexical"] = "iso4217:EUR"
        else:
            fact["context"]["dimensions"] = [{"axis": "example", "member": "test"}]
        assert _candidate(raw, changed, parsed["sha256"], ref, concept, period) is None

    duplicated = deepcopy(parsed)
    duplicated["facts"].append(deepcopy(original))
    assert _candidate(raw, duplicated, parsed["sha256"], ref, concept, period) is None
    missing_row = deepcopy(parsed)
    next(f for f in missing_row["facts"] if f.get("id") == original["id"])["id"] = "not-in-source"
    assert _candidate(raw, missing_row, parsed["sha256"], ref, concept, period) is None


def test_southwest_all_67_accounted_no_model_or_workbook(filing_originals):
    packet = build_southwest_packet(filing_originals["southwest"])
    refs = [entry["ref"] for entry in load_standard()["entries"]]
    assert len(refs) == len(set(refs)) == 67
    assert set(packet["cells"]) == set(refs)
    assert all(cell["decision"] is None and cell["write_eligible"] is False for cell in packet["cells"].values())
    assert sum(cell["status"].startswith("source_candidate") for cell in packet["cells"].values()) <= 3
    assert "No model" in " ".join(packet["limitations"])
    assert "workbook write" in " ".join(packet["limitations"])
