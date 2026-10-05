from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from airline_valuation.trial_guidance import (
    _formula_dependencies,
    CONTRACT,
    GuidanceInventoryError,
    PINNED_WORKBOOK_SHA256,
    inspect_trial_guidance,
)

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "fcffsimpleginzu.xlsx"


def test_inventory_covers_all_pinned_detailed_refs_with_hashed_evidence():
    result = inspect_trial_guidance(WORKBOOK)
    assert result["source_matches_pin"] is True
    assert result["observed_source_sha256"] == PINNED_WORKBOOK_SHA256
    assert result["pinned_source_sha256"] == CONTRACT["sha256"]
    assert result["reference_count"] == 67
    assert [entry["reference"] for entry in result["entries"]] == CONTRACT["input_allowlist"]
    assert all(len(entry["worksheet_xml_sha256"]) == 64 for entry in result["entries"])
    assert all(entry["comment_status"] in {"present", "absent in related comment parts"} for entry in result["entries"])
    assert all(entry["cell_present"] and len(entry["cell_xml_sha256"]) == 64 for entry in result["entries"])
    assert all("nearby_headers_and_labels" in entry and "validations" in entry for entry in result["entries"])


def test_exact_comment_and_absence_are_distinguished():
    result = inspect_trial_guidance(WORKBOOK)
    by_ref = {item["reference"]: item for item in result["entries"]}
    with_comments = [item for item in by_ref.values() if item["comments"]]
    without_comments = [item for item in by_ref.values() if not item["comments"]]
    assert with_comments and without_comments
    for entry in with_comments:
        assert entry["comment_status"] == "present"
        assert all(isinstance(comment["text"], str) and len(comment["part_sha256"]) == 64
                   for comment in entry["comments"])
    assert any(comment["kind"] == "threaded" for entry in with_comments for comment in entry["comments"])
    # An existing empty comment is still a comment record, not an absence.
    assert all(entry["comment_status"] == "present" for entry in with_comments)
    for entry in without_comments:
        assert entry["comment_status"] == "absent in related comment parts"


def test_extension_selector_validations_retain_targets_and_list_sources():
    by_ref = {item["reference"]: item for item in inspect_trial_guidance(WORKBOOK)["entries"]}
    for ref, source in {
        "Input sheet!B53": "'Answer keys'!$B$2:$B$3",
        "Cost of capital worksheet!B11": "'Answer keys'!$I$2:$I$5",
        "Cost of capital worksheet!B21": "'Answer keys'!$F$2:$F$6",
        "Cost of capital worksheet!B25": "'Answer keys'!$C$2:$C$5",
        "Cost of capital worksheet!B33": "'Answer keys'!$D$2:$D$4",
        "Cost of capital worksheet!B35": "'Answer keys'!$G$2:$G$16",
    }.items():
        assert any(v["type"] == "list" and v["formula1"] == source
                   and len(v["xml_sha256"]) == 64 for v in by_ref[ref]["validations"])
        assert by_ref[ref]["formula"] is None
        assert by_ref[ref]["formula_sha256"] == __import__("hashlib").sha256(b"").hexdigest()


def test_conversion_and_non_input_guards_are_explicit():
    result = inspect_trial_guidance(WORKBOOK)
    by_ref = {item["reference"]: item for item in result["entries"]}
    for ref in ("Input sheet!B27", "Input sheet!B28", "Input sheet!B32"):
        assert "scenario_conversion_guard" in by_ref[ref]["guard_classification"]
        assert by_ref[ref]["formula"]
        assert by_ref[ref]["dependencies"]
    guard = result["excluded_formula_guard"]
    assert guard["reference"] == "Cost of capital worksheet!D62"
    assert "non_input_formula_guard" in guard["guard_classification"]
    assert guard["formula"] == "B50/B49"
    assert set(guard["dependencies"]) == {"B50", "B49"}
    assert len(guard["cell_xml_sha256"]) == 64
    assert _formula_dependencies("IF('Input sheet'!B17=\"Yes\",'Sheet 2'!$A$1,B50)") == [
        "'Input sheet'!B17", "'Sheet 2'!$A$1", "B50"
    ]


def test_reader_fails_closed_on_a_non_pinned_workbook(tmp_path: Path):
    changed = tmp_path / "changed.xlsx"
    shutil.copyfile(WORKBOOK, changed)
    changed.write_bytes(changed.read_bytes() + b"x")
    with pytest.raises(GuidanceInventoryError, match="does not match pinned"):
        inspect_trial_guidance(changed)


def test_guidance_inventory_all_detailed_refs():
    result = inspect_trial_guidance(WORKBOOK)
    assert result["reference_count"] == len(CONTRACT["input_allowlist"]) == 67
    assert [entry["reference"] for entry in result["entries"]] == CONTRACT["input_allowlist"]
    assert set(result["selector_domains"]) == {
        "Input sheet!B16", "Input sheet!B17", "Input sheet!B37", "Input sheet!B45",
        "Input sheet!B48", "Input sheet!B51", "Input sheet!B56", "Input sheet!B59",
        "Input sheet!B61", "Input sheet!B64", "Input sheet!B67", "Input sheet!B70",
        "Input sheet!B53", "Input sheet!B8", "Input sheet!B57",
        "Cost of capital worksheet!B11", "Cost of capital worksheet!B21",
        "Cost of capital worksheet!B25", "Cost of capital worksheet!B33", "Cost of capital worksheet!B35",
    }
    assert all(item["source_matches_pin"] is True and item["issuer_specific_decision"] is False
               for item in result["selector_domains"].values())


def test_guidance_comment_header_hash_mutation():
    result = inspect_trial_guidance(WORKBOOK)
    by_ref = {entry["reference"]: entry for entry in result["entries"]}
    for ref in ("Input sheet!B23", "Input sheet!B24", "Input sheet!B38", "Input sheet!B39",
                "Input sheet!B40", "Cost of capital worksheet!B32"):
        entry = by_ref[ref]
        assert entry["cell_xml_sha256"]
        assert entry["worksheet_xml_sha256"]
        assert entry["comments"]
        assert all(comment["part_sha256"] for comment in entry["comments"])
        assert entry["nearby_headers_and_labels"]


def test_guidance_formula_and_route_classification():
    result = inspect_trial_guidance(WORKBOOK)
    by_ref = {entry["reference"]: entry for entry in result["entries"]}
    for ref in ("Input sheet!B27", "Input sheet!B28", "Input sheet!B32"):
        assert "scenario_conversion_guard" in by_ref[ref]["guard_classification"]
        assert by_ref[ref]["formula"] and by_ref[ref]["dependencies"]
        assert ref not in result["selector_domains"]
    for ref in ("Cost of capital worksheet!B11", "Cost of capital worksheet!B21",
                "Cost of capital worksheet!B25", "Cost of capital worksheet!B33"):
        assert by_ref[ref]["formula"] is None
        assert by_ref[ref]["validations"]
        assert ref in result["selector_domains"]
    assert result["excluded_formula_guard"]["reference"] == "Cost of capital worksheet!D62"


def test_cross_issuer_selector_domains():
    result = inspect_trial_guidance(WORKBOOK)
    domains = result["selector_domains"]
    assert domains["Cost of capital worksheet!B21"]["allowed"] == [
        "Direct input", "Single Business(US)", "Single Business(Global)",
        "Multibusiness(US)", "Multibusiness(Global)",
    ]
    assert "Southwest" not in repr(domains)
    # A domain describes template options; it does not select or authorize an issuer route.
    assert all(not domain["issuer_specific_decision"] for domain in domains.values())
    assert CONTRACT["route_selectors"]["Cost of capital worksheet!B21"] in domains["Cost of capital worksheet!B21"]["allowed"]


def test_mutated_workbook_never_yields_authentic_selector_domains(tmp_path: Path):
    changed = tmp_path / "changed.xlsx"
    shutil.copyfile(WORKBOOK, changed)
    changed.write_bytes(changed.read_bytes() + b"x")
    with pytest.raises(GuidanceInventoryError, match="does not match pinned"):
        inspect_trial_guidance(changed)
    result = inspect_trial_guidance(changed, verify_pin=False)
    assert result["source_matches_pin"] is False
    assert result["selector_domains"] == {}
